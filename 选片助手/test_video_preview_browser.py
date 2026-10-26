"""真实视频封面、设置保存和大图浏览的浏览器验收。"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from catalog_store import Store
from model_providers import Providers
from test_video_preview import make_clip

ROOT = Path(__file__).resolve().parents[1]


class VideoPreviewBrowserTests(unittest.TestCase):
    def test_settings_real_video_frame_change_large_preview_and_theme(self):
        import server
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            base=Path(folder); photos=base/'clips'; photos.mkdir(); make_clip(photos/'black-first.mkv')
            store=Store(base/'data',ROOT); catalog=AssetCatalog(store.data); catalog.add_root(str(photos))
            archive=AssetArchive(store,autostart=False);catalog.metadata.start()
            for key,value in dict(STORE=store,DATA=store.data,CATALOG=catalog,ARCHIVE=archive,MODELS=Providers(store.data),
                                  MOUSE=None,WATCH=None,SKU=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,REFERENCES=None).items():
                stack.enter_context(patch.object(server,key,value))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler); http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':950},reduced_motion='reduce'); errors=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                        page.locator('#asset-archive-open').click()
                        page.wait_for_selector('.catalog-thumb[data-preview-state=ready]')
                        def rgb(selector):
                            return page.locator(selector).evaluate("e=>{const c=document.createElement('canvas');c.width=c.height=1;c.getContext('2d').drawImage(e,0,0,1,1);return [...c.getContext('2d').getImageData(0,0,1,1).data].slice(0,3)}")
                        self.assertGreater(rgb('#catalog-grid img')[0],240)
                        page.locator('#app-settings-open').click(); page.locator('[data-settings-section=preview]').click()
                        page.wait_for_function('()=>!document.getElementById("video-preview-frame").disabled')
                        self.assertEqual(page.locator('#video-preview-frame').input_value(),'3')
                        self.assertIn('已就绪',page.locator('#video-preview-status').inner_text())
                        page.locator('#video-preview-frame').fill('4');page.locator('#video-preview-save').click()
                        page.wait_for_function('()=>document.getElementById("video-preview-message").textContent.includes("已保存")')
                        self.assertEqual(catalog.video.frame,4)
                        out=ROOT/'build/video-preview';out.mkdir(parents=True,exist_ok=True)
                        for theme in ('dark','light','pure-white','hardcore'):
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                            page.screenshot(path=str(out/f'settings-{theme}.png'))
                            page.set_viewport_size({'width':375,'height':950});self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                            page.set_viewport_size({'width':1440,'height':950})
                        page.locator('#asset-archive-open').click()
                        page.wait_for_function('()=>document.querySelector("#catalog-grid img")?.getAttribute("src")?.includes("frame=4")')
                        page.wait_for_selector('.catalog-thumb[data-preview-state=ready]')
                        self.assertIn('frame=4',page.locator('#catalog-grid img').get_attribute('src'))
                        self.assertGreater(rgb('#catalog-grid img')[1],240)
                        page.locator('#catalog-grid .catalog-card').dblclick()
                        page.wait_for_function('()=>document.getElementById("asset-viewer-video")?.readyState>=2')
                        self.assertEqual(page.locator('#asset-viewer-video').evaluate('e=>e.videoWidth'),256)
                        page.wait_for_function('()=>document.getElementById("asset-viewer-info-status").textContent===""')
                        self.assertIn('视频编码',page.locator('#asset-viewer-fields').inner_text())
                        page.screenshot(path=str(out/'video-playback-large.png'))
                        page.keyboard.press('Escape');self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:catalog.close();http.shutdown();http.server_close();archive.close()


if __name__=='__main__':unittest.main()
