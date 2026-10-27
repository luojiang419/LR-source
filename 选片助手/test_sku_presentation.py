"""表头改名、封面持久化、全部汇总与真实工作簿重导入。"""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
import unittest
from xml.etree import ElementTree as E
from zipfile import ZipFile

import test_ocr_metadata as fixture
import test_document_reconciliation as document_fixture
from sku_presentation import rename_header,gallery,select_images,preferences
from sku_service import SkuService
from sku_workbook import inspect_workbook,N,read_image
from sku_match import normalize


class PresentationTests(unittest.TestCase):
    setUp=fixture.OcrMetadataTests.setUp
    tearDown=fixture.OcrMetadataTests.tearDown
    parse=fixture.OcrMetadataTests.parse
    choose=fixture.OcrMetadataTests.choose
    drain=document_fixture.DocumentTests.drain

    def cells(self,path,part):
        with ZipFile(path) as z:return {c.get('r'):''.join(c.itertext()) for c in E.fromstring(z.read(part)).findall('m:sheetData/m:row/m:c',N)}

    def test_headers_persist_export_escape_and_reimport_without_losing_schema(self):
        source=self.sku.root/self.pid/'source.xlsx';before=hashlib.sha256(source.read_bytes()).hexdigest()
        for key,title in [('sku','商品编码 & <款>'),('status','完成情况'),('photo','拍摄选图'),('time','完成时刻'),('column:模特图:C','拍摄说明'),('seq','编号'),('row','来源行'),('source','拍摄分组')]:
            rename_header(self.sku,self.pid,key,title)
        out=self.sku.export(self.pid);cells=self.cells(out,'xl/worksheets/sheet1.xml')
        self.assertEqual([cells.get(c+'1') for c in ['A','C','D','E','F']],['商品编码 & <款>','完成情况','拍摄选图','完成时刻','拍摄说明'])
        summary=self.cells(out,'xl/worksheets/framekeep-all-skus.xml')
        self.assertEqual([summary[c+'1'] for c in 'ABCDE'],['编号','来源行','商品编码 & <款>','拍摄选图','拍摄分组'])
        self.assertEqual(summary['B2'],'模特图 · 2；细节图 · 2')
        plans=inspect_workbook(out);self.assertEqual(len(plans),2);self.assertEqual(plans[0]['sku_col'],1);self.assertEqual(plans[0]['image_col'],4)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),before)
        self.sku.close();self.sku=SkuService(self.store,self.providers,archive=self.archive,autostart=False)
        self.assertEqual(self.sku.state()['project']['presentation']['headers']['sku'],'商品编码 & <款>')
        pid=self.sku.import_file('renamed.xlsx',out.read_bytes())['project']['id']
        self.assertEqual(len(self.sku.rows(pid)),4)
        self.assertEqual(preferences(self.sku,pid)['headers']['sku'],'商品编码 & <款>')
        self.assertEqual(preferences(self.sku,pid)['headers']['column:模特图:F'],'拍摄说明')
        again=self.sku.export(pid)
        with ZipFile(again) as z:
            book=E.fromstring(z.read('xl/workbook.xml'))
            self.assertEqual(len(book.find('m:sheets',N)),3)
        self.assertEqual(self.cells(again,'xl/worksheets/sheet1.xml')['F1'],'拍摄说明')

    def test_invalid_header_and_foreign_cover_do_not_mutate_preferences(self):
        before=preferences(self.sku,self.pid)
        for key,title in [('sku',' '),('sku','x'*41),('sku','bad\nname'),('unknown','文字')]:
            with self.assertRaises(ValueError):rename_header(self.sku,self.pid,key,title)
        with self.assertRaises(ValueError):select_images(self.sku,self.pid,{fixture.CODE:'capture:foreign:photo'})
        self.assertEqual(preferences(self.sku,self.pid),before)

    def test_preferences_backup_and_failed_export_rollback(self):
        from sku_metadata_backup import export_metadata
        rename_header(self.sku,self.pid,'sku','商品款号')
        before=preferences(self.sku,self.pid)
        with patch.object(self.sku,'export',side_effect=ValueError('模拟 Excel 被占用')):
            with self.assertRaisesRegex(ValueError,'被占用'):rename_header(self.sku,self.pid,'sku','未保存名称')
        self.assertEqual(preferences(self.sku,self.pid),before)
        output=export_metadata(self.sku)
        project=next(p for p in output['backup']['payload']['databases']['sku']['projects'] if p['id']==self.pid)
        self.assertEqual(json.loads(project['metadata'])['presentation'],before)

    def test_structured_table_references_follow_names_without_changing_strings(self):
        from sku_workbook_presentation import _structured_references
        from sku_workbook import NS
        raw=f'<worksheet xmlns="{NS}"><sheetData><row r="2"><c r="B2"><f>[@款号]&amp;Plan[[#Headers],[款号]]&amp;IF(A2="Plan[款号]",1,0)</f></c></row></sheetData></worksheet>'.encode()
        parts={'xl/worksheets/sheet1.xml':raw}
        _structured_references(parts,[{'names':{'Plan'},'part':'xl/tables/table1.xml','sheet':'xl/worksheets/sheet1.xml','bounds':(1,1,3,5),'renames':{'款号':'商品]编码'}}])
        formula=E.fromstring(parts['xl/worksheets/sheet1.xml']).find('.//m:f',N).text
        self.assertEqual(formula,'[@商品\']编码]&Plan[[#Headers],[商品\']编码]]&IF(A2="Plan[款号]",1,0)')

    def test_cover_selection_syncs_all_sheets_and_embeds_selected_pixels(self):
        self.parse();self.choose();self.sku.parse_capture(self.sid,self.ids[3]);self.choose(cid=self.ids[3]);self.drain()
        before={r['id']:r['status'] for r in self.sku.state()['rows']}
        data=gallery(self.sku,self.pid,fixture.CODE)
        choices=[im for im in data['images'] if im.get('capture_id')==self.ids[2]]
        self.assertEqual(len(choices),1)
        key=choices[0]['key'];state=select_images(self.sku,self.pid,{normalize(fixture.CODE):key})
        own=[r for r in state['rows'] if r['sku']==fixture.CODE]
        self.assertTrue(all(r['sku_image']['capture_id']==self.ids[2] for r in own if r['status']=='已拍摄'))
        self.assertTrue(all(r['sku_image'] is None for r in own if r['status']!='已拍摄'))
        self.assertEqual({r['id']:r['status'] for r in state['rows']},before)
        self.assertEqual(next(g for g in gallery(self.sku,self.pid)['groups'] if g['sku']==fixture.CODE)['selected_key'],key)
        out=self.sku.export(self.pid);plan=inspect_workbook(out)[0];image=next(im for im in plan['images'] if im['cell']=='D2')
        from PIL import Image
        import io
        with Image.open(io.BytesIO(read_image(out,image['media'])[0])) as embedded:pixel=embedded.convert('RGB').getpixel((5,5))
        with Image.open(self.photos/'2.JPG') as actual:expected=actual.getpixel((5,5))
        self.assertTrue(all(abs(a-b)<5 for a,b in zip(pixel,expected)),(pixel,expected))
        self.sku.close();self.sku=SkuService(self.store,self.providers,archive=self.archive,autostart=False)
        self.assertEqual(next(r for r in self.sku.state()['rows'] if r['sku']==fixture.CODE)['sku_image']['capture_id'],self.ids[2])

    def test_aggregate_sources_deduplicate_and_do_not_change_ocr_scope(self):
        state=self.sku.state();self.assertEqual(len(state['all_rows']),2)
        first=next(r for r in state['all_rows'] if r['sku']==fixture.CODE)
        self.assertEqual([(r['sheet'],r['excel_row']) for r in first['rows']],[('模特图',2),('细节图',2)])
        self.assertEqual(list(first['memberships']),['模特图','细节图'])
        self.parse();self.choose()
        group=next(r for r in self.sku.state()['all_rows'] if r['sku']==fixture.CODE)
        self.assertFalse(group['shot']);self.assertEqual(self.sku.project()['scope'],'模特图')

    def test_gallery_pages_include_entire_group_and_omit_label_if_products_exist(self):
        self.parse();self.choose()
        rows=[{'session_id':self.sid,'capture_id':str(i),'normalized':normalize(fixture.CODE),'sku':fixture.CODE,'name':f'{i}.JPG','preview':'/api/preview/test/'+str(i),'source':'interval'} for i in range(135)]
        rows.append(dict(rows[0],capture_id='label',source='ocr'))
        with patch('sku_library.browse',return_value={'rows':rows}):
            first=gallery(self.sku,self.pid,fixture.CODE,0,60);second=gallery(self.sku,self.pid,fixture.CODE,60,60);third=gallery(self.sku,self.pid,fixture.CODE,120,60)
        self.assertEqual(first['total'],135);self.assertEqual([len(x['images']) for x in [first,second,third]],[60,60,15])
        self.assertFalse(third['has_more']);self.assertEqual(len({im['key'] for page in [first,second,third] for im in page['images']}),135)

    def test_browser_plain_header_all_view_gallery_save_cancel_and_reload(self):
        from contextlib import ExitStack
        from http.server import ThreadingHTTPServer
        import threading
        from playwright.sync_api import sync_playwright
        import server
        self.parse();self.choose();self.sku.parse_capture(self.sid,self.ids[3]);self.choose(cid=self.ids[3]);self.drain()
        values=dict(STORE=self.store,DATA=self.store.data,MODELS=self.providers,SKU=self.sku,ARCHIVE=self.archive,CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,CANON=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None)
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        with ExitStack() as stack:
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1500,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.locator('#sku-open').click();page.locator('#sku-all-tab').click()
                        self.assertEqual(page.locator('#sku-rows th').all_text_contents(),['序号','原表行','款号','图片','所属工作表'])
                        self.assertEqual(page.locator('#sku-rows tbody tr').count(),2)
                        self.assertIn('模特图 · 2',page.locator('#sku-rows tbody').inner_text());self.assertIn('细节图 · 2',page.locator('#sku-rows tbody').inner_text())
                        self.assertEqual(self.sku.project()['scope'],'模特图')
                        header=page.locator('#sku-rows [data-header-key="sku"]');self.assertEqual(header.locator('svg,i').count(),0)
                        cell=header.locator('..');bounds=cell.bounding_box()
                        # 真实坐标覆盖表头四周留白，单击不编辑；输入框内双击不会重建编辑器。
                        cell.click(position={'x':bounds['width']-3,'y':3});self.assertEqual(page.locator('.sku-header-input').count(),0)
                        for position in ({'x':3,'y':3},{'x':bounds['width']-3,'y':3},{'x':3,'y':bounds['height']-3},{'x':bounds['width']-3,'y':bounds['height']-3}):
                            cell.dblclick(position=position);blank_editor=page.locator('.sku-header-input');self.assertTrue(blank_editor.is_visible())
                            blank_editor.fill('取消改名');blank_editor.dblclick();self.assertEqual(blank_editor.input_value(),'取消改名');blank_editor.press('Escape')
                            self.assertEqual(header.inner_text(),'款号')
                        header.dblclick();editor=page.locator('.sku-header-input');editor.fill('商品编码');page.wait_for_timeout(1350)
                        self.assertEqual(editor.input_value(),'商品编码')
                        with page.expect_response('**/api/sku/header') as response:editor.press('Enter')
                        self.assertEqual(response.value.status,200);page.wait_for_function('()=>document.querySelector("#sku-rows [data-header-key=sku]")?.textContent==="商品编码"')
                        header.dblclick();editor.fill('不保存');editor.press('Escape');self.assertEqual(header.inner_text(),'商品编码')
                        header.dblclick();editor.fill(' ');editor.press('Enter');self.assertEqual(header.inner_text(),'商品编码')
                        cover=page.locator('[data-all-sku="'+normalize(fixture.CODE)+'"] [data-sku-cover]')
                        original=cover.locator('img').get_attribute('src');cover.click();page.wait_for_selector('#sku-cover-groups [data-cover-key]')
                        candidate=page.locator('#sku-cover-groups [data-cover-key="capture:'+self.sid+':'+self.ids[2]+'"]')
                        candidate.click();page.locator('#sku-cover-cancel').click();self.assertEqual(cover.locator('img').get_attribute('src'),original)
                        cover.click();page.wait_for_selector('#sku-cover-groups [data-cover-key]');candidate.click()
                        with page.expect_response('**/api/sku/cover') as response:page.locator('#sku-cover-save').click()
                        self.assertEqual(response.value.status,200);page.wait_for_selector('#sku-cover-dialog',state='hidden')
                        self.assertIn(self.ids[2],cover.locator('img').get_attribute('src'))
                        out=fixture.ROOT/'build/shooting-presentation';out.mkdir(parents=True,exist_ok=True);page.screenshot(path=str(out/'all-dark.png'))
                        for theme in ('dark','light','pure-white','hardcore'):
                            page.evaluate('(t)=>FramekeepTheme.set(t)',theme)
                            for width in (1500,375,320):
                                page.set_viewport_size({'width':width,'height':1050});self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                                cover.click();page.wait_for_selector('#sku-cover-groups img')
                                page.locator('#sku-cover-dialog').evaluate('e=>Promise.all(e.getAnimations().map(a=>a.finished.catch(()=>{})))')
                                bounds=page.locator('#sku-cover-dialog').bounding_box();self.assertGreaterEqual(bounds['x'],-1);self.assertLessEqual(bounds['x']+bounds['width'],width+1)
                                self.assertFalse(page.locator('#sku-cover-dialog').evaluate('e=>e.scrollWidth>e.clientWidth+1'))
                                self.assertTrue(page.locator('#sku-cover-groups').evaluate('e=>e.firstElementChild.getBoundingClientRect().width>=e.clientWidth-8'))
                                if width in (1500,320):page.screenshot(path=str(out/f'gallery-{theme}-{width}.png'))
                                page.keyboard.press('Escape')
                        page.set_viewport_size({'width':1500,'height':1050});page.locator('[data-progress-sheet="模特图"]').click()
                        page.wait_for_selector('#sku-rows [data-sku-row]');self.assertEqual(page.locator('#sku-rows [data-header-key="sku"]').inner_text(),'商品编码')
                        page.reload(wait_until='domcontentloaded');page.locator('#sku-open').click();page.locator('#sku-all-tab').click()
                        page.wait_for_function('(cid)=>document.querySelector("#sku-rows [data-all-sku] img")?.src.includes(cid)',arg=self.ids[2])
                        self.assertEqual(page.locator('#sku-rows [data-header-key="sku"]').inner_text(),'商品编码')
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
