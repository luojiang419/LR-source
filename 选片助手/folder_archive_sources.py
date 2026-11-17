"""正式资产库到独立归档任务的来源快照与移动路径同步。"""
from contextlib import contextmanager, ExitStack, closing
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

import media_engine as media
from asset_catalog import pair_key
from sku_folder_archive import files_under, camera_metadata, capture_key
from folder_archive_metadata import capture_metadata_batches,metadata_sidecars
from sku_archive_paths import directory_sku

# 与现场入库及已确认OCR归属一致；path_rule/vlm候选不构成归档确认。
CONFIRMED_CATALOG_SOURCES=('manual','embedded','shooting','ocr_folder','folder_default','portable')


class FolderArchiveSources:
    def __init__(self,store,archive,catalog,watch=None):
        self.store,self.archive,self.catalog,self.watch=store,archive,catalog,watch

    def selection(self,root_id,folder=''):
        with self.catalog.connect() as c:
            root=c.execute('SELECT * FROM roots WHERE id=? AND removed=0',(root_id,)).fetchone()
        if not root:raise ValueError('请在左侧选择一个实际资产文件夹')
        relative=str(folder or '').replace('\\','/');parts=relative.split('/')
        if (relative.startswith('/') and relative!='/') or any(p in ('.','..') or ':' in p for p in parts):raise ValueError('无效的子目录路径')
        base=Path(root['path']).resolve();path=base.joinpath(*filter(None,parts)).resolve()
        if not path.is_relative_to(base):raise ValueError('所选目录超出已登记位置')
        if not path.is_dir() and relative.strip('/'):
            # 旧共享索引曾保留子根root，却写入上级根的relative/folder。
            # 只接受同一登记根下所有精确匹配记录一致指向的真实目录，绝不猜前缀。
            actual=set();found=False
            with self.catalog.connect() as c:
                for row in c.execute('SELECT path FROM items WHERE root=? AND folder=?',(root_id,'/'.join(filter(None,parts)))):
                    found=True;parent=Path(row['path']).absolute().parent
                    if not parent.is_relative_to(base):raise ValueError('目录索引与登记位置不一致，请刷新目录后重选')
                    actual.add(parent)
                    if len(actual)>1:raise ValueError('旧目录索引对应多个实际位置，请刷新目录后重选')
            if found:path=next(iter(actual)).resolve()
        if not path.is_relative_to(base):raise ValueError('所选目录超出已登记位置')
        if not path.is_dir():raise ValueError('所选目录当前无法访问：'+str(path))
        return {'root_id':root_id,'folder':path.relative_to(base).as_posix() if path!=base else '', 'path':str(path)}

    def scope(self,root_id,folder=''):
        return Path(self.selection(root_id,folder)['path'])

    def guard(self,roots):
        roots=list(map(lambda p:Path(p).resolve(),roots))
        if self.watch:
            state=self.watch.state()
            value=state.get('folder') or state.get('root')
            if state.get('enabled') and value:
                watched=Path(value).resolve()
                if any(watched==r or watched in r.parents or r in watched.parents for r in roots):
                    raise ValueError('此目录正在拍摄监控，请先停止监控后归档')
        with self.archive.connect() as c:
            paths=[r[0] for r in c.execute("SELECT path FROM metadata_jobs WHERE status IN ('queued','running')")]
        busy=[p for p in paths if any(Path(p).is_relative_to(root) for root in roots)]
        if busy:
            capture=getattr(self.archive,'capture_service',None)
            if not capture or any(not (d:=capture.dataset_for_path(p)) or not d.editing or not d.meta('archive_freeze') for p in busy):
                raise ValueError('此目录仍有元数据写入任务，完成后再归档')

    def snapshot(self,scope,database,*,direct=False,parent_length=4,progress=None,append=False,allow_unassigned=False,allow_empty=False):
        progress=progress or (lambda **_:None)
        files=files_under(scope,progress=progress,stage='scanning_sources')
        if not files and not allow_empty:raise ValueError('所选目录没有支持归档的照片')
        sidecars=metadata_sidecars(files,progress=progress,stage='source_sidecars')
        with closing(sqlite3.connect(database)) as c,c:
            c.execute('CREATE TABLE IF NOT EXISTS captures(id TEXT PRIMARY KEY,source TEXT UNIQUE,fingerprint TEXT,sku TEXT,scope TEXT,rating INTEGER,metadata TEXT,asset_id TEXT)')
            if not append:c.execute('DELETE FROM captures')
            known_paths={};catalog_paths={}
            with self.archive.connect() as ac,self.catalog.connect() as cc:
                for offset in range(0,len(files),256):
                    progress(stage='source_index',done=offset,total=len(files),path=str(files[offset]))
                    paths=[str(p) for p in files[offset:offset+256]];marks=','.join('?' for _ in paths)
                    for row in ac.execute('SELECT l.path AS archive_path,a.* FROM locations l JOIN files f ON f.id=l.file JOIN assets a ON a.id=f.asset WHERE l.path IN ('+marks+')',paths):known_paths[row['archive_path']]=row
                    for row in cc.execute('SELECT path,id,sku,source FROM items WHERE path IN ('+marks+')',paths):catalog_paths[row['path']]=row
                for index,(path,full) in enumerate(capture_metadata_batches(self.archive.writer,files,progress=progress,stage='sources')):
                    progress(stage='sources',done=index,total=len(files),path=str(path))
                    # RAW与旧现场JPG的业务字段可能保存于配套XMP中。
                    for side in sidecars[str(path)]:
                        full.update({k:v for k,v in self.archive.writer.read(side).items() if k.startswith(('XMP-fk:','XMP-xmp:','XMP-dc:'))})
                    known=known_paths.get(str(path));catalog=catalog_paths.get(str(path))
                    # 文件名中提取的XSY等字符串不是已确认业务款号，不能盖过内嵌SKU。
                    confirmed=catalog['sku'] if catalog and catalog['source'] in CONFIRMED_CATALOG_SOURCES else ''
                    sku=(confirmed if catalog and catalog['source']=='portable' else '') or (known['sku'] if known and known['sku'] else '') or (confirmed if catalog and catalog['source']!='embedded' else '') or full.get('XMP-fk:SKU') or confirmed
                    folder_sku=directory_sku(path,scope,parent_length,self.catalog._guess_sku)
                    if folder_sku:
                        if sku and folder_sku.casefold()!=str(sku).casefold() and not (catalog and catalog['source']=='portable'):
                            raise ValueError('款号目录与已确认SKU冲突，请先复核：'+str(path))
                        sku=sku or folder_sku
                    if not sku and direct and not allow_unassigned:
                        raise ValueError('来源图片尚无确认SKU，请先备案：'+str(path))
                    rating=self.archive.ratings.get([path],connection=ac,explicit_only=True)
                    rating=int(rating if rating is not None else full.get('XMP-xmp:Rating') or 0)
                    if not 0<=rating<=5:raise ValueError('来源星级需要复核：'+str(path))
                    shoot=(known['scope'] if known else '') or full.get('XMP-fk:ShootType') or '未分类'
                    asset=(known['id'] if known else '') or full.get('XMP-fk:AssetId') or ''
                    metadata=camera_metadata(full)
                    if getattr(self.archive,'capture_service',None):
                        _,portable=self.archive.capture_service.record_for_path(path)
                        if portable:
                            metadata['portable_decision']=portable.get('decision_id','')
                            expected_file=portable.get('file_id') or portable['id']
                            actual_file=full.get('XMP-fk:FileId')
                            if actual_file and str(actual_file).replace('-','')!=str(expected_file).replace('-',''):raise ValueError('来源文件永久身份与档案不一致：'+str(path))
                            if portable.get('capture_identity'):
                                try:actual_key=capture_key(metadata)
                                except ValueError:actual_key=capture_key(camera_metadata(self.archive.writer.read(path,full=True)))
                                if actual_key!=portable['capture_identity']:raise ValueError('来源快门身份与档案不一致：'+str(path))
                            elif not actual_file and portable.get('file_sha256'):
                                from metadata_writer import sha
                                if sha(path)!=portable['file_sha256']:raise ValueError('来源内容指纹与档案不一致：'+str(path))
                    metadata['directory_sku']=folder_sku
                    try:key=capture_key(metadata)
                    except ValueError:
                        if not direct:
                            try:metadata=camera_metadata(self.archive.writer.read(path,full=True));key=capture_key(metadata)
                            except ValueError:raise ValueError('来源相机身份字段不完整：'+str(path)) from None
                        else:key='direct:'+str(asset or (catalog['id'] if catalog else path))
                    metadata['source_signature']=full.get('_archive_source_signature')
                    capture=getattr(self.archive,'capture_service',None)
                    saved=capture.record_for_path(path)[1] if capture else None
                    if saved:metadata['portable_decision']=saved.get('decision_id','')
                    c.execute('INSERT OR REPLACE INTO captures VALUES (?,?,?,?,?,?,?,?)',
                              (uuid.uuid5(uuid.NAMESPACE_URL,str(path)).hex,str(path),key,str(sku or ''),shoot,rating,json.dumps(metadata),str(asset)))
        progress(stage='sources',done=len(files),total=len(files))

    @contextmanager
    def preprocess_guard(self,plan):
        self.guard([plan['source'],plan['reference_root']])
        with ExitStack() as stack:
            stack.enter_context(self.archive.metadata_io_lock)
            stack.enter_context(self.archive.lock)
            paths={r['source'] for r in plan['reference_records']}|{r['source'] for r in plan['items']}
            for path in sorted(paths):stack.enter_context(self.archive.writer.file_lock(path))
            self.guard([plan['source'],plan['reference_root']])
            yield

    def validate_assignments(self,database,*,progress=None,plan=None):
        progress=progress or (lambda **_:None)
        with closing(sqlite3.connect(database)) as c:
            c.row_factory=sqlite3.Row
            records=c.execute("SELECT source,sku,scope,rating,asset_id,metadata FROM captures WHERE sku<>''").fetchall()
        if plan is not None:
            selected={r['reference'] for r in plan['items']}
            records=[r for r in records if r['source'] in selected]
        with self.archive.connect() as ac,self.catalog.connect() as cc:
            for index,record in enumerate(records):
                progress(stage='validating_assignments',done=index,total=len(records),path=record['source'])
                portable=cc.execute("SELECT sku FROM items WHERE path=? AND source='portable'",(record['source'],)).fetchone()
                if portable:
                    if portable['sku']!=record['sku']:raise ValueError('当前数据集确认款号已改变，请重新匹配：'+record['source'])
                    rating=self.archive.ratings.get([record['source']],connection=ac,explicit_only=True)
                    if rating is not None and rating!=record['rating']:raise ValueError('当前数据集星级已改变，请重新匹配：'+record['source'])
                    expected=json.loads(record['metadata']).get('portable_decision')
                    capture=getattr(self.archive,'capture_service',None)
                    current=capture.record_for_path(record['source'])[1] if capture else None
                    if expected and current and current.get('decision_id')!=expected:raise ValueError('当前数据集确认版本已改变，请重新匹配：'+record['source'])
                    continue
                known=None
                if record['asset_id']:
                    known=ac.execute('SELECT sku,scope FROM assets WHERE id=?',(str(record['asset_id']).replace('-',''),)).fetchone()
                if known and ((known['sku'] and known['sku']!=record['sku']) or (known['scope'] and known['scope']!=record['scope'])):
                    raise ValueError('确认归档前SKU归属已改变，请重新匹配：'+record['source'])
                confirmed=cc.execute('SELECT sku FROM items WHERE path=? AND source IN ('+','.join('?' for _ in CONFIRMED_CATALOG_SOURCES)+')',
                                        (record['source'],*CONFIRMED_CATALOG_SOURCES)).fetchone()
                if confirmed and not (known and known['sku']) and confirmed['sku']!=record['sku']:
                    raise ValueError('已入库款号已改变，请重新匹配：'+record['source'])
                if not (known and known['sku']) and not confirmed and Path(record['source']).is_file():
                    meta=self.archive.writer.read_asset(record['source'])
                    if not (known and known['sku']) and not confirmed and meta.get('XMP-fk:SKU') and meta['XMP-fk:SKU']!=record['sku']:
                        raise ValueError('来源元数据款号已改变，请重新匹配')

    def synchronize(self,plan,*,progress=None):
        moves=plan.get('preprocess_moves',[])
        if not moves:return
        progress=progress or (lambda **_:None)
        # 幂等重放，任务文件先保存完整移动映射，程序中断后可继续同步。
        mapping={r['before']:r['after'] for r in moves}
        with self.archive.lock,self.store.lock:
            with self.store.connect() as c:
                sessions={r['id']:Path(r['root']) for r in c.execute('SELECT id,root FROM sessions')}
            def relocate(data,root):
                changed=False
                for key in ('raw','jpg'):
                    if not data.get(key):continue
                    new=mapping.get(str(root/data[key]))
                    if new:
                        data[key]=Path(new).relative_to(root).as_posix();changed=True
                if changed:
                    data['fingerprint']=media.fingerprint([root/data[k] for k in ('raw','jpg') if data.get(k)])
                return changed
            progress(stage='sync_asset_paths',done=0,total=len(mapping))
            with self.archive.connect() as c:
                for before,after in mapping.items():
                    c.execute('UPDATE locations SET path=? WHERE path=?',(after,before))
                    c.execute('UPDATE file_fingerprints SET path=?,filename=?,stem=?,folder=? WHERE path=?',
                              (after,Path(after).name,Path(after).stem.casefold(),str(Path(after).parent),before))
                    c.execute("UPDATE metadata_jobs SET path=? WHERE path=? AND status!='running'",(after,before))
                    old_key,new_key=pair_key(before),pair_key(after)
                    c.execute('INSERT OR REPLACE INTO shared_ratings SELECT ?,rating,updated FROM shared_ratings WHERE path_key=?',(new_key,old_key))
                    c.execute('INSERT OR IGNORE INTO rating_capture_paths SELECT session,capture,? FROM rating_capture_paths WHERE path_key=?',(new_key,old_key))
                    c.execute('DELETE FROM rating_capture_paths WHERE path_key=?',(old_key,))
                    c.execute('DELETE FROM shared_ratings WHERE path_key=?',(old_key,))
                # JPG→RAW继承服务还保留一份现场记录，必须与主会话同时迁移。
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='raw_sources'").fetchone():
                    for row in c.execute('SELECT session,capture,record FROM raw_sources').fetchall():
                        data=json.loads(row['record']);root=sessions.get(row['session'])
                        if root and relocate(data,root):
                            c.execute('UPDATE raw_sources SET record=? WHERE session=? AND capture=?',
                                      (json.dumps(data,ensure_ascii=False),row['session'],row['capture']))
            progress(stage='sync_asset_paths',done=len(mapping),total=len(mapping))
            progress(stage='sync_session_paths',done=0,total=len(mapping))
            with self.store.connect() as c:
                for row in c.execute('SELECT session,id,data FROM captures').fetchall():
                    data=json.loads(row['data']);root=sessions[row['session']]
                    before=data['fingerprint']
                    if relocate(data,root):
                        # 先登记受管移动版本，再提交会话；中断后可按同一移动映射重放。
                        if self.archive.sku:self.archive.sku.metadata_changed(row['session'],row['id'],before,data['fingerprint'])
                        c.execute('UPDATE captures SET data=? WHERE session=? AND id=?',(json.dumps(data,ensure_ascii=False),row['session'],row['id']))
                        c.execute('UPDATE sessions SET revision=revision+1 WHERE id=?',(row['session'],))
            progress(stage='sync_session_paths',done=len(mapping),total=len(mapping))
            progress(stage='sync_catalog_paths',done=0,total=len(mapping))
            with self.catalog.connect() as c:
                from folder_archive_catalog_sync import synchronize_catalog_paths
                synchronize_catalog_paths(c,moves)
            progress(stage='sync_catalog_paths',done=len(mapping),total=len(mapping))
            progress(stage='sync_metadata_cache',done=0,total=len(mapping))
            cache=self.archive.writer.data/'folder-archive-metadata.sqlite3'
            if cache.is_file():
                versions={r['after']:r.get('signature') for r in moves}
                with closing(sqlite3.connect(cache)) as c,c:
                    for before,after in mapping.items():
                        row=c.execute('SELECT size,mtime,payload FROM metadata WHERE path=?',(before,)).fetchone()
                        if not row:continue
                        from folder_archive_metadata import signature
                        if [row[0],row[1]]!=(versions.get(after) or signature(after)):continue
                        try:payload=json.loads(row[2])
                        except ValueError:continue
                        if not isinstance(payload,dict):continue
                        payload['SourceFile']=after
                        c.execute('INSERT OR REPLACE INTO metadata VALUES (?,?,?,?)',(after,row[0],row[1],json.dumps(payload,ensure_ascii=False)))
                        c.execute('DELETE FROM metadata WHERE path=?',(before,))
            progress(stage='sync_metadata_cache',done=len(mapping),total=len(mapping))
        self.store.browser_totals.clear()
        if getattr(self.archive,'capture_service',None):
            with self.archive.connect() as c:
                for before,after in mapping.items():
                    for row in c.execute("SELECT id,fields,journal FROM metadata_jobs WHERE path=? AND status IN ('queued','failed')",(before,)).fetchall():
                        fields=json.loads(row['fields']);journal=json.loads(row['journal'])
                        if fields.get('_sidecar_hashes'):fields['_sidecar_hashes']={mapping.get(path,path):value for path,value in fields['_sidecar_hashes'].items()}
                        if journal.get('target'):journal['target']=mapping.get(journal['target'],journal['target'])
                        for side in journal.get('sidecars',[]):
                            if side.get('target'):side['target']=mapping.get(side['target'],side['target'])
                        c.execute('UPDATE metadata_jobs SET path=?,fields=?,journal=? WHERE id=?',(after,json.dumps(fields,ensure_ascii=False),json.dumps(journal,ensure_ascii=False),row['id']))
                        if fields.get('_catalog_ocr'):c.execute("UPDATE metadata_jobs SET status='failed',reason='来源已整理，请重新核对该自动识别资料后写入' WHERE id=?",(row['id'],))
                    if c.execute('SELECT 1 FROM rating_file_sync WHERE path=?',(after,)).fetchone():c.execute('DELETE FROM rating_file_sync WHERE path=?',(before,))
                    else:c.execute('UPDATE rating_file_sync SET path=? WHERE path=?',(after,before))
        if getattr(self.archive,'capture_service',None):self.archive.capture_service.relocate(moves)
