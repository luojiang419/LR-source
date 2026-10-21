from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from catalog_store import Store
from model_providers import Providers


class BuiltinSwitchBrowserTests(unittest.TestCase):
    def test_toggle_locks_all_cards_and_restores_external_selection(self):
        from playwright.sync_api import sync_playwright
        import server
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'data',root);providers=Providers(store.data)
            for model in ('external-a','external-b'):
                providers.save({'name':model,'model':model,'category':'multimodal','protocol':'chat','url':'https://example.invalid/v1','select':model=='external-a'})
            original=providers.public()['selected'].copy();remote=providers.public()['providers'][-1]['id']
            legacy=providers.save({'name':'原内置 Qwen','model':'qwen2.5-vl-7b-q4','category':'multimodal','protocol':'local'})['providers'][-1]['id']
            patches=[patch.object(server,k,v) for k,v in [('STORE',store),('DATA',store.data),('MODELS',providers),('MOUSE',None),('WATCH',None),('SKU',None),('ARCHIVE',None),('ANALYSIS',None),('RAWLINK',None),('FILESEARCH',None),('CATALOG',None),('UPDATES',None)]]
            for item in patches:item.start()
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':1100});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='networkidle')
                        page.locator('#app-settings-open').click();page.locator('#assistant-settings').click()
                        page.locator('#model-builtin-enabled').check()
                        page.wait_for_function('()=>document.querySelectorAll("[data-model-card][aria-disabled=true]").length===4')
                        self.assertEqual(page.locator('[data-model-card][aria-disabled=true]').count(),4)
                        self.assertEqual(page.locator('[data-model-card][tabindex="-1"]').count(),4)
                        for selector in ['#model-add','[data-model-edit]','[data-model-delete]','#analysis-provider','#analysis-bind-provider','#model-lm-preset','#model-ocr-preset']:
                            self.assertTrue(page.locator(selector).first.is_disabled(),selector)
                        page.locator(f'[data-model-card="{remote}"]').evaluate('(e)=>{e.click();e.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true}))}')
                        self.assertEqual(providers.data['selected'],original)
                        self.assertEqual(providers.selected('ocr')['protocol'],'local')
                        self.assertIsNone(providers.purpose('asset_analysis'));self.assertIsNone(providers.selected('multimodal'))
                        self.assertIn('多模态预留',page.locator('#model-builtin-status').inner_text())
                        out=root/'build/local-model-validation/switch';out.mkdir(parents=True,exist_ok=True)
                        page.wait_for_timeout(450);page.screenshot(path=str(out/'enabled.png'))
                        page.reload(wait_until='networkidle');page.locator('#app-settings-open').click();page.locator('#assistant-settings').click()
                        page.wait_for_selector('[data-model-card][aria-disabled=true]');self.assertTrue(page.locator('#model-builtin-enabled').is_checked())
                        page.locator('#model-builtin-enabled').uncheck();page.wait_for_selector('[data-model-card][aria-disabled=false]')
                        self.assertTrue(page.locator('#model-lm-preset').is_disabled())
                        reserved=page.locator(f'[data-model-card="{legacy}"]');self.assertEqual(reserved.get_attribute('aria-disabled'),'true');self.assertIn('后续开发预留',reserved.inner_text())
                        reserved.evaluate('(e)=>e.click()');self.assertEqual(providers.selected('multimodal')['model'],'external-a')
                        self.assertEqual(page.locator(f'#analysis-provider option[value="{legacy}"]').count(),0)
                        self.assertEqual(providers.public()['selected'],original)
                        page.locator(f'[data-model-card="{remote}"]').click()
                        page.wait_for_function('(id)=>document.querySelector(`[data-model-card="${id}"]`).getAttribute("aria-pressed")==="true"',arg=remote)
                        self.assertEqual(providers.selected('multimodal')['model'],'external-b')
                        self.assertFalse(page.locator('#model-add').is_disabled());page.screenshot(path=str(out/'disabled.png'))
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:
                http.shutdown();http.server_close()
                for item in reversed(patches):item.stop()


if __name__=='__main__':unittest.main()
