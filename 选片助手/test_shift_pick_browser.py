"""真实鼠标滑选：隔离照片、评级保存、整批撤销和原备案范围选择。"""
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import Store
from asset_catalog import AssetCatalog
from model_providers import Providers
import media_engine as media

ROOT=Path(__file__).resolve().parents[1]


class ShiftPickTests(unittest.TestCase):
    def test_shift_hover_drag_cancel_filter_scroll_and_save(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir();store=Store(base/'data',ROOT)
            sid=store.ensure_live_session(photos);ids=[]
            for i in range(30):
                path=photos/f'{i:03}.JPG';Image.new('RGB',(300,200),(70+i*4,120,160)).save(path)
                record=media.capture_record(photos,path,None,{});store.append_live(sid,[record]);ids.append(record['id'])
            store.activate(sid);store.save(sid,0,{'overrides':{ids[1]:4,ids[3]:2},'skuMarkers':{ids[3]:{'ignore':True}}})
            patches=[patch.object(server,k,v) for k,v in [('STORE',store),('DATA',store.data),('MODELS',Providers(store.data)),('WATCH',None),('MOUSE',None),('SKU',None),('ARCHIVE',None),('CATALOG',AssetCatalog(store.data)),('ANALYSIS',None),('RAWLINK',None),('FILESEARCH',None)]]
            for p in patches:p.start()
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1920,'height':1080},reduced_motion='reduce');errors=[]
                        page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                        def ratings():return page.evaluate('()=>Framekeep.photoItems().map(i=>i.rating)')
                        def point(i,cover=False):
                            box=page.locator(f'#photo-grid [data-focus="{ids[i]}"]' if cover else f'[data-pick="{ids[i]}"]').bounding_box()
                            return (box['x']+box['width']/2,box['y']+box['height']/2)
                        def start(i,drag=False,cover=False):
                            page.mouse.move(*point(i,cover));page.keyboard.down('Shift')
                            if drag:page.mouse.down()
                        def slide(i,steps=1,cover=False):page.mouse.move(*point(i,cover),steps=steps)
                        def end(drag=False):
                            if drag:page.mouse.up()
                            page.keyboard.up('Shift')
                        def undo():
                            page.locator('#catalog-view').focus();page.keyboard.press('Control+z')
                        original=ratings();self.assertEqual(original[:5],[0,4,0,2,0])
                        # 悬停滑选无需按鼠标，高速线段覆盖中间照片，高星不降级，回划不反复切换。
                        start(0);slide(3);slide(0);self.assertEqual(ratings()[:4],[1,4,1,2])
                        self.assertEqual(page.locator('#filter-picked-count').inner_text(),'4')
                        self.assertEqual(page.locator(f'[data-pick="{ids[0]}"]').is_checked(),True)
                        end();page.evaluate('()=>FramekeepIO.flush()')
                        self.assertEqual(store.session(sid)['state']['overrides'][ids[0]],1)
                        undo();self.assertEqual(ratings(),original)
                        # 从已选起滑整批取消，包括 SKU 标记；一次撤销恢复星级及 SKU 上下文。
                        start(1);slide(3);end();self.assertEqual(ratings()[:4],[0,0,0,0])
                        self.assertNotIn(ids[3],page.evaluate('()=>Framekeep.persistedState().skuMarkers'))
                        page.evaluate('()=>FramekeepIO.flush()');self.assertEqual(store.session(sid)['state']['overrides'][ids[3]],0)
                        undo();self.assertEqual(ratings(),original)
                        self.assertIn(ids[3],page.evaluate('()=>Framekeep.persistedState().skuMarkers'))
                        # Shift 左键拖选；mouseup 生成的 click 不额外反转复选框。
                        start(0,True,True);slide(2,steps=6,cover=True);end(True);self.assertEqual(ratings()[:4],[1,4,1,2])
                        undo();self.assertEqual(ratings(),original)
                        # Escape 和窗口失焦分别取消/提交，不留下未结束手势。
                        start(0);slide(2);page.keyboard.press('Escape');page.keyboard.up('Shift')
                        self.assertEqual(ratings(),original)
                        start(0);slide(2);page.evaluate('()=>window.dispatchEvent(new Event("blur"))');page.keyboard.up('Shift')
                        self.assertEqual(ratings()[:3],[1,4,1]);undo();self.assertEqual(ratings(),original)
                        # 普通悬停和 Shift 仅按下松开不能修改；单击仍维持备案范围选择。
                        slide(0);slide(2);page.keyboard.down('Shift');page.keyboard.up('Shift');self.assertEqual(ratings(),original)
                        page.locator('#photo-grid [data-focus]').nth(0).click()
                        page.locator('#photo-grid [data-focus]').nth(3).click(modifiers=['Shift'])
                        self.assertEqual(page.locator('#asset-count').inner_text(),'4 张');self.assertEqual(ratings(),original)
                        # 四主题保持同一交互；临时框选样式退出后清除。
                        for theme in ['light','dark','pure-white','hardcore']:
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                            start(0);slide(2);end();self.assertEqual(ratings()[:3],[1,4,1]);undo()
                            self.assertEqual(ratings(),original)
                            self.assertIsNone(page.locator('#catalog-view').get_attribute('data-pick-stroke'))
                        page.evaluate('()=>FramekeepTheme.set("light")')
                        # 已选筛选中延迟排布更新，避免第一张消失导致下一张被误划。
                        page.locator('[data-filter="picked"]').click();start(1);slide(3)
                        self.assertEqual(page.locator('#photo-grid [data-photo]').count(),2)
                        end();self.assertEqual(page.locator('#photo-grid [data-photo]').count(),0)
                        undo();self.assertEqual(ratings(),original)
                        page.locator('[data-filter="all"]').click()
                        # 列表滑选以及滚轮移入的新行。
                        page.locator('[data-view="list"]').click();start(0);slide(2,steps=5)
                        self.assertEqual(ratings()[:3],[1,4,1])
                        before=set(page.locator('[data-pick]:checked').evaluate_all('(nodes)=>nodes.map(n=>n.dataset.pick)'))
                        page.mouse.wheel(0,450);page.wait_for_timeout(200)
                        after=set(page.locator('[data-pick]:checked').evaluate_all('(nodes)=>nodes.map(n=>n.dataset.pick)'))
                        self.assertGreater(len(after),len(before));end();undo();self.assertEqual(ratings(),original)
                        page.locator('#catalog-view').evaluate('(e)=>e.scrollTop=0');page.wait_for_timeout(100)
                        start(0);page.mouse.wheel(0,450);page.wait_for_timeout(200)
                        self.assertEqual(ratings()[0],1);self.assertGreater(sum(r>0 for r in ratings()),3)
                        end();undo();self.assertEqual(ratings(),original)
                        # 文本编辑、弹窗、其他工作页禁止误触；恢复后正常普通勾选和持久化。
                        page.locator('#search').focus();start(0);slide(2);end();self.assertEqual(ratings(),original)
                        page.locator('#search').fill('');page.locator('#help').click()
                        page.keyboard.down('Shift');page.mouse.move(800,300);page.mouse.move(1000,300);page.keyboard.up('Shift')
                        self.assertEqual(ratings(),original);page.locator('#help-dialog .close').click()
                        page.locator('#asset-archive-open').click();page.keyboard.down('Shift');page.mouse.move(500,250);page.mouse.move(700,350);page.keyboard.up('Shift')
                        self.assertEqual(ratings(),original);page.locator('#selection-tab').click()
                        page.locator('[data-view="grid"]').click();page.locator('[data-pick]').first.check()
                        page.evaluate('()=>FramekeepIO.flush()');page.reload(wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                        self.assertEqual(ratings()[:4],[1,4,0,2]);self.assertFalse(errors,errors)
                        out=ROOT/'build/shift-pick';out.mkdir(parents=True,exist_ok=True)
                        start(2);slide(3);page.screenshot(path=str(out/'shift-pick.png'));end()
                    finally:browser.close()
            finally:
                http.shutdown();http.server_close()
                for p in reversed(patches):p.stop()


if __name__=='__main__':unittest.main()
