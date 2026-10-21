from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import io
from xml.etree import ElementTree as E
import threading
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import Store
from asset_archive import AssetArchive
from asset_analysis import AssetAnalysis
from model_providers import Providers
from vision_scheduler import VisionScheduler
from sku_service import SkuService
import media_engine as media
import test_sku_workflow as fixture

ROOT=Path(__file__).resolve().parents[1]


class ProgressBrowserTests(unittest.TestCase):
    def test_tabs_live_counts_promote_completed_capture_time_proof_and_export(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir();store=Store(base/'data',ROOT);sid=store.ensure_live_session(photos);store.activate(sid)
            providers=Providers(store.data);public=providers.save({'name':'test','category':'multimodal','protocol':'chat','url':'https://example.invalid','model':'test'});providers.select(public['providers'][0]['id'])
            archive=AssetArchive(store,autostart=False);analysis=AssetAnalysis(archive,providers,VisionScheduler(),autostart=False)
            image=photos/'SKU-label.JPG';Image.new('RGB',(1000,600),'white').save(image)
            archive.writer.run('-overwrite_original','-DateTimeOriginal=2026:10:20 15:16:17','-SubSecTimeOriginal=123','-OffsetTimeOriginal=+08:00',image)
            meta=media.read_metadata([image],media.find_exiftool(ROOT))[0];store.append_live(sid,[media.capture_record(photos,image,None,meta)])
            sku=SkuService(store,providers,recognizer=lambda p,b:fixture.reading('LC7877496-P804'))
            pid=sku.import_file('拍摄清单.xlsx',fixture.fixture_book(),sid)['project']['id'];sku.configure(pid,sid,'模特图')
            patches=[patch.object(server,k,v) for k,v in [('STORE',store),('DATA',store.data),('MODELS',providers),('ARCHIVE',archive),('ANALYSIS',analysis),('SKU',sku),('WATCH',None),('MOUSE',None),('RAWLINK',None),('FILESEARCH',None)]]
            for p in patches:p.start()
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1680,'height':1050},accept_downloads=True);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.add_init_script("localStorage.setItem('framekeep-layout-v1',JSON.stringify({selection:{sections:{'progress-table':false}},monitor:{sections:{'progress-table':false}}}))")
                        out=ROOT/'build/shooting-grid-acceptance';out.mkdir(exist_ok=True)
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='networkidle')
                        page.wait_for_function('()=>FramekeepSku.currentType()==="模特图"')
                        self.assertEqual(page.locator('#asset-scope,#asset-custom-scope').count(),0)
                        page.locator('#current-shooting-tab').click();page.locator('#sku-live-scope').select_option('细节图');page.wait_for_timeout(1200)
                        self.assertEqual(page.evaluate('FramekeepSku.currentType()'),'细节图')
                        page.locator('#sku-live-scope').select_option('模特图');page.wait_for_function('()=>FramekeepSku.currentType()==="模特图"')
                        page.locator('#photo-settings-tab').click()
                        page.locator('.asset-panel').screenshot(path=str(out/'asset-types.png'))
                        page.locator('#sku-open').click()
                        page.wait_for_function('()=>document.getElementById("progress-total")?.textContent==="2"');self.assertEqual(page.locator('#progress-pending').inner_text(),'2')
                        self.assertFalse(errors,errors)
                        self.assertIn(fixture.CODE,page.locator('#sku-rows tbody tr').first.inner_text())
                        self.assertEqual(page.locator('#sku-rows thead th').first.inner_text(),'序号');self.assertEqual(page.locator('#sku-rows tbody .sku-sequence').all_text_contents(),['1','2'])
                        self.assertEqual(page.locator('#progress-table').evaluate('(el)=>el.tagName'),'SECTION');self.assertEqual(page.locator('#progress-table > summary').count(),0)
                        page.locator('#progress-table > h3').click();self.assertTrue(page.locator('#sku-rows').is_visible())
                        page.wait_for_function('()=>document.getElementById("sku-sheet-panel").getAnimations().every(a=>a.playState!=="running")')
                        geometry=page.evaluate('()=>{const main=document.querySelector(".workspace-tabs").getBoundingClientRect(),panel=document.getElementById("sku-dialog").getBoundingClientRect(),sidebar=document.getElementById("progress-sidebar").getBoundingClientRect(),list=document.getElementById("progress-table").getBoundingClientRect();return {main:(main.left+main.right)/2,sidebarLeft:sidebar.left,listRight:list.right,listTop:list.top,panelTop:panel.top,width:innerWidth}}')
                        self.assertTrue(page.locator('.workspace-bottom-bar .workspace-tabs').is_visible())
                        self.assertLess(geometry['listRight'],geometry['sidebarLeft']);self.assertLessEqual(geometry['listTop']-geometry['panelTop'],14)
                        for selector in ['#sku-sheet-tabs','.progress-heading','#progress-controls','#progress-statistics','.progress-tools','#sku-message','#sku-export','#sku-close']:
                            self.assertTrue(page.locator(selector).evaluate('(el)=>!!el.closest("#progress-sidebar")'),selector)
                        self.assertFalse(page.locator('#sku-sheet-tabs').evaluate('(el)=>!!el.closest("#progress-controls")'));self.assertEqual(page.locator('#sku-sheet-tabs [data-progress-sheet]').count(),2);self.assertEqual(page.locator('#sku-sheet-tabs [role=tab]').count(),3);self.assertIn('公共',page.locator('#sku-common-tab').inner_text())
                        page.locator('#selection-tab').click();page.keyboard.press('2');page.locator('#sku-open').click()
                        page.wait_for_function('()=>document.getElementById("progress-shot")?.textContent==="1"');self.assertEqual(page.locator('#progress-pending').inner_text(),'1')
                        first=page.locator('#sku-rows tbody tr').first;self.assertIn('LC7877496-P804',first.inner_text());self.assertIn('2026-10-20 15:16:17.123+08:00',first.inner_text())
                        self.assertEqual(first.locator('.sku-sequence').inner_text(),'1');self.assertEqual(first.locator('.progress-row-number').inner_text(),'3')
                        self.assertEqual(page.locator('#sku-rows .sku-sheet-badge').count(),0)
                        page.locator('#sku-search').fill('P804');self.assertEqual(page.locator('#sku-rows tbody .sku-sequence').all_text_contents(),['1'])
                        searchSummary=page.locator('#sku-search-summary');sheetCounts=page.locator('#sku-sheet-tabs [data-progress-sheet] span')
                        self.assertEqual(searchSummary.inner_text(),'款号搜索结果：模特图 1张，细节图 1张');self.assertEqual(sheetCounts.all_text_contents(),['1/2','1/2'])
                        self.assertIn('匹配款号',page.locator('#sku-sheet-tab-0').get_attribute('title'))
                        self.assertTrue(searchSummary.evaluate('(el)=>{const a=el.getBoundingClientRect(),b=el.closest("h3").getBoundingClientRect();return Math.abs((a.left+a.right-b.left-b.right)/2)<1&&getComputedStyle(el).textAlign==="center"}'))
                        page.locator('#sku-filter').select_option('pending');self.assertEqual(searchSummary.inner_text(),'款号搜索结果：模特图 0张，细节图 1张');self.assertEqual(sheetCounts.all_text_contents(),['0/2','1/2']);self.assertEqual(page.locator('#sku-rows tbody tr').count(),0)
                        page.locator('#sku-filter').select_option('shot');self.assertEqual(searchSummary.inner_text(),'款号搜索结果：模特图 1张，细节图 0张');self.assertEqual(sheetCounts.all_text_contents(),['1/2','0/2'])
                        page.locator('#sku-filter').select_option('all');page.wait_for_timeout(1200);self.assertEqual(sheetCounts.all_text_contents(),['1/2','1/2'])
                        page.locator('#sku-search').fill('no-such-sku');self.assertEqual(searchSummary.inner_text(),'款号搜索结果：模特图 0张，细节图 0张');self.assertEqual(sheetCounts.all_text_contents(),['0/2','0/2'])
                        page.locator('#sku-search').fill('lc7877496');self.assertEqual(searchSummary.inner_text(),'款号搜索结果：模特图 2张，细节图 2张');self.assertEqual(sheetCounts.all_text_contents(),['2/2','2/2'])
                        page.locator('#sku-search').fill('P804')
                        badges=page.locator('#sku-rows .sku-sheet-badge');self.assertEqual(badges.all_text_contents(),['模特图','细节图'])
                        self.assertTrue(page.locator('#sku-rows .sku-sheet-buttons').evaluate('(el)=>{const [a,b]=[...el.children].map(n=>n.getBoundingClientRect());return a.bottom<=b.top&&Math.abs(a.left-b.left)<1&&el.getBoundingClientRect().right<=el.nextElementSibling.getBoundingClientRect().left}'))
                        page.screenshot(path=str(out/'search-sheet-buttons.png'))
                        page.locator('#sku-rows [data-sku-sheet="细节图"]').click();page.wait_for_function('()=>document.querySelector("#sku-rows [data-sku-sheet=细节图]")?.getAttribute("aria-pressed")==="true"')
                        self.assertEqual(page.locator('#sku-rows tbody tr').count(),1)
                        self.assertEqual(badges.all_text_contents(),['模特图','细节图']);self.assertEqual(page.locator('#sku-search').input_value(),'P804')
                        self.assertEqual(searchSummary.inner_text(),'款号搜索结果：模特图 1张，细节图 1张');self.assertEqual(sheetCounts.all_text_contents(),['1/2','1/2'])
                        page.locator('#sku-rows [data-sku-sheet="模特图"]').focus();page.keyboard.press('Enter');page.wait_for_function('()=>document.querySelector("#sku-rows [data-sku-sheet=模特图]")?.getAttribute("aria-pressed")==="true"')
                        page.locator('#sku-search').fill('');self.assertEqual(page.locator('#sku-rows .sku-sheet-badge').count(),0)
                        self.assertTrue(searchSummary.is_hidden());self.assertEqual(sheetCounts.all_text_contents(),['1/2','0/2']);self.assertIn('已拍摄',page.locator('#sku-sheet-tab-0').get_attribute('title'))
                        page.locator('#sku-filter').select_option('pending');self.assertEqual(page.locator('#sku-rows tbody .sku-sequence').all_text_contents(),['1']);self.assertEqual(page.locator('#progress-total').inner_text(),'2');page.locator('#sku-filter').select_option('all');self.assertEqual(page.locator('#sku-rows tbody tr').count(),2)
                        self.assertTrue(page.locator('#selection-page').is_hidden());self.assertFalse(page.locator('.film-panel').is_visible())
                        page.locator('#sku-open').focus();page.keyboard.press('3');self.assertEqual(store.session(sid)['state']['overrides'][store.records(sid)[0]['id']],2)
                        first.locator('[data-completion]').click();page.wait_for_function('()=>document.querySelector(".sku-image-dialog>img")?.naturalWidth>0');self.assertIn('标签照片',page.locator('.sku-image-dialog h2').inner_text());page.locator('.sku-image-dialog button').click()
                        page.screenshot(path=str(out/'progress-browser.png'))
                        for _ in range(2):
                            page.locator('#progress-controls > summary').click();page.wait_for_function('()=>document.getElementById("progress-controls").getBoundingClientRect().height<60')
                            self.assertTrue(page.locator('#progress-controls > summary').is_visible());self.assertTrue(page.locator('#sku-search').is_visible());self.assertTrue(page.locator('#sku-export').is_visible())
                            page.locator('#progress-controls > summary').click();page.wait_for_selector('#sku-projects',state='visible')
                        page.locator('#progress-controls > summary').click();self.assertTrue(page.locator('#sku-sheet-tabs').is_visible())
                        page.locator('[data-progress-sheet="模特图"]').focus();page.keyboard.press('ArrowRight');self.assertEqual(page.locator('[data-progress-sheet="细节图"]').evaluate('(el)=>el===document.activeElement'),True);self.assertEqual(page.locator('[data-progress-sheet="模特图"]').get_attribute('aria-selected'),'true');page.keyboard.press('Enter');
                        page.wait_for_function('()=>document.getElementById("progress-shot").textContent==="0"');self.assertEqual(page.locator('#progress-pending').inner_text(),'2')
                        self.assertEqual(page.locator('#sku-rows tbody .sku-sequence').all_text_contents(),['1','2']);page.keyboard.press('3');self.assertEqual(store.session(sid)['state']['overrides'][store.records(sid)[0]['id']],2)
                        page.keyboard.press('Home');page.keyboard.press('Enter');page.wait_for_function('()=>document.getElementById("progress-shot").textContent==="1"')
                        self.assertEqual(page.locator('#sku-sheet-panel').get_attribute('aria-labelledby'),'sku-sheet-tab-0')

                        self.assertEqual(page.locator('#sku-export').inner_text(),'导出拍摄记录');self.assertTrue(page.locator('#sku-export').is_visible());self.assertFalse(page.locator('#sku-export').evaluate('(el)=>!!el.closest("#progress-controls")'))
                        page.locator('#sku-filter').select_option('pending')
                        with page.expect_download() as download:page.locator('#sku-export').click()
                        target=out/'browser-export.xlsx';download.value.save_as(str(target));self.assertIsNone(download.value.failure());self.assertRegex(download.value.suggested_filename,r'^拍摄清单-拍摄记录-\d{8}-\d{6}\.xlsx$')
                        with ZipFile(io.BytesIO(fixture.fixture_book())) as original,ZipFile(target) as z:
                            self.assertEqual(set(original.namelist()),set(z.namelist()))
                            for name in original.namelist():
                                if not name.startswith('xl/worksheets/'):self.assertEqual(original.read(name),z.read(name),name)
                                else:
                                    old=E.fromstring(original.read(name));new=E.fromstring(z.read(name));ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                                    cells={c.get('r'):E.tostring(c) for c in new.findall('m:sheetData/m:row/m:c',ns)}
                                    self.assertTrue(all(E.tostring(c)==cells[c.get('r')] for c in old.findall('m:sheetData/m:row/m:c',ns)))
                                    self.assertIn('拍摄状态',z.read(name).decode());self.assertIn('拍摄完成时间',z.read(name).decode())
                            self.assertIn('2026-10-20T15:16:17.123+08:00',z.read('xl/worksheets/sheet1.xml').decode());self.assertIn('已拍摄',z.read('xl/worksheets/sheet1.xml').decode())
                        # 桌面沿用原生另存为桥接；这里只验证前端传参及取消反馈，不声称操作了系统对话框。
                        page.evaluate('()=>window.FramekeepDesktop={saveSkuWorkbook:async(projectId,name)=>{window.__recordExport={projectId,name};return true}}');page.locator('#sku-export').click();page.wait_for_function('()=>document.getElementById("sku-message").textContent.startsWith("拍摄记录已导出")')
                        self.assertEqual(page.evaluate('()=>window.__recordExport.projectId'),pid);self.assertRegex(page.evaluate('()=>window.__recordExport.name'),r'拍摄记录-\d{8}-\d{6}\.xlsx$')
                        page.evaluate('()=>window.FramekeepDesktop.saveSkuWorkbook=async()=>false');page.locator('#sku-export').click();page.wait_for_function('()=>document.getElementById("sku-message").textContent==="已取消导出"');self.assertFalse(page.locator('#sku-export').is_disabled())
                        page.reload(wait_until='networkidle');page.locator('#sku-open').click();page.wait_for_function('()=>document.getElementById("progress-shot")?.textContent==="1"')
                        # 宽屏模块只在右侧；窄屏从右侧展开设置，清单不被顶部模块挤占。
                        page.locator('#progress-controls').evaluate('(el)=>el.open=true')
                        page.emulate_media(reduced_motion='reduce')
                        page.wait_for_function('()=>document.getElementById("sku-sheet-panel").getAnimations().every(a=>a.playState!=="running")')
                        for width in [1800,1440,1100,1000,850,650,400]:
                            page.set_viewport_size({'width':width,'height':1050})
                            if width<=760 and not page.locator('#progress-sidebar').is_visible():page.locator('#progress-sidebar-toggle').click()
                            boxes=page.evaluate('''()=>{const box=id=>{const r=document.getElementById(id).getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,height:r.height}},el=document.getElementById('sku-dialog');return {sidebar:box('progress-sidebar'),list:box('progress-table'),controls:box('progress-controls'),stats:box('progress-statistics'),overflow:el.scrollWidth-el.clientWidth,columns:getComputedStyle(document.querySelector('.progress-overview')).gridTemplateColumns.split(' ').length}}''')
                            self.assertLessEqual(boxes['overflow'],1,(width,boxes));self.assertEqual(boxes['columns'],1)
                            self.assertLessEqual(boxes['controls']['bottom'],boxes['stats']['top'],(width,boxes))
                            if width>760:
                                self.assertLess(boxes['list']['right'],boxes['sidebar']['left']);self.assertLessEqual(abs(boxes['list']['top']-boxes['sidebar']['top']),1)
                                self.assertGreater(boxes['list']['height'],700)
                            # 长清单必须在自身容器滚动，不能超出 details 的匿名内容盒而被裁掉。
                            scrolling=page.evaluate('''()=>{const el=document.getElementById('sku-rows'),body=el.querySelector('tbody'),row=body.firstElementChild,side=document.getElementById('progress-sidebar');const nodes=Array.from({length:30},()=>row.cloneNode(true));body.append(...nodes);el.scrollTop=300;const r=el.getBoundingClientRect(),head=el.querySelector('th').getBoundingClientRect();const result={height:el.clientHeight,scroll:el.scrollHeight,top:el.scrollTop,sticky:Math.abs(r.top-head.top),side:side.scrollTop};nodes.forEach(n=>n.remove());el.scrollTop=0;return result}''')
                            self.assertLess(scrolling['height'],900);self.assertGreater(scrolling['scroll'],scrolling['height']);self.assertGreater(scrolling['top'],0);self.assertLessEqual(scrolling['sticky'],1)
                            for selector in ['#sku-projects','#sku-panel-scope','#sku-search','#sku-filter','#sku-recognize']:
                                self.assertTrue(page.locator(selector).evaluate('(el)=>{const r=el.getBoundingClientRect();return r.width>0&&r.left>=0&&r.right<=innerWidth}'),(width,selector))
                            self.assertEqual(page.locator('#sku-rows th').first.evaluate('(el)=>getComputedStyle(el).top'),'0px')
                            fixed=page.evaluate('''()=>{const box=el=>{const r=el.getBoundingClientRect();return {top:r.top,bottom:r.bottom,left:r.left,right:r.right,width:r.width,height:r.height}},side=document.getElementById('progress-sidebar'),scroll=document.getElementById('progress-sidebar-scroll'),search=document.getElementById('sku-search'),footer=document.querySelector('.progress-sidebar-footer'),title=document.getElementById('sku-title'),buttons=[...footer.children].map(box),before={search:box(search),footer:box(footer)};scroll.scrollTop=scroll.scrollHeight;const after={search:box(search),footer:box(footer)};scroll.scrollTop=0;return {buttons,before,after,side:box(side),title:box(title),scrollable:scroll.scrollHeight>scroll.clientHeight}}''')
                            self.assertAlmostEqual(fixed['buttons'][0]['width'],fixed['buttons'][1]['width'],delta=1);self.assertEqual(fixed['buttons'][0]['height'],fixed['buttons'][1]['height'])
                            self.assertEqual(fixed['before'],fixed['after']);self.assertTrue(fixed['scrollable']);self.assertLessEqual(fixed['side']['bottom']-fixed['after']['footer']['bottom'],16)
                            self.assertAlmostEqual((fixed['side']['left']+fixed['side']['right'])/2,(fixed['title']['left']+fixed['title']['right'])/2,delta=1)
                            if width in [1800,650,400]:page.screenshot(path=str(out/f'sidebar-{width}.png'))
                            page.locator('#sku-search').fill('P804')
                            self.assertTrue(searchSummary.evaluate('(el)=>{const a=el.getBoundingClientRect(),b=el.closest("h3").getBoundingClientRect();return Math.abs((a.left+a.right-b.left-b.right)/2)<1&&a.left>=b.left&&a.right<=b.right&&a.top>=b.top&&a.bottom<=b.bottom}'),width)
                            if width in [1800,400]:page.screenshot(path=str(out/f'search-counts-{width}.png'))
                            page.locator('#sku-search').fill(' ');self.assertTrue(searchSummary.is_hidden());page.locator('#sku-search').fill('')
                            if width<=760:
                                page.locator('#sku-search').fill('P804');self.assertEqual(page.locator('#sku-rows tbody tr').count(),1);page.locator('#sku-search').fill('')
                                page.locator('.progress-sidebar-close').click();self.assertFalse(page.locator('#progress-sidebar').is_visible());self.assertTrue(page.locator('#progress-table').is_visible())
                                page.locator('#progress-sidebar-toggle').click();page.keyboard.press('Escape');self.assertFalse(page.locator('#progress-sidebar').is_visible())
                                self.assertEqual(page.locator('#progress-sidebar-toggle').get_attribute('aria-expanded'),'false')
                                if width==400:page.screenshot(path=str(out/'sidebar-400-closed.png'))
                        page.set_viewport_size({'width':1680,'height':1050})
                        # 从浏览器真实导入新文档，即使未选择拍摄页，也立即换用新表的选项。
                        data=io.BytesIO()
                        with ZipFile(io.BytesIO(fixture.fixture_book())) as original,ZipFile(data,'w') as modified:
                            for name in original.namelist():
                                content=original.read(name)
                                if name=='xl/workbook.xml':content=content.replace('模特图'.encode(),'平铺图'.encode()).replace('细节图'.encode(),'挂拍图'.encode())
                                modified.writestr(name,content)
                        page.locator('#sku-file').set_input_files({'name':'新拍摄文档.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':data.getvalue()})
                        page.wait_for_function('()=>document.querySelector("#sku-live-scope option[value=挂拍图]")')
                        self.assertEqual(page.evaluate('FramekeepSku.currentType()'),'');self.assertNotIn('模特图',page.locator('#sku-live-scope').inner_text())
                        page.screenshot(path=str(out/'imported-unselected.png'))
                        page.locator('#sku-panel-scope').select_option('平铺图');page.wait_for_function('()=>FramekeepSku.currentType()==="平铺图"')
                        page.locator('#sku-search').fill('P804');self.assertEqual(searchSummary.inner_text(),'款号搜索结果：平铺图 1张，挂拍图 1张');self.assertEqual(sheetCounts.all_text_contents(),['1/2','1/2']);page.locator('#sku-search').fill('')
                        page.locator('#sku-auto').uncheck();page.wait_for_function('()=>!document.getElementById("sku-auto").checked')
                        page.locator('#selection-tab').click();page.locator('#current-shooting-tab').click();page.locator('#sku-live-scope').select_option('挂拍图');page.locator('#photo-settings-tab').click();page.locator('#asset-sku').fill('MANUAL-123')
                        with page.expect_response(lambda r:'/api/metadata/preview' in r.url) as response:page.locator('#asset-preview').click()
                        data=response.value.json();self.assertEqual(data['scope'],'挂拍图');self.assertEqual(data['project'],sku.project()['id'])
                        page.locator('#asset-preview-dialog [data-close]').click();page.locator('#sku-open').click()
                        page.locator('#sku-projects').select_option(pid);page.wait_for_function('()=>FramekeepSku.currentType()==="模特图"')
                        self.assertEqual(page.evaluate('FramekeepSku.shootingTypes().sheets'),['模特图','细节图'])
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:
                http.shutdown();http.server_close();sku.close();analysis.close();archive.close()
                for p in reversed(patches):p.stop()


if __name__=='__main__':unittest.main()
