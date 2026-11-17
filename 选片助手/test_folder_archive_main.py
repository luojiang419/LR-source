"""主项目真实HTTP接口、目录移动、原生报表与任务恢复集成验证。"""
from contextlib import ExitStack
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from zipfile import ZipFile
import xml.etree.ElementTree as E

from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
os.environ.setdefault('FRAMEKEEP_DATA',str(ROOT/'build/test-folder-archive-import'))
import server
from catalog_store import Store
from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from folder_archive_jobs import FolderArchiveJobs
from folder_archive_report import report_filename
from metadata_writer import sha


class MainFolderArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.small=self.base/'来源';self.small.mkdir();self.target=self.base/'归档';self.target.mkdir()
        for i in range(2):Image.new('RGB',(80,100),('red','blue')[i]).save(self.small/f'照片{i}.jpg')
        self.before={str(p):sha(p) for p in self.small.glob('*.jpg')}
        self.store=Store(self.base/'data',ROOT);self.archive=AssetArchive(self.store,autostart=False)
        self.catalog=AssetCatalog(self.store.data)
        self.catalog.add_root(str(self.small));self.root=self.catalog.roots()[0]['id']
        for row in self.catalog.items()['rows']:self.catalog.assign(row['id'],'LC7877847')
        scan=self.store.scan(str(self.small),True)
        deadline=time.monotonic()+15
        while self.store.jobs[scan]['status'] not in ('completed','failed') and time.monotonic()<deadline:time.sleep(.02)
        self.stack=ExitStack()
        self.stack.enter_context(patch.multiple(server,STORE=self.store,ARCHIVE=self.archive,CATALOG=self.catalog,WATCH=None,PREVIEWS=None,DIRECT=None,SKU=None))
        self.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);self.http.daemon_threads=True
        threading.Thread(target=self.http.serve_forever,daemon=True).start()
        self.url=f'http://127.0.0.1:{self.http.server_port}'

    def tearDown(self):
        if hasattr(self.http,'folder_archives'):self.http.folder_archives.close()
        self.http.shutdown();self.http.server_close();self.stack.close()
        self.archive.close();self.catalog.close();self.temp.cleanup()

    def request(self,path,body=None,token=True):
        headers={'Content-Type':'application/json'}
        if token:headers['X-Framekeep-Token']=server.TOKEN
        request=Request(self.url+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        with urlopen(request,timeout=20) as response:
            raw=response.read()
            return (raw,dict(response.headers)) if 'spreadsheetml' in response.headers.get('Content-Type','') else json.loads(raw)

    def prepare(self,preprocess=False,fast=False):
        data=self.request('/api/folder-archive/prepare',{'root_id':self.root,'folder':'','target_path':str(self.target),'route':'folders','preprocess':preprocess,'fast_archive':fast})
        return self.wait(data['id'],('ready',))

    def wait(self,jid,statuses):
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            result=self.request('/api/folder-archive/state?id='+jid)
            if result['status'] in statuses:return result
            if result['status']=='failed':self.fail(result['error'])
            time.sleep(.03)
        self.fail('任务超时：'+str(result))

    def test_auth_and_scope_escape_are_rejected(self):
        with self.assertRaises(HTTPError) as e:self.request('/api/folder-archive/prepare',{},token=False)
        self.assertEqual(e.exception.code,403)
        with self.assertRaises(HTTPError) as e:
            self.request('/api/folder-archive/prepare',{'root_id':self.root,'folder':'../外部','route':'folders','target_path':str(self.target)})
        self.assertEqual(e.exception.code,400)
        self.assertEqual(self.before,{str(p):sha(p) for p in self.small.glob('*.jpg')})

    def test_default_fast_preview_thumbnail_and_execution(self):
        data=self.request('/api/folder-archive/prepare',{'root_id':self.root,'target_path':str(self.target),'route':'folders','preprocess':False})
        job=self.wait(data['id'],('ready',));self.assertTrue(job['input']['fast_archive'])
        self.assertIsNone(job['items'][0]['input_sha256'])
        content=self.http.folder_archives.thumbnail(job['id'],job['items'][0]['id'])
        with Image.open(io.BytesIO(content)) as image:self.assertGreater(image.width,0)
        self.request('/api/folder-archive/start',{'id':job['id']})
        done=self.wait(job['id'],('completed',));self.assertEqual(done['done'],2)

    def test_scanning_progress_is_visible_before_metadata_starts(self):
        from sku_folder_archive import files_under
        entered=threading.Event();release=threading.Event()
        def gated(root,**options):
            options['progress'](stage='scanning_sources',done=1,total=0,discovered=1,directories=0,path=str(root),indeterminate=True)
            entered.set();release.wait(10)
            return files_under(root,**options)
        with patch('folder_archive_sources.files_under',side_effect=gated):
            data=self.request('/api/folder-archive/prepare',{'root_id':self.root,'route':'folders','target_path':str(self.target),'preprocess':False})
            try:
                self.assertTrue(entered.wait(5))
                state=self.request('/api/folder-archive/state?id='+data['id'])
                self.assertEqual(state['stage'],'scanning_sources');self.assertTrue(state['indeterminate'])
                self.assertEqual(state['discovered'],1);self.assertEqual(state['path'],str(self.small))
                self.assertIn('elapsed_seconds',state)
            finally:release.set()
            self.wait(data['id'],('ready',))

    def test_manifest_hash_stage_has_visible_counts(self):
        from folder_archive_api import service
        # 初始化同一HTTP服务后对真实进度入口加门，观察摘要阶段的状态。
        self.request('/api/folder-archive/state')
        jobs=self.http.folder_archives;original=jobs._progress;entered=threading.Event();release=threading.Event()
        def gated(jid,**values):
            original(jid,**values)
            if values.get('stage')=='manifest_hashing' and values.get('done')==0:
                entered.set();release.wait(10)
        with patch.object(jobs,'_progress',side_effect=gated):
            data=self.request('/api/folder-archive/prepare',{'root_id':self.root,'route':'folders','target_path':str(self.target),'preprocess':False,'fast_archive':False})
            try:
                self.assertTrue(entered.wait(10))
                state=self.request('/api/folder-archive/state?id='+data['id'])
                self.assertEqual(state['stage'],'manifest_hashing');self.assertFalse(state['indeterminate'])
                self.assertEqual(state['total'],2);self.assertTrue(state['path'])
            finally:release.set()
            self.wait(data['id'],('ready',))

    def test_legacy_folder_selection_resolves_to_actual_directory(self):
        legacy='共享/现场小档/2026-11-04'
        with self.catalog.connect() as c:
            for row in c.execute('SELECT id,path FROM items').fetchall():
                c.execute('UPDATE items SET relative=?,folder=? WHERE id=?',(legacy+'/'+Path(row['path']).name,legacy,row['id']))
        scope=self.request('/api/folder-archive/scope',{'root_id':self.root,'folder':legacy})
        self.assertEqual(scope['path'],str(self.small));self.assertEqual(scope['folder'],'')
        data=self.request('/api/folder-archive/prepare',{'root_id':self.root,'folder':legacy,'route':'folders','target_path':str(self.target),'preprocess':False})
        job=self.wait(data['id'],('ready',));self.assertEqual(job['expected'],2)
        self.assertEqual(job['input']['source'],str(self.small))
        self.assertEqual(self.before,{str(p):sha(p) for p in self.small.glob('*.jpg')})

    def test_legacy_scope_never_escapes_registered_root(self):
        with self.catalog.connect() as c:c.execute("UPDATE items SET folder='旧目录',path=? WHERE id=(SELECT id FROM items LIMIT 1)",(str(self.base/'根目录以外'/'图片.jpg'),))
        with self.assertRaises(HTTPError) as error:self.request('/api/folder-archive/scope',{'root_id':self.root,'folder':'旧目录'})
        self.assertIn('登记位置不一致',error.exception.read().decode('utf8'))

    def test_legacy_scope_ambiguity_does_not_guess(self):
        with self.catalog.connect() as c:
            c.execute("UPDATE items SET folder='旧目录'")
            row=c.execute('SELECT id FROM items LIMIT 1').fetchone()
            c.execute('UPDATE items SET path=? WHERE id=?',(str(self.small/'其他子目录'/'照片.jpg'),row['id']))
        with self.assertRaises(HTTPError) as error:self.request('/api/folder-archive/scope',{'root_id':self.root,'folder':'旧目录'})
        self.assertIn('多个实际位置',error.exception.read().decode('utf8'))

    def test_missing_scope_reports_the_actual_checked_path(self):
        with self.assertRaises(HTTPError) as error:self.request('/api/folder-archive/scope',{'root_id':self.root,'folder':'确实不存在'})
        message=json.loads(error.exception.read())['error']
        self.assertIn(str(self.small/'确实不存在'),message)

    def test_real_copy_keeps_sources_and_exports_native_workbook(self):
        job=self.prepare();self.assertEqual(job['expected'],2)
        self.assertFalse(list(self.target.glob('LC*')))
        self.request('/api/folder-archive/start',{'id':job['id']})
        finished=self.wait(job['id'],('completed',));self.assertEqual(finished['done'],2)
        self.assertEqual(self.before,{str(p):sha(p) for p in self.small.glob('*.jpg')})
        content,headers=self.request('/api/folder-archive/report?id='+job['id'])
        self.assertIn('202',headers['Content-Disposition'])
        with ZipFile(io.BytesIO(content)) as z:
            self.assertEqual(len([n for n in z.namelist() if n.startswith('xl/media/')]),2)
            ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            sheet=E.fromstring(z.read('xl/worksheets/sheet1.xml'))
            self.assertIsNotNone(sheet.find('m:autoFilter',ns))
            self.assertEqual(sheet.find('.//m:pane',ns).get('xSplit'),'2')
            self.assertIn('文件缩略图',z.read('xl/worksheets/sheet1.xml').decode())
            self.assertNotIn('标签图片',z.read('xl/worksheets/sheet1.xml').decode())
        exported=self.request('/api/folder-archive/export',{'id':job['id']})
        self.assertTrue(Path(exported['path']).is_file())

    def test_confirmed_shooting_database_sku_works_without_embedded_sku(self):
        with self.catalog.connect() as c:c.execute("UPDATE items SET source='shooting',confidence=1")
        self.assertTrue(all('XMP-fk:SKU' not in self.archive.writer.read(p) for p in self.small.glob('*.jpg')))
        job=self.prepare()
        self.request('/api/folder-archive/start',{'id':job['id']})
        result=self.wait(job['id'],('completed',))
        self.assertEqual(result['done'],2)
        for item in result['items']:
            self.assertEqual(self.archive.writer.read(item['destination'])['XMP-fk:SKU'],'LC7877847')
        self.assertEqual(self.before,{str(p):sha(p) for p in self.small.glob('*.jpg')})

    def test_shooting_sku_changed_after_preview_blocks_before_move(self):
        with self.catalog.connect() as c:c.execute("UPDATE items SET source='shooting',confidence=1")
        job=self.prepare(True)
        with self.catalog.connect() as c:c.execute("UPDATE items SET sku='LC7979103'")
        self.request('/api/folder-archive/start',{'id':job['id']})
        failed=self.wait(job['id'],('failed',))
        self.assertIn('已入库款号已改变',failed['error'])
        self.assertEqual(self.before,{str(p):sha(p) for p in self.small.glob('*.jpg')})

    def test_confirmed_shooting_suffix_takes_precedence_over_old_embedded_sku(self):
        for path in self.small.glob('*.jpg'):
            self.archive.writer.run('-overwrite_original','-XMP-fk:SKU=LC7877847',path)
        before={str(p):sha(p) for p in self.small.glob('*.jpg')}
        with self.catalog.connect() as c:c.execute("UPDATE items SET sku='LC7877847-P1',source='shooting',confidence=1")
        job=self.prepare()
        self.assertTrue(all(row['sku']=='LC7877847-P1' for row in job['items']))
        self.request('/api/folder-archive/start',{'id':job['id']})
        result=self.wait(job['id'],('completed',))
        self.assertEqual(result['done'],2)
        for row in result['items']:self.assertEqual(self.archive.writer.read(row['destination'])['XMP-fk:SKU'],'LC7877847-P1')
        self.assertEqual(before,{str(p):sha(p) for p in self.small.glob('*.jpg')})

    def test_path_guess_cannot_be_treated_as_confirmed_sku(self):
        with self.catalog.connect() as c:c.execute("UPDATE items SET source='path_rule'")
        data=self.request('/api/folder-archive/prepare',{'root_id':self.root,'route':'folders','target_path':str(self.target)})
        failed=self.wait(data['id'],('failed',))
        self.assertIn('来源图片尚无确认SKU',failed['error'])

    def test_source_sku_can_be_read_from_existing_sidecar(self):
        with self.catalog.connect() as c:c.execute("UPDATE items SET sku='',source='unmatched'")
        for path in self.small.glob('*.jpg'):
            path.with_suffix('.xmp').write_text('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:fk="https://framekeep.local/ns/asset/1.0/" fk:SKU="LC7877847"/></rdf:RDF></x:xmpmeta>',encoding='utf8')
        job=self.prepare()
        self.request('/api/folder-archive/start',{'id':job['id']})
        result=self.wait(job['id'],('completed',))
        self.assertEqual(result['done'],2)
        for item in result['items']:
            self.assertEqual(self.archive.writer.read(item['destination'])['XMP-fk:SKU'],'LC7877847')
            self.assertFalse(Path(item['destination']).with_suffix('.xmp').exists())

    def test_file_cache_does_not_hide_changed_sidecar_sku(self):
        with self.catalog.connect() as c:c.execute("UPDATE items SET sku='',source='unmatched'")
        def write_sidecars(sku):
            for path in self.small.glob('*.jpg'):
                path.with_suffix('.xmp').write_text('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:fk="https://framekeep.local/ns/asset/1.0/" fk:SKU="'+sku+'"/></rdf:RDF></x:xmpmeta>',encoding='utf8')
        write_sidecars('LC7877847');first=self.prepare()
        self.assertTrue(all(row['sku']=='LC7877847' for row in first['items']))
        write_sidecars('LC7979103');second=self.prepare()
        self.assertTrue(all(row['sku']=='LC7979103' for row in second['items']))
        self.assertEqual(self.before,{str(p):sha(p) for p in self.small.glob('*.jpg')})

    def test_default_preprocessing_updates_store_and_catalog_paths(self):
        session=self.store.sessions()[0]
        with self.archive.connect() as c:
            c.execute('CREATE TABLE raw_sources(session TEXT,capture TEXT,record TEXT,rating INTEGER,updated REAL,PRIMARY KEY(session,capture))')
            for record in self.store.records(session['id']):
                c.execute('INSERT INTO raw_sources VALUES (?,?,?,?,?)',(session['id'],record['id'],json.dumps(record),2,time.time()))
        body={'root_id':self.root,'target_path':str(self.target),'route':'folders'}
        job=self.request('/api/folder-archive/prepare',body);job=self.wait(job['id'],('ready',))
        self.assertTrue(job['input']['preprocess'])
        self.request('/api/folder-archive/start',{'id':job['id']});self.wait(job['id'],('completed',))
        for before,digest in self.before.items():
            after=self.small/'LC78/LC7877847'/Path(before).name
            self.assertFalse(Path(before).exists());self.assertEqual(sha(after),digest)
            with self.catalog.connect() as c:
                self.assertIsNotNone(c.execute('SELECT id FROM items WHERE path=?',(str(after),)).fetchone())
        session=self.store.sessions()[0]
        for record in self.store.records(session['id']):self.assertTrue((self.small/record['jpg']).is_file())
        with self.archive.connect() as c:
            inherited=[json.loads(r[0]) for r in c.execute('SELECT record FROM raw_sources')]
        self.assertEqual(len(inherited),2)
        for record in inherited:self.assertTrue((self.small/record['jpg']).is_file())
        # 新任务也必须复用已有副本身份，不能因来源整理后路径改变而误报冲突。
        second=self.prepare(True)
        self.request('/api/folder-archive/start',{'id':second['id']})
        again=self.wait(second['id'],('completed',))
        self.assertTrue(all(r['status']=='identical_verified_skip' for r in again['items']))

    def test_cancelled_ready_job_does_not_move_files(self):
        job=self.prepare(True)
        result=self.request('/api/folder-archive/cancel',{'id':job['id']})
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(self.before,{str(p):sha(p) for p in self.small.glob('*.jpg')})

    def test_target_blocked_after_preview_does_not_rearrange_sources(self):
        job=self.prepare(True)
        (self.target/'LC78').write_text('保留用户已有文件',encoding='utf8')
        self.request('/api/folder-archive/start',{'id':job['id']})
        failed=self.wait(job['id'],('failed',))
        self.assertIn('同名文件占用',failed['error'])
        self.assertEqual(self.before,{str(p):sha(p) for p in self.small.glob('*.jpg')})
        self.assertEqual((self.target/'LC78').read_text(encoding='utf8'),'保留用户已有文件')

    def test_folder_sku_conflict_requires_review(self):
        folder=self.small/'LC7979103';folder.mkdir()
        for path in self.small.glob('*.jpg'):path.rename(folder/path.name)
        self.catalog.scan(self.root)
        for row in self.catalog.items()['rows']:self.catalog.assign(row['id'],'LC7877847')
        data=self.request('/api/folder-archive/prepare',{'root_id':self.root,'route':'folders','target_path':str(self.target)})
        failed=self.wait(data['id'],('failed',))
        self.assertIn('目录与已确认SKU冲突',failed['error'])
        self.assertEqual(len(list(folder.glob('*.jpg'))),2)
        self.assertFalse(list(self.target.iterdir()))

    def test_final_verification_detects_target_changed_after_copy(self):
        self.target_changed_after_copy()

    def test_fast_final_verification_detects_target_changed_after_copy(self):
        self.target_changed_after_copy(fast=True)

    def target_changed_after_copy(self,fast=False):
        job=self.prepare(fast=fast);service=self.http.folder_archives
        original=service._progress
        def corrupt(jid,**values):
            if values.get('stage')=='verifying' and values.get('done')==0:
                Image.new('RGB',(80,100),'green').save(values['path'])
            return original(jid,**values)
        with patch.object(service,'_progress',side_effect=corrupt):
            self.request('/api/folder-archive/start',{'id':job['id']})
            failed=self.wait(job['id'],('failed',))
        self.assertIn('已归档目标在提交后改变' if fast else '像素校验失败',failed['error'])
        with self.assertRaises(HTTPError):self.request('/api/folder-archive/report?id='+job['id'])
        self.assertEqual(self.before,{str(p):sha(p) for p in self.small.glob('*.jpg')})

    def test_new_nested_target_is_created(self):
        target=self.target/'新上级'/'归档根目录'
        data=self.request('/api/folder-archive/prepare',{'root_id':self.root,'route':'folders','target_path':str(target),'preprocess':False})
        job=self.wait(data['id'],('ready',))
        self.request('/api/folder-archive/start',{'id':job['id']})
        result=self.wait(job['id'],('completed',))
        self.assertEqual(result['done'],2)
        self.assertEqual(len(list((target/'LC78/LC7877847').glob('*.jpg'))),2)

    def test_pause_and_restart_resume_partial_copy(self):
        self.pause_and_restart_resume()

    def test_fast_pause_and_restart_resume_partial_copy(self):
        self.pause_and_restart_resume(fast=True)

    def pause_and_restart_resume(self,fast=False):
        job=self.prepare(True,fast=fast);entered=threading.Event();release=threading.Event()
        original=self.archive.writer.write
        def gated(*args,**kwargs):
            entered.set();release.wait(10);return original(*args,**kwargs)
        with patch.object(self.archive.writer,'write',side_effect=gated):
            self.request('/api/folder-archive/start',{'id':job['id']});self.assertTrue(entered.wait(10))
            self.request('/api/folder-archive/pause',{'id':job['id']})
            release.set()
            self.wait(job['id'],('paused',))
            self.http.folder_archives.close()
        self.http.folder_archives=FolderArchiveJobs(self.store,self.archive,self.catalog)
        restored=self.request('/api/folder-archive/state?id='+job['id']);self.assertEqual(restored['status'],'interrupted')
        self.request('/api/folder-archive/resume',{'id':job['id']})
        result=self.wait(job['id'],('completed',));self.assertEqual(result['done'],2)

    def test_beijing_report_name_midnight(self):
        self.assertEqual(report_filename(datetime(2026,11,6,16,0,tzinfo=timezone.utc)),'归档校验报表-202611070000.xlsx')

    def test_background_scan_after_moves_keeps_original_catalog_identity(self):
        with self.catalog.connect() as c:c.execute("UPDATE items SET source='shooting'")
        job=self.prepare(True,fast=True);service=self.http.folder_archives
        with self.catalog.connect() as c:identities={r['path']:r['id'] for r in c.execute('SELECT id,path FROM items')}
        synchronize=service.sources.synchronize
        def raced(plan,**options):
            self.catalog._scan(self.root)
            return synchronize(plan,**options)
        with patch.object(service.sources,'synchronize',side_effect=raced):
            self.request('/api/folder-archive/start',{'id':job['id']});self.wait(job['id'],('completed',))
        self.catalog._scan(self.root)
        with self.catalog.connect() as c:
            for before,item_id in identities.items():
                after=str(self.small/'LC78/LC7877847'/Path(before).name)
                row=c.execute('SELECT id,sku,online,source FROM items WHERE path=?',(after,)).fetchone()
                self.assertEqual(tuple(row),(item_id,'LC7877847',1,'shooting'))
            self.assertEqual(c.execute('SELECT count(*) FROM items').fetchone()[0],4)
            self.assertEqual(c.execute('SELECT count(*) FROM items WHERE online=0').fetchone()[0],2)

    def test_resume_after_sync_failure_skips_completed_moves_and_replanning(self):
        job=self.prepare(True,fast=True);service=self.http.folder_archives
        with patch.object(service.sources,'synchronize',side_effect=RuntimeError('模拟路径同步失败')):
            self.request('/api/folder-archive/start',{'id':job['id']});self.wait(job['id'],('failed',))
        with patch('sku_archive_preprocess.move_no_replace',side_effect=AssertionError('不能重复搬文件')),patch('folder_archive_jobs.SkuFolderArchive.plan',side_effect=AssertionError('不能重建已完成整理计划')):
            self.request('/api/folder-archive/resume',{'id':job['id']});done=self.wait(job['id'],('completed',))
        self.assertEqual(done['done'],2)

    def test_catalog_sync_has_distinct_visible_stage(self):
        from folder_archive_catalog_sync import synchronize_catalog_paths
        job=self.prepare(True,fast=True);entered=threading.Event();release=threading.Event()
        def gated(*args):entered.set();release.wait(10);return synchronize_catalog_paths(*args)
        with patch('folder_archive_catalog_sync.synchronize_catalog_paths',side_effect=gated):
            self.request('/api/folder-archive/start',{'id':job['id']})
            try:
                self.assertTrue(entered.wait(10));state=self.request('/api/folder-archive/state?id='+job['id'])
                self.assertEqual(state['stage'],'sync_catalog_paths');self.assertIn('来源已整理',state['message'])
                self.assertEqual(state['done'],0)
            finally:release.set()
            self.wait(job['id'],('completed',))


if __name__=='__main__':unittest.main()
