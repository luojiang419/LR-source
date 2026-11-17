"""跨数据集接续后，既有二星恢复，新标二星能保存并触发OCR。"""
from contextlib import contextmanager,ExitStack
from pathlib import Path
import json
import shutil
import threading
import time
import unittest
from unittest.mock import patch

import test_manual_metadata_backfill as fixture
import test_portable_foreign_recovery as recovery_fixture
from portable_capture_store import Dataset
from portable_capture_recovery import recover


class ForeignCaptureRatingTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ManualBackfillTests();self.f.setUp();f=self.f
        f.api.edit(f.rid,enabled=False)
        incoming=f.base/'incoming';incoming.mkdir()
        records,groups=recovery_fixture.ForeignRecoveryTests.make_group(incoming,['new.jpg','label.jpg'],'LC7715075')
        self.source=Dataset(incoming,f.base/'incoming-data');self.source.acquire()
        self.source.change(records,groups,kind='live_confirm');self.source.release()
        self.commit=next((self.source.folder/'commits').glob('*.json')).name
        for directory in ('events','commits'):
            for path in (self.source.folder/directory).glob('*.json'):shutil.copy2(path,f.dataset.folder/directory/path.name)
        for name in ('new.jpg','label.jpg'):shutil.copy2(incoming/name,f.photos/name)
        f.catalog.scan(f.rid)
        until=time.monotonic()+10
        while time.monotonic()<until:
            with f.catalog.connect() as c:found=c.execute("SELECT count(*) FROM items WHERE name IN ('new.jpg','label.jpg')").fetchone()[0]
            if found==2:break
            time.sleep(.02)

    def tearDown(self):self.source.close();self.f.tearDown()

    def recovery(self):
        dataset=Dataset(self.f.photos,self.f.base/'repair')
        try:result=recover(dataset,self.commit)
        finally:dataset.close()
        self.f.dataset.refresh();self.f.api._scan(self.f.dataset)
        return result

    def test_multisource_records_and_two_stars_ocr_persist(self):
        f=self.f
        before={p:p.read_bytes() for p in f.photos.glob('*.JPG')}
        f.api._scan(f.dataset)
        label=f.dataset.record(path='label.jpg')
        self.assertEqual((label['rating'],label['sku']),(2,'LC7715075'))
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        with patch.object(f.sku,'recognizer',wraps=f.sku.recognizer) as recognize:
            result=f.api.rate(f.item(4),2);self.assertTrue(result['ocr_queued']);f.drain()
            self.assertEqual(recognize.call_count,1)
        f.api._scan(f.dataset)
        record=f.dataset.record(path='IMG_0004.JPG')
        self.assertEqual((record['rating'],record['sku']),(2,'LC123456'))
        self.assertEqual(f.dataset.record(path='label.jpg')['rating'],2)

    def test_already_confirmed_restored_label_does_not_need_ocr_again(self):
        f=self.f;self.recovery()
        with f.catalog.connect() as c:item=c.execute("SELECT id FROM items WHERE name='label.jpg'").fetchone()[0]
        with patch.object(f.sku,'recognizer',side_effect=AssertionError('已有SKU不应重复OCR')):
            result=f.api.rate(item,2);self.assertFalse(result['ocr_queued']);f.drain()
        state=f.api.state(f.rid)
        self.assertEqual(state['total'],6)
        self.assertTrue(any(g['sku']=='LC7715075' and len(g['members'])==2 for g in state['groups']))

    def test_camera_swap_or_clock_order_cannot_split_confirmed_source_manifest(self):
        f=self.f;self.recovery();records=f.dataset.records()
        for row in records:
            if row['relative_path']=='new.jpg':row['camera']={'serial':'CAMERA-A'};row['sequence']=999
            if row['relative_path']=='label.jpg':row['camera']={'serial':'CAMERA-B'};row['sequence']=1
        _,changes=f.api._partition(f.dataset,records=records,apply=False)
        group=next(g for g in f.dataset.groups() if g.get('sku')=='LC7715075')
        self.assertFalse(any(g['id']==group['id'] for g in changes))
        self.assertEqual(len(group['members']),2)

    def test_new_boundary_inside_restored_group_still_allows_explicit_repartition(self):
        f=self.f;self.recovery();records=f.dataset.records()
        for row in records:
            if row['relative_path']=='new.jpg':row.update(rating=2,boundary=True)
        _,changes=f.api._partition(f.dataset,records=records,apply=False)
        self.assertTrue(changes)

    def test_manual_backfill_adopts_source_and_writes_only_confirmed_members(self):
        f=self.f;f.api._scan(f.dataset)
        before={p:p.read_bytes() for p in f.photos.glob('IMG_*.JPG')}
        result=f.api.write_metadata(f.rid);self.assertEqual(result['queued']+result['pending'],2)
        f.drain()
        for name in ('new.jpg','label.jpg'):
            self.assertEqual(f.archive.writer.read(f.photos/name)['XMP-fk:SKU'],'LC7715075')
        self.assertEqual(before,{p:p.read_bytes() for p in before})

    def test_new_camera_source_is_loaded_during_existing_edit_session(self):
        f=self.f;f.api.edit(f.rid)
        incoming=f.base/'second-camera';incoming.mkdir()
        rows,groups=recovery_fixture.ForeignRecoveryTests.make_group(incoming,['camera2-product.jpg','camera2-label.jpg'],'CAMERA-TWO')
        other=Dataset(incoming,f.base/'camera2-data')
        try:
            other.acquire();other.change(rows,groups,kind='live_confirm');other.release()
            for directory in ('events','commits'):
                for path in (other.folder/directory).glob('*.json'):shutil.copy2(path,f.dataset.folder/directory/path.name)
            for r in rows:shutil.copy2(incoming/r['relative_path'],f.photos/r['relative_path'])
            f.api.shared_refresh[str(f.dataset.root)]=0
            f.api._scan(f.dataset)
            self.assertTrue(f.dataset.editing)
            self.assertEqual(f.dataset.record(path='camera2-label.jpg')['rating'],2)
            self.assertEqual(f.dataset.record(path='camera2-label.jpg')['sku'],'CAMERA-TWO')
            self.assertTrue(any(g.get('sku')=='CAMERA-TWO' and len(g['members'])==2 for g in f.dataset.groups()))
        finally:other.close()

    @contextmanager
    def browser(self):
        import server
        from playwright.sync_api import sync_playwright
        f=self.f
        with ExitStack() as stack:
            values=dict(STORE=f.store,CATALOG=f.catalog,ARCHIVE=f.archive,SKU=f.sku,MODELS=f.sku.providers,DATA=f.store.data,WATCH=None,MOUSE=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None,CANON=None,PREVIEWS=None,REFERENCES=None)
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1600,'height':1000});page.set_default_timeout(10000);errors=[]
                        page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                        page.wait_for_function('()=>window.FramekeepArchiveOpenItem&&window.FramekeepArchiveCapture')
                        page.evaluate('(id)=>FramekeepArchiveOpenItem(id)',f.item(4))
                        page.wait_for_function("()=>!document.querySelector('[data-catalog-rating=\"2\"]').disabled")
                        yield page
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()

    def test_browser_failure_is_visible_and_recovery_keeps_two_stars_after_reload(self):
        f=self.f
        source_file=f.dataset.folder/'events'/json.loads((f.dataset.folder/'commits'/self.commit).read_text())['event']
        source_bytes=source_file.read_bytes();source_file.write_bytes(source_bytes+b' ')
        with self.browser() as page:
            page.locator('[data-catalog-rating="2"]').click()
            page.wait_for_function('()=>document.getElementById("toast").textContent.includes("评级未保存")')
            self.assertIn('摘要不一致',page.locator('#catalog-rating-status').inner_text())
            source_file.write_bytes(source_bytes)
            self.recovery();page.evaluate('()=>FramekeepArchiveCapture.refresh(true)')
            page.wait_for_function("()=>!document.querySelector('[data-catalog-rating=\"2\"]').disabled")
            page.locator('[data-catalog-rating="2"]').click()
            page.wait_for_function('()=>/二星已保存|已保存 2 星/.test(document.getElementById("catalog-rating-status").textContent)')
            f.drain();page.evaluate('()=>FramekeepArchiveCapture.refresh(true)')
            page.wait_for_function('()=>document.getElementById("capture-groups").textContent.includes("LC123456")')
            page.reload(wait_until='domcontentloaded')
            page.wait_for_function('()=>window.FramekeepArchiveOpenItem&&window.FramekeepArchiveCapture')
            page.evaluate('(id)=>FramekeepArchiveOpenItem(id)',f.item(4))
            page.wait_for_function('()=>document.getElementById("catalog-selected-rating").textContent==="★★"')
            self.assertEqual(f.dataset.record(path='IMG_0004.JPG')['sku'],'LC123456')
            out=Path(__file__).resolve().parents[1]/'build/foreign-capture-recovery/star-verified.png';page.screenshot(path=str(out))


if __name__=='__main__':unittest.main()
