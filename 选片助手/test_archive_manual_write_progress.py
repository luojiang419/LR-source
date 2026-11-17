"""手工确认与补写共享真实写入进度，实际文件回读后才显示完成。"""
from contextlib import ExitStack
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import test_archive_capture_service as fixtures


class ManualWriteProgressTests(unittest.TestCase):
    setUp=fixtures.ArchiveCaptureTests.setUp
    tearDown=fixtures.ArchiveCaptureTests.tearDown
    client=fixtures.ArchiveCaptureTests.client
    item=fixtures.ArchiveCaptureTests.item
    drain=fixtures.ArchiveCaptureTests.drain

    def test_confirmation_progress_waits_for_real_file_write(self):
        result=self.api.confirm(self.item(1),'中文 Ab-001',whole_group=False,root_id=self.rid)
        state=result['write_request']
        self.assertEqual(state['result']['mode'],'manual')
        self.assertEqual((state['progress']['total'],state['progress']['queued'],state['progress']['done']),(1,1,0))
        self.assertFalse(self.archive.writer.read(self.photos/'IMG_0001.JPG').get('XMP-fk:SKU'))
        self.drain()
        state=self.api.metadata_write_state(self.rid)
        self.assertEqual((state['progress']['done'],state['progress']['failed']),(1,0))
        self.assertEqual(self.archive.writer.read(self.photos/'IMG_0001.JPG')['XMP-fk:SKU'],'中文 Ab-001')
        self.api.request_items.clear()
        self.assertEqual(self.api.metadata_write_state(self.rid)['progress'],state['progress'])

    def test_confirmation_does_not_replace_pending_backfill(self):
        with patch.object(self.api,'_start_metadata_request_worker'):
            backfill=self.api.request_metadata_write(self.rid)
        manual=self.api.confirm(self.item(1),'手动款号',whole_group=False,root_id=self.rid)['write_request']
        self.assertNotEqual(manual['id'],backfill['id'])
        with self.archive.connect() as c:
            row=c.execute('SELECT id,status FROM capture_metadata_requests WHERE root=?',(self.rid,)).fetchone()
        self.assertEqual(tuple(row),(backfill['id'],'queued'))
        self.assertEqual(self.api.metadata_write_state(self.rid)['id'],manual['id'])
        with self.archive.connect() as c:c.execute('UPDATE capture_metadata_requests SET updated=updated+100')
        self.assertEqual(self.api.metadata_write_state(self.rid)['id'],manual['id'])
        with patch.object(self.api,'_start_metadata_request_worker'):
            self.assertEqual(self.api.request_metadata_write(self.rid)['id'],backfill['id'])

    def test_superseded_confirmation_progress_cannot_claim_new_revision(self):
        old=self.api.confirm(self.item(1),'第一款',whole_group=False,root_id=self.rid)['write_request']
        record=self.dataset.record(path='IMG_0001.JPG');revision=record['decision_id']
        self.api.confirm(self.item(1),'第二款',whole_group=False,root_id=self.rid)
        self.drain()
        progress=self.api._request_progress(old['id'],self.dataset,[record['id']],{record['id']:revision})
        self.assertEqual((progress['done'],progress['skipped']),(0,1))

    def test_current_write_failure_is_reported(self):
        self.api.confirm(self.item(1),'失败验证',whole_group=False,root_id=self.rid)
        with patch.object(self.archive.writer,'write',side_effect=ValueError('实际文件写入失败')):
            self.api.tick()
        progress=self.api.metadata_write_state(self.rid)['progress']
        self.assertEqual((progress['done'],progress['failed']),(0,1))
        self.assertIn('实际文件写入失败',progress['failures'][0]['reason'])

    def test_catalog_reader_preserves_literal_text_and_numeric_camera_fields(self):
        path=self.photos/'IMG_0001.JPG'
        path.write_bytes(self.archive.writer.edit(path,['-XPSubject=123.00','-XMP-dc:Title=TRUE','-XMP-dc:Subject=1E3','-ISO=400']))
        with self.assertRaisesRegex(ValueError,'文件已变化'):
            self.catalog.metadata._extract(self.catalog.item(self.item(1)))
        payload,_=self.catalog.metadata._extract(self.catalog.item(self.item(1)))
        self.assertEqual(payload['tags']['IFD0:XPSubject'],'123.00')
        self.assertEqual(payload['tags']['XMP-dc:Title'],'TRUE')
        self.assertEqual(payload['tags']['XMP-dc:Subject'],'1E3')
        self.assertEqual(payload['capture']['iso'],400)

    def test_browser_manual_write_shows_both_panels_and_card_animation(self):
        import server
        from playwright.sync_api import sync_playwright
        self.api.rate(self.item(2),2,root_id=self.rid);self.drain()
        entered=threading.Event();release=threading.Event();errors=[];worker=None
        original=self.archive.writer.write
        def blocked(*args,**kwargs):
            entered.set()
            if not release.wait(15):raise TimeoutError('test writer was not released')
            return original(*args,**kwargs)
        def run():
            try:self.api.tick()
            except BaseException as error:errors.append(str(error))
        with ExitStack() as stack:
            values=dict(STORE=self.store,CATALOG=self.catalog,ARCHIVE=self.archive,SKU=self.sku,MODELS=self.sku.providers,DATA=self.store.data,WATCH=None,MOUSE=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None,CANON=None,PREVIEWS=None,REFERENCES=None)
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1600,'height':1000})
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        confirmations=[]
                        page.on('request',lambda req:confirmations.append(req.url) if req.method=='POST' and req.url.endswith('/api/archive-capture/confirm') else None)
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                        page.wait_for_function('()=>window.FramekeepArchiveOpenItem&&window.FramekeepArchiveCapture')
                        page.evaluate('(id)=>FramekeepArchiveOpenItem(id)',self.item(2))
                        page.wait_for_function("()=>!document.querySelector('#catalog-save-sku').disabled")
                        page.locator('#catalog-selected-sku').fill('中文 Ab-001')
                        page.locator('#catalog-selected-sku').press('Enter')
                        page.wait_for_function("()=>document.getElementById('capture-photo-write-phase').textContent==='排队等待写入'")
                        self.assertEqual(page.locator('#catalog-grid [data-capture-write="queued"]').count(),2)
                        self.assertEqual(page.locator('#capture-ocr-feedback').get_attribute('data-status'),'running')
                        self.assertIn('中文 Ab-001',page.locator('#catalog-rating-status').inner_text())
                        with patch.object(self.archive.writer,'write',side_effect=blocked):
                            worker=threading.Thread(target=run);worker.start()
                            try:
                                self.assertTrue(entered.wait(5))
                                page.wait_for_function("()=>document.getElementById('capture-photo-write-phase').textContent==='正在写入元数据'")
                                card=page.locator('#catalog-grid [data-capture-write="running"]')
                                self.assertEqual(card.count(),1)
                                self.assertEqual(card.evaluate("n=>getComputedStyle(n,'::after').animationName"),'capture-glow-spin')
                                self.assertEqual(page.locator('#capture-photo-write-count').inner_text(),'0 / 2')
                                out=Path(__file__).resolve().parents[1]/'build/task227-manual-progress';out.mkdir(parents=True,exist_ok=True)
                                page.screenshot(path=str(out/'writing.png'))
                            finally:release.set();worker.join(10)
                        self.drain()
                        page.wait_for_function("()=>document.getElementById('capture-photo-write-phase').textContent==='写入完成'")
                        self.assertEqual(page.locator('#capture-photo-write-count').inner_text(),'2 / 2')
                        self.assertEqual(page.locator('#capture-ocr-feedback').get_attribute('data-status'),'completed')
                        page.locator('#capture-folder-tab').click()
                        self.assertEqual(page.locator('#capture-write-count').inner_text(),'2 / 2')
                        self.assertEqual(page.locator('#capture-write-phase').inner_text(),'写入完成')
                        self.assertEqual(len(confirmations),1,'Enter 只能提交一次确认，不能再触发旧独立写入入口')
                        self.assertEqual(page.locator('#catalog-inspector-form > #catalog-save-sku').count(),0)
                        for n in (1,2):self.assertEqual(self.archive.writer.read(self.photos/f'IMG_{n:04d}.JPG')['XMP-fk:SKU'],'中文 Ab-001')
                        page.screenshot(path=str(out/'complete.png'))
                        self.assertFalse(errors,errors)
                    finally:release.set();browser.close()
            finally:
                if worker:worker.join(10)
                http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
