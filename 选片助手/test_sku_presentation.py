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

    def test_image_columns_expand_source_table_and_keep_formula_targets(self):
        import io
        from sku_workbook import REL
        source=self.sku.root/self.pid/'source.xlsx'
        with ZipFile(source) as archive:parts={name:archive.read(name) for name in archive.namelist()}
        parts['xl/worksheets/sheet1.xml']=parts['xl/worksheets/sheet1.xml'].replace(b'</row></sheetData>',b'<c r="B3"><f>LEN(B2)</f></c></row></sheetData>')
        parts['xl/worksheets/_rels/sheet1.xml.rels']=f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="table1" Type="{REL}/table" Target="../tables/table1.xml"/></Relationships>'.encode()
        parts['xl/tables/table1.xml']=f'<table xmlns="{N["m"]}" id="1" name="Plan" displayName="Plan" ref="A1:B3"><autoFilter ref="A1:B3"/><tableColumns count="2"><tableColumn id="1" name="款号"/><tableColumn id="2" name="图片"/></tableColumns></table>'.encode()
        parts['xl/worksheets/sheet1.xml']=parts['xl/worksheets/sheet1.xml'].replace(b'</worksheet>',f'<tableParts count="1"><tablePart xmlns:r="{REL}" r:id="table1"/></tableParts></worksheet>'.encode())
        data=io.BytesIO()
        with ZipFile(data,'w') as archive:
            for name,raw in parts.items():archive.writestr(name,raw)
        pid=self.sku.import_file('table.xlsx',data.getvalue())['project']['id']
        rename_header(self.sku,pid,'sku','商品款号')
        output=self.sku.export(pid);plan=inspect_workbook(output)[0]
        self.assertEqual((plan['sku_col'],plan['image_col'],plan['label_col']),(2,3,4))
        self.assertEqual(self.cells(output,'xl/worksheets/sheet1.xml')['E3'],'LEN(E2)')
        with ZipFile(output) as archive:
            table=E.fromstring(archive.read('xl/tables/table1.xml'));columns=table.find('m:tableColumns',N)
            self.assertEqual(table.get('ref'),'B1:E3');self.assertEqual(columns.get('count'),'4')
            self.assertEqual([c.get('name') for c in columns],['商品款号','款号图片','标签图片','图片'])
            self.assertEqual(table.find('m:autoFilter',N).get('ref'),'B1:E3')
        again=self.sku.import_file('table-again.xlsx',output.read_bytes())['project']['id']
        with ZipFile(self.sku.export(again)) as archive:self.assertEqual(E.fromstring(archive.read('xl/tables/table1.xml')).get('ref'),'B1:E3')

    def test_legacy_record_time_uses_ingestion_title_and_keeps_timestamp(self):
        import io
        from sku_workbook_auto import replace_sheet
        source=(self.sku.root/self.pid/'source.xlsx').read_bytes()
        stamp='2026-11-03T15:45:12+08:00'
        content,part=replace_sheet(source,'现场记录',[['TEST-123','a.JPG','拍摄目录',stamp,'未拍摄','']])
        data=io.BytesIO()
        with ZipFile(io.BytesIO(content)) as original,ZipFile(data,'w') as output:
            for name in original.namelist():output.writestr(name,original.read(name).replace('入库时间'.encode(),'记录时间'.encode()) if name==part else original.read(name))
        pid=self.sku.import_file('legacy-time.xlsx',data.getvalue())['project']['id']
        output=self.sku.export(pid);cells=self.cells(output,part)
        self.assertEqual([cells[c+'1'] for c in 'ABCDEFGHI'],['序号','款号','款号图片','标签图片','来源文件','现场文件夹','入库时间','拍摄完成时间','拍摄状态'])
        self.assertEqual(cells['G2'],'2026-11-03 15:45:12')
        again=self.sku.import_file('time-again.xlsx',output.read_bytes())['project']['id']
        cells=self.cells(self.sku.export(again),part);self.assertEqual(cells['G1'],'入库时间');self.assertEqual(cells['G2'],'2026-11-03 15:45:12')

    def test_export_beijing_times_sequence_and_repeated_import_preserve_evidence(self):
        from sku_workbook_auto import replace_sheet
        from sku_workbook_presentation import _beijing_time
        source=(self.sku.root/self.pid/'source.xlsx').read_bytes()
        content,part=replace_sheet(source,'现场记录',[
            ['TEST-101','a.JPG','2026-11-04T16:12:18Z','2026-11-04T16:12:18Z','已拍摄','2026-11-04T10:12:18.58+08:00'],
            ['TEST-102','b.JPG','目录','2026-11-05T01:20:14','未拍摄','待补录']])
        pid=self.sku.import_file('beijing.xlsx',content)['project']['id']
        original=(self.sku.root/pid/'source.xlsx').read_bytes()
        before={r['id']:(r['source_values'],r['baseline_completed_at']) for r in self.sku.rows(pid)}
        output=self.sku.export(pid);cells=self.cells(output,part)
        self.assertEqual([cells[c+'1'] for c in 'ABCDEFGHI'],['序号','款号','款号图片','标签图片','来源文件','现场文件夹','入库时间','拍摄完成时间','拍摄状态'])
        self.assertEqual([cells['A2'],cells['A3']],['1','2'])
        self.assertEqual(cells['G2'],'2026-11-05 00:12:18')
        self.assertEqual(cells['H2'],'2026-11-04 10:12:18.58')
        self.assertEqual(cells['G3'],'2026-11-05 01:20:14')
        self.assertEqual(cells['H3'],'待补录')
        self.assertEqual(cells['F2'],'2026-11-04T16:12:18Z')  # 普通文本列不改写。
        self.assertEqual((self.sku.root/pid/'source.xlsx').read_bytes(),original)
        self.assertEqual({r['id']:(r['source_values'],r['baseline_completed_at']) for r in self.sku.rows(pid)},before)
        for _ in range(2):
            pid=self.sku.import_file('again.xlsx',output.read_bytes())['project']['id'];output=self.sku.export(pid)
            plan=next(p for p in inspect_workbook(output) if p['name']=='现场记录')
            self.assertEqual((plan['seq_col'],plan['sku_col'],plan['time_col'],plan['status_col']),(1,2,8,9))
            cells=self.cells(output,part);self.assertEqual(cells['G2'],'2026-11-05 00:12:18');self.assertEqual(cells['H2'],'2026-11-04 10:12:18.58')
            self.assertEqual(sum(v=='序号' for k,v in cells.items() if k.endswith('1')),1)
        for value,expected in [('2026-11-04T16:00:00.000001-05:00','2026-11-05 05:00:00.000001'),('2026-02-30T01:02:03+08:00','2026-02-30T01:02:03+08:00'),('2026-11-04T10:12:18.123456789+08:00','2026-11-04 10:12:18.123456789'),('待补录','待补录')]:
            self.assertEqual(_beijing_time(value),expected)

    def test_headers_persist_export_escape_and_reimport_without_losing_schema(self):
        source=self.sku.root/self.pid/'source.xlsx';before=hashlib.sha256(source.read_bytes()).hexdigest()
        for key,title in [('sku','商品编码 & <款>'),('status','完成情况'),('photo','拍摄选图'),('time','完成时刻'),('column:模特图:C','拍摄说明'),('seq','编号'),('row','来源行'),('source','拍摄分组')]:
            rename_header(self.sku,self.pid,key,title)
        out=self.sku.export(self.pid);cells=self.cells(out,'xl/worksheets/sheet1.xml')
        self.assertEqual([cells.get(c+'1') for c in 'ABCDEFGH'],['编号','商品编码 & <款>','拍摄选图','标签图片','图片','完成时刻','完成情况','拍摄说明'])
        summary=self.cells(out,'xl/worksheets/framekeep-all-skus.xml')
        self.assertEqual([summary[c+'1'] for c in 'ABCDE'],['编号','来源行','商品编码 & <款>','拍摄选图','拍摄分组'])
        self.assertEqual(summary['B2'],'模特图 · 2；细节图 · 2')
        plans=inspect_workbook(out);self.assertEqual(len(plans),2);self.assertEqual(plans[0]['sku_col'],2);self.assertEqual(plans[0]['image_col'],3)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),before)
        self.sku.close();self.sku=SkuService(self.store,self.providers,archive=self.archive,autostart=False)
        self.assertEqual(self.sku.state()['project']['presentation']['headers']['sku'],'商品编码 & <款>')
        pid=self.sku.import_file('renamed.xlsx',out.read_bytes())['project']['id']
        self.assertEqual(len(self.sku.rows(pid)),4)
        self.assertEqual(preferences(self.sku,pid)['headers']['sku'],'商品编码 & <款>')
        self.assertEqual(preferences(self.sku,pid)['headers']['column:模特图:H'],'拍摄说明')
        again=self.sku.export(pid)
        with ZipFile(again) as z:
            book=E.fromstring(z.read('xl/workbook.xml'))
            self.assertEqual(len(book.find('m:sheets',N)),3)
        self.assertEqual(self.cells(again,'xl/worksheets/sheet1.xml')['H1'],'拍摄说明')

    def test_invalid_header_and_foreign_cover_do_not_mutate_preferences(self):
        before=preferences(self.sku,self.pid)
        for key,title in [('sku',' '),('sku','x'*41),('sku','bad\nname'),('unknown','文字')]:
            with self.assertRaises(ValueError):rename_header(self.sku,self.pid,key,title)
        with self.assertRaises(ValueError):select_images(self.sku,self.pid,{fixture.CODE:'capture:foreign:photo'})
        self.assertEqual(preferences(self.sku,self.pid),before)

    def test_preferences_persist_without_waiting_for_document_export(self):
        from sku_metadata_backup import export_metadata
        rename_header(self.sku,self.pid,'sku','商品款号')
        before=preferences(self.sku,self.pid)
        with patch.object(self.sku,'export',side_effect=ValueError('模拟 Excel 被占用')):
            rename_header(self.sku,self.pid,'sku','新保存名称')
            with self.assertRaisesRegex(ValueError,'被占用'):self.sku.export(self.pid)
        before['headers']['sku']='新保存名称'
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
        self.parse();self.choose();self.sku.parse_capture(self.sid,self.ids[3])
        # 本用例验证同组换封面；明确手填相同款号，避免自动分配独立颜色组。
        details=self.sku.ocr_details(self.sid,self.ids[3])
        self.sku.choose_ocr_sku(self.sid,self.ids[3],fixture.CODE,details['reading_key'],details['image_key'],literal=True);self.drain()
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
        out=self.sku.export(self.pid);plan=inspect_workbook(out)[0];image=next(im for im in plan['images'] if im['cell']=='C2')
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

    def test_browser_plain_header_sheet_gallery_save_cancel_and_reload(self):
        from contextlib import ExitStack
        from http.server import ThreadingHTTPServer
        import threading
        from playwright.sync_api import sync_playwright
        import server
        self.parse();self.choose();self.sku.parse_capture(self.sid,self.ids[3])
        details=self.sku.ocr_details(self.sid,self.ids[3])
        self.sku.choose_ocr_sku(self.sid,self.ids[3],fixture.CODE,details['reading_key'],details['image_key'],literal=True);self.drain()
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
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.locator('#sku-open').click();page.locator('[data-progress-sheet="模特图"]').click()
                        page.wait_for_selector('#sku-rows [data-header-key="sku"]')
                        self.assertEqual(page.locator('#sku-rows th').all_text_contents(),['序号','原表行','款号','款号图片','标签图片','图片','拍摄类型','拍摄状态','拍摄完成时间','备注'])
                        self.assertEqual(page.locator('#sku-rows tbody tr').count(),2)
                        self.assertIn('模特图',page.locator('#sku-rows tbody').inner_text());self.assertIn('细节图',page.locator('#sku-sheet-tabs').inner_text())
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
                        cover=page.locator('#sku-rows [data-sku-cover="'+normalize(fixture.CODE)+'"]').first
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
                        page.reload(wait_until='domcontentloaded');page.locator('#sku-open').click();page.locator('[data-progress-sheet="模特图"]').click()
                        page.wait_for_function('(cid)=>[...document.querySelectorAll("#sku-rows [data-sku-cover] img")].some(img=>(img.dataset.viewPreviewSource||img.src).includes(cid))',arg=self.ids[2])
                        self.assertEqual(page.locator('#sku-rows [data-header-key="sku"]').inner_text(),'商品编码')
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
