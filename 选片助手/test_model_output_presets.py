"""最大输出预设、自定义数值与已有模型配置的真实浏览器往返。"""
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import unittest
from playwright.sync_api import sync_playwright
import test_asset_retrieval_browser as fixture
from test_asset_analysis import LocalVision

ROOT=Path(__file__).resolve().parents[1]


class ModelOutputPresetTests(unittest.TestCase):
    setUp=fixture.RetrievalBrowserTests.setUp
    tearDown=fixture.RetrievalBrowserTests.tearDown

    def test_presets_custom_values_and_saved_budget_round_trip(self):
        endpoint=ThreadingHTTPServer(('127.0.0.1',0),LocalVision);endpoint.daemon_threads=True
        threading.Thread(target=endpoint.serve_forever,daemon=True).start()
        card=self.providers.save({'name':'视觉旧配置','category':'multimodal','protocol':'chat','url':f'http://127.0.0.1:{endpoint.server_port}/v1','model':'qwen-local-test','max_tokens':2000})['providers'][-1]
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel='msedge',headless=True)
                try:
                    page=browser.new_page(viewport={'width':1440,'height':960})
                    page.goto(self.url,wait_until='domcontentloaded');page.locator('#app-settings-open').click();page.locator('[data-settings-section=models]').click();page.locator('#assistant-settings').click()
                    page.locator(f'[data-model-edit="{card["id"]}"]').click()
                    page.locator('#model-max-tokens').evaluate('e=>e.closest("details").open=true')
                    self.assertEqual(page.locator('#model-max-tokens').input_value(),'2000')
                    self.assertEqual(page.locator('#model-max-tokens-preset').input_value(),'custom')
                    page.locator('#model-max-tokens-preset').select_option('4096');self.assertEqual(page.locator('#model-max-tokens').input_value(),'4096')
                    page.locator('#model-max-tokens').fill('6000');self.assertEqual(page.locator('#model-max-tokens-preset').input_value(),'custom')
                    page.locator('#model-max-tokens-preset').select_option('8192');page.locator('#model-save').click()
                    page.wait_for_selector('#model-catalog:not(.hidden)')
                    self.assertEqual(self.providers.by_id(card['id'])['max_tokens'],8192)
                    self.assertNotIn('context_length',self.providers.by_id(card['id']))
                    page.locator(f'[data-model-edit="{card["id"]}"]').click()
                    self.assertEqual(page.locator('#model-max-tokens').input_value(),'8192')
                    self.assertEqual(page.locator('#model-max-tokens-preset').input_value(),'8192')
                    page.locator('#model-max-tokens').fill('9000');self.assertFalse(page.locator('#model-max-tokens').evaluate('e=>e.checkValidity()'))
                    page.locator('#model-max-tokens-preset').select_option('4096')
                    out=ROOT/'build/archive-analysis-pipeline';out.mkdir(exist_ok=True);page.screenshot(path=str(out/'output-token-presets.png'))
                finally:browser.close()
        finally:endpoint.shutdown();endpoint.server_close()


if __name__=='__main__':unittest.main()
