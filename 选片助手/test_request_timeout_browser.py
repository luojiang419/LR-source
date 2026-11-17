"""验证超时提示、主动取消及评级保存失败后的恢复，不调用真实模型。"""
import unittest

from playwright.sync_api import sync_playwright
from test_jpeg_receiving_preview import application_runtime


class RequestTimeoutTests(unittest.TestCase):
    def test_timeout_translation_cancellation_and_rating_save_recovery(self):
        with application_runtime() as (_,_,_,store,_,sid,url),sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(url,wait_until='domcontentloaded')
                page.wait_for_function('()=>window.FramekeepIO&&!FramekeepBoot.loading')
                page.evaluate('''()=>{const original=window.fetch;window.__saveAttempts=0;window.__failSave=false;
                  window.fetch=(path,...args)=>{
                    if(String(path)==='/api/test-timeout')throw new DOMException('signal timed out','TimeoutError');
                    if(String(path)==='/api/test-cancel')throw new DOMException('cancelled by caller','AbortError');
                    if(['/api/session','/api/session/patch'].includes(String(path))){++window.__saveAttempts;
                      if(window.__failSave)return Promise.reject(new DOMException('signal timed out','TimeoutError'))}
                    return original(path,...args)}
                }''')
                result=page.evaluate('''async()=>{try{await FramekeepIO.api('/api/test-timeout')}catch(e){return {name:e.name,message:e.message,cause:e.cause?.name}}}''')
                self.assertEqual(result,{'name':'TimeoutError','message':'请求超时，请稍后重试','cause':'TimeoutError'})
                result=page.evaluate('''async()=>{const c=new AbortController();c.abort();try{await FramekeepIO.api('/api/test-cancel',undefined,100,c.signal)}catch(e){return {name:e.name,message:e.message}}}''')
                self.assertEqual(result,{'name':'AbortError','message':'cancelled by caller'})
                page.evaluate("()=>Framekeep.notify('signal timed out')")
                self.assertEqual(page.locator('#toast').inner_text(),'请求超时，请稍后重试')
                page.evaluate('()=>window.__failSave=true');page.keyboard.press('4')
                page.wait_for_function('()=>document.getElementById("save-status").textContent==="请求超时，请稍后重试"')
                cid=store.records(sid)[0]['id']
                self.assertEqual(page.evaluate('(id)=>Framekeep.persistedState().overrides[id]',cid),4)
                self.assertNotEqual(store.session(sid)['state'].get('overrides',{}).get(cid),4)
                attempts=page.evaluate('()=>window.__saveAttempts');page.wait_for_timeout(600)
                self.assertEqual(page.evaluate('()=>window.__saveAttempts'),attempts,'写请求超时不能盲目重发')
                page.evaluate('()=>window.__failSave=false');page.evaluate('()=>FramekeepIO.flush()')
                self.assertEqual(store.session(sid)['state']['overrides'][cid],4)
                self.assertEqual(page.locator('#save-status').inner_text(),'已保存')
                self.assertFalse(errors,errors)
            finally:browser.close()
