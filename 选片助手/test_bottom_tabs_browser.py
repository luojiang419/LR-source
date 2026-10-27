"""隔离目录验证底部六页签占位、左侧照片浏览及旧胶片条偏好兼容。"""
from http.server import ThreadingHTTPServer
from pathlib import Path
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import Store
from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from asset_analysis import AssetAnalysis
from sku_service import SkuService
from model_providers import Providers
from vision_scheduler import VisionScheduler
import media_engine as media

ROOT=Path(__file__).resolve().parents[1]


class BottomTabsTests(unittest.TestCase):
    def test_six_tabs_geometry_sidebar_selection_and_folder_switch(self):
        import server
        out=ROOT/'build/bottom-tabs';out.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);store=Store(base/'data',ROOT);sessions=[];ids=[]
            for name in ['普通选片','切换目录']:
                photos=base/name;photos.mkdir();sid=store.ensure_live_session(photos);sessions.append(sid)
                for i in range(8):
                    path=photos/f'{i:03}.JPG';w,h=(900,600) if i%2==0 else (600,900)
                    Image.new('RGB',(w,h),(65+i*14,100,130)).save(path)
                    record=media.capture_record(photos,path,None,{'ImageWidth':w,'ImageHeight':h,'DateTimeOriginal':f'2026:10:20 10:00:0{i}'})
                    store.append_live(sid,[record])
                    if name=='普通选片':ids.append(record['id'])
            store.activate(sessions[0]);providers=Providers(store.data);archive=AssetArchive(store,autostart=False);scheduler=VisionScheduler()
            sku=SkuService(store,providers,autostart=False,archive=archive,scheduler=scheduler)
            analysis=AssetAnalysis(archive,providers,scheduler,autostart=False)
            patches=[patch.object(server,k,v) for k,v in dict(STORE=store,DATA=store.data,WATCH=None,MODELS=providers,MOUSE=None,SKU=sku,ARCHIVE=archive,CATALOG=AssetCatalog(store.data),ANALYSIS=analysis,RAWLINK=None,FILESEARCH=None).items()]
            for p in patches:p.start()
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1680,'height':1050},reduced_motion='reduce');errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.add_init_script("localStorage.setItem('framekeep-layout-v1',JSON.stringify({selection:{header:true,left:false,right:false,tools:true,film:true,sections:{}},monitor:{header:true,left:false,right:false,tools:false,film:true,sections:{}}}))")
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>window.FramekeepBoot?.loading===false')
                        tabs=['selection-tab','asset-retrieval-open','sku-open','asset-archive-open','sku-database-open','asset-analysis-open','app-settings-open']
                        panels=['selection-page','asset-retrieval-page','sku-dialog','asset-archive-dialog','sku-database-page','analysis-dialog','app-settings-page']
                        self.assertEqual(page.locator('.workspace-bottom-bar [role=tab]').evaluate_all('(nodes)=>nodes.map(n=>n.id)'),tabs)
                        self.assertEqual(page.locator('.app-header .workspace-tabs,.film-panel,#filmstrip,#layout-film').count(),0)
                        self.assertTrue(page.locator('#watch-thumbnails').is_visible());self.assertTrue(page.locator('#watch-follow-toggle').is_hidden())
                        self.assertIn('照片浏览',page.locator('.watch-browser-heading').inner_text())
                        # 左侧在普通模式中承接定位、排序、过滤及备案多选，评级与草稿在切页后保留。
                        page.locator(f'[data-watch-photo="{ids[0]}"]').click();self.assertEqual(page.locator('#filename').inner_text(),'000.JPG')
                        page.locator(f'[data-watch-photo="{ids[1]}"]').click(modifiers=['Control'])
                        self.assertEqual(page.evaluate('FramekeepAssets.selection()'),ids[:2])
                        page.locator(f'[data-watch-photo="{ids[2]}"]').click(modifiers=['Shift'])
                        self.assertEqual(page.evaluate('FramekeepAssets.selection()'),ids[1:3])
                        page.locator('#stars [data-rating="4"]').click()
                        selected=page.evaluate('Framekeep.remoteTarget().photoId')
                        page.locator('#sort').select_option('time-desc');page.locator('#search').fill('002')
                        self.assertEqual(page.locator('[data-watch-photo]').count(),1)
                        page.locator('#search').fill('');self.assertEqual(page.evaluate('Framekeep.photoItems()[0].photo.jpg'),'007.JPG')
                        page.locator('#asset-sku').fill('DRAFT-033')
                        page.locator('#selection-tab').press('End');self.assertTrue(page.locator('#app-settings-page').is_visible())
                        page.locator('#app-settings-open').press('ArrowLeft');self.assertTrue(page.locator('#analysis-dialog').is_visible())
                        page.locator('#asset-analysis-open').press('Home');self.assertTrue(page.locator('#selection-page').is_visible())
                        self.assertEqual(page.locator('#asset-sku').input_value(),'DRAFT-033');self.assertEqual(page.evaluate('Framekeep.remoteTarget().photoId'),selected)
                        self.assertEqual(page.locator('#stars .on').count(),4)
                        checks=[]
                        for theme in ['dark','light','pure-white','hardcore']:
                            page.evaluate('(t)=>FramekeepTheme.set(t)',theme)
                            for width,height in [(1920,1080),(1280,720),(1024,600),(1000,480),(375,812),(320,568)]:
                                page.set_viewport_size({'width':width,'height':height})
                                for tab,panel in zip(tabs,panels):
                                    page.locator('#'+tab).click();self.assertTrue(page.locator('#'+panel).is_visible())
                                    geometry=page.evaluate('''()=>{const nav=document.querySelector('.workspace-bottom-bar').getBoundingClientRect(),panel=document.querySelector('.workbench>[role=tabpanel]:not(.hidden)').getBoundingClientRect();return {top:nav.top,bottom:nav.bottom,panelBottom:panel.bottom,width:nav.width,overflow:document.documentElement.scrollWidth>innerWidth,selected:document.querySelectorAll('.workspace-tabs>[aria-selected=true]').length,icons:document.querySelectorAll('.workspace-tabs>[role=tab]>svg').length}}''')
                                    self.assertAlmostEqual(geometry['bottom'],height,delta=1,msg=(theme,width,tab,geometry))
                                    self.assertAlmostEqual(geometry['width'],width,delta=1)
                                    self.assertLessEqual(geometry['panelBottom'],geometry['top']+1)
                                    self.assertFalse(geometry['overflow'],(theme,width,tab));self.assertEqual(geometry['selected'],1);self.assertEqual(geometry['icons'],len(tabs))
                                    checks.append([theme,width,height,tab,geometry])
                                if width in (1920,320):
                                    page.locator('#selection-tab').click();page.locator('#photo-settings-panel').evaluate('(e)=>e.scrollTop=0');page.screenshot(path=str(out/f'{theme}-{width}.png'))
                        # 顶部隐藏及专注预览均保留主导航，目录切换不会残留旧目录缩略图。
                        page.set_viewport_size({'width':1680,'height':1050});page.locator('#selection-tab').click()
                        page.locator('#view-open').hover();page.locator('#layout-header').click()
                        self.assertTrue(page.locator('.workspace-bottom-bar').is_visible())
                        page.locator('#sku-open').click();page.locator('#selection-tab').click()
                        page.locator('#view-open').hover();page.locator('#layout-header').click()
                        page.locator('#view-open').hover();page.locator('#layout-focus').click()
                        self.assertTrue(page.locator('.workspace-bottom-bar').is_visible())
                        page.locator('#view-open').hover();page.locator('#layout-focus').click()
                        page.evaluate('(sid)=>FramekeepIO.openSession(sid)',sessions[1])
                        page.wait_for_function('(sid)=>FRAMEKEEP_SESSION.id===sid',arg=sessions[1])
                        page.wait_for_function('()=>document.getElementById("watch-thumbnails").scrollTop<1')
                        self.assertEqual(page.locator('.film-panel,#filmstrip,#layout-film').count(),0);self.assertFalse(errors,errors)
                        (out/'result.json').write_text(json.dumps({'passed':True,'checks':checks,'page_errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
                    finally:browser.close()
            finally:
                http.shutdown();http.server_close();analysis.close();sku.close();archive.close()
                for p in reversed(patches):p.stop()


if __name__=='__main__':unittest.main()
