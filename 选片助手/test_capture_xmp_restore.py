"""二星完整XMP、随行档案及改星一致性：真实ExifTool和隔离文件验证。"""
import hashlib
import json
from pathlib import Path
import unittest
import uuid

from PIL import Image
from portable_capture_store import Dataset,digest,encoded
from rating_xmp import sidecar_paths,sidecar_metadata
import test_archive_capture_service as fixtures
import test_portable_live_bridge as live_fixtures

ROOT=Path(__file__).resolve().parents[1]


class CaptureXmpRestoreTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ArchiveCaptureTests();self.f.setUp();self.addCleanup(self.f.tearDown)

    def assert_synced(self,n):
        f=self.f;path=f.photos/f'IMG_{n:04d}.JPG';record=f.dataset.record(path=path.name)
        self.assertTrue(sidecar_paths(path),'完整解析后必须存在随行XMP')
        embedded=f.archive.writer.read(path)
        for actual in [embedded,*[f.archive.writer.read(side) for side in sidecar_paths(path)]]:
            for field in ('Rating','SKU','DecisionId','SegmentId','PhotoRole','AssignmentState','GroupProductCount','GroupLabelCount','GroupMembersDigest'):
                wanted=f.api._fields(f.dataset,record)[field];tag='XMP-xmp:Rating' if field=='Rating' else 'XMP-fk:'+field
                self.assertEqual(str(actual.get(tag) if actual.get(tag) is not None else ''),str(wanted),field)
            if record.get('sku'):
                subjects=actual['XMP-dc:Subject'];subjects=subjects if isinstance(subjects,list) else [subjects]
                self.assertIn('SKU:'+record['sku'],subjects)
        reader=Dataset(f.photos,f.base/'independent-reader')
        try:
            reader.refresh();copy=reader.record(path=path.name)
            self.assertEqual(copy['rating'],record['rating']);self.assertEqual(copy['decision_id'],record['decision_id']);self.assertEqual(copy['sku'],record['sku'])
        finally:reader.close()
        return record

    def test_two_star_parse_creates_complete_xmp_for_label_and_products(self):
        f=self.f;f.api.rate(f.item(2),2);f.drain()
        first=self.assert_synced(1);label=self.assert_synced(2)
        self.assertEqual(label['rating'],2);self.assertEqual(label['sku'],'LC123456');self.assertEqual(first['group_id'],label['group_id'])
        self.assertEqual(label['role'],'sku_label');self.assertEqual(first['role'],'product')
        self.assertEqual(sidecar_metadata(f.photos/'IMG_0002.JPG')['Rating'],2)

    def test_rating_changes_clear_and_recreate_keep_all_copies_and_presets(self):
        f=self.f;f.api.rate(f.item(2),2);f.drain();path=f.photos/'IMG_0002.JPG';side=sidecar_paths(path)[0]
        f.archive.writer.run('-overwrite_original','-XMP-crs:Exposure2012=1.25','-XMP-dc:Title=保留调色标题',side)
        with Image.open(path) as image:pixels=hashlib.sha256(image.tobytes()).hexdigest()
        stamp=path.stat().st_mtime_ns;results=[]
        for rating in (5,1,0,2):
            f.api.rate(f.item(2),rating);f.drain();record=self.assert_synced(2)
            self.assertEqual(record['rating'],rating)
            actual=f.archive.writer.read(side,True)
            self.assertEqual(actual['XMP-crs:Exposure2012'],1.25);self.assertEqual(actual['XMP-dc:Title'],'保留调色标题')
            histories=[json.loads(p.read_text(encoding='utf8')) for p in (f.store.data/'metadata-history').glob('*.json')]
            self.assertTrue(any(h['source']==str(path) and h['status']=='applied' and h['journal']['after'].get('Rating')==rating and h['journal']['after'].get('DecisionId')==record['decision_id'] for h in histories))
            results.append({'rating':rating,'xmp_rating':actual['XMP-xmp:Rating'],'portable_rating':record['rating'],'preset_exposure':actual['XMP-crs:Exposure2012']})
        with Image.open(path) as image:self.assertEqual(hashlib.sha256(image.tobytes()).hexdigest(),pixels)
        self.assertEqual(path.stat().st_mtime_ns,stamp)
        out=ROOT/'build/capture-xmp-restore';out.mkdir(parents=True,exist_ok=True);(out/'ratings.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf8')

    def test_verified_old_photo_recovers_missing_xmp_without_rewriting_jpg(self):
        f=self.f;f.api.confirm(f.item(1),'RESTORE-XMP-001',whole_group=False);f.drain()
        path=f.photos/'IMG_0001.JPG';before=path.read_bytes();stamp=path.stat().st_mtime_ns
        for side in sidecar_paths(path):side.unlink()
        record=f.dataset.record(path=path.name)
        f.dataset.change([{**record,'sidecar_signature':digest(encoded([]))}],kind='test_missing_sidecar')
        f.api._recover_tasks(f.dataset)
        with f.dataset.connect() as db:self.assertTrue(db.execute("SELECT 1 FROM tasks WHERE kind='metadata_sidecar' AND status='queued'").fetchone())
        f.drain();self.assertEqual(path.read_bytes(),before);self.assertEqual(path.stat().st_mtime_ns,stamp);self.assert_synced(1)

    def test_existing_raw_component_id_is_preserved_when_both_sidecars_follow_rating(self):
        f=self.f;f.api.confirm(f.item(1),'PAIR-001',whole_group=False);f.drain();path=f.photos/'IMG_0001.JPG'
        path.with_suffix('.xmp').replace(Path(str(path)+'.xmp'))
        record=f.dataset.record(path=path.name);raw_id=uuid.uuid4().hex;fields={**f.api._fields(f.dataset,record),'FileId':raw_id}
        content,target,journal=f.archive.writer.write(path,fields,hashlib.sha256(path.read_bytes()).hexdigest(),_target=path.with_suffix('.xmp'))
        f.archive.writer.commit(content,target,journal)
        f.api.rate(f.item(1),4);f.drain();self.assert_synced(1)
        self.assertEqual(f.archive.writer.read(path.with_suffix('.xmp'))['XMP-fk:FileId'].replace('-',''),raw_id)
        self.assertEqual(f.archive.writer.read(Path(str(path)+'.xmp'))['XMP-fk:FileId'].replace('-',''),record['file_id'])


class LiveCaptureXmpRestoreTests(unittest.TestCase):
    def test_live_two_star_then_manual_rating_updates_xmp_and_portable_folder(self):
        f=live_fixtures.PortableLiveBridgeTests();f.setUp();self.addCleanup(f.tearDown);label,dataset=f.ready();f.finish(dataset)
        for path in f.paths:
            self.assertTrue(sidecar_paths(path));actual=sidecar_metadata(path)
            self.assertEqual(actual['SKU'],'TEMP-NEW-900');self.assertTrue(actual['DecisionId']);self.assertTrue(actual['SegmentId'])
        path=f.paths[-1]
        for rating in (4,0):
            f.rate(label,rating);f.sku.tick();f.drain();f.finish(dataset)
            row=dataset.record(path=path.name)
            self.assertEqual(row['rating'],rating);self.assertEqual(f.archive.writer.read(path)['XMP-xmp:Rating'],rating)
            for side in sidecar_paths(path):self.assertEqual(f.archive.writer.read(side)['XMP-xmp:Rating'],rating)


class CaptureXmpBrowserTests(unittest.TestCase):
    def test_live_page_two_star_parse_then_five_and_zero_writes_real_xmp(self):
        from contextlib import ExitStack
        from http.server import ThreadingHTTPServer
        import threading
        import time
        from unittest.mock import patch
        from playwright.sync_api import sync_playwright
        from archive_capture_service import ArchiveCaptureService
        import server
        f=live_fixtures.PortableLiveBridgeTests();f.setUp();self.addCleanup(f.tearDown)
        f.capture=ArchiveCaptureService(f.store,f.archive,f.catalog,f.sku,autostart=False)
        f.add('IMG_0001.JPG');label=f.add('IMG_0002.JPG');path=f.paths[-1]
        stopped=threading.Event();driver_errors=[]
        def drive():
            while not stopped.is_set():
                try:
                    f.sku.tick();f.archive.ratings.sync.process_one();f.archive.process_one();f.capture.tick()
                except Exception as error:driver_errors.append(str(error));return
                stopped.wait(.025)
        driver=threading.Thread(target=drive,daemon=True)
        with ExitStack() as stack:
            values=dict(STORE=f.store,DATA=f.store.data,MODELS=f.providers,ARCHIVE=f.archive,SKU=f.sku,CATALOG=f.catalog,WATCH=None,MOUSE=None,CANON=None,ANALYSIS=None,RAWLINK=f.link,FILESEARCH=None,PREVIEWS=None,INVENTORY=None,REFERENCES=None,STORAGE=None,CAPTURESYNC=None)
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            stack.enter_context(patch.object(server,'workbench_views',return_value=None))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start();stack.callback(http.server_close);stack.callback(http.shutdown)
            driver.start();stack.callback(lambda:(stopped.set(),driver.join(timeout=15)))
            out=ROOT/'build/capture-xmp-restore';out.mkdir(parents=True,exist_ok=True)
            with sync_playwright() as pw,pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True) as browser:
                page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading')
                page.evaluate('(id)=>{FramekeepWorkspace.show("selection");Framekeep.browsePhoto(id)}',label)
                checks=[]
                for rating in (2,5,0):
                    if rating:page.locator(f'#stars [data-rating="{rating}"]').click()
                    else:page.keyboard.press('0')
                    deadline=time.perf_counter()+30
                    while time.perf_counter()<deadline:
                        metadata=sidecar_metadata(path);dataset=f.capture.dataset_for_path(path);record=dataset.record(path=path.name) if dataset else None
                        if metadata.get('Rating')==rating and metadata.get('SKU') and metadata.get('DecisionId') and record and record.get('rating')==rating and record.get('write_state')=='verified':break
                        if driver_errors:self.fail(driver_errors)
                        page.wait_for_timeout(50)
                    else:self.fail({'rating':rating,'metadata':metadata,'record':record,'driver_errors':driver_errors})
                    self.assertEqual(f.archive.writer.read(path)['XMP-xmp:Rating'],rating)
                    checks.append({'clicked_rating':rating,'xmp_rating':metadata['Rating'],'portable_rating':record['rating'],'sku':metadata['SKU'],'sidecars':[side.name for side in sidecar_paths(path)]})
                    (out/f'sample-rating-{rating}.xmp').write_bytes(sidecar_paths(path)[0].read_bytes())
                    page.screenshot(path=str(out/f'live-rating-{rating}.png'))
                self.assertFalse(errors,errors);self.assertFalse(driver_errors,driver_errors)
                (out/'browser.json').write_text(json.dumps({'checks':checks,'page_errors':errors,'driver_errors':driver_errors},ensure_ascii=False,indent=2),encoding='utf8')


if __name__=='__main__':unittest.main()
