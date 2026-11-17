"""改款接口不读原片，持久文件队列独立执行，页面状态自动收尾。"""
import hashlib
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
from PIL import Image

from asset_archive import AssetArchive
from sku_workbook import inspect_workbook
from test_shooting_documents import ShootingDocumentsTests


class BackgroundRowWriteTests(unittest.TestCase):
    setUp=ShootingDocumentsTests.setUp
    tearDown=ShootingDocumentsTests.tearDown
    prepare=ShootingDocumentsTests.prepare
    rate=ShootingDocumentsTests.rate
    choose=ShootingDocumentsTests.choose
    drain=ShootingDocumentsTests.drain

    def ready(self):
        self.pid=self.prepare()['id'];self.rate(self.records[1]['id']);self.choose(self.records[1]['id'],'OLD-100');self.drain()
        self.row=next(r for r in self.sku.state()['rows'] if r['sku']=='OLD-100')

    def rename(self,old='OLD-100',new='NEW-200'):
        return self.sku.rename_shooting_row(self.pid,self.row['id'],old,new)

    def wait_written(self,sku='NEW-200'):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            row=next(r for r in self.sku.state()['rows'] if r['sku']==sku)
            if row['metadata']['status']=='applied':return row
            time.sleep(.04)
        self.fail('持久队列没有自动完成：'+str(row['metadata']))

    def test_submit_never_reads_original_or_rewrites_workbook_and_exports_saved_sku(self):
        self.ready();source=self.sku.root/self.pid/'source.xlsx';before=source.read_bytes()
        with patch('asset_archive.sha',side_effect=AssertionError('提交不得计算原片哈希')),patch('pathlib.Path.is_file',side_effect=AssertionError('提交不得探测原片')),patch('sku_workbook_auto.rename_codes',side_effect=AssertionError('提交不得压缩源表')):
            result=self.rename()
        self.assertTrue(result['operation_ids']);row=next(r for r in result['state']['rows'] if r['id']==self.row['id'])
        self.assertEqual((row['sku'],row['metadata']['status']),('NEW-200','queued'))
        self.assertEqual(source.read_bytes(),before)
        self.drain();self.assertEqual(self.archive.writer.read(self.photos/'0.JPG')['XMP-fk:SKU'],'NEW-200')
        exported=inspect_workbook(self.sku.export(self.pid))
        self.assertEqual(next(s for s in exported if s['name']=='现场记录')['rows'][0]['sku'],'NEW-200')
        self.assertEqual(source.read_bytes(),before)

    def test_directory_sync_errors_cannot_starve_existing_file_jobs(self):
        self.ready();self.rename()
        with patch.object(self.archive,'sync_stream',side_effect=ValueError('模拟旧目录不可用')):
            self.archive.thread.start();row=self.wait_written()
        self.assertEqual(row['metadata']['status'],'applied')
        self.assertEqual(self.archive.writer.read(self.photos/'1.JPG')['XMP-fk:SKU'],'NEW-200')

    def test_queued_write_survives_restart_and_runs_without_open_page(self):
        self.ready();self.rename();self.archive.close()
        self.archive=AssetArchive(self.store,autostart=False);self.sku.archive=self.archive;self.archive.sku=self.sku
        self.archive.thread.start();self.wait_written()
        self.assertEqual(self.archive.writer.read(self.photos/'0.JPG')['XMP-fk:SKU'],'NEW-200')

    def test_latest_rename_wins_before_background_write_and_keeps_neighbor_pixels(self):
        self.ready();pixels={}
        for path in self.photos.glob('*.JPG'):
            with Image.open(path) as image:pixels[path]=hashlib.sha256(image.tobytes()).hexdigest()
        neighbor=(self.photos/'2.JPG').read_bytes()
        self.rename();self.rename('NEW-200','FINAL-300');self.drain()
        for name in ('0.JPG','1.JPG'):self.assertEqual(self.archive.writer.read(self.photos/name)['XMP-fk:SKU'],'FINAL-300')
        self.assertEqual((self.photos/'2.JPG').read_bytes(),neighbor)
        for path,digest in pixels.items():
            with Image.open(path) as image:self.assertEqual(hashlib.sha256(image.tobytes()).hexdigest(),digest)

    def test_imported_plan_exports_renamed_sku_without_changing_original_or_source(self):
        from test_sku_workflow import fixture_book
        original=fixture_book();p=self.sku.import_file('plan.xlsx',original)['project'];self.pid=p['id']
        self.row=self.sku.rows(self.pid)[0];source=self.sku.root/self.pid/'source.xlsx'
        self.sku.rename_shooting_row(self.pid,self.row['id'],self.row['sku'],'PLAN-NEW')
        self.assertEqual(source.read_bytes(),original)
        exported=inspect_workbook(self.sku.export(self.pid))
        self.assertEqual(exported[0]['rows'][0]['sku'],'PLAN-NEW');self.assertEqual(source.read_bytes(),original)

    def test_browser_failure_is_inline_retry_resumes_and_success_removes_hint(self):
        from test_sku_input_confirm import InputConfirmTests
        from playwright.sync_api import sync_playwright,expect
        self.ready();original=self.archive.writer.write;attempts=[]
        def fail_once(*args,**kwargs):
            attempts.append(1)
            if len(attempts)==1:raise ValueError('模拟文件被占用')
            return original(*args,**kwargs)
        with InputConfirmTests.http(self) as url,sync_playwright() as pw,patch.object(self.archive.writer,'write',side_effect=fail_once):
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':1000})
                page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading');page.locator('#sku-open').click()
                page.locator('[data-edit-sku]').first.dblclick();page.locator('.sku-code-input').fill('NEW-200');page.locator('.sku-code-input').press('Enter')
                expect(page.locator('[data-edit-sku]').first).to_have_text('NEW-200');self.archive.thread.start()
                retry=page.locator('#sku-rows [data-sku-write-retry]');expect(retry).to_be_visible(timeout=10000)
                expect(page.locator('#sku-rows .sku-metadata-status')).to_contain_text('未完成')
                self.assertIn('模拟文件被占用',page.locator('#sku-rows .sku-metadata-status').get_attribute('title'))
                retry.click();expect(page.locator('#sku-rows .sku-metadata-status')).to_have_count(0,timeout=10000)
                self.assertEqual(self.archive.writer.read(self.photos/'0.JPG')['XMP-fk:SKU'],'NEW-200')
            finally:browser.close()

    def test_external_file_change_is_reported_and_not_overwritten(self):
        self.ready();self.rename();photo=self.photos/'0.JPG'
        with photo.open('ab') as stream:stream.write(b'external-change')
        before=photo.read_bytes()
        for _ in range(3):self.archive.process_one()
        row=next(r for r in self.sku.state()['rows'] if r['id']==self.row['id'])
        self.assertEqual(row['metadata']['status'],'failed');self.assertTrue(any('修改' in j['reason'] for j in row['metadata']['jobs']))
        self.assertEqual(photo.read_bytes(),before)

    def test_each_registered_copy_uses_its_own_saved_fingerprint(self):
        import shutil
        from metadata_writer import sha
        self.ready();copy_path=self.base/'copy.JPG';shutil.copy2(self.photos/'0.JPG',copy_path)
        with copy_path.open('ab') as stream:stream.write(b'known-copy-version')
        digest=sha(copy_path)
        with self.archive.connect() as c:
            record=c.execute('SELECT * FROM file_fingerprints WHERE path=?',(str(self.photos/'0.JPG'),)).fetchone()
            c.execute('INSERT INTO locations VALUES (?,?,?,?)',(str(copy_path),record['file'],time.time(),'online'))
            values=dict(record,path=str(copy_path),sha=digest)
            columns=list(values);c.execute('INSERT INTO file_fingerprints('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',[values[k] for k in columns])
        self.rename();self.drain()
        self.assertEqual(self.archive.writer.read(copy_path)['XMP-fk:SKU'],'NEW-200')

    def test_browser_slow_background_write_does_not_block_navigation_and_status_disappears(self):
        from test_sku_input_confirm import InputConfirmTests
        from playwright.sync_api import sync_playwright,expect
        self.ready();entered=threading.Event();release=threading.Event();original=self.archive.writer.write
        def slow(*args,**kwargs):
            entered.set()
            if not release.wait(12):raise RuntimeError('慢写测试超时')
            return original(*args,**kwargs)
        out=Path(__file__).resolve().parents[1]/'build/background-row-write';out.mkdir(parents=True,exist_ok=True)
        with InputConfirmTests.http(self) as url,sync_playwright() as pw,patch.object(self.archive.writer,'write',side_effect=slow):
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            try:
                page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading');page.locator('#sku-open').click()
                page.locator('#sku-all-tab').click();expect(page.locator('#sku-rows [data-edit-sku]')).to_have_count(1)
                page.locator('[data-edit-sku]').first.dblclick();page.locator('.sku-code-input').fill('NEW-200');page.locator('.sku-code-input').press('Enter')
                expect(page.locator('[data-edit-sku]').first).to_have_text('NEW-200')
                expect(page.locator('#sku-row-write-dialog')).to_have_count(0);page.wait_for_function('()=>!FramekeepSku.isBusy()')
                expect(page.locator('#sku-rows .sku-metadata-status')).to_contain_text('等待后台写入')
                self.archive.thread.start();self.assertTrue(entered.wait(3));page.screenshot(path=str(out/'writing.png'))
                page.locator('#selection-tab').click();page.locator('#sku-open').click()
                expect(page.locator('#sku-rows .sku-metadata-status')).to_contain_text('正在写入中')
                page.reload(wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading');page.locator('#sku-open').click()
                expect(page.locator('[data-edit-sku]').first).to_have_text('NEW-200')
                release.set();expect(page.locator('#sku-rows .sku-metadata-status')).to_have_count(0,timeout=15000)
                self.assertEqual(self.archive.writer.read(self.photos/'0.JPG')['XMP-fk:SKU'],'NEW-200')
                page.screenshot(path=str(out/'complete.png'));self.assertFalse(errors,errors)
            finally:release.set();browser.close()


if __name__=='__main__':unittest.main()
