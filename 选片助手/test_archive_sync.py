"""最终目录同步：真实 JPEG、离线来源、重启、递归和幂等。"""
import copy
from http.server import ThreadingHTTPServer
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from asset_archive import AssetArchive
from catalog_store import Store
from metadata_writer import sha
from raw_reconcile import RawReconcile

ROOT=Path(__file__).resolve().parents[1]

class ArchiveSyncTests(unittest.TestCase):
    def test_clearing_completed_metadata_retains_target_and_reappears_on_resync(self):
        target_id=self.link.add_target(str(self.destination))['target_id'];self.finish(target_id)
        # 单 JPG 夹具按现有规则因缺少 RAW 需复核，不能被清理。
        self.assertEqual(self.link.clear_completed_tasks(),0)
        self.assertEqual(self.link.task_state()['batches'][0]['status'],'attention')
        # 注入已完成校验快照，验证展示清理边界，不将此当作真实 RAW 验收。
        with self.archive.connect() as c:
            c.execute("UPDATE archive_sync_targets SET verification=? WHERE id=?", ('{"status":"completed"}',target_id))
        self.assertEqual(self.link.clear_completed_tasks(),1)
        self.assertEqual(self.link.task_state()['batches'],[])
        self.assertEqual(self.link.targets()[0]['id'],target_id)
        before=sha(self.target)
        self.link.close();self.link=RawReconcile(self.archive,autostart=False)
        self.assertEqual(self.link.task_state()['batches'],[])
        self.link.sync_target(target_id)
        self.assertEqual(self.link.task_state()['batches'][0]['status'],'indexing')
        self.assertEqual(sha(self.target),before)
        self.wait_scan(self.link.targets()[0]['scan'])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.source=self.base/'现场';self.source.mkdir();self.destination=self.base/'NAS归档';self.destination.mkdir()
        self.store=Store(self.base/'data',ROOT);self.archive=AssetArchive(self.store,autostart=False);self.link=RawReconcile(self.archive,autostart=False)
        self.store.on_catalog=self.link.request
        self.jpg=self.source/'IMG_0001.JPG';Image.new('RGB',(80,60),'steelblue').save(self.jpg)
        self.archive.writer.run('-overwrite_original','-Model=Canon EOS R6m2','-ExifIFD:SerialNumber=123456','-DateTimeOriginal=2026:10:20 12:00:00','-SubSecTimeOriginal=37',self.jpg)
        self.original=self.jpg.read_bytes();self.sid=self.scan(self.source);self.cid=self.store.records(self.sid)[0]['id']
        session=self.store.session(self.sid);state=copy.deepcopy(session['state']);state['overrides']={self.cid:4};self.store.save(self.sid,session['revision'],state)
        preview=self.archive.preview(self.sid,[self.cid],'归档-123','细节图');self.archive.apply(preview['preview_id']);self.archive.process_one();self.link.index_sources(self.sid)
        nested=self.destination/'存储卡'/'DCIM';nested.mkdir(parents=True);self.target=nested/self.jpg.name;self.target.write_bytes(self.original)
    def tearDown(self):
        self.link.close();self.archive.close();self.temp.cleanup()
    def scan(self,path):
        job=self.store.scan(str(path),activate=False);self.wait_scan(job);return self.store.jobs[job]['session_id']
    def wait_scan(self,job):
        deadline=time.monotonic()+15
        while self.store.jobs[job]['status'] not in ('completed','failed') and time.monotonic()<deadline:time.sleep(.02)
        self.assertEqual(self.store.jobs[job]['status'],'completed',self.store.jobs[job])
    def finish(self,target_id):
        self.link.sync_target(target_id)
        for _ in range(400):
            self.link.tick();self.archive.process_one();self.link.tick()
            target=next(t for t in self.link.targets() if t['id']==target_id)
            if target['status'] in ('completed','failed'):return target
            time.sleep(.02)
        self.fail('归档同步未结束')
    def test_deleted_source_removed_session_and_restart_still_sync_nested_jpg(self):
        self.store.activate(self.sid);active=self.store.active_id();self.store.remove_session(self.sid);self.jpg.unlink()
        target_id=self.link.add_target(str(self.destination))['target_id'];before=sha(self.target)
        self.assertEqual(self.link.targets()[0]['status'],'ready');self.assertEqual(sha(self.target),before)
        self.link.close();self.link=RawReconcile(self.archive,autostart=False);self.store.on_catalog=self.link.request
        self.assertEqual(self.link.targets()[0]['id'],target_id)
        self.assertEqual(self.finish(target_id)['status'],'completed')
        pair=self.link.state(self.link.targets()[0]['session'])['pairs'][0];self.assertEqual(pair['status'],'applied',pair)
        fields=self.archive.writer.read(self.target);self.assertEqual(fields['XMP-xmp:Rating'],4);self.assertEqual(fields['XMP-fk:SKU'],'归档-123');self.assertEqual(fields['XMP-fk:ShootType'],'细节图')
        before=sha(self.target);self.finish(target_id);self.assertEqual(sha(self.target),before)
        self.assertEqual(self.store.active_id(),None);self.assertIsNotNone(active)
    def test_rating_only_source_without_asset_or_file_and_explicit_zero(self):
        # 即使没有备案产生的资产链接，也能仅凭已保存的拍摄/人工评级建立关联。
        other=self.source/'IMG_0002.JPG';other.write_bytes(self.original);self.archive.writer.run('-overwrite_original','-DateTimeOriginal=2026:10:20 12:00:02',other)
        self.scan(self.source);cid=next(r['id'] for r in self.store.records(self.sid) if r['jpg']==other.name)
        self.target.unlink();self.target=self.target.with_name(other.name);shutil.copy2(other,self.target)
        session=self.store.session(self.sid);state=copy.deepcopy(session['state']);state['overrides'][cid]=0;self.store.save(self.sid,session['revision'],state)
        shutil.rmtree(self.source);target_id=self.link.add_target(str(self.destination))['target_id'];self.finish(target_id)
        pair=self.link.state(self.link.targets()[0]['session'])['pairs'][0];self.assertEqual(pair['status'],'applied',pair)
        self.assertEqual(self.archive.writer.read(self.target)['XMP-xmp:Rating'],0)
    def test_conflict_and_unrelated_camera_are_not_overwritten(self):
        self.archive.writer.run('-overwrite_original','-XMP-xmp:Rating=5',self.target)
        other=self.target.with_name('IMG_0002.JPG');other.write_bytes(self.original);self.archive.writer.run('-overwrite_original','-ExifIFD:SerialNumber=other-camera',other)
        before=[sha(p) for p in (self.target,other)];target_id=self.link.add_target(str(self.destination))['target_id'];self.finish(target_id)
        counts=self.link.state(self.link.targets()[0]['session'])['counts'];self.assertEqual(counts,{'review':1,'unmatched':1});self.assertEqual([sha(p) for p in (self.target,other)],before)
    def test_offline_destination_reports_failure_and_can_retry(self):
        missing=self.base/'离线NAS';target_id=self.link.add_target(str(missing))['target_id']
        self.assertEqual(self.finish(target_id)['status'],'failed');missing.mkdir();shutil.copy2(self.target,missing/self.target.name)
        self.assertEqual(self.finish(target_id)['status'],'completed')
        moved=self.base/'NAS已移走';missing.rename(moved)
        self.assertEqual(self.finish(target_id)['status'],'failed')
        batch=self.link.task_state()['batches'][0];self.assertEqual(batch['total'],0);self.assertEqual(batch['files'],[]);self.assertTrue(batch['reason'])
    def test_metadata_progress_reports_queue_running_failure_retry_and_recheck(self):
        target_id=self.link.add_target(str(self.destination))['target_id'];self.link.sync_target(target_id)
        self.assertEqual(self.link.task_state()['batches'][0]['status'],'indexing')
        self.wait_scan(self.link.targets()[0]['scan'])
        for _ in range(4):self.link.tick()
        batch=self.link.task_state()['batches'][0];self.assertEqual(batch['total'],1);self.assertEqual(batch['finished'],0);self.assertEqual(batch['counts'],{'queued':1})
        observed=[]
        def fail(*args,**kwargs):
            observed.append(self.link.task_state()['batches'][0]['files'][0]['status']);raise ValueError('模拟磁盘写入失败')
        with patch.object(self.archive.writer,'write',side_effect=fail):self.archive.process_one()
        self.link.tick();self.assertEqual(observed,['running'])
        batch=self.link.task_state()['batches'][0];self.assertEqual(batch['finished'],1);self.assertEqual(batch['counts'],{'failed':1});self.assertIn('模拟磁盘',batch['files'][0]['reason'])
        pair=self.link.state(self.link.targets()[0]['session'])['pairs'][0];self.link.retry(pair['id'])
        self.finish(target_id);batch=self.link.task_state()['batches'][0];self.assertEqual(batch['counts'],{'applied':1})
        before=sha(self.target);self.finish(target_id);batch=self.link.task_state()['batches'][0];self.assertEqual(batch['counts'],{'applied':1});self.assertEqual(batch['finished'],1);self.assertEqual(sha(self.target),before)
        self.archive.writer.run('-overwrite_original','-XMP-xmp:Rating=5',self.target)
        target_sid=self.link.targets()[0]['session'];self.link.process_session(target_sid)
        pair=self.link.state(target_sid)['pairs'][0];self.assertEqual(self.link.task_state()['batches'][0]['status'],'attention')
        self.link.review(pair['id'],self.sid,self.cid,overwrite=True)
        self.assertEqual(self.link.task_state()['batches'][0]['status'],'writing')
        self.archive.process_one();self.link.tick();self.assertEqual(self.link.task_state()['batches'][0]['counts'],{'applied':1})
    def test_source_changes_auto_sync_and_final_audit_reports_missing_photos(self):
        target_id=self.link.add_target(str(self.destination))['target_id'];self.finish(target_id)
        def drain():
            for _ in range(1200):
                self.link.tick();self.archive.process_one();self.link.tick()
                target=self.link.targets()[0]
                if not self.link.pending and target['status']=='completed' and target['verification']!='{}':return self.link.task_state()['batches'][0]
                time.sleep(.02)
            self.fail('自动同步未结束')
        session=self.store.session(self.sid);state=copy.deepcopy(session['state']);state['overrides'][self.cid]=5;self.store.save(self.sid,session['revision'],state);self.link.request(self.sid)
        batch=drain();self.assertEqual(self.archive.writer.read(self.target)['XMP-xmp:Rating'],5);self.assertEqual(batch['verification']['checked'],1)
        self.assertTrue(batch['verification']['missing_components'])
        other=self.source/'IMG_0002.JPG';other.write_bytes(self.original);self.archive.writer.run('-overwrite_original','-DateTimeOriginal=2026:10:20 12:00:02',other)
        self.scan(self.source);cid=next(r['id'] for r in self.store.records(self.sid) if r['jpg']==other.name)
        session=self.store.session(self.sid);state=copy.deepcopy(session['state']);state['overrides'][cid]=3;self.store.save(self.sid,session['revision'],state);self.link.request(self.sid)
        batch=drain();self.assertIn('IMG_0002.JPG',batch['verification']['missing_assets']);self.assertEqual(batch['status'],'attention')
    def test_new_source_rating_during_queued_write_is_rechecked_after_commit(self):
        target_id=self.link.add_target(str(self.destination))['target_id'];self.finish(target_id);target_sid=self.link.targets()[0]['session']
        for rating in (5,3):
            session=self.store.session(self.sid);state=copy.deepcopy(session['state']);state['overrides'][self.cid]=rating;self.store.save(self.sid,session['revision'],state)
            self.link.index_sources(self.sid);self.link.process_session(target_sid)
        self.assertEqual(self.link.state(target_sid)['pairs'][0]['status'],'queued')
        for _ in range(800):
            self.archive.process_one();self.link.tick()
            if not self.link.pending and self.link.targets()[0]['status']=='completed':break
            time.sleep(.02)
        self.assertEqual(self.archive.writer.read(self.target)['XMP-xmp:Rating'],3)
    def test_browser_add_destination_click_sync_without_switching_source(self):
        import server
        self.store.activate(self.sid);self.jpg.unlink();self.archive.thread.start();self.link.thread.start()
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        patches=[patch.object(server,key,value) for key,value in [('STORE',self.store),('DATA',self.store.data),('ARCHIVE',self.archive),('RAWLINK',self.link),('SKU',None),('MODELS',None),('ANALYSIS',None),('WATCH',None),('MOUSE',None)]]
        for p in patches:p.start()
        threading.Thread(target=http.serve_forever,daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                try:
                    page=browser.new_page(viewport={'width':1440,'height':1000},reduced_motion='reduce');errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='networkidle')
                    page.once('dialog',lambda d:d.accept(str(self.destination)));page.locator('#raw-sync-browse').click()
                    page.wait_for_function('()=>!document.getElementById("raw-sync-start").disabled')
                    self.assertEqual(self.link.targets()[0]['status'],'ready');self.assertEqual(self.archive.writer.read(self.target).get('XMP-xmp:Rating'),None)
                    page.locator('#raw-sync-start').click();page.wait_for_function('()=>document.getElementById("raw-sync-summary").textContent==="已同步 1"')
                    page.wait_for_function('()=>document.getElementById("metadata-percent").textContent==="100%"')
                    self.assertEqual(page.locator('#asset-analysis-open').get_attribute('aria-selected'),'true')
                    self.assertEqual(page.locator('#metadata-files [data-metadata-status="applied"]').count(),1)
                    self.assertEqual(page.evaluate('FRAMEKEEP_SESSION.id'),self.sid);self.assertEqual(self.archive.writer.read(self.target)['XMP-xmp:Rating'],4)
                    out=ROOT/'build/archive-sync';out.mkdir(parents=True,exist_ok=True)
                    self.assertEqual(page.locator('#archive-sync-panel button').count(),2);self.assertTrue(page.locator('#raw-sync-open').is_hidden())
                    page.screenshot(path=str(out/'metadata-tasks.png'));page.locator('#analysis-close').click()
                    page.locator('#raw-sync-start').scroll_into_view_if_needed();page.screenshot(path=str(out/'workbench.png'))
                    page.reload(wait_until='networkidle');self.assertEqual(page.locator('#raw-sync-path-display').inner_text(),str(self.destination))
                    self.assertFalse(errors,errors)
                finally:browser.close()
        finally:
            http.shutdown();http.server_close()
            for p in reversed(patches):p.stop()

if __name__=='__main__':unittest.main()
