"""二星入库到真实 JPG 属性、改号、恢复和失败边界的集成验证。"""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image
from capture_parsing import capture_status
from test_ocr_metadata import OcrMetadataTests


class LiveSkuMetadataTests(unittest.TestCase):
    setUp=OcrMetadataTests.setUp
    tearDown=OcrMetadataTests.tearDown
    parse=OcrMetadataTests.parse
    choose=OcrMetadataTests.choose

    def seed_photo_metadata(self,*args):
        # 基础fixture已登记图片；测试原始属性须同步种子哈希，不能伪装为登记后的外部改图。
        from metadata_writer import sha
        self.archive.writer.run(*args);path=str(args[-1]);digest=sha(path)
        with self.archive.connect() as c:
            c.execute('UPDATE files SET sha=? WHERE id IN (SELECT file FROM locations WHERE path=?)',(digest,path))
            c.execute('UPDATE file_fingerprints SET sha=? WHERE path=?',(digest,path))

    def drain(self):
        for _ in range(100):
            with self.archive.connect() as c:
                if not c.execute("SELECT 1 FROM metadata_jobs WHERE status='queued'").fetchone():break
            self.archive.process_one()
        else:self.fail('文件写入队列未排完')

    def status(self):return capture_status(self.store,self.sku,self.archive,None,self.sid,self.ids[1])['metadata']

    def check_two_star_sidecar_sku(self,*,rating_first):
        from rating_xmp import sidecar_metadata
        path=self.photos/'1.JPG'
        def rate():
            self.archive.ratings.set([path],2)
            for _ in range(20):
                if not self.archive.ratings.sync.process_one():break
            self.assertEqual(self.archive.ratings.sync.state()['failed'],0)
        if rating_first:rate()
        self.parse();self.choose('款号-100');self.drain()
        if not rating_first:rate()
        for code in ('款号-100','款号-200'):
            if code=='款号-200':self.choose(code);self.drain()
            actual=sidecar_metadata(path,grouped=True)
            self.assertEqual(actual['XMP-xmp:Rating'],2)
            self.assertEqual(actual['XMP-fk:SKU'],code)
            self.assertIn('SKU:'+code,actual['XMP-dc:Subject'])
            if code=='款号-200':self.assertNotIn('SKU:款号-100',actual['XMP-dc:Subject'])

    def test_two_star_sidecar_after_sku_contains_rating_and_sku(self):
        self.check_two_star_sidecar_sku(rating_first=False)

    def test_two_star_sidecar_before_sku_contains_rating_and_sku(self):
        self.check_two_star_sidecar_sku(rating_first=True)

    def test_old_applied_keyword_residue_is_repaired_once_without_new_assignment(self):
        self.parse();self.choose('KEYWORD-200');self.drain();path=self.photos/'0.JPG'
        self.seed_photo_metadata('-overwrite_original','-XMP-dc:Subject+=SKU:STALE-100',path)
        with self.archive.connect() as c:
            job=dict(c.execute("SELECT m.* FROM metadata_jobs m WHERE path=? AND status='applied' ORDER BY created DESC LIMIT 1",(str(path),)).fetchone())
            journal=json.loads(job['journal']);journal.pop('keyword_policy',None);journal['subjects_before']=['SKU:STALE-100'];journal['before']['SKU']=None
            c.execute('UPDATE metadata_jobs SET journal=? WHERE id=?',(json.dumps(journal),job['id']))
            segment=c.execute('SELECT segment FROM assets WHERE id=?',(job['asset'],)).fetchone()[0]
            revision=c.execute('SELECT revision FROM assets WHERE id=?',(job['asset'],)).fetchone()[0]
        self.archive.repair_segment_jobs(segment);self.drain()
        self.assertNotIn('SKU:STALE-100',self.archive.writer.read(path)['XMP-dc:Subject'])
        self.archive.repair_segment_jobs(segment)
        with self.archive.connect() as c:
            self.assertEqual(c.execute('SELECT revision FROM assets WHERE id=?',(job['asset'],)).fetchone()[0],revision)
            self.assertFalse(c.execute("SELECT 1 FROM metadata_jobs WHERE status='queued'").fetchone())

    def test_two_star_auto_confirm_writes_windows_properties_then_progress_rename_updates_them(self):
        self.reading.update(text='LC7877836-P1',codes=['LC7877836-P1'],ocr_candidates=['LC7877836-P1'])
        self.sku.set_sku_rules([{'kind':'contains','value':'LC'}])
        pixels=[]
        for i in range(4):
            with Image.open(self.photos/f'{i}.JPG') as im:pixels.append(im.tobytes())
        self.seed_photo_metadata('-overwrite_original','-XPKeywords=已有标记;棉质','-XPSubject=原有主题',self.photos/'0.JPG')
        result=self.parse();self.assertEqual(result['auto_applied']['sku'],'LC7877836-P1')
        self.assertEqual(self.status()['status'],'queued')
        self.assertEqual(next(r for r in self.sku.state()['rows'] if r['sku']=='LC7877836-P1')['status'],'已拍摄')
        self.drain();self.assertEqual(self.status()['status'],'applied')
        self.assertEqual(self.status()['applied'],2)
        for i in range(2):
            value=self.archive.writer.read(self.photos/f'{i}.JPG')
            self.assertEqual(value['IFD0:XPSubject'],'LC7877836-P1')
            self.assertIn('SKU:LC7877836-P1',value['IFD0:XPKeywords'])
        row=next(r for r in self.sku.state()['rows'] if r['sku']=='LC7877836-P1')
        renamed=self.sku.rename_shooting_row(self.pid,row['id'],row['sku'],'LC7877836-P2')
        self.assertTrue(renamed['operation_ids']);self.assertEqual(self.status()['status'],'queued')
        self.drain();self.assertEqual(self.status()['status'],'applied')
        for i in range(2):
            path=self.photos/f'{i}.JPG';value=self.archive.writer.read(path)
            self.assertEqual(value['XMP-fk:SKU'],'LC7877836-P2')
            self.assertEqual(value['IFD0:XPSubject'],'LC7877836-P2')
            self.assertIn('SKU:LC7877836-P2',value['IFD0:XPKeywords'])
            self.assertNotIn('SKU:LC7877836-P1',value['IFD0:XPKeywords'])
            if i==0:self.assertTrue({'已有标记','棉质'}<=set(value['IFD0:XPKeywords'].split(';')))
            if os.name=='nt':
                import win32com.client
                shell=win32com.client.Dispatch('Shell.Application');item=shell.NameSpace(str(path.parent)).ParseName(path.name)
                self.assertEqual(item.ExtendedProperty('System.Subject'),'LC7877836-P2')
                self.assertIn('SKU:LC7877836-P2',item.ExtendedProperty('System.Keywords'))
        for i in range(4):
            with Image.open(self.photos/f'{i}.JPG') as im:self.assertEqual(im.tobytes(),pixels[i])
        self.assertNotIn('XMP-fk:SKU',self.archive.writer.read(self.photos/'2.JPG'))

    def test_selection_correction_replaces_old_property_tags_and_revert_restores_original(self):
        self.seed_photo_metadata('-overwrite_original','-XPKeywords=保留标签','-XPSubject=原主题',self.photos/'0.JPG')
        self.parse();self.choose('FIRST-100');self.drain();self.choose('SECOND-200');self.drain()
        for i in range(2):self.assertEqual(self.archive.writer.read(self.photos/f'{i}.JPG')['IFD0:XPSubject'],'SECOND-200')
        # 单次独立备案撤销必须恢复 Windows 字段，而非仅恢复私有 XMP。
        preview=self.archive.preview(self.sid,[self.ids[2]],'REVERT-100','测试')
        op=self.archive.apply(preview['preview_id'])['operation']['id'];self.drain()
        self.archive.revert(op);self.drain()
        value=self.archive.writer.read(self.photos/'2.JPG')
        self.assertFalse(value.get('IFD0:XPSubject'));self.assertFalse(value.get('IFD0:XPKeywords'))

    def test_confirmed_segment_without_operation_or_component_job_is_repaired(self):
        self.parse();self.choose('RECOVER-100')
        with self.archive.connect() as c:
            c.execute('DELETE FROM metadata_jobs');c.execute("UPDATE segments SET operation='' WHERE sku='RECOVER-100'")
        self.assertEqual(self.status()['status'],'missing')
        self.sku._recover_confirmed_intervals();self.sku.deliver_events(self.archive)
        self.assertEqual(self.status()['status'],'queued')
        with self.archive.connect() as c:
            previous=dict(c.execute('SELECT * FROM metadata_jobs WHERE path=? ORDER BY created DESC LIMIT 1',(str(self.photos/'0.JPG'),)).fetchone())
            c.execute('DELETE FROM metadata_jobs WHERE path=?',(str(self.photos/'0.JPG'),))
            c.execute("INSERT INTO operations(id,created,kind,source) VALUES ('description-only',0,'description','test')")
            c.execute('INSERT INTO metadata_jobs(id,operation,file,asset,revision,path,expected_sha,mode,fields,status,created) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                      ('description-only','description-only',previous['file'],previous['asset'],previous['revision'],previous['path'],previous['expected_sha'],'sidecar',json.dumps({'_descriptive':True}),'applied',0))
        self.sku._recover_confirmed_intervals();self.assertEqual(self.status()['total'],2)
        self.assertTrue(all(j['status']=='queued' for j in self.status()['jobs']))
        self.drain();self.assertEqual(self.status()['status'],'applied')

    def test_disk_verification_failure_is_not_success_and_retry_reuses_committed_file(self):
        self.parse();chosen=self.choose('VERIFY-100')
        with patch.object(self.archive.writer,'verify_committed',side_effect=ValueError('隔离：文件回读失败')):self.archive.process_one()
        self.assertEqual(self.status()['status'],'failed')
        self.assertIn('文件回读失败',' '.join(j['reason'] for j in self.status()['jobs']))
        self.sku._recover_confirmed_intervals();self.assertEqual(self.status()['status'],'failed')
        self.archive.retry(chosen['operation_id']);self.drain();self.assertEqual(self.status()['status'],'applied')

    def test_camera_jpg_thumbnail_offset_may_move_only_when_thumbnail_bytes_are_identical(self):
        thumbnail=self.base/'thumbnail.jpg';Image.new('RGB',(80,60),'orange').save(thumbnail)
        self.seed_photo_metadata('-overwrite_original','-ThumbnailImage<='+str(thumbnail),self.photos/'0.JPG')
        original=self.archive.writer.binary('-b','-ThumbnailImage',str(self.photos/'0.JPG'),input_data=None)
        self.parse();self.choose('THUMB-100');self.drain();self.assertEqual(self.status()['status'],'applied')
        actual=self.archive.writer.binary('-b','-ThumbnailImage',str(self.photos/'0.JPG'),input_data=None)
        self.assertTrue(original);self.assertEqual(original,actual)
        with self.archive.connect() as c:
            journal=json.loads(c.execute('SELECT journal FROM metadata_jobs WHERE path=?',(str(self.photos/'0.JPG'),)).fetchone()[0])
        self.assertIn('thumbnail_integrity',journal)

    def test_changed_thumbnail_is_rejected_before_touching_jpg(self):
        from metadata_writer import sha
        path=self.photos/'0.JPG';thumbnail=self.base/'thumbnail.jpg';Image.new('RGB',(80,60),'orange').save(thumbnail)
        self.archive.writer.run('-overwrite_original','-ThumbnailImage<='+str(thumbnail),path)
        before=sha(path);binary=self.archive.writer.binary
        def changed(*args,input_data):
            return b'changed thumbnail' if '-ThumbnailImage' in args and input_data is not None else binary(*args,input_data=input_data)
        # 此用例验证无可用范围时的 ExifTool 回退提取；直接范围篡改另有专项覆盖。
        with patch('metadata_writer.jpeg_thumbnail',return_value=None),patch.object(self.archive.writer,'binary',side_effect=changed),self.assertRaisesRegex(ValueError,'缩略图内容校验失败'):
            self.archive.writer.write(path,{'SKU':'REJECT-100'},before)
        self.assertEqual(sha(path),before)

    def test_legacy_applied_private_xmp_is_upgraded_once_and_keeps_undo_baseline(self):
        from metadata_writer import sha
        self.parse();choice=self.choose('LEGACY-100');self.drain()
        with self.archive.connect() as c:
            jobs=c.execute('SELECT * FROM metadata_jobs WHERE operation=?',(choice['operation_id'],)).fetchall()
            for job in jobs:
                self.archive.writer.run('-overwrite_original','-XPSubject=','-XPKeywords=',job['path'])
                journal=json.loads(job['journal'])
                for part in ('before','after'):
                    for key in ('WindowsSubject','WindowsKeywords'):journal[part].pop(key,None)
                c.execute('UPDATE metadata_jobs SET journal=? WHERE id=?',(json.dumps(journal),job['id']))
                c.execute('UPDATE files SET sha=? WHERE id=?',(sha(job['path']),job['file']))
        self.sku._recover_confirmed_intervals();self.assertEqual(self.status()['status'],'queued')
        self.drain();self.assertEqual(self.status()['status'],'applied')
        for i in range(2):self.assertEqual(self.archive.writer.read(self.photos/f'{i}.JPG')['IFD0:XPSubject'],'LEGACY-100')
        self.sku._recover_confirmed_intervals();self.assertEqual(self.status()['status'],'applied')
        self.archive.revert(choice['operation_id']);self.drain()
        for i in range(2):
            value=self.archive.writer.read(self.photos/f'{i}.JPG');self.assertFalse(value.get('XMP-fk:SKU'));self.assertFalse(value.get('IFD0:XPSubject'))

    @unittest.skipUnless(os.environ.get('FRAMEKEEP_TEST_RAW'),'通过 FRAMEKEEP_TEST_RAW 指定只读真实 RAW 样本')
    def test_jpg_sku_change_updates_matched_raw_xmp_without_changing_raw_bytes(self):
        import test_raw_reconcile as raw_tests
        from metadata_writer import sha
        sample=Path(os.environ['FRAMEKEEP_TEST_RAW']);original=sha(sample)
        with patch.object(raw_tests,'SAMPLE',sample):
            f=raw_tests.RawWorkflowTests();f.setUp()
            try:
                sid=f.scan(f.raw);self.assertEqual(f.finish(sid)['status'],'applied')
                before=sha(f.target)
                f.archive.apply(f.archive.preview(f.sid,[f.cid],'RAW-NEW-200','细节图',overwrite=True)['preview_id'])
                for _ in range(12):f.archive.process_one()
                pair=f.finish(sid);self.assertEqual(pair['status'],'applied',pair)
                value=f.archive.writer.read_asset(f.target)
                self.assertEqual(value['XMP-fk:SKU'],'RAW-NEW-200');self.assertEqual(value['XMP-xmp:Rating'],4)
                self.assertEqual(sha(f.target),before);self.assertEqual(sha(sample),original)
            finally:f.tearDown()

    def test_browser_reports_actual_file_state_in_selection_and_progress_and_retries(self):
        from test_sku_input_confirm import InputConfirmTests
        from playwright.sync_api import sync_playwright,expect
        self.parse();self.choose('UI-100')
        with InputConfirmTests.http(self) as url,sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            try:
                page=browser.new_page(viewport={'width':1600,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading')
                page.evaluate('(id)=>Framekeep.browsePhoto(id)',self.ids[1])
                expect(page.locator('#capture-metadata-status')).to_contain_text('款号待写入')
                with patch.object(self.archive.writer,'verify_committed',side_effect=ValueError('隔离：文件读回失败')):self.archive.process_one()
                expect(page.locator('#capture-metadata-retry')).to_be_visible(timeout=10000)
                page.locator('#sku-open').click();page.locator('#sku-search').fill('UI-100');page.locator('[data-progress-sheet="临时插拍"]').click()
                expect(page.locator('#sku-rows .sku-metadata-status')).to_contain_text('款号写入未完成')
                page.locator('#sku-rows [data-sku-write-retry]').click();self.drain()
                expect(page.locator('#sku-rows .sku-metadata-status')).to_have_count(0,timeout=15000)
                out=Path(__file__).resolve().parents[1]/'build/live-sku-metadata';out.mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(out/'progress-written.png'))
                page.locator('#selection-tab').click();expect(page.locator('#capture-metadata-status')).to_contain_text('款号元数据已写入',timeout=10000)
                page.screenshot(path=str(out/'selection-written.png'));self.assertFalse(errors,errors)
                (out/'browser.json').write_text(json.dumps({'passed':True,'errors':errors,'file_status':self.status()},ensure_ascii=False,indent=2),encoding='utf8')
            finally:browser.close()


if __name__=='__main__':unittest.main()
