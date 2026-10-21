from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import Store
from model_providers import Providers
import media_engine as media

ROOT=Path(__file__).resolve().parents[1]


class MotionBrowserTests(unittest.TestCase):
    def test_real_intermediate_frames_reversal_close_semantics_and_reduced_motion(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir();store=Store(base/'data',ROOT);sid=store.ensure_live_session(photos);store.activate(sid)
            for i in range(2):
                path=photos/f'{i}.JPG';Image.new('RGB',(600,400),(60+i*60,120,90)).save(path);store.append_live(sid,[media.capture_record(photos,path,None,{})])
            patches=[patch.object(server,k,v) for k,v in [('STORE',store),('DATA',store.data),('MODELS',Providers(store.data)),('MOUSE',None),('WATCH',None),('SKU',None),('ARCHIVE',None),('ANALYSIS',None),('RAWLINK',None),('FILESEARCH',None)]]
            for p in patches:p.start()
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':1050},reduced_motion='no-preference');errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='networkidle')
                        self.assertTrue(page.evaluate('()=>CSS.supports("transition-behavior","allow-discrete") && CSS.supports("interpolate-size","allow-keywords")'))
                        page.evaluate('''()=>{window.sampleMotion=(el)=>{getComputedStyle(el).opacity;const animations=el.getAnimations({subtree:true});for(const a of animations){a.pause();a.currentTime=Number(a.effect.getTiming().duration)*.25}return {opacity:Number(getComputedStyle(el).opacity),easings:animations.flatMap(a=>[a.effect.getTiming().easing,...a.effect.getKeyframes().map(k=>k.easing)]),count:animations.length}};window.finishMotion=()=>document.getAnimations().forEach(a=>a.finish())}''')
                        # 页面状态先同步改变，入场动画实际存在非终态的中间帧。
                        sample=page.evaluate('()=>{document.getElementById("sku-open").click();return sampleMotion(document.getElementById("sku-dialog"))}')
                        self.assertGreater(sample['opacity'],0);self.assertLess(sample['opacity'],1);self.assertTrue(any('cubic-bezier' in e for e in sample['easings']))
                        page.evaluate('()=>{finishMotion();for(let i=0;i<5;i++){document.getElementById("selection-tab").click();document.getElementById("sku-open").click()}document.getElementById("selection-tab").click();finishMotion()}')
                        self.assertEqual(page.locator('#selection-tab').get_attribute('aria-selected'),'true')
                        # 原生弹窗的入场和离场都具有中间帧，close仍同步完成。
                        sample=page.evaluate('()=>{document.getElementById("help").click();return sampleMotion(document.getElementById("help-dialog"))}')
                        self.assertGreater(sample['opacity'],0);self.assertLess(sample['opacity'],1)
                        out=ROOT/'build/motion-acceptance';out.mkdir(exist_ok=True);page.screenshot(path=str(out/'dialog-mid-frame.png'))
                        result=page.evaluate('()=>{finishMotion();const d=document.getElementById("help-dialog");d.close("done");return {open:d.open,value:d.returnValue,sample:sampleMotion(d)}}')
                        self.assertFalse(result['open']);self.assertEqual(result['value'],'done');self.assertGreater(result['sample']['opacity'],0);self.assertLess(result['sample']['opacity'],1)
                        page.evaluate('()=>finishMotion()');page.wait_for_function('()=>getComputedStyle(document.getElementById("help-dialog")).display==="none"')
                        page.locator('#help').click();page.keyboard.press('Escape');page.wait_for_function('()=>!document.getElementById("help-dialog").open');page.evaluate('()=>finishMotion()')
                        # 内容折叠的高度可插值，连续反向不会排队。
                        result=page.evaluate('async()=>{const d=document.getElementById("selection-tools"),before=parseFloat(getComputedStyle(d,"::details-content").blockSize);d.open=false;await new Promise(resolve=>setTimeout(resolve,60));return {before,middle:parseFloat(getComputedStyle(d,"::details-content").blockSize)}}')
                        self.assertGreater(result['middle'],0);self.assertLess(result['middle'],result['before'])
                        page.wait_for_function('()=>parseFloat(getComputedStyle(document.getElementById("selection-tools"),"::details-content").blockSize)===0')
                        page.evaluate('()=>{const d=document.getElementById("selection-tools");for(let i=0;i<7;i++)d.open=!d.open}');self.assertTrue(page.locator('#selection-tools').evaluate('(el)=>el.open'))
                        page.wait_for_function('()=>getComputedStyle(document.getElementById("selection-tools"),"::details-content").opacity==="1"')
                        widths=page.evaluate('()=>{const main=document.querySelector(".library-main"),before=main.getBoundingClientRect().width;document.getElementById("layout-left").click();sampleMotion(document.querySelector(".library-layout"));const middle=main.getBoundingClientRect().width;finishMotion();return {before,middle,after:main.getBoundingClientRect().width}}')
                        self.assertGreater(widths['middle'],widths['before']);self.assertLess(widths['middle'],widths['after']);page.locator('#left-panel-toggle').click();page.evaluate('()=>finishMotion()')
                        page.locator('[data-photo]').first.click(button='right');sample=page.evaluate('()=>sampleMotion(document.getElementById("photo-context-menu"))');self.assertGreater(sample['count'],0)
                        page.keyboard.press('Escape');self.assertFalse(page.evaluate('()=>Framekeep.remoteTarget().paused'));page.evaluate('()=>finishMotion()')
                        # 动画不延迟标星，系统减少动态效果时弹窗/页面直接落入最终状态。
                        page.locator('#stars [data-rating="3"]').click();self.assertEqual(page.locator('#stars .on').count(),3);page.evaluate('()=>FramekeepIO.flush()')
                        page.emulate_media(reduced_motion='reduce');page.locator('#help').click();self.assertEqual(page.locator('#help-dialog').evaluate('(d)=>getComputedStyle(d).opacity'),'1');page.locator('#help-dialog .close-action').click()
                        self.assertEqual(page.locator('#help-dialog').evaluate('(d)=>getComputedStyle(d).display'),'none')
                        page.locator('#sku-open').click();page.locator('#selection-tab').click();self.assertEqual(page.evaluate('()=>document.getAnimations().filter(a=>a.playState==="running").length'),0)
                        self.assertIn(3,store.session(sid)['state']['overrides'].values());self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:
                http.shutdown();http.server_close()
                for p in reversed(patches):p.stop()


if __name__=='__main__':unittest.main()
