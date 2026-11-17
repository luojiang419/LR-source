"""真实浏览器验证共享读取的取消隔离及写后失效恢复。"""
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


class ViewCacheCancellationTests(unittest.TestCase):
    def test_caller_abort_does_not_poison_shared_read(self):
        self.run_browser("""async()=>{
          const cache=FramekeepViewCache,first=new AbortController();let calls=0,release;
          const fetchLive=signal=>{calls++;return new Promise((resolve,reject)=>{
            release=()=>resolve({value:'ready'});
            signal.addEventListener('abort',()=>reject(signal.reason),{once:true});
          })};
          const old=cache.read('/api/intelligence/search-state',fetchLive,first.signal)
            .then(()=>({name:'resolved'}),error=>({name:error.name}));
          await new Promise(resolve=>setTimeout(resolve,0));
          const next=cache.read('/api/intelligence/search-state',fetchLive);
          first.abort();release();
          return {old:await old,next:await next,calls};
        }""", {'old': {'name': 'AbortError'}, 'next': {'value': 'ready'}, 'calls': 1})

    def test_invalidated_first_read_retries_without_delivering_stale_data(self):
        self.run_browser("""async()=>{
          const cache=FramekeepViewCache;let calls=0;
          const fetchLive=signal=>{const value=++calls;if(value>1)return Promise.resolve({value});
            return new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(signal.reason),{once:true}));};
          const waiting=cache.read('/api/intelligence/search-state',fetchLive);
          await new Promise(resolve=>setTimeout(resolve,0));
          cache.invalidate('/api/intelligence/config');
          const next=cache.read('/api/intelligence/search-state',fetchLive);
          return {waiting:await waiting,next:await next,calls};
        }""", {'waiting': {'value': 2}, 'next': {'value': 2}, 'calls': 2})

    def test_late_old_response_is_replaced_and_real_failures_still_reject(self):
        self.run_browser("""async()=>{
          const cache=FramekeepViewCache;let calls=0,release,failures=0;
          const fetchLive=()=>{const value=++calls;return value===1?new Promise(resolve=>release=()=>resolve({value})):Promise.resolve({value})};
          const waiting=cache.read('/api/intelligence/search-state',fetchLive);
          await new Promise(resolve=>setTimeout(resolve,0));
          cache.invalidate('/api/intelligence/config');release();
          const result=await waiting;
          const failure=await cache.read('/api/intelligence/state',()=>{failures++;throw Error('offline')})
            .then(()=>null,error=>error.message);
          return {result,calls,failure,failures};
        }""", {'result': {'value': 2}, 'calls': 2, 'failure': 'offline', 'failures': 1})

    def run_browser(self, script, expected):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=True)
            try:
                page = browser.new_page(); errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.route('http://cache.test/**', lambda route: route.fulfill(body='<html><body></body></html>', content_type='text/html'))
                page.goto('http://cache.test/')
                page.evaluate("""()=>{
                  window.FRAMEKEEP_SESSION={id:'test'};
                  window.FramekeepWorkspace={current:()=> 'retrieval',refresh:()=>{}};
                }""")
                page.add_script_tag(path=str(ROOT/'演示方案/view-cache.js'))
                self.assertEqual(page.evaluate(script), expected)
                self.assertEqual(errors, [])
            finally:
                browser.close()
