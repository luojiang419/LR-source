"""表格线框设置：真实表格、示例预览、持久化和四主题隔离验收。"""
from contextlib import ExitStack
import copy
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
import test_document_reconciliation as document_fixture
from test_ocr_metadata import ROOT


class TableDisplayTests(unittest.TestCase):
    def test_grid_preview_settings_persistence_themes_and_narrow_layout(self):
        import server
        fixture=document_fixture.DocumentTests();fixture.setUp()
        try:
            fixture.parse();fixture.choose();fixture.drain()
            original=copy.deepcopy(fixture.store.session(fixture.sid)['state']['overrides'])
            values=dict(STORE=fixture.store,DATA=fixture.store.data,MODELS=fixture.providers,SKU=fixture.sku,ARCHIVE=fixture.archive,
                        CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,CANON=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None)
            with ExitStack() as stack:
                for key,value in values.items():stack.enter_context(patch.object(server,key,value))
                http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
                threading.Thread(target=http.serve_forever,daemon=True).start()
                try:
                    with sync_playwright() as pw:
                        browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                        try:
                            page=browser.new_page(viewport={'width':1440,'height':900},reduced_motion='reduce');errors=[]
                            page.on('pageerror',lambda e:errors.append(str(e)))
                            out=ROOT/'build/table-display-acceptance';out.mkdir(parents=True,exist_ok=True)
                            page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='networkidle')
                            page.locator('#sku-open').click();page.wait_for_selector('#sku-rows tbody td')
                            def grid():
                                return page.evaluate('''()=>['#sku-rows','#sku-current-table','#table-display-preview'].map(selector=>{const table=document.querySelector(selector+' table'),cell=table.querySelector('tbody td'),style=getComputedStyle(cell),box=getComputedStyle(table);return {color:style.borderRightColor,bottom:style.borderBottomColor,width:style.borderRightWidth,layout:box.borderCollapse,background:style.backgroundColor,text:style.color,opacity:style.opacity}})''')
                            def parity():
                                styles=grid()
                                for style in styles:
                                    self.assertEqual(style['color'],styles[0]['color']);self.assertEqual(style['bottom'],styles[0]['color'])
                                    self.assertEqual(style['width'],'1px');self.assertEqual(style['layout'],'separate');self.assertEqual(style['opacity'],'1')
                                return styles
                            self.assertEqual(page.locator('#table-lines-transparency').input_value(),'78');self.assertTrue(page.locator('#table-lines-visible').is_checked());parity()
                            page.screenshot(path=str(out/'progress-default.png'))
                            page.locator('#app-settings-open').click();page.locator('[data-settings-section="table"]').click()
                            switch=page.locator('#table-lines-visible').bounding_box();self.assertEqual(switch['width'],36);self.assertEqual(switch['height'],20)
                            baseline=grid();colors=[]
                            for percent in [0,45,78,100]:
                                page.locator('#table-lines-transparency').fill(str(percent))
                                self.assertEqual(page.locator('#table-lines-transparency-value').inner_text(),str(percent)+'%')
                                self.assertIn(str(percent)+'%',page.locator('#table-display-summary').inner_text())
                                styles=parity();colors.append(styles[0]['color'])
                                self.assertEqual([s['text'] for s in styles],[s['text'] for s in baseline]);self.assertEqual([s['background'] for s in styles],[s['background'] for s in baseline])
                                page.screenshot(path=str(out/f'settings-{percent}.png'))
                            self.assertEqual(len(set(colors)),4)
                            page.locator('#table-lines-transparency').fill('65');page.locator('#table-lines-visible').uncheck()
                            self.assertEqual(grid()[0]['color'],colors[-1]);self.assertIn('已隐藏',page.locator('#table-display-summary').inner_text())
                            page.reload(wait_until='networkidle');self.assertFalse(page.locator('#table-lines-visible').is_checked());self.assertEqual(page.locator('#table-lines-transparency').input_value(),'65')
                            page.locator('#app-settings-open').click();page.locator('[data-settings-section="table"]').click();page.locator('#table-lines-visible').check()
                            page.locator('#table-lines-transparency').press('Home');page.locator('#table-lines-transparency').press('ArrowRight');self.assertEqual(page.locator('#table-lines-transparency-value').inner_text(),'1%')
                            # 真实拖动滑杆，随后确认重载后保存的是最终值。
                            box=page.locator('#table-lines-transparency').bounding_box();page.mouse.move(box['x']+box['width']*.55,box['y']+box['height']/2);page.mouse.down();page.mouse.move(box['x']+box['width']*.7,box['y']+box['height']/2,steps=4);page.mouse.up()
                            saved=page.locator('#table-lines-transparency').input_value();self.assertGreater(int(saved),60)
                            page.reload(wait_until='networkidle');self.assertEqual(page.locator('#table-lines-transparency').input_value(),saved);self.assertTrue(page.locator('#table-lines-visible').is_checked())
                            page.locator('#sku-open').click();page.wait_for_selector('#sku-rows tbody td')
                            for theme in ['dark','light','pure-white','hardcore']:
                                page.evaluate('(t)=>FramekeepTheme.set(t)',theme)
                                for width in [1440,1024,375]:
                                    page.set_viewport_size({'width':width,'height':900})
                                    page.locator('#app-settings-open').click();page.locator('[data-settings-section="table"]').click()
                                    page.locator('#table-lines-transparency').fill('78');parity()
                                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                                    if width in [1440,375]:page.screenshot(path=str(out/f'settings-{theme}-{width}.png'))
                                    page.locator('#sku-open').click();page.wait_for_selector('#sku-rows tbody td');parity()
                                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                                    if width==1440:page.screenshot(path=str(out/f'progress-{theme}.png'))
                            page.set_viewport_size({'width':1440,'height':900});page.locator('#sku-rows [data-completion]').first.click()
                            page.wait_for_function('()=>!document.querySelector("#sku-image-stage img").hidden');page.keyboard.press('Escape');self.assertFalse(page.locator('.sku-image-dialog').evaluate('e=>e.open'))
                            for invalid in ['{broken',json.dumps({'visible':'false','transparency':101}),json.dumps({'visible':False,'transparency':'45'})]:
                                page.evaluate('(value)=>localStorage.setItem("framekeep-table-display-v1",value)',invalid);page.reload(wait_until='networkidle')
                                self.assertEqual(page.locator('#table-lines-transparency').input_value(),'78')
                            self.assertFalse(page.locator('#table-lines-visible').is_checked())
                            page.evaluate('localStorage.removeItem("framekeep-table-display-v1")');page.reload(wait_until='networkidle')
                            self.assertTrue(page.locator('#table-lines-visible').is_checked());self.assertEqual(page.locator('#table-lines-transparency').input_value(),'78')
                            self.assertEqual(fixture.store.session(fixture.sid)['state']['overrides'],original);self.assertFalse(errors,errors)
                            (out/'browser-result.json').write_text(json.dumps({'passed':True,'errors':errors,'themes':4,'widths':[1440,1024,375],'checks':['grid and preview parity','line-only transparency','toggle and reload','slider mouse and keyboard','invalid storage fallback','image modal','ratings preserved']},ensure_ascii=False,indent=2),encoding='utf8')
                        finally:browser.close()
                finally:http.shutdown();http.server_close()
        finally:fixture.tearDown()


if __name__=='__main__':unittest.main()
