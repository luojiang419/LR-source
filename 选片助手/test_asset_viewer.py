"""归档／SKU 全屏浏览：真实 HTTP、隔离照片与可控的异步资料响应。"""
from contextlib import ExitStack
import copy
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image,TiffImagePlugin
from playwright.sync_api import sync_playwright
from asset_catalog import AssetCatalog
import test_ocr_metadata as ocr_fixture
from test_sku_workflow import CODE

ROOT=Path(__file__).resolve().parents[1]


class AssetViewerTests(unittest.TestCase):
    def test_archive_details_and_large_preview_are_readonly_and_preserve_missing_capture_time(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir();photo=photos/'IMG_1234.JPG'
            exif=Image.Exif();exif[36867]='2026:10:22 08:09:10';exif[272]='TEST CAMERA';exif[34855]=400
            exif[33437]=TiffImagePlugin.IFDRational(28,10);exif[33434]=TiffImagePlugin.IFDRational(1,125)
            Image.new('RGB',(2400,1600),'teal').save(photo,exif=exif)
            catalog=AssetCatalog(base/'data');catalog.add_root(str(photos));item=catalog.items()['rows'][0]
            before=hashlib.sha256(photo.read_bytes()).hexdigest();catalog.metadata.process_one();details=catalog.details(item['id'],ROOT)
            self.assertEqual(details['capture']['time'],'2026-10-22T08:09:10');self.assertEqual(details['capture']['camera'],'TEST CAMERA')
            self.assertEqual(details['capture']['iso'],400);self.assertEqual(details['capture']['width'],2400);self.assertEqual(details['files'][0]['path'],str(photo))
            with Image.open(catalog.preview(item['id'],2400)) as image:self.assertEqual(max(image.size),2048)
            self.assertEqual(hashlib.sha256(photo.read_bytes()).hexdigest(),before)
            Image.new('RGB',(50,30),'red').save(photos/'no-time.JPG');catalog.scan(catalog.state()['roots'][0]['id'])
            missing=next(row for row in catalog.items()['rows'] if row['name']=='no-time.JPG')
            catalog.metadata.process_one()
            self.assertIsNone(catalog.details(missing['id'],ROOT)['capture']['time'])
            photo.unlink();catalog.scan(catalog.state()['roots'][0]['id']);offline=catalog.details(item['id'],ROOT)
            self.assertIn('离线',offline['metadata_error']);self.assertFalse(offline['files'][0]['online'])
            catalog.close()

    def test_browser_fullscreen_zoom_pan_details_ocr_races_and_exit(self):
        import server
        fixture=ocr_fixture.OcrMetadataTests();fixture.setUp();fixture.reading['text']+='\n<script>window.ocrInjected=true</script>'
        fixture.parse();fixture.choose();fixture.sku.parse_capture(fixture.sid,fixture.ids[3]);fixture.choose('NEXT-200',fixture.ids[3])
        catalog=AssetCatalog(fixture.store.data);catalog.add_root(str(fixture.photos));catalog.metadata.start();rows=catalog.items()['rows']
        before={path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in fixture.photos.iterdir()}
        original_state=copy.deepcopy(fixture.store.session(fixture.sid)['state'])
        out=ROOT/'build/asset-viewer-acceptance';out.mkdir(parents=True,exist_ok=True)
        values=dict(STORE=fixture.store,DATA=fixture.store.data,MODELS=fixture.providers,SKU=fixture.sku,ARCHIVE=fixture.archive,CATALOG=catalog,
                    ANALYSIS=None,WATCH=None,MOUSE=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None,CANON=None,PREVIEWS=None)
        try:
            with ExitStack() as stack:
                for key,value in values.items():stack.enter_context(patch.object(server,key,value))
                http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
                threading.Thread(target=http.serve_forever,daemon=True).start()
                try:
                    with sync_playwright() as pw:
                        browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                        try:
                            page=browser.new_page(viewport={'width':1440,'height':1000},reduced_motion='reduce');errors=[];writes=[]
                            page.on('pageerror',lambda error:errors.append(str(error)))
                            page.on('request',lambda request:writes.append(request.url) if request.method=='POST' and any(path in request.url for path in ['/api/catalog/rating','/api/catalog/assign','/api/sku/ocr/choose','/api/metadata/']) else None)
                            page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                            page.wait_for_function('()=>document.querySelectorAll("#catalog-grid .catalog-card").length===4')
                            first=page.locator('#catalog-grid .catalog-card').first;first_name=first.locator('img').get_attribute('alt')
                            top=page.locator('#catalog-grid').evaluate('e=>e.scrollTop');first.dblclick()
                            viewer=page.locator('#asset-image-viewer');stage=page.locator('#asset-viewer-stage')
                            page.wait_for_function('()=>document.getElementById("asset-viewer-image").naturalWidth>0&&!document.getElementById("asset-viewer-image").hidden&&document.getElementById("asset-viewer-info-status").textContent===""')
                            self.assertEqual(page.locator('#asset-viewer-title').inner_text(),first_name)
                            self.assertIn('原图路径',page.locator('#asset-viewer-fields').inner_text());self.assertIn('未提供',page.locator('#asset-viewer-fields').inner_text())
                            initial_preview=page.locator('#asset-viewer-image').get_attribute('src')
                            self.assertRegex(initial_preview,r'size=2048')
                            self.assertIn('blur',viewer.evaluate('e=>getComputedStyle(e,"::backdrop").backdropFilter'))
                            box=stage.bounding_box();cx=box['x']+box['width']/2;cy=box['y']+box['height']/2
                            page.mouse.move(cx,cy);page.mouse.wheel(0,-450)
                            page.wait_for_function('()=>parseInt(document.getElementById("asset-viewer-scale").value)>100')
                            page.wait_for_function('()=>document.getElementById("asset-viewer-image").src.includes("size=2048")')
                            start=page.locator('#asset-viewer-image').evaluate('e=>e.style.transform')
                            page.mouse.down();page.mouse.move(cx+70,cy+45,steps=5);page.mouse.up()
                            self.assertNotEqual(page.locator('#asset-viewer-image').evaluate('e=>e.style.transform'),start)
                            page.mouse.down(button='middle');page.mouse.move(cx+35,cy+20,steps=3);page.mouse.up(button='middle')
                            page.locator('#asset-viewer-fit').click();self.assertEqual(page.locator('#asset-viewer-scale').inner_text(),'100%')
                            page.keyboard.press('Equal');self.assertGreater(int(page.locator('#asset-viewer-scale').inner_text().strip('%')),100)
                            page.keyboard.press('ArrowRight');self.assertEqual(page.locator('#asset-viewer-scale').inner_text(),'100%')
                            self.assertNotEqual(page.locator('#asset-viewer-title').inner_text(),first_name)
                            page.keyboard.press('Escape');self.assertFalse(viewer.evaluate('e=>e.open'));self.assertTrue(first.evaluate('e=>e===document.activeElement'))
                            self.assertEqual(page.locator('#catalog-grid').evaluate('e=>e.scrollTop'),top)
                            first.focus();page.keyboard.press('Enter');self.assertTrue(viewer.evaluate('e=>e.open'),page.evaluate('()=>({workspace:FramekeepWorkspace.current(),active:document.activeElement.outerHTML,errors:[...document.querySelectorAll("dialog[open]")].map(e=>e.id)})'));page.locator('#asset-viewer-close').click()
                            for kind,tab,selector in [('archive','#asset-archive-open','#catalog-grid .catalog-card'),('sku-database','#sku-database-open','#sku-database-grid .catalog-card')]:
                                page.locator(tab).click()
                                if kind=='sku-database':page.locator('#sku-database-search').fill(CODE)
                                if kind=='sku-database':
                                    page.locator(selector).first.click();page.keyboard.press('Space')
                                else:page.locator(selector).first.dblclick()
                                page.wait_for_function('()=>!document.getElementById("asset-viewer-image").hidden&&document.getElementById("asset-viewer-info-status").textContent===""')
                                if kind=='sku-database':
                                    self.assertIn('COTTON',page.locator('.asset-viewer-ocr pre').inner_text());self.assertIn('2026-10-21',page.locator('#asset-viewer-fields').inner_text())
                                    self.assertFalse(page.evaluate('()=>window.ocrInjected===true'))
                                for theme in ['dark','light','pure-white','hardcore']:
                                    page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                                    for width in [1440,1024,375]:
                                        page.set_viewport_size({'width':width,'height':1000})
                                        self.assertFalse(page.evaluate('()=>document.documentElement.scrollWidth>innerWidth'),(kind,theme,width))
                                        self.assertTrue(viewer.evaluate('e=>e.scrollWidth<=e.clientWidth'))
                                        page.screenshot(path=str(out/f'{kind}-{theme}-{width}.png'))
                                page.keyboard.press('Escape');page.set_viewport_size({'width':1440,'height':1000})
                            # 故意让资料响应不理会取消，确认过期响应也不会覆盖新图。
                            page.evaluate('''()=>{window.originalViewerApi=FramekeepIO.api;window.viewerRequests=[];FramekeepIO.api=(path,...args)=>path.startsWith('/api/sku/ocr/details')?new Promise(resolve=>viewerRequests.push({path,resolve})):originalViewerApi(path,...args)}''')
                            page.locator('#sku-database-grid .catalog-card').first.dblclick();page.wait_for_function('()=>viewerRequests.length===1')
                            page.locator('#asset-viewer-next').click();page.wait_for_function('()=>viewerRequests.length===2')
                            new_name=page.locator('#asset-viewer-title').inner_text()
                            page.evaluate('()=>viewerRequests[1].resolve({text:"NEW OCR",selected_sku:"NEW-SKU"})')
                            page.wait_for_function('()=>document.querySelector(".asset-viewer-ocr pre").textContent==="NEW OCR"')
                            page.evaluate('()=>viewerRequests[0].resolve({text:"STALE OCR"})');self.assertEqual(page.locator('#asset-viewer-title').inner_text(),new_name)
                            self.assertEqual(page.locator('.asset-viewer-ocr pre').inner_text(),'NEW OCR')
                            page.locator('#asset-viewer-prev').click();page.wait_for_function('()=>viewerRequests.length===3');page.keyboard.press('Escape')
                            page.evaluate('()=>{viewerRequests[2].resolve({text:"AFTER CLOSE"});FramekeepIO.api=originalViewerApi}')
                            self.assertFalse(viewer.evaluate('e=>e.open'))
                            # 图片和资料都失败时仍可退出，不改动原始记录。
                            page.locator('#asset-archive-open').click()
                            page.route('**/api/catalog/preview/**',lambda route:route.abort() if 'size=420' not in route.request.url else route.continue_())
                            page.evaluate('()=>{FramekeepIO.api=(path,...args)=>path.startsWith("/api/catalog/details/")?Promise.reject(new Error("offline")):originalViewerApi(path,...args)}')
                            page.locator('#catalog-grid .catalog-card').first.dblclick()
                            page.wait_for_function('()=>document.getElementById("asset-viewer-image-status").textContent.includes("无法加载")&&document.getElementById("asset-viewer-info-status").textContent.includes("读取失败")')
                            page.keyboard.press('Escape');self.assertFalse(viewer.evaluate('e=>e.open'));self.assertFalse(errors,errors);self.assertFalse(writes,writes)
                            self.assertEqual(fixture.store.session(fixture.sid)['state'].get('overrides'),original_state.get('overrides'))
                            self.assertEqual({path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in fixture.photos.iterdir()},before)
                            (out/'browser-result.json').write_text(json.dumps({'passed':True,'page_errors':errors,'business_writes':writes,'themes':4,'widths':3,'pages':2,'zoom_pan_esc':True,'stale_response_protected':True,'photos_unchanged':True,'failures_can_close':True},ensure_ascii=False,indent=2),encoding='utf8')
                        finally:browser.close()
                finally:http.shutdown();http.server_close()
        finally:catalog.close();fixture.tearDown()


if __name__=='__main__':unittest.main()
