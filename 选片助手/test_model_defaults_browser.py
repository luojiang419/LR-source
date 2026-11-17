"""默认模型下拉、卡片选用、持久化与内置模式的真实浏览器联动。"""
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
from catalog_store import Store
from model_providers import Providers

ROOT=Path(__file__).resolve().parents[1]


class ModelDefaultsBrowserTests(unittest.TestCase):
    def test_defaults_cards_failure_reload_builtin_and_visuals(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'data',ROOT);providers=Providers(store.data)
            cards={}
            for category,name in [('multimodal','视觉 A'),('multimodal','视觉 B'),('ocr','OCR A'),('text','文本 A')]:
                cards[name]=providers.save({'name':name,'model':name+'-model','category':category,'protocol':'chat','url':'https://example.invalid/v1','select':name!='视觉 B'})['providers'][-1]['id']
            patches=[patch.object(server,k,v) for k,v in {'STORE':store,'DATA':store.data,'MODELS':providers,'MOUSE':None,'WATCH':None,'SKU':None,'ARCHIVE':None,'ANALYSIS':None,'RAWLINK':None,'FILESEARCH':None,'CATALOG':None,'UPDATES':None}.items()]
            for item in patches:item.start()
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':1100});errors=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        def open_settings():
                            page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                            page.wait_for_function('()=>window.FramekeepBoot?.loading===false')
                            page.locator('#app-settings-open').click();page.locator('[data-settings-section=models]').click();page.locator('#assistant-settings').click()
                            page.wait_for_function('()=>getComputedStyle(document.getElementById("assistant-settings-dialog")).opacity==="1"')
                        def selected(name):return page.locator(f'[data-model-card="{cards[name]}"]')
                        open_settings()
                        expect(page.locator('#model-default-multimodal')).to_have_value(cards['视觉 A'])
                        # 类别筛选不改变下拉候选；下拉切换只影响对应类别。
                        page.locator('[data-model-category=ocr]').click()
                        page.locator('#model-default-multimodal').select_option(cards['视觉 B'])
                        expect(page.locator('#model-message')).to_have_text('多模态默认模型已保存')
                        self.assertEqual(Providers(store.data).public()['selected'],{'multimodal':cards['视觉 B'],'ocr':cards['OCR A'],'text':cards['文本 A']})
                        page.locator('[data-model-category=all]').click()
                        expect(selected('视觉 B')).to_have_class('model-card selected')
                        expect(selected('视觉 A')).to_have_attribute('aria-pressed','false')
                        selected('视觉 A').press('Enter')
                        expect(page.locator('#model-default-multimodal')).to_have_value(cards['视觉 A'])
                        expect(page.locator('#model-default-multimodal')).to_be_enabled()
                        # 保存失败必须恢复下拉，卡片继续显示实际生效的默认项。
                        page.route('**/api/providers/select',lambda route:route.fulfill(status=400,json={'error':'测试保存失败'}))
                        page.locator('#model-default-multimodal').select_option(cards['视觉 B'])
                        expect(page.locator('#model-message')).to_contain_text('测试保存失败')
                        expect(page.locator('#model-default-multimodal')).to_have_value(cards['视觉 A'])
                        expect(selected('视觉 A')).to_have_attribute('aria-pressed','true')
                        page.unroute('**/api/providers/select')
                        open_settings()
                        expect(page.locator('#model-default-multimodal')).to_have_value(cards['视觉 A'])
                        # 四个主题均保持边框、静态选中点和呼吸微光，减少动态效果时只保留静态高亮。
                        out=ROOT/'build/model-defaults-acceptance';out.mkdir(parents=True,exist_ok=True)
                        for theme in ('dark','light','pure-white','hardcore'):
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                            self.assertEqual(selected('视觉 A').evaluate('e=>getComputedStyle(e,"::before").animationName'),'model-selected-glow')
                            page.wait_for_function('(id)=>{const e=document.querySelector(`[data-model-card="${id}"]`);return getComputedStyle(e).borderTopColor===getComputedStyle(e.querySelector("footer>span")).color}',arg=cards['视觉 A'])
                            self.assertNotEqual(selected('视觉 B').evaluate('e=>getComputedStyle(e,"::before").animationName'),'model-selected-glow')
                            page.screenshot(path=str(out/f'{theme}.png'),animations='disabled')
                        page.emulate_media(reduced_motion='reduce')
                        self.assertEqual(selected('视觉 A').evaluate('e=>getComputedStyle(e,"::before").animationName'),'none')
                        for width in (1440,768,390):
                            page.set_viewport_size({'width':width,'height':1000})
                            self.assertFalse(page.locator('#assistant-settings-dialog').evaluate('e=>e.scrollWidth>e.clientWidth'))
                            if width==390:page.screenshot(path=str(out/'mobile.png'),animations='disabled')
                        page.set_viewport_size({'width':1440,'height':1100})
                        with patch('local_inference.preload',return_value={'status':'ready'}):
                            page.locator('#model-builtin-enabled').check()
                            expect(page.locator('#model-default-ocr')).to_be_disabled()
                            expect(page.locator('#model-default-ocr')).to_have_value(providers.public()['selected']['ocr'])
                            for pid in providers.public()['selected'].values():expect(page.locator(f'[data-model-card="{pid}"]')).to_have_attribute('aria-pressed','true')
                            page.locator('#model-builtin-enabled').uncheck()
                            expect(page.locator('#model-default-ocr')).to_be_enabled()
                            expect(page.locator('#model-default-multimodal')).to_have_value(cards['视觉 A'])
                        # 删除当前项后留空，不显示已经失效的默认项；空类别禁用。
                        page.locator(f'[data-model-delete="{cards["文本 A"]}"]').click()
                        expect(page.locator('#model-default-text')).to_be_disabled()
                        expect(page.locator('#model-default-text')).to_have_value('')
                        self.assertNotIn('text',Providers(store.data).public()['selected'])
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:
                http.shutdown();http.server_close()
                for item in reversed(patches):item.stop()


if __name__=='__main__':unittest.main()
