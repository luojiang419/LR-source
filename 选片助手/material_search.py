"""资产检索与 MaterialSearch 独立库的适配；不向元数据库写入特征。"""
import importlib.util
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from file_safety import atomic_write


class MaterialSearch:
    def __init__(self,catalog,material_root,*,autostart=True):
        self.catalog = catalog; self.directory = catalog.data/'visual-search'
        self.config_path = catalog.data/'visual-search-settings.json'
        self.config = {'image_index_enabled':False,'image_index_paused':False,'material_root':str(material_root)}
        if self.config_path.is_file(): self.config.update(json.loads(self.config_path.read_text('utf8')))
        self.lock = threading.RLock(); self.rpc_lock = threading.RLock()
        self.closed = threading.Event(); self.wake = threading.Event(); self.process = None; self.lines = queue.Queue()
        self.log = None; self.last_roots = None
        self.watchers={};self.watch_failed={}
        self.status = {'engine':'MaterialSearch','ready':False,'phase':'disabled','indexed':0,'total':0,'done':0,'failed':0,'error':''}
        self.thread = threading.Thread(target=self._run,name='materialsearch-supervisor',daemon=True)
        if autostart: self.start()

    def start(self):
        with self.lock:
            if not self.thread.ident and not self.closed.is_set(): self.thread.start()

    def state(self):
        return {**self.status,'enabled':self.config['image_index_enabled'],'paused':self.config['image_index_paused'],
                'database':str(self.directory/'assets.db')}

    def model_directory(self):
        hub = Path(self.config['material_root'])/'huggingface/hub/models--OFA-Sys--chinese-clip-vit-large-patch14-336px/snapshots'
        return next((p.parent for p in hub.glob('*/config.json')),None)

    @staticmethod
    def worker_directory():
        if not getattr(sys,'frozen',False):return Path(__file__).parent
        directory=Path(sys._MEIPASS)/'material-search-runtime'
        if not (directory/'material_search_worker.py').is_file():raise ValueError('图搜图运行文件不完整，请修复安装')
        return directory

    @staticmethod
    def worker_command(python,runtime,settings_path):
        return [str(python),'-B','-X','utf8','-u',str(Path(runtime)/'material_search_worker.py'),str(settings_path)]

    def roots(self):
        with self.catalog.connect() as c:
            roots = [dict(row) for row in c.execute('SELECT id,path FROM roots WHERE removed=0 ORDER BY id')]
            exclusions = list(c.execute('SELECT root,folder FROM exclusions'))
        for root in roots:
            root['excluded'] = [str(Path(root['path'])/folder) for rid,folder in exclusions if rid == root['id']]
            from preview_cache import _LOCATIONS
            root['excluded'].extend([str(self.catalog.data),str(_LOCATIONS.get(self.catalog.data.absolute(),self.catalog.data)/'catalog_previews')])
        return roots

    def configure(self,body):
        with self.lock:
            updated = dict(self.config)
            for name in ('image_index_enabled','image_index_paused'):
                if name in body:
                    if type(body[name]) is not bool: raise ValueError('图搜图开关参数无效')
                    updated[name] = body[name]
            if 'material_root' in body:
                if not isinstance(body['material_root'],str) or not body['material_root'].strip(): raise ValueError('模型目录不能为空')
                updated['material_root'] = body['material_root']
            action = body.get('image_index_action')
            if action not in (None,'refresh','retry'): raise ValueError('图搜图操作无效')
            changed_model = updated['material_root'] != self.config['material_root']
            atomic_write(self.config_path,json.dumps(updated,ensure_ascii=False).encode('utf8')); self.config = updated
        if changed_model: self.stop_worker()
        self.start(); self.wake.set()
        if action or self.process:
            self.sync(retry=action == 'retry', refresh=action == 'refresh')
        return self.state()

    def _start(self):
        if self.closed.is_set(): raise ValueError('图搜图服务已关闭')
        if self.process and self.process.poll() is None: return
        if self.process:self.stop_worker()
        model = self.model_directory()
        if model is None: raise ValueError('尚未发现 Chinese-CLIP 模型，请在检索设置选择 MaterialSearch 安装目录')
        required = ('torch','transformers','sqlalchemy','cv2')
        python = sys.executable if not getattr(sys,'frozen',False) and all(importlib.util.find_spec(name) for name in required) else str(Path(self.config['material_root'])/'python.exe')
        if not Path(python).is_file(): raise ValueError('图搜图 Python 运行环境不可用')
        self.directory.mkdir(parents=True,exist_ok=True)
        from asset_catalog import MEDIA_EXTENSIONS,media_type
        settings = {'directory':str(self.directory),'model':str(model),
                    'media_types':{extension:media_type(extension) for extension in MEDIA_EXTENSIONS},
                    'frozen_parent':bool(getattr(sys,'frozen',False))}
        bundle=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))
        runtime=self.worker_directory()
        program_root=Path(os.environ.get('FRAMEKEEP_APP_ROOT',bundle.parent.parent if settings['frozen_parent'] else bundle.parent))
        settings['exiftool'] = next((str(p) for p in (program_root/'runtime/exiftool/exiftool.exe',bundle/'runtime/exiftool/exiftool.exe',program_root/'.runtime/exiftool/exiftool.exe') if p.is_file()),'')
        settings_path = self.directory/'worker-settings.json'; atomic_write(settings_path,json.dumps(settings,ensure_ascii=False).encode('utf8'))
        self.log = (self.directory/'worker.log').open('a',encoding='utf8')
        self.lines = queue.Queue(); lines = self.lines
        env = os.environ.copy(); env.update(PYTHONDONTWRITEBYTECODE='1',PYTHONUNBUFFERED='1',PYTHONUTF8='1',PYTHONIOENCODING='utf-8',USE_TF='0',USE_FLAX='0',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
        if settings['frozen_parent']:
            env.pop('PYTHONPATH',None);env.pop('PYTHONHOME',None)
            env['PATH']=os.pathsep.join(path for path in env.get('PATH','').split(os.pathsep) if path and not Path(path).absolute().is_relative_to(bundle.absolute()))
        self.status = {**self.status,'phase':'loading','error':''}
        process = subprocess.Popen(self.worker_command(python,runtime,settings_path),
                                   stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,text=True,encoding='utf8',env=env,
                                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.process = process
        def reader():
            try:
                for line in process.stdout:
                    try:
                        value=json.loads(line)
                        if isinstance(value,dict):lines.put(value)
                    except ValueError: continue
            except (OSError,UnicodeError,ValueError) as error:
                lines.put({'error':'MaterialSearch 通信失败：'+str(error)[:200]})
            finally:lines.put({'error':'MaterialSearch 工作进程已退出'})
        threading.Thread(target=reader,name='materialsearch-response',daemon=True).start()
        try:
            value = lines.get(timeout=90)
            if value.get('error'): raise ValueError(value['error'])
            self.status = value['ready']; self.last_roots = None
        except Exception:
            self.stop_worker(); raise

    def request(self,command,body=None,timeout=180):
        with self.rpc_lock:
            self._start()
            self.process.stdin.write(json.dumps({'command':command,'body':body or {}},ensure_ascii=False)+'\n'); self.process.stdin.flush()
            try: value = self.lines.get(timeout=timeout)
            except queue.Empty:
                self.stop_worker(); raise ValueError('图搜图处理超时，请重试') from None
            if value.get('error'): raise ValueError(value['error'])
            return value['result']

    def sync(self,*,retry=False,refresh=False):
        roots = self.roots()
        self.status = self.request('configure',{'roots':roots,'enabled':self.config['image_index_enabled'],
                                               'paused':self.config['image_index_paused'],'retry':retry,'refresh':refresh})
        self.last_roots = roots

    def _run(self):
        while not self.closed.is_set():
            if self.config['image_index_enabled'] or self.process:
                try:
                    if self.last_roots != self.roots() or self.process is None: self.sync()
                    else:
                        state = self.request('state',timeout=15)
                        if state.get('enabled') != self.config['image_index_enabled'] or state.get('paused') != self.config['image_index_paused']: self.sync()
                        else: self.status = state
                    self._watch_changes()
                except Exception as error: self.status = {**self.status,'phase':'failed','error':str(error)[:400]}
            self.wake.wait(1); self.wake.clear()

    def _watch_changes(self):
        from native_directory_index import DirectoryChanges
        from asset_catalog import MEDIA_EXTENSIONS
        roots=self.roots() if self.config['image_index_enabled'] else []
        wanted={root['id'] for root in roots}
        for key in list(self.watchers):
            if key not in wanted:self.watchers.pop(key).close()
        changed=set();refresh=False
        for root in roots:
            key=root['id'];watcher=self.watchers.get(key)
            if watcher is None and time.monotonic()-self.watch_failed.get(key,-60)>60:
                try:watcher=DirectoryChanges(root['path']);self.watchers[key]=watcher
                except OSError:self.watch_failed[key]=time.monotonic()
            if watcher:
                delta=watcher.poll();refresh=refresh or delta['overflow']
                for relative in delta['paths']:
                    path=Path(root['path'])/relative
                    if any(path.is_relative_to(excluded) for excluded in root.get('excluded',[])):continue
                    if path.suffix.lower() in MEDIA_EXTENSIONS:changed.add(str(path))
                    elif not path.suffix:refresh=True
                if not watcher.active:watcher.close();self.watchers.pop(key,None);self.watch_failed[key]=time.monotonic()
        if changed or refresh:self.request('changes',{'paths':list(changed),'refresh':refresh})

    def search(self,body):
        if not self.config['image_index_enabled']: raise ValueError('请先在检索设置开启“自动建立图搜图库”')
        self.start()
        with self.rpc_lock:
            self.sync()
            request = dict(body)
            if body.get('item_id'): request['path'] = self.catalog.item(body['item_id'])['path']
            if request.get('text') and (not isinstance(request['text'],str) or len(request['text']) > 300): raise ValueError('检索文字应为 1–300 字')
            result = self.request('search',request)
        roots=self.roots();paths=[row['path'] for row in result['rows']]
        with self.catalog.connect() as c:
            existing={row['path']:dict(row) for row in c.execute('SELECT * FROM items WHERE path IN ('+','.join('?' for _ in paths)+')',paths)} if paths else {}
        result['rows'] = [self.hydrate(row,roots,existing) for row in result['rows']]
        self.status = result['index_state']; return result

    def hydrate(self,row,roots=None,existing=None):
        from asset_catalog import _id,media_type,media_format,pair_key,archive_folder
        path = Path(row['path']); owner = next((root for root in (roots if roots is not None else self.roots()) if root['id'] == row['root']),None)
        if not owner: raise ValueError('归档位置已移除')
        identifier = _id(str(path).casefold())
        signature=row.get('signature')
        if signature is None:
            stat=path.stat();signature=[stat.st_size,stat.st_mtime_ns]
        if existing is None:
            with self.catalog.connect() as c: found = c.execute('SELECT * FROM items WHERE id=?',(identifier,)).fetchone()
        else:found=existing.get(str(path))
        if found and found['online'] and found['size']==signature[0] and abs(found['modified']-signature[1]/1e9)<.001: value = dict(found)
        else:
            relative = path.relative_to(owner['path']).as_posix(); extension = path.suffix.lower()
            fingerprint = __import__('hashlib').sha256(f'{str(path).casefold()}:{signature[0]}:{signature[1]}'.encode()).hexdigest()
            value = {'id':row['material_id'],'root':owner['id'],'path':str(path),'relative':relative,'folder':archive_folder(relative),
                     'name':path.name,'extension':extension,'size':signature[0],'modified':signature[1]/1e9,'fingerprint':fingerprint,
                     'sku':'','normalized':'','source':'unmatched','confidence':None,'online':1,'media_type':media_type(extension),
                     'media_format':media_format(extension),'pair_key':pair_key(str(path)),'codec':'','duration_ms':None,'width':None,'height':None}
        value.update(material_id=row['material_id'],preview='/api/catalog/preview/'+value['id'])
        if 'score' in row:
            value.update(score=row['score'],match_mode=row.get('match_mode','semantic'),semantic_score=row.get('semantic_score',row['score']),
                         detail=row.get('detail'),reasons=['画面结构与局部细节接近' if row.get('match_mode')=='frame' else 'CLIP 内容相似'])
        if 'clip' in row:
            value.update(clip=row['clip'],timestamp_ms=row['timestamp_ms'],result_id=value['id']+':clip:'+str(row['timestamp_ms']))
            value['preview']+='?frame='+str(row['timestamp_ms'])
        return value

    def item(self,identifier):
        result = self.request('item',{'id':identifier})
        from pathlib import Path
        roots = [root for root in self.roots() if Path(result['path']).is_relative_to(root['path'])]
        if not roots: raise ValueError('素材已移出归档范围')
        result['root'] = max(roots,key=lambda root:len(root['path']))['id']
        return self.hydrate(result)

    def preview(self,identifier,size=420,frame=None):
        import io
        from material_search_preview import preview
        item = self.item(identifier)
        if item['media_type']=='video':
            from video_preview import extract_frame,find_tool
            if frame is None:image = extract_frame(item['path'],min(2048,int(size)),3)
            else:
                from PIL import Image
                payload=subprocess.run([str(find_tool('ffmpeg')),'-v','error','-ss',str(max(0,int(frame))/1000),'-i',item['path'],
                    '-frames:v','1','-vf',f'scale={min(2048,int(size))}:{min(2048,int(size))}:force_original_aspect_ratio=decrease',
                    '-f','image2pipe','-vcodec','mjpeg','pipe:1'],capture_output=True,timeout=30,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),check=True).stdout
                with Image.open(io.BytesIO(payload)) as source:image=source.convert('RGB')
        else:image = preview(item['path'],item['media_type'])
        image.thumbnail((min(2048,int(size)),min(2048,int(size))))
        output = io.BytesIO(); image.save(output,'JPEG',quality=85); return output.getvalue()

    def stop_worker(self):
        with self.rpc_lock:
            process = self.process; self.process = None; self.last_roots = None
            if process:
                if process.stdin:
                    try:process.stdin.close()
                    except OSError:pass
                try: process.wait(timeout=8)
                except subprocess.TimeoutExpired: process.terminate(); process.wait(timeout=5)
                if process.stdout: process.stdout.close()
            if self.log: self.log.close(); self.log = None

    def close(self):
        self.closed.set(); self.wake.set()
        if self.thread.is_alive(): self.thread.join(timeout=2)
        self.stop_worker()
        for watcher in self.watchers.values():watcher.close()
        self.watchers.clear()
