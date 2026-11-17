"""移除解析任务页后，旧偏好、导航和共享模型配置仍可正常工作。"""
from pathlib import Path
import unittest
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect
import test_asset_retrieval_browser as fixture


class RemovedParsingPageTests(unittest.TestCase):
    setUp = fixture.RetrievalBrowserTests.setUp
    tearDown = fixture.RetrievalBrowserTests.tearDown

    def test_old_workspace_navigation_models_and_no_task_polling(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=True)
            try:
                page = browser.new_page(viewport={'width':1440,'height':960}, reduced_motion='reduce')
                errors, requests = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: requests.append(request.url))
                page.add_init_script("localStorage.setItem('framekeep.last-workspace','analysis')")
                page.goto(self.url, wait_until='domcontentloaded')
                page.wait_for_function('()=>FramekeepBoot.loading===false')
                expect(page.locator('#selection-page')).to_be_visible()
                self.assertEqual(page.locator('#asset-analysis-open,#analysis-dialog').count(),0)
                tabs = page.locator('.workspace-bottom-bar [role=tab]')
                for tab in tabs.all():
                    tab.click()
                    panel = page.locator('#'+tab.get_attribute('aria-controls'))
                    expect(panel).to_be_visible()
                page.locator('#app-settings-open').click()
                page.locator('[data-settings-section=models]').click()
                page.locator('#full-auto-archive-manage').click()
                expect(page.locator('#catalog-analysis-dialog')).to_be_visible()
                page.locator('#catalog-analysis-close').click()
                page.locator('#assistant-settings').click()
                expect(page.locator('#analysis-provider')).to_be_visible()
                page.locator('#models-close').click()
                expect(page.locator('#assistant-settings-dialog')).not_to_be_visible()
                out = Path(__file__).resolve().parents[1]/'build/remove-parsing-page'
                out.mkdir(parents=True,exist_ok=True)
                for theme in ('dark','light','pure-white','hardcore'):
                    page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                    for width in (1440,375):
                        page.set_viewport_size({'width':width,'height':960})
                        self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    page.set_viewport_size({'width':1440,'height':960})
                    page.screenshot(path=str(out/(theme+'.png')))
                page.wait_for_timeout(1400)
                self.assertFalse([url for url in requests if urlsplit(url).path in ('/api/parsing','/asset-analysis.js')])
                self.assertFalse(errors,errors)
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
