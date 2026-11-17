"""现场手动任意文本SKU：无需模型、整款写入、真实读回与浏览器确认。"""
import hashlib
import unittest
from unittest.mock import patch

from PIL import Image
import test_sku_input_confirm as fixtures
from asset_archive import AssetArchive
from sku_service import SkuService
from sku_library import labels
from sku_match import normalize,normalize_code,match,match_automatic,extract_ocr_codes


class LiveManualTextTests(unittest.TestCase):
    setUp=fixtures.InputConfirmTests.setUp
    tearDown=fixtures.InputConfirmTests.tearDown
    confirm=fixtures.InputConfirmTests.confirm
    drain=fixtures.InputConfirmTests.drain
    http=fixtures.InputConfirmTests.http

    def assert_written(self,code):
        for i in (0,1):
            path=self.photos/f'{i}.JPG';actual=self.archive.writer.read(path)
            self.assertEqual(actual['XMP-fk:SKU'],code)
            self.assertEqual(actual['IFD0:XPSubject'],code)
            self.assertIn('SKU:'+code,actual['IFD0:XPKeywords'])
            self.assertIn('SKU:'+code,actual['XMP-dc:Subject'])
            self.assertEqual(self.archive.writer.read(path.with_suffix('.xmp'))['XMP-fk:SKU'],code)
            with Image.open(path) as image:
                self.assertEqual(bytes(image.getexif()[40095]).decode('utf-16-le').rstrip('\0'),code)

    def test_literal_text_whole_interval_keeps_pixels_neighbors_and_clears_old_name(self):
        pixels={}
        for i in range(4):
            with Image.open(self.photos/f'{i}.JPG') as image:pixels[i]=hashlib.sha256(image.tobytes()).hexdigest()
        neighbors=[(self.photos/f'{i}.JPG').read_bytes() for i in (2,3)]
        codes=['纯中文款号','另一个中文款号','样品 红 / 特别款','///','★🙂','000123','0','123.00','1E3','TRUE','false','中文 Ab-001 / 样品']
        with patch.object(self.providers,'check_inference',side_effect=AssertionError('手动输入不得调用模型')):
            for code in codes:
                with self.subTest(code=code):
                    self.assertEqual(self.confirm(' '+code+' ')['sku'],code);self.drain();self.assert_written(code)
                    self.assertEqual(labels(self.sku,self.sid)['labels'],dict.fromkeys(self.ids[:2],code))
        self.assertEqual(neighbors,[(self.photos/f'{i}.JPG').read_bytes() for i in (2,3)])
        for i,digest in pixels.items():
            with Image.open(self.photos/f'{i}.JPG') as image:self.assertEqual(hashlib.sha256(image.tobytes()).hexdigest(),digest)

    def test_pending_text_rename_survives_restart(self):
        self.confirm('旧款 中文');self.confirm('最新款 🙂');self.sku.close();self.archive.close()
        self.archive=AssetArchive(self.store,autostart=False)
        self.sku=SkuService(self.store,self.providers,archive=self.archive,autostart=False)
        self.archive.sku=self.sku;self.drain();self.assert_written('最新款 🙂')
        self.assertEqual(labels(self.sku,self.sid)['labels'],dict.fromkeys(self.ids[:2],'最新款 🙂'))

    def test_unsealed_unrated_selection_accepts_text_without_ocr(self):
        self.archive.ratings.set([self.photos/f'{i}.JPG' for i in range(4)],0)
        code='未封口 手动款号'
        with patch.object(self.providers,'check_inference',side_effect=AssertionError('手动输入不得调用模型')):
            result=self.confirm(code,self.ids[0]);self.drain()
        self.assertEqual((result['group_kind'],result['capture_count']),('selection',1))
        self.assertEqual(self.archive.writer.read(self.photos/'0.JPG')['XMP-fk:SKU'],code)
        self.assertFalse(self.archive.writer.read(self.photos/'1.JPG').get('XMP-fk:SKU'))

    def test_archive_metadata_reader_shows_same_unicode_windows_fields(self):
        from asset_catalog import AssetCatalog
        code='★🙂';self.confirm(code);self.drain();catalog=AssetCatalog(self.store.data)
        try:
            catalog.add_root(str(self.photos))
            with catalog.connect() as c:row=c.execute('SELECT * FROM items WHERE path=?',(str(self.photos/'0.JPG'),)).fetchone()
            self.assertIsNotNone(row)
            payload,_=catalog.metadata._extract(dict(row))
            self.assertEqual(payload['tags']['IFD0:XPSubject'],code)
            self.assertIn('SKU:'+code,payload['tags']['IFD0:XPKeywords'])
        finally:catalog.close()

    def test_pure_text_records_are_distinct_and_automatic_matching_stays_strict(self):
        self.confirm('上衣');self.drain();self.confirm('裤子',self.ids[3]);self.drain()
        rows=self.sku.rows(self.pid)
        self.assertEqual({r['sku'] for r in rows if r['sku'] in ('上衣','裤子')},{'上衣','裤子'})
        self.assertNotEqual(normalize('上衣'),normalize('裤子'))
        self.assertEqual(normalize('ｌｃ００１－Ａ'),normalize_code('LC001-A'))
        reading=[{'codes':['纯中文款号'],'confidence':1,'is_label':True}]
        row=[{'id':'text','sku':'纯中文款号','normalized':normalize('纯中文款号'),'sheet':'现场记录','excel_row':2}]
        self.assertEqual(match(reading,row)['status'],'review')
        self.assertEqual(match_automatic(reading,row)['status'],'review')
        self.assertEqual(extract_ocr_codes('纯中文款号 ★🙂'),[])
        original={'status':'review','row_ids':[],'candidates':[]}
        self.assertEqual(self.sku._record_unplanned({'project':self.pid,'session_id':self.sid,'capture_id':self.ids[1],'scope':'模特图'},
            [{'codes':['未确认的中文原文'],'unscored':True}],original,accept_observed=True),original)

    def test_invalid_text_leaves_no_assignment_and_maximum_length_is_accepted(self):
        with self.archive.connect() as c:before=c.execute('SELECT count(*) FROM assignments').fetchone()[0]
        for code in ('','   ','中文\n款号','中文\x00款号','文'*81):
            with self.subTest(code=code),self.assertRaises(ValueError):self.confirm(code)
        with self.archive.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM assignments').fetchone()[0],before)
        code='文'*80;self.confirm(code);self.drain();self.assert_written(code)

    def test_browser_button_and_enter_write_arbitrary_text_without_ocr(self):
        from playwright.sync_api import sync_playwright,expect
        from pathlib import Path
        calls=[];self.sku.recognizer=lambda *_:calls.append(True) or {}
        out=Path(__file__).resolve().parents[1]/'build/task229-live-text-sku';out.mkdir(parents=True,exist_ok=True)
        with self.http() as url,sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1580,'height':1000});errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                page.evaluate('(cid)=>Framekeep.browsePhoto(cid)',self.ids[1]);page.locator('#sku-asset-panel').evaluate('el=>el.open=true')
                field=page.locator('#asset-sku');button=page.locator('#asset-parse-sku')
                expect(page.locator('#asset-write-help')).to_contain_text('中文、数字、字母、符号')
                for code,enter in [('纯中文手填款号',False),('/// ★🙂',True)]:
                    field.fill(code);expect(button).to_have_text('确定')
                    with page.expect_response('**/api/metadata/group/confirm') as response:
                        if enter:field.press('Enter')
                        else:button.click()
                    self.assertEqual(response.value.status,200)
                    page.wait_for_function('(code)=>document.getElementById("asset-message").textContent.includes("已确认 SKU "+code)',arg=code)
                    self.drain();self.assert_written(code);expect(field).to_have_value(code)
                page.locator('#sku-asset-panel').screenshot(path=str(out/'manual-text-written.png'))
                field.fill('');expect(button).to_have_text('OCR 解析当前图')
                self.assertEqual(calls,[]);self.assertEqual(errors,[])
            finally:browser.close()


if __name__=='__main__':unittest.main()
