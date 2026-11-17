"""拍摄零图片导出、后台快照和元数据准备期并发保护。"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import hashlib
import json
import threading
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from playwright.sync_api import expect,sync_playwright
import test_ocr_metadata as fixture
import sku_reconciliation


class DeferredExportTests(unittest.TestCase):
    setUp=fixture.OcrMetadataTests.setUp
    tearDown=fixture.OcrMetadataTests.tearDown
    parse=fixture.OcrMetadataTests.parse
    choose=fixture.OcrMetadataTests.choose

    def wait_job(self,jid):
        end=time.monotonic()+10
        while time.monotonic()<end:
            job=self.sku.exports.state(jid)
            if job['status'] in ('complete','failed'):return job
            time.sleep(.02)
        self.fail('导出没有结束')

    def test_capture_edits_and_tick_never_generate_picture_workbooks(self):
        with patch('sku_reconciliation.export_images',side_effect=AssertionError('拍摄不得生成图片文档')):
            self.parse();self.choose();self.sku.tick()
            from sku_presentation import rename_header
            rename_header(self.sku,self.pid,'sku','产品款号')
            state=self.sku.state()
            self.assertEqual(sum(p['shot'] for p in state['progress']),1)
            self.assertTrue(any(r['sku_image'] for r in state['rows']))
        self.assertFalse((self.sku.root/self.pid/'拍摄对账.xlsx').exists())
        self.assertTrue(self.sku.export(self.pid).is_file())

    def test_export_snapshot_does_not_block_new_confirmation_or_mutate_source(self):
        self.parse();self.choose();source=self.sku.root/self.pid/'source.xlsx';before=source.read_bytes()
        entered=threading.Event();release=threading.Event();done=threading.Event();errors=[]
        original=sku_reconciliation.export_images
        def slow(*args,**kwargs):
            entered.set()
            if not release.wait(10):raise RuntimeError('测试超时')
            return original(*args,**kwargs)
        try:
            with patch('sku_reconciliation.export_images',side_effect=slow):
                job=self.sku.exports.start(self.pid,'导出.xlsx');self.assertTrue(entered.wait(4))
                self.assertEqual(self.sku.exports.start(self.pid)['id'],job['id'])
                pending=self.sku.exports.state(job['id']);self.assertLess(pending['percent'],100)
                def confirm():
                    try:
                        self.sku.set_sku_rules([{'kind':'contains','value':'LC'}])
                        self.reading.update(text='LC-NEW-999',codes=['LC-NEW-999'],ocr_candidates=['LC-NEW-999'])
                        self.sku.parse_capture(self.sid,self.ids[3]);self.sku.state()
                    except Exception as exc:errors.append(exc)
                    finally:done.set()
                thread=threading.Thread(target=confirm);thread.start()
                self.assertTrue(done.wait(4),'新确认与进度读取不得等待导出图片')
                self.assertFalse(errors,errors);self.assertFalse(release.is_set())
                release.set();thread.join(4)
                finished=self.wait_job(job['id']);self.assertEqual(finished['status'],'complete',finished)
            self.assertEqual(finished['percent'],100)
            self.assertEqual(finished['images_done'],finished['images_total'])
            output,_=self.sku.exports.output(job['id'])
            with ZipFile(output) as archive:
                text=''.join(archive.read(n).decode() for n in archive.namelist() if n.startswith('xl/worksheets/') and n.endswith('.xml'))
                self.assertNotIn('LC-NEW-999',text)
                self.assertTrue(any(n.startswith('xl/media/') for n in archive.namelist()))
            self.assertEqual(source.read_bytes(),before)
        finally:release.set()

    def test_export_failure_is_visible_and_can_start_again(self):
        self.parse();self.choose()
        with patch('sku_reconciliation.export_images',side_effect=RuntimeError('测试图片处理失败')):
            first=self.sku.exports.start(self.pid);failed=self.wait_job(first['id'])
        self.assertEqual(failed['status'],'failed');self.assertLess(failed['percent'],100)
        self.assertIn('测试图片处理失败',failed['error'])
        retry=self.sku.exports.start(self.pid);self.assertNotEqual(first['id'],retry['id'])
        self.assertEqual(self.wait_job(retry['id'])['status'],'complete')

    def test_2048_label_image_is_only_created_for_explicit_export(self):
        import io
        from PIL import Image
        import media_engine as media
        from sku_workbook import inspect_workbook,read_image
        photo=self.photos/'1.JPG';Image.new('RGB',(3600,2400),'#789181').save(photo)
        self.store.append_live(self.sid,[media.capture_record(self.photos,photo,None,{'DateTimeOriginal':'2026:10:21 10:00:01'})])
        with patch('sku_reconciliation.export_images',side_effect=AssertionError('拍摄不应压缩文档图片')):
            self.parse();self.choose();self.sku.state()
        with Image.open(photo) as original:self.assertEqual(original.size,(3600,2400))
        output=self.sku.export(self.pid);plan=inspect_workbook(output)[0]
        label=next(image for image in plan['images'] if image['cell']=='D2')
        with Image.open(io.BytesIO(read_image(output,label['media'])[0])) as embedded:
            self.assertEqual(max(embedded.size),2048)
        with Image.open(photo) as original:self.assertEqual(original.size,(3600,2400))

    def test_metadata_preparation_does_not_block_other_label_confirmation(self):
        self.parse();self.choose();entered=threading.Event();release=threading.Event();done=threading.Event();errors=[]
        original=self.archive.writer.write
        def slow(*args,**kwargs):
            entered.set()
            if not release.wait(10):raise RuntimeError('测试超时')
            return original(*args,**kwargs)
        try:
            with patch.object(self.archive.writer,'write',side_effect=slow):
                writer=threading.Thread(target=self.archive.process_one);writer.start();self.assertTrue(entered.wait(4))
                def confirm():
                    try:
                        self.sku.set_sku_rules([{'kind':'contains','value':'LC'}])
                        self.sku.parse_capture(self.sid,self.ids[3]);self.sku.state()
                    except Exception as exc:errors.append(exc)
                    finally:done.set()
                parser=threading.Thread(target=confirm);parser.start()
                self.assertTrue(done.wait(4),'新标签不应等待旧原图的写入准备')
                self.assertFalse(errors,errors)
                release.set();writer.join(5);parser.join(5)
        finally:release.set()

    def test_revert_during_metadata_preparation_does_not_commit_stale_payload(self):
        self.parse();self.choose();entered=threading.Event();release=threading.Event()
        with self.archive.connect() as c:job=dict(c.execute("SELECT * FROM metadata_jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone())
        from pathlib import Path
        photo=Path(job['path']);before=hashlib.sha256(photo.read_bytes()).hexdigest();original=self.archive.writer.write
        def slow(*args,**kwargs):entered.set();release.wait(8);return original(*args,**kwargs)
        try:
            with patch.object(self.archive.writer,'write',side_effect=slow):
                writer=threading.Thread(target=self.archive.process_one);writer.start();self.assertTrue(entered.wait(4))
                self.archive.revert(job['operation']);release.set();writer.join(5)
            self.assertFalse(writer.is_alive());self.assertEqual(hashlib.sha256(photo.read_bytes()).hexdigest(),before)
            with self.archive.connect() as c:self.assertEqual(c.execute('SELECT status FROM metadata_jobs WHERE id=?',(job['id'],)).fetchone()[0],'cancelled')
        finally:release.set()

    def test_browser_export_animation_progress_and_single_download(self):
        import server
        self.parse();self.choose();entered=threading.Event();release=threading.Event()
        original=sku_reconciliation.export_images;calls=[]
        def slow(*args,**kwargs):
            calls.append(1);entered.set()
            if not release.wait(12):raise RuntimeError('测试超时')
            return original(*args,**kwargs)
        values=dict(STORE=self.store,DATA=self.store.data,SKU=self.sku,MODELS=self.providers,ARCHIVE=self.archive,
                    CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,CANON=None,RAWLINK=None,FILESEARCH=None,
                    UPDATES=None,COMPONENTS=None,DRIVER=None)
        out=fixture.ROOT/'build/deferred-shooting-export';out.mkdir(parents=True,exist_ok=True)
        with ExitStack() as stack:
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            stack.enter_context(patch('sku_reconciliation.export_images',side_effect=slow))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1500,'height':1050},accept_downloads=True);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.add_init_script('window.showSaveFilePicker=undefined')
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.locator('#startup-screen').wait_for(state='detached')
                        page.locator('#sku-open').click();page.locator('#sku-export').click()
                        expect(page.locator('#shooting-export-dialog')).to_be_visible();self.assertTrue(entered.wait(4))
                        expect(page.locator('#shooting-export-images')).to_contain_text('0 /')
                        self.assertLess(page.locator('#shooting-export-progress').evaluate('(el)=>el.value'),100)
                        self.assertEqual(page.locator('#shooting-export-dialog svg').count(),1)
                        page.screenshot(path=str(out/'export-running.png'))
                        with page.expect_download(timeout=15000) as download:
                            release.set();page.wait_for_function('()=>document.querySelector("#shooting-export-dialog").dataset.status==="complete"')
                        saved=download.value;saved.save_as(str(out/'拍摄文档.xlsx'));self.assertEqual(ZipFile(saved.path()).testzip(),None)
                        expect(page.locator('#shooting-export-percent')).to_have_text('100%')
                        count=len(calls)
                        with page.expect_download():page.locator('#shooting-export-save').click()
                        self.assertEqual(len(calls),count,'下载不得重新生成或压缩图片')
                        page.wait_for_timeout(600)
                        page.screenshot(path=str(out/'export-complete.png'))
                        page.emulate_media(reduced_motion='reduce')
                        self.assertEqual(page.locator('.export-sheet').evaluate('(e)=>getComputedStyle(e).animationName'),'none')
                        page.set_viewport_size({'width':430,'height':900})
                        self.assertLessEqual(page.locator('#shooting-export-dialog').bounding_box()['width'],430)
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:release.set();http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
