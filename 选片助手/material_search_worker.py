"""MaterialSearch 独立工作进程；沿用原 SQLAlchemy/SQLite 四表及搜索接口。"""
import base64
from contextlib import redirect_stdout
import datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parent))
sys.dont_write_bytecode=True
if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8',line_buffering=True)


class ArchiveIndex:
    def __init__(self, settings):
        if settings.get('frozen_parent') and os.name=='nt':
            import ctypes
            # 只清理模型子进程继承的冻结 DLL 搜索目录，不改动主程序。
            ctypes.windll.kernel32.SetDllDirectoryW(None)
        self.directory = Path(settings['directory']); self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self.directory/'assets.db'; self.model = Path(settings['model'])
        self.media_types = settings['media_types']; self.exiftool = settings.get('exiftool','')
        self.lock = threading.RLock(); self.write_lock = threading.Lock()
        self.closed = threading.Event(); self.wake = threading.Event()
        self.roots = []; self.epoch = 0; self.enabled = False; self.paused = False
        self.phase = 'idle'; self.error = ''; self.total = 0; self.done = 0; self.indexed = 0
        self.reused = 0; self.updated = 0; self.offline = []; self.queries = 0
        self.cache_path = self.directory/'scan-state.json'
        self.pending=set()
        saved = json.loads(self.cache_path.read_text('utf8')) if self.cache_path.is_file() else {}
        self.files = saved.get('files', {}); self.failures = saved.get('failures', {})
        self._saved = time.monotonic(); self._last_scan = 0
        manifest = {'model': self.model.name, 'config': hashlib.sha256((self.model/'config.json').read_bytes()).hexdigest(),
                    'processor': hashlib.sha256((self.model/'preprocessor_config.json').read_bytes()).hexdigest(),
                    'dimension': 768, 'preprocessing': 'rgb-exif-preview-1600-v1'}
        manifest_path = self.directory/'model-manifest.json'
        if manifest_path.is_file() and json.loads(manifest_path.read_text('utf8')) != manifest:
            raise ValueError('图搜图库模型版本不一致，请为新模型使用独立检索目录')
        if self.database.is_file():
            from contextlib import closing
            with closing(sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro',uri=True)) as c:
                tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables - {'image','image_features','video','video_features'}:
                raise ValueError('图搜图必须使用独立的 MaterialSearch 数据库，不能使用元数据库')
            if tables and not manifest_path.is_file(): raise ValueError('已有图搜图库缺少模型版本说明，未覆盖原数据')
        os.environ.update(SQLALCHEMY_DATABASE_URL='sqlite:///'+str(self.database), MODEL_NAME=str(self.model),
                          DEVICE='cpu', SCAN_PROCESS_BATCH_SIZE='1', IGNORE_STRINGS='', IMAGE_MIN_WIDTH='0',
                          IMAGE_MIN_HEIGHT='0', TEMP_PATH=str(self.directory/'tmp'), FRAME_INTERVAL='2',
                          USE_TF='0', USE_FLAX='0', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                          TOKENIZERS_PARALLELISM='false')
        sys.path.insert(0,str(Path(__file__).parent/'vendor'))
        # 上游配置会打印环境变量，捕获后丢弃；stdout 只保留 JSON 行协议。
        with redirect_stdout(io.StringIO()):
            import torch
            torch.set_num_threads(4)
            from materialsearch_core import models, database, search
            from materialsearch_core.embeddings import FeatureEncoder
        self.models = models; self.db = database; self.search_api = search
        self.encoder = FeatureEncoder(str(self.model), dimension=768, max_batch_size=1)
        from material_search_detail import FrameMatcher
        self.frame_matcher=FrameMatcher()
        from materialsearch_core import process_assets
        process_assets.encoder = self.encoder
        models.create_tables()
        with models.engine.connect() as c:
            c.exec_driver_sql('PRAGMA journal_mode=WAL'); c.exec_driver_sql('PRAGMA busy_timeout=5000')
        self._json(manifest_path,manifest); self._counts()
        self.thread = threading.Thread(target=self._run, name='materialsearch-index', daemon=True); self.thread.start()

    @staticmethod
    def _json(path, value):
        temporary = path.with_suffix(path.suffix+'.tmp')
        temporary.write_text(json.dumps(value,ensure_ascii=False,separators=(',',':')),encoding='utf8'); os.replace(temporary,path)

    def checkpoint(self, force=False):
        if force or time.monotonic()-self._saved > 5:
            self._json(self.cache_path,{'files':self.files,'failures':self.failures}); self._saved = time.monotonic()

    @staticmethod
    def inside(path, root):
        try: Path(path).absolute().relative_to(Path(root).absolute()); return True
        except ValueError: return False

    def owner(self, path):
        if any(part.casefold() in ('#recycle','$recycle.bin','system volume information') for part in Path(path).parts):return None
        matches = [r for r in self.roots if self.inside(path,r['path']) and not any(self.inside(path,x) for x in r.get('excluded',[]))]
        return max(matches,key=lambda r:len(r['path'])) if matches else None

    def configure(self, body):
        with self.lock:
            roots = body['roots']
            changed = roots != self.roots or body.get('refresh') or body.get('retry') or self.paused and not body['paused']
            if roots != self.roots: self.roots = roots
            if changed:self.epoch += 1; self._last_scan = 0
            self.enabled = bool(body['enabled']); self.paused = bool(body['paused'])
            if body.get('retry'): self.failures.clear(); self._last_scan = 0
            if self.paused:self.phase='paused'
            elif not self.enabled:self.phase='disabled'
            elif not roots:self.phase='idle'
            elif changed:self.phase='discovering'
        self.wake.set(); return self.state()

    def state(self):
        with self.lock:
            return {'engine':'MaterialSearch','database':str(self.database),'enabled':self.enabled,'paused':self.paused,
                    'phase':self.phase,'total':self.total,'done':self.done,'indexed':self.indexed,
                    'updated':self.updated,'reused':self.reused,'failed':len(self.failures),
                    'failures':[dict(path=k,**v) for k,v in list(self.failures.items())[:20]],
                    'offline':list(self.offline),'error':self.error,'ready':True}

    def valid(self, epoch): return not self.closed.is_set() and self.enabled and not self.paused and self.epoch == epoch

    def _counts(self):
        with self.models.DatabaseSession() as c:
            self.indexed = c.query(self.models.Image).count()+c.query(self.models.Video).count()

    def _enumerate(self, root, epoch):
        if not Path(root['path']).is_dir(): raise OSError('归档位置离线或不可访问')
        result = []; errors = []
        for folder, directories, names in os.walk(root['path'], followlinks=False, onerror=errors.append):
            if not self.valid(epoch): raise InterruptedError('建库已暂停')
            directories[:] = [name for name in directories if not Path(folder,name).is_symlink()
                               and name.casefold() not in ('#recycle','$recycle.bin','system volume information')
                               and not getattr(Path(folder,name),'is_junction',lambda:False)()
                               and not self.inside(Path(folder,name),self.directory.parent)
                               and not any(self.inside(Path(folder,name),x) for x in root.get('excluded',[]))]
            for name in names:
                path = Path(folder,name)
                if path.suffix.lower() in self.media_types and not path.is_symlink() and not self.inside(path,self.directory.parent): result.append(str(path))
        return result,errors

    def _signature(self, path):
        stat = Path(path).stat(); return [stat.st_size,stat.st_mtime_ns]

    def _hash(self, path, epoch):
        value = hashlib.sha1()
        with open(path,'rb') as stream:
            while True:
                if not self.valid(epoch): raise InterruptedError('建库已暂停')
                block = stream.read(1024*1024)
                if not block: break
                value.update(block)
        return value.hexdigest()

    def _preview(self, path):
        from material_search_preview import preview
        return preview(path,self.media_types[Path(path).suffix.lower()],self.exiftool)

    def _one(self, path, epoch):
        video = self.media_types[Path(path).suffix.lower()] == 'video'
        table = self.models.Video if video else self.models.Image
        features = self.models.VideoFeatures if video else self.models.ImageFeatures
        signature = self._signature(path)
        with self.models.DatabaseSession() as session:
            old = session.query(table).filter_by(path=path).first()
            cached = self.files.get(path)
            if old and cached and cached[:2] == signature and old.checksum == cached[2] and session.query(features.id).filter_by(checksum=old.checksum).first():
                self.reused += 1; return
        failure = self.failures.get(path)
        if failure and failure['signature'] == signature: return
        checksum = self._hash(path,epoch)
        with self.models.DatabaseSession() as session:
            reused = bool(session.query(features.id).filter_by(checksum=checksum).first())
        values = None
        if not reused:
            while self.queries and self.valid(epoch): self.closed.wait(.05)
            if not self.valid(epoch): raise InterruptedError('建库已暂停')
            if video:
                from materialsearch_core.process_assets import process_video
                values = []
                for seconds, vector in process_video(path):
                    if not self.valid(epoch): raise InterruptedError('建库已暂停')
                    values.append((seconds,vector.tobytes()))
                if not values: raise ValueError('视频没有可解码的采样帧')
            else: values = self.encoder.encode_images([self._preview(path)])[0].tobytes()
        if not self.valid(epoch) or signature != self._signature(path): raise InterruptedError('源文件变化或建库已暂停')
        with self.write_lock,self.models.DatabaseSession() as session:
            if not self.valid(epoch):raise InterruptedError('归档范围已变化')
            session.query(table).filter_by(path=path).delete(synchronize_session=False)
            modified = datetime.datetime.fromtimestamp(signature[1]/1e9)
            if video: self.db.add_video(session,path,modified,checksum,iter(values or []))
            else: self.db.add_image(session,path,modified,checksum,values or b'')
        self.files[path] = [*signature,checksum]; self.failures.pop(path,None); self.updated += 1
        self.indexed += 1 if not old else 0
        self.search_api.clean_cache()

    def _scan(self, epoch):
        self.phase = 'discovering'; self.done = self.total = self.reused = self.updated = 0; self.offline = []; self.error = ''
        for root in list(self.roots):
            if not self.valid(epoch): return
            try: paths,read_errors = self._enumerate(root,epoch)
            except InterruptedError: return
            except OSError as error:
                self.offline.append({'root':root['id'],'path':root['path'],'error':str(error)}); continue
            if read_errors:self.offline.append({'root':root['id'],'path':root['path'],'error':'部分目录未完整读取：'+str(read_errors[0])})
            self.total += len(paths); self.phase = 'indexing'; complete = not read_errors
            for path in paths:
                if not self.valid(epoch): complete = False; break
                try: self._one(path,epoch)
                except InterruptedError:
                    if not self.valid(epoch): complete = False; break
                    self.failures[path] = {'signature':self._signature(path) if Path(path).exists() else [],'error':'文件正在变化，稍后重试'}
                except Exception as error:
                    self.failures[path] = {'signature':self._signature(path) if Path(path).exists() else [],'error':str(error)[:400]}
                self.done += 1; self.checkpoint()
            if complete and self.valid(epoch):
                present = set(paths)
                # 仅完整成功枚举的根范围可移除缺失记录；断线不执行全库清理。
                with self.write_lock,self.models.DatabaseSession() as session:
                    for table in (self.models.Image,self.models.Video):
                        for row in session.query(table).filter(table.path.startswith(str(Path(root['path'])).rstrip('\\/')+os.sep,autoescape=True)):
                            if row.path not in present: session.delete(row); self.files.pop(row.path,None); self.failures.pop(row.path,None)
                    session.commit()
        self._counts(); self.checkpoint(True); self.search_api.clean_cache()
        if self.epoch == epoch:
            self.phase = 'paused' if self.paused else 'idle'; self._last_scan = time.monotonic()

    def _run(self):
        while not self.closed.is_set():
            if self.enabled and not self.paused and self.roots and time.monotonic()-self._last_scan >= 1800:
                try: self._scan(self.epoch)
                except Exception as error: self.error = str(error)[:400]; self.phase = 'failed'; self._last_scan = time.monotonic()
            elif self.enabled and not self.paused and self.pending:
                with self.lock:paths=list(self.pending);self.pending.clear();epoch=self.epoch
                self.phase='indexing';self.total=len(paths);self.done=self.updated=self.reused=0
                for path in paths:
                    if not self.valid(epoch):break
                    if not self.owner(path):continue
                    try:
                        if Path(path).is_file():self._one(path,epoch)
                        else:
                            with self.write_lock,self.models.DatabaseSession() as session:
                                for table in (self.models.Image,self.models.Video):session.query(table).filter_by(path=path).delete(synchronize_session=False)
                                session.commit()
                            self.files.pop(path,None);self.failures.pop(path,None)
                    except InterruptedError:pass
                    except Exception as error:self.failures[path]={'signature':self._signature(path) if Path(path).exists() else [],'error':str(error)[:400]}
                    self.done+=1
                self._counts();self.checkpoint(True);self.search_api.clean_cache()
                self.phase='paused' if self.paused else 'idle'
            else:
                if self.paused: self.phase = 'paused'
                self.wake.wait(.3); self.wake.clear()

    def item(self, identifier):
        kind, number, *version = identifier.split(':')
        if kind not in ('material','materialv') or len(version)!=1:raise ValueError('图片检索标识已失效，请重新搜索')
        table = self.models.Video if kind == 'materialv' else self.models.Image
        with self.models.DatabaseSession() as session:
            row = session.query(table).filter_by(id=int(number)).first()
            if not row or not self.owner(row.path): raise ValueError('素材不在当前归档范围')
            signature=self._signature(row.path)
            if identifier != self.reference_id(kind,number,row.path,signature):raise ValueError('素材已变化，请重新搜索')
            return {'path':row.path,'material_id':identifier,'signature':signature}

    @staticmethod
    def reference_id(kind,number,path,signature):
        token=hashlib.sha256((str(path).casefold()+':'+str(signature[0])+':'+str(signature[1])).encode('utf8')).hexdigest()[:32]
        return f'{kind}:{number}:{token}'

    @staticmethod
    def similarity_threshold(body,image_default=.85,text_default=.36):
        text_only=bool(body.get('text')) and not any(body.get(key) for key in ('path','image','item_id'))
        value=body.get('threshold',text_default if text_only else image_default)
        if isinstance(value,bool):raise ValueError('相似度阈值应为 0–1')
        try:value=float(value)
        except (ValueError,TypeError):raise ValueError('相似度阈值应为 0–1') from None
        if not math.isfinite(value) or not 0<=value<=1:raise ValueError('相似度阈值应为 0–1')
        return value

    @staticmethod
    def match_mode(body):
        mode=body.get('match_mode','frame')
        if mode not in ('frame','semantic'):raise ValueError('请选择近似画面或相似内容')
        if body.get('text') and not any(body.get(key) for key in ('path','image','item_id')):return 'semantic'
        return mode

    @staticmethod
    def _video_frame(path,seconds):
        import cv2
        from PIL import Image
        stream=cv2.VideoCapture(str(path))
        try:
            if seconds>0:stream.set(cv2.CAP_PROP_POS_MSEC,seconds*1000)
            ok,frame=stream.read()
            if not ok:raise ValueError('视频匹配帧无法解码')
            return Image.fromarray(cv2.cvtColor(frame,cv2.COLOR_BGR2RGB))
        finally:stream.release()

    def _best_video_frame(self,path,vector,start,end):
        import numpy as np
        with self.models.DatabaseSession() as session:
            rows=session.query(self.models.VideoFeatures.frame_time,self.models.VideoFeatures.features).join(
                self.models.Video,self.models.Video.checksum==self.models.VideoFeatures.checksum).filter(
                self.models.Video.path==path,self.models.VideoFeatures.frame_time>=max(0,start),
                self.models.VideoFeatures.frame_time<=end).all()
        if not rows:raise ValueError('视频匹配片段没有可核验采样帧')
        scores=np.stack([np.frombuffer(row.features,dtype=np.float32) for row in rows])@vector.reshape(-1)
        return float(rows[int(np.argmax(scores))].frame_time)

    def search(self, body):
        if not self.enabled: raise ValueError('请先在检索设置开启“自动建立图搜图库”')
        threshold=self.similarity_threshold(body,self.search_api.IMAGE_THRESHOLD/100,self.search_api.POSITIVE_THRESHOLD/100)
        mode=self.match_mode(body)
        self.queries += 1
        start = time.perf_counter()
        try:
            from PIL import Image, ImageOps
            vector = None;reference=None
            if body.get('path'):
                if not self.owner(body['path']): raise ValueError('参考图不在已添加归档内')
                if self.media_types[Path(body['path']).suffix.lower()]=='video':
                    import numpy as np
                    with self.models.DatabaseSession() as session:
                        saved=session.query(self.models.VideoFeatures.features,self.models.VideoFeatures.frame_time).join(self.models.Video,self.models.Video.checksum==self.models.VideoFeatures.checksum).filter(self.models.Video.path==body['path']).order_by(self.models.VideoFeatures.frame_time).first()
                    if not saved:raise ValueError('参考视频尚未完成图像索引')
                    vector=np.frombuffer(saved[0],dtype=np.float32).reshape(1,-1)
                    if mode=='frame':reference=self._video_frame(body['path'],saved.frame_time)
                else:
                    reference=self._preview(body['path']);vector=self.encoder.encode_images([reference])
            elif body.get('text'): vector = self.encoder.encode_texts([body['text']])
            else:
                raw = base64.b64decode(body['image'].split(',',1)[-1],validate=True)
                with Image.open(io.BytesIO(raw)) as image:
                    image.thumbnail((1600,1600)); reference = ImageOps.exif_transpose(image).convert('RGB')
                vector = self.encoder.encode_images([reference])
            reference_frame=self.frame_matcher.prepare(reference) if mode=='frame' else None
            recall_threshold=min(.85,threshold) if mode=='frame' else threshold
            with redirect_stdout(io.StringIO()):
                results = self.search_api.search_image_by_feature(vector,positive_threshold=recall_threshold*100) if body.get('media')!='video' else []
                if body.get('media') in (None,'','video'):
                    videos=self.search_api.search_video_by_feature(vector,positive_threshold=recall_threshold*100)
                    with self.models.DatabaseSession() as session:
                        ids={row.path:row.id for row in session.query(self.models.Video.id,self.models.Video.path)}
                    for result in videos:result['material_id']='materialv:'+str(ids[result['path']])
                    results.extend(videos)
                results.sort(key=lambda row:row['score'],reverse=True)
            output = [];verified=0;verification_errors=[];incomplete=False
            limit=max(1,min(100,int(body.get('limit',60))))
            verification_limit=max(32,min(256,limit*3));verification_deadline=time.monotonic()+5
            for result in results:
                owner = self.owner(result['path'])
                if not owner or body.get('root') and owner['id'] != body['root']: continue
                if body.get('media') and self.media_types[Path(result['path']).suffix.lower()] != body['media']: continue
                # 当前实际文件版本变化后，旧特征立即不参与命中。
                try:
                    signature=self._signature(result['path'])
                    if self.files.get(result['path'],[])[:2] != signature: continue
                except OSError: continue
                detail=None;matched_seconds=None
                if mode=='frame':
                    if verified>=verification_limit or time.monotonic()>verification_deadline:incomplete=True;break
                    verified+=1
                    try:
                        if 'start_time' in result:
                            matched_seconds=self._best_video_frame(result['path'],vector,result['start_time'],result['end_time'])
                        key=(result['path'],*signature,matched_seconds)
                        candidate=self.frame_matcher.cached(key)
                        if candidate is None:
                            pixels=self._video_frame(result['path'],matched_seconds) if matched_seconds is not None else self._preview(result['path'])
                            candidate=self.frame_matcher.prepare(pixels,key=key)
                        detail=self.frame_matcher.compare(reference_frame,candidate)
                        if signature!=self._signature(result['path']):continue
                        if not detail['comparable'] or detail['score']<threshold:continue
                    except Exception as error:
                        incomplete=True
                        if len(verification_errors)<5:verification_errors.append({'path':result['path'],'error':str(error)[:200]})
                        continue
                number = result['url'].rsplit('/',1)[-1]
                semantic_score=min(1.,max(0.,result['score']))
                value={'path':result['path'],'score':detail['score'] if detail else semantic_score,
                       'semantic_score':semantic_score,'detail':detail,'match_mode':mode,
                       'root':owner['id'],'material_id':result.get('material_id','material:'+number),'signature':signature}
                kind,number=value['material_id'].split(':')
                value['material_id']=self.reference_id(kind,number,result['path'],signature)
                if 'start_time' in result:
                    center=matched_seconds if matched_seconds is not None else (result['start_time']+result['end_time'])/2
                    value.update(timestamp_ms=round(center*1000),clip={'start':max(0,result['start_time']),'end':result['end_time'],'center':center})
                output.append(value)
            output.sort(key=lambda row:row['score'],reverse=True)
            return {'rows':output[:limit],'candidates':len(output),'indexed_vectors':self.indexed,'has_more':False,
                    'threshold_applied':threshold,'match_mode':mode,'verified_candidates':verified,
                    'verification_errors':verification_errors,
                    'next_cursor':'','backend':'chinese-clip-large-336:768','engine':'MaterialSearch',
                    'approximate':False,'incomplete':incomplete,'index_state':self.state(),
                    'elapsed_ms':round((time.perf_counter()-start)*1000,2)}
        finally: self.queries -= 1

    def close(self):
        self.closed.set(); self.wake.set(); self.thread.join(timeout=5)
        if not self.thread.is_alive(): self.checkpoint(True); self.encoder.close(); self.models.engine.dispose()


def main():
    index = ArchiveIndex(json.loads(Path(sys.argv[1]).read_text('utf8')))
    print(json.dumps({'ready':index.state()},ensure_ascii=False),flush=True)
    try:
        for line in sys.stdin:
            request = json.loads(line)
            try:
                command = request['command']; body = request.get('body',{})
                result = index.configure(body) if command == 'configure' else index.search(body) if command == 'search' else index.item(body['id']) if command == 'item' else index.state()
                if command=='changes':
                    with index.lock:
                        index.pending.update(body.get('paths',[]))
                        if body.get('refresh'):index._last_scan=0
                    index.wake.set()
                print(json.dumps({'result':result},ensure_ascii=False),flush=True)
            except Exception as error: print(json.dumps({'error':str(error)[:600]},ensure_ascii=False),flush=True)
    finally: index.close()


if __name__ == '__main__':
    try: main()
    except Exception as error: print(json.dumps({'error':str(error)[:600]},ensure_ascii=False),flush=True)
