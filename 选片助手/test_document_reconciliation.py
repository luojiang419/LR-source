"""导入文档的人工确认、商品首图和内嵌图片工作副本，全部为生成夹具。"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as E
from zipfile import ZipFile

from PIL import Image
from playwright.sync_api import sync_playwright
import test_ocr_metadata as fixture
from sku_service import SkuService
from sku_library import browse
from sku_workbook import NS,N,DRAW,ART,REL,inspect_workbook,export_workbook,read_image
from sku_workbook_auto import PKG


class DocumentTests(unittest.TestCase):
    setUp=fixture.OcrMetadataTests.setUp
    tearDown=fixture.OcrMetadataTests.tearDown
    parse=fixture.OcrMetadataTests.parse
    choose=fixture.OcrMetadataTests.choose
    def own(self,sheet='模特图',code=fixture.CODE):return next(r for r in self.sku.state()['rows'] if r['sheet']==sheet and r['sku']==code)
    def drain(self):
        for _ in range(100):
            with self.archive.connect() as c:
                if not c.execute("SELECT 1 FROM metadata_jobs WHERE status='queued'").fetchone():return
            self.archive.process_one()
        self.fail('隔离写入队列未结束')
    def cells(self,path,part='xl/worksheets/sheet1.xml'):
        with ZipFile(path) as z:return {c.get('r'):''.join(c.itertext()) for c in E.fromstring(z.read(part)).findall('m:sheetData/m:row/m:c',N)}
    def test_manual_choice_updates_document_and_library_with_first_product_image(self):
        original=(self.sku.root/self.pid/'source.xlsx').read_bytes();self.parse();self.choose();self.drain()
        row=self.own();self.assertEqual(row['status'],'已拍摄');self.assertEqual(row['completed_at'],'2026-10-21T10:00:01')
        self.assertEqual(row['completion']['capture_id'],self.ids[1]);self.assertEqual(row['sku_image']['capture_id'],self.ids[0])
        self.assertEqual(self.own('细节图')['status'],'未拍摄');self.assertEqual(browse(self.sku,code=fixture.CODE)['total'],2)
        for index in (0,1):self.assertEqual(self.archive.writer.read(self.photos/f'{index}.JPG')['XMP-fk:SKU'],fixture.CODE)
        output=self.sku.export(self.pid);cells=self.cells(output)
        self.assertEqual([cells[x+'1'] for x in 'CDEF'],['拍摄状态','款号图片','拍摄完成时间','备注']);self.assertEqual(cells['E2'],row['completed_at'])
        plan=inspect_workbook(output)[0];picture=next(i for i in plan['images'] if i['cell']=='D2')
        data,_=read_image(output,picture['media'])
        with Image.open(io.BytesIO(data)) as image:self.assertLess(sum(abs(a-b) for a,b in zip(image.getpixel((10,10)),(40,80,100))),8)
        self.assertEqual(original,(self.sku.root/self.pid/'source.xlsx').read_bytes())
    def test_identical_repeated_ocr_keeps_confirmation_key_but_content_changes_invalidate(self):
        self.parse();before=self.sku.ocr_details(self.sid,self.ids[1]);self.parse()
        after=self.sku.ocr_details(self.sid,self.ids[1]);self.assertEqual(before['reading_key'],after['reading_key'])
        self.sku.choose_ocr_sku(self.sid,self.ids[1],fixture.CODE,before['reading_key'],before['image_key'])
        self.reading['text']+='\n新增颜色资料';self.parse()
        with self.assertRaisesRegex(ValueError,'已更新'):self.sku.choose_ocr_sku(self.sid,self.ids[1],fixture.CODE,before['reading_key'],before['image_key'])
    def test_full_auto_choice_and_nonstarred_picture_survive_restart(self):
        self.sku.set_full_auto(True,self.sid)
        for _ in range(4):self.sku.tick()
        self.choose(cid=self.ids[0]);row=self.own();self.assertEqual(row['status'],'已拍摄');self.assertEqual(row['sku_image']['capture_id'],self.ids[0])
        reopened=SkuService(self.store,self.providers,archive=self.archive,autostart=False,require_sku_choice=True)
        try:
            reopened.tick();self.assertEqual(next(r for r in reopened.state()['rows'] if r['id']==row['id'])['status'],'已拍摄')
        finally:reopened.close()
    def test_importing_new_document_after_old_stream_still_writes_whole_current_interval(self):
        content=(self.sku.root/self.pid/'source.xlsx').read_bytes()
        self.pid=self.sku.import_file('新文档.xlsx',content,self.sid)['project']['id'];self.sku.configure(self.pid,self.sid,'模特图')
        self.parse();result=self.choose();self.assertTrue(result['operation_id']);self.assertEqual(result['capture_count'],2);self.drain()
        for index in (0,1):self.assertEqual(self.archive.writer.read(self.photos/f'{index}.JPG')['XMP-fk:SKU'],fixture.CODE)
        self.assertEqual(self.own()['status'],'已拍摄');self.assertEqual(self.own()['sku_image']['capture_id'],self.ids[0])
    def test_correcting_sku_moves_document_status_and_keeps_other_sheet_pending(self):
        self.parse();self.choose();self.choose('LC7877496-P804')
        self.assertEqual(self.own()['status'],'未拍摄');self.assertEqual(self.own(code='LC7877496-P804')['status'],'已拍摄')
        self.assertEqual(self.own('细节图')['status'],'未拍摄');self.assertEqual(self.own(code='LC7877496-P804')['sku_image']['capture_id'],self.ids[0])
    def test_explicit_choice_updates_repeated_rows_only_in_current_sheet(self):
        from sku_match import normalize
        with self.sku.connect() as c:c.execute("UPDATE sku_rows SET sku=?,normalized=? WHERE project=? AND sheet='模特图' AND excel_row=3",(fixture.CODE,normalize(fixture.CODE),self.pid))
        self.parse();self.choose()
        rows=self.sku.state()['rows'];self.assertEqual([r['status'] for r in rows if r['sheet']=='模特图'],['已拍摄','已拍摄'])
        self.assertTrue(all(r['status']=='未拍摄' for r in rows if r['sheet']=='细节图'))
    def test_original_product_picture_header_can_coexist_with_reconciliation_picture(self):
        original=(self.sku.root/self.pid/'source.xlsx').read_bytes();source=self.base/'product-header.xlsx'
        with ZipFile(io.BytesIO(original)) as a,ZipFile(source,'w') as b:
            for info in a.infolist():b.writestr(info,a.read(info).replace('<t>图片</t>'.encode(),'<t>款号图片</t>'.encode()))
        plans=inspect_workbook(source);output=self.base/'product-header-out.xlsx'
        export_workbook(source,output,plans,{},capture_images={})
        again=inspect_workbook(output)[0];self.assertEqual(again['image_col'],4);self.assertTrue(any(c['title']=='款号图片' for c in again['columns']))
    def test_notes_values_and_formula_references_move_to_last_column(self):
        source=self.base/'notes.xlsx';original=(self.sku.root/self.pid/'source.xlsx').read_bytes()
        with ZipFile(io.BytesIO(original)) as a,ZipFile(source,'w') as b:
            for info in a.infolist():
                raw=a.read(info)
                if info.filename=='xl/worksheets/sheet1.xml':
                    raw=raw.replace(b'</f></c></row>',b'</f></c><c r="C2" t="inlineStr"><is><t>'+ '需要补拍'.encode()+b'</t></is></c></row>',1)
                    raw=raw.replace(b'<row r="3">',b'<row r="3"><c r="B3"><f>LEN(C2)</f></c>')
                b.writestr(info,raw)
        output=self.base/'notes-output.xlsx';export_workbook(source,output,inspect_workbook(source),{},capture_images={})
        cells=self.cells(output);self.assertEqual(cells['F1'],'备注');self.assertEqual(cells['F2'],'需要补拍');self.assertEqual(cells['B3'],'LEN(F2)')
    def test_other_sheet_only_code_is_recorded_as_unplanned_without_marking_other_sheet(self):
        with self.sku.connect() as c:c.execute("UPDATE sku_rows SET sku='OTHER-100',normalized='OTHER100' WHERE project=? AND sheet='模特图' AND excel_row=2",(self.pid,))
        self.parse();self.choose()
        self.assertEqual(self.own('细节图')['status'],'未拍摄')
        planned=next(r for r in self.sku.state()['rows'] if r['sheet']=='临时插拍' and r['sku']==fixture.CODE)
        self.assertEqual(planned['status'],'已拍摄')
    def test_repeat_export_and_reimport_keep_single_picture_column_and_baseline_image(self):
        self.parse();self.choose();output=self.sku.export(self.pid);first=output.read_bytes();self.sku.export(self.pid)
        with ZipFile(output) as z:
            for name in z.namelist():
                if name.startswith('xl/drawings/_rels/'):self.assertNotIn(b"encoding='utf8'",z.read(name))  # Excel / .NET 要求标准编码名。
            # 同款图片现在也同步到其他工作表及新增汇总页；每张工作表每行仍只有一个受管锚点。
            drawings=[E.fromstring(z.read(n)) for n in z.namelist() if n.startswith('xl/drawings/') and n.endswith('.xml')]
            for drawing in drawings:
                names=[p.get('name') for p in drawing.iter('{'+DRAW+'}cNvPr') if p.get('name','').startswith('FramekeepSkuRow')]
                self.assertEqual(len(names),len(set(names)))
        pid=self.sku.import_file('again.xlsx',first,self.sid)['project']['id'];row=next(r for r in self.sku.state()['rows'] if r['sheet']=='模特图' and r['excel_row']==2)
        self.assertTrue(row['sku_image']['imported']);out=self.sku.export(pid);cells=self.cells(out)
        self.assertEqual(sum(value=='款号图片' for ref,value in cells.items() if ref.endswith('1')),1)
        self.assertEqual(next(i for i in inspect_workbook(out)[0]['images'] if i['cell']=='D2')['cell'],'D2')
    def test_missing_image_keeps_status_and_time_and_records_unavailability(self):
        self.parse();self.choose();(self.photos/'0.JPG').unlink()
        with patch('media_engine.preview_bytes',side_effect=OSError('fixture missing')):output=self.sku.export(self.pid)
        cells=self.cells(output);self.assertEqual(cells['D2'],'图片暂不可用');self.assertEqual(cells['C2'],'已拍摄');self.assertEqual(cells['E2'],'2026-10-21T10:00:01')
    def test_legacy_time_column_shifts_references_and_preserves_original_images(self):
        source=self.base/'legacy.xlsx'
        with ZipFile(self.sku.root/self.pid/'source.xlsx') as z:parts={name:z.read(name) for name in z.namelist()}
        parts['xl/worksheets/sheet1.xml']=parts['xl/worksheets/sheet1.xml'].replace(b'</row>',('<c r="D1" t="inlineStr"><is><t>拍摄状态</t></is></c><c r="E1" t="inlineStr"><is><t>拍摄完成时间</t></is></c><c r="F1" t="inlineStr"><is><t>金额</t></is></c></row>').encode(),1).replace(b'</sheetData>',b'</sheetData><drawing xmlns:r="'+REL.encode()+b'" r:id="oldDrawing"/>').replace(b'<row r="3">',b'<row r="3"><c r="C3"><f>F2+SUM($E$2:E2)+IF(A2="E2",1,0)+SUM(E:E)</f></c>')
        parts['xl/worksheets/_rels/sheet1.xml.rels']=f'<Relationships xmlns="{PKG}"><Relationship Id="oldDrawing" Type="{REL}/drawing" Target="../drawings/old.xml"/><Relationship Id="table1" Type="{REL}/table" Target="../tables/table1.xml"/></Relationships>'.encode()
        parts['xl/tables/table1.xml']=f'<table xmlns="{NS}" id="1" name="Plan" displayName="Plan" ref="A1:F3"><autoFilter ref="A1:F3"/><tableColumns count="6">'.encode()+''.join(f'<tableColumn id="{i}" name="{name}"/>' for i,name in enumerate(['款号','图片','备注','拍摄状态','拍摄完成时间','金额'],1)).encode()+b'</tableColumns></table>'
        parts['xl/drawings/old.xml']=f'<xdr:wsDr xmlns:xdr="{DRAW}" xmlns:a="{ART}" xmlns:r="{REL}"><xdr:oneCellAnchor><xdr:from><xdr:col>5</xdr:col><xdr:row>1</xdr:row></xdr:from><xdr:pic><xdr:nvPicPr><xdr:cNvPr id="1" name="Original"/></xdr:nvPicPr><xdr:blipFill><a:blip r:embed="oldImage"/></xdr:blipFill></xdr:pic></xdr:oneCellAnchor></xdr:wsDr>'.encode()
        parts['xl/drawings/_rels/old.xml.rels']=f'<Relationships xmlns="{PKG}"><Relationship Id="oldImage" Target="../media/image1.png"/></Relationships>'.encode()
        parts['xl/worksheets/sheet2.xml']=parts['xl/worksheets/sheet2.xml'].replace(b'</row>',b'<c r="H1"><f>\'\xe6\xa8\xa1\xe7\x89\xb9\xe5\x9b\xbe\'!E2</f></c></row>',1)
        with ZipFile(source,'w') as z:
            for name,data in parts.items():z.writestr(name,data)
        plans=inspect_workbook(source);output=self.base/'shifted.xlsx';data=(self.photos/'0.JPG').read_bytes()
        export_workbook(source,output,plans,{('模特图',2):'已拍摄'},{('模特图',2):'2026-10-21T10:00:01'},capture_images={('模特图',2):data})
        with ZipFile(output) as z:
            self.assertIn('F2+SUM($E$2:E2)+IF(A2="E2",1,0)+SUM(E:E)',z.read('xl/worksheets/sheet1.xml').decode())
            table=E.fromstring(z.read('xl/tables/table1.xml'));self.assertEqual(table.get('ref'),'A1:G3');self.assertEqual(table.find('m:tableColumns',N)[3].get('name'),'款号图片');self.assertEqual(table.find('m:tableColumns',N)[-1].get('name'),'备注');self.assertEqual(table.find('m:tableColumns',N).get('count'),'7')
            self.assertIn("'模特图'!E2",z.read('xl/worksheets/sheet2.xml').decode())
            self.assertEqual(z.read('xl/cellimages.xml'),parts['xl/cellimages.xml']);self.assertEqual(z.read('xl/media/image1.png'),parts['xl/media/image1.png'])
        pictures=inspect_workbook(output)[0]['images'];self.assertTrue(any(i['cell']=='F2' and i['media']=='xl/media/image1.png' for i in pictures));self.assertTrue(any(i['cell']=='D2' for i in pictures))
    def test_browser_document_picture_after_status_and_preview(self):
        import server
        # 竖幅高分辨率标签与横幅商品图分别覆盖完整适应窗口。
        from PIL import ImageDraw
        label=Image.new('RGB',(2400,3600),'#375b72');draw=ImageDraw.Draw(label)
        draw.rectangle((0,0,2399,3599),outline='#f5dc70',width=50)
        for x,y in [(80,80),(2000,80),(80,3300),(2000,3300)]:draw.rectangle((x,y,x+300,y+180),fill='white')
        label.save(self.photos/'1.JPG')
        values=dict(STORE=self.store,DATA=self.store.data,MODELS=self.providers,SKU=self.sku,ARCHIVE=self.archive,CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,CANON=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None)
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        with ExitStack() as stack:
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1680,'height':1050},reduced_motion='reduce');errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading');page.locator('[data-watch-photo="'+self.ids[1]+'"]').click();page.locator('.asset-panel').evaluate('(el)=>el.open=true')
                        page.locator('#asset-parse-sku').click();page.wait_for_selector('#asset-ocr-codes [data-ocr-code="'+fixture.CODE+'"]');page.locator('#asset-ocr-codes [data-ocr-code="'+fixture.CODE+'"]').click()
                        page.wait_for_function('async()=>{const s=await(await fetch("/api/sku")).json();return s.rows.some(r=>r.sheet==="模特图"&&r.status==="已拍摄")}',timeout=15000)
                        self.drain();page.locator('#sku-open').click();page.wait_for_selector('#sku-rows [data-sku-row] .sku-product-images img')
                        headers=page.locator('#sku-rows th').all_text_contents();at=headers.index('拍摄状态');self.assertEqual(headers[at+1:at+3],['款号图片','拍摄完成时间']);self.assertEqual(headers[-1],'备注')
                        row=page.locator('#sku-rows [data-sku-row="'+self.own()['id']+'"]');image=row.locator('td').nth(at+1).locator('img')
                        viewer=page.locator('.sku-image-dialog');preview=page.locator('#sku-image-stage img');stage=page.locator('#sku-image-stage')
                        def loaded():page.wait_for_function('()=>{const e=document.querySelector("#sku-image-stage img");return e?.naturalWidth>0&&!e.hidden}')
                        def fits():
                            geometry=page.evaluate('''()=>{const d=document.querySelector('.sku-image-dialog'),s=document.getElementById('sku-image-stage'),i=s.querySelector('img'),box=e=>{const r=e.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height}};return {dialog:box(d),stage:box(s),image:box(i),overflow:d.scrollWidth-d.clientWidth,ratio:i.naturalWidth/i.naturalHeight}}''')
                            d,s,i=geometry['dialog'],geometry['stage'],geometry['image']
                            self.assertLessEqual(geometry['overflow'],1);self.assertGreater(s['height'],60)
                            self.assertGreaterEqual(d['left'],0);self.assertLessEqual(d['right'],page.viewport_size['width'])
                            self.assertGreaterEqual(d['top'],0);self.assertLessEqual(d['bottom'],page.viewport_size['height'])
                            for start,end in [('left','right'),('top','bottom')]:
                                self.assertGreaterEqual(i[start],s[start]-.5);self.assertLessEqual(i[end],s[end]+.5)
                            self.assertAlmostEqual(i['width']/i['height'],geometry['ratio'],places=3)
                            self.assertEqual(page.locator('#sku-image-scale').inner_text(),'100%')
                        page.wait_for_function('(cid)=>Array.from(document.querySelectorAll("#sku-rows [data-sku-cover] img")).some(i=>i.src.includes(cid))',arg=self.ids[0])
                        self.assertIn(self.ids[0],image.get_attribute('src'));image.click();page.wait_for_selector('#sku-cover-dialog[open]')
                        page.wait_for_selector('#sku-cover-groups img');self.assertTrue(page.locator('#sku-cover-group').is_visible());page.keyboard.press('Escape')
                        self.assertFalse(viewer.evaluate('e=>e.open'))
                        trigger=row.locator('[data-completion]');trigger.click();loaded();fits();self.assertIn(self.ids[1],preview.get_attribute('src'))
                        original=self.store.session(self.sid)['state']['overrides'].copy()
                        box=stage.bounding_box();cx=box['x']+box['width']/2;cy=box['y']+box['height']/2
                        page.mouse.move(cx,cy);page.mouse.wheel(0,-450);page.wait_for_function('()=>parseInt(document.getElementById("sku-image-scale").value)>100')
                        for button in ['left','middle']:
                            before=preview.evaluate('e=>e.style.transform');page.mouse.move(cx,cy);page.mouse.down(button=button);page.mouse.move(cx+60,cy+40,steps=4);page.mouse.up(button=button)
                            self.assertNotEqual(preview.evaluate('e=>e.style.transform'),before)
                        page.locator('#sku-image-fit').click();fits()
                        page.locator('#sku-image-plus').click();self.assertEqual(page.locator('#sku-image-scale').inner_text(),'125%')
                        page.locator('#sku-image-minus').click();fits();page.keyboard.press('Equal');self.assertEqual(page.locator('#sku-image-scale').inner_text(),'125%')
                        stage.dblclick();fits();stage.dblclick();self.assertEqual(page.locator('#sku-image-scale').inner_text(),'200%')
                        page.keyboard.press('0');fits();page.keyboard.press('3');self.assertEqual(self.store.session(self.sid)['state']['overrides'],original)
                        stage.dispatch_event('wheel',{'deltaY':-10000,'deltaMode':1});self.assertLessEqual(int(page.locator('#sku-image-scale').inner_text().strip('%')),1600)
                        for _ in range(4):stage.dispatch_event('wheel',{'deltaY':10000,'deltaMode':1})
                        fits();page.keyboard.press('Escape');self.assertFalse(viewer.evaluate('e=>e.open'));self.assertTrue(trigger.evaluate('e=>e===document.activeElement'))
                        trigger.click();loaded();fits();page.locator('#sku-image-close').click();self.assertFalse(viewer.evaluate('e=>e.open'))
                        out=fixture.ROOT/'build/sku-document-acceptance';out.mkdir(exist_ok=True)
                        for theme in ['dark','light','pure-white','hardcore']:
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                            for width in [1680,1024,600,375]:
                                page.set_viewport_size({'width':width,'height':1050});self.assertFalse(page.evaluate('()=>document.documentElement.scrollWidth>innerWidth'))
                                if width==1680:page.screenshot(path=str(out/f'document-{theme}.png'))
                                trigger.click();loaded();fits()
                                if width in (1680,375):page.screenshot(path=str(out/f'preview-{theme}-{width}.png'))
                                page.keyboard.press('Escape')
                        page.set_viewport_size({'width':1440,'height':900});trigger.click();loaded();page.locator('#sku-image-plus').click()
                        page.set_viewport_size({'width':700,'height':500});page.locator('#sku-image-fit').click();fits();page.keyboard.press('Escape')
                        # 网络失败仍可退出，重新打开恢复正常图片和初始缩放。
                        route='**/api/preview/**?size=1600';page.route(route,lambda r:r.abort());trigger.click()
                        page.wait_for_function('()=>document.getElementById("sku-image-status").textContent.includes("无法加载")')
                        self.assertTrue(page.locator('#sku-image-plus').is_disabled());page.keyboard.press('Escape');self.assertFalse(viewer.evaluate('e=>e.open'))
                        page.unroute(route);trigger.click();loaded();fits();page.keyboard.press('Escape')
                        (out/'document.xlsx').write_bytes(self.sku.export(self.pid).read_bytes());self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
