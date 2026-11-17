"""静默下载、完整性门槛、缓存复用及顶栏轮询无闪烁回归。"""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from playwright.sync_api import sync_playwright
from update_client import UpdateClient
import updater

ROOT=Path(__file__).resolve().parents[1]


class SilentDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        base=Path(self.tmp.name);self.root=base/'app';self.root.mkdir();self.data=base/'data'
        updater.write_json(self.root/'update-config.json',{'version':'1.0.0','releaseId':'20260101000000'})
        self.package=b'complete software package'
        self.manifest={'protocol':1,'product':'framekeep','platform':'win32-x64','version':'1.0.0',
            'releaseId':'20261109000000','minVersion':'1.0.0','notes':'更新说明',
            'package':{'url':'https://modelscope.cn/api/v1/models/'+updater.TRUST['repository']+'/repo?test=1',
                       'size':len(self.package),'sha256':hashlib.sha256(self.package).hexdigest()}}
        key=Ed25519PrivateKey.generate();payload=json.dumps(self.manifest)
        self.envelope={'payload':payload,'signature':key.sign(payload.encode()).hex()}
        self.entered=threading.Event();self.release=threading.Event();self.release.set();self.downloads=0;self.fail=False
        self.addCleanup(self.release.set)
        self.trust=patch.dict(updater.TRUST,{'publicKey':key.public_key().public_bytes_raw().hex()});self.trust.start();self.addCleanup(self.trust.stop)
        network=patch.object(updater.urllib.request,'urlopen',side_effect=self.response);network.start();self.addCleanup(network.stop)

    def response(self,request,**kwargs):
        if request.full_url==updater.TRUST['manifestUrl']:return io.BytesIO(json.dumps(self.envelope).encode())
        self.downloads+=1;self.entered.set()
        if not self.release.wait(5):raise TimeoutError('测试下载没有释放')
        if self.fail:raise OSError('网络中断')
        response=io.BytesIO(self.package);response.status=200
        return response

    def wait(self,client,status):
        deadline=time.monotonic()+5
        while client.state()['status']!=status:
            self.assertLess(time.monotonic(),deadline,client.state());time.sleep(.01)
        return client.state()

    def test_download_is_single_flight_and_open_requires_complete_package(self):
        self.release.clear();client=UpdateClient(self.root,self.data,8769);client.check()
        try:
            self.assertTrue(self.entered.wait(2));self.assertEqual(client.state()['status'],'downloading')
            for _ in range(5):client.check()
            self.assertEqual(self.downloads,1)
            with self.assertRaisesRegex(ValueError,'尚未下载完成'):client.open()
        finally:self.release.set()
        state=self.wait(client,'downloaded');self.assertTrue(state['downloaded'])
        worker=self.root/'update';worker.mkdir();(worker/'SHIYING 更新器.exe').write_bytes(b'native');(worker/'update-worker.exe').write_bytes(b'worker')
        with patch.dict(os.environ,{'FRAMEKEEP_DESKTOP_PID':'12345'}),patch('update_client.subprocess.Popen',return_value=Mock(poll=lambda:None)) as launch:
            client.open();client.open();self.assertEqual(launch.call_count,1)
        settings=updater.read_json(client.session/'session.json')
        self.assertEqual(updater.read_json(settings['localEnvelope']),self.envelope)
        self.assertEqual(Path(settings['localPackage']).read_bytes(),self.package)
        with patch.object(updater,'fetch_envelope',side_effect=AssertionError('不应重复联网')):
            self.assertEqual(updater.select_install_release(settings)[1],self.manifest)

    def test_download_failure_can_retry_and_restart_reuses_verified_cache(self):
        self.fail=True;client=UpdateClient(self.root,self.data,8769);client.check()
        state=self.wait(client,'error');self.assertFalse(state['downloaded']);self.assertIn('下载更新失败',state['message'])
        self.fail=False;client.check();self.wait(client,'downloaded');calls=self.downloads
        restarted=UpdateClient(self.root,self.data,8769);restarted.check();self.wait(restarted,'downloaded')
        self.assertEqual(self.downloads,calls)
        (restarted.download_session/'package.zip').write_bytes(b'corrupt')
        repaired=UpdateClient(self.root,self.data,8769);repaired.check();self.wait(repaired,'downloaded')
        self.assertEqual(self.downloads,calls+1)

    def test_bad_signature_or_hash_never_reports_downloaded(self):
        self.envelope['signature']='00'*64;client=UpdateClient(self.root,self.data,8769);client.check()
        self.assertFalse(self.wait(client,'error')['downloaded']);self.assertEqual(self.downloads,0)
        # 签名恢复后，模拟传输内容损坏。
        key=Ed25519PrivateKey.generate();updater.TRUST['publicKey']=key.public_key().public_bytes_raw().hex()
        self.envelope['signature']=key.sign(self.envelope['payload'].encode()).hex();self.package=b'corrupt'
        client.check();self.assertFalse(self.wait(client,'error')['downloaded'])
        self.assertFalse((self.data/'maintenance/updates/downloads/1.0.0-20261109000000/package.zip').exists())

    def test_latest_version_does_not_download(self):
        updater.write_json(self.root/'update-config.json',{'version':self.manifest['version'],'releaseId':self.manifest['releaseId']})
        client=UpdateClient(self.root,self.data,8769);client.check();state=self.wait(client,'checked')
        self.assertFalse(state['available']);self.assertFalse(state['downloaded']);self.assertEqual(self.downloads,0)


class SilentUpdateBrowserTests(unittest.TestCase):
    def test_startup_download_is_quiet_and_prompts_once_only_after_completion(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();page.set_content('<header class="app-header"><div class="header-actions"></div></header>')
                page.evaluate('''()=>{
                  window.calls=[];window.notices=[];window.FramekeepDesktop={};
                  window.state={status:'downloading',available:true,downloaded:false,currentVersion:'1.0.0',currentReleaseId:'old',version:'1.0.0',releaseId:'new'};
                  window.Framekeep={notify:message=>notices.push(message)};
                  window.FramekeepIO={api:async path=>{calls.push(path);return {...state}}};
                }''')
                page.add_script_tag(path=str(ROOT/'演示方案/update.js'))
                page.wait_for_function("calls.includes('/api/update')",timeout=5000)
                self.assertEqual(page.evaluate('notices'),[])
                self.assertEqual(page.locator('#check-update').inner_text(),'检查更新')
                self.assertNotIn('/api/update/open',page.evaluate('calls'))
                page.evaluate("Object.assign(state,{status:'downloaded',downloaded:true,message:'完成'})")
                page.wait_for_function('notices.length===1')
                self.assertEqual(page.locator('#check-update').inner_text(),'立即更新')
                self.assertNotIn('/api/update/open',page.evaluate('calls'))
                page.locator('#check-update').click();page.wait_for_function("calls.includes('/api/update/open')")
                page.wait_for_function("calls.filter(p=>p==='/api/update').length>=3")
                self.assertEqual(page.evaluate('notices.length'),1)
                self.assertEqual(page.evaluate("calls.filter(p=>p==='/api/update/open').length"),1)
                page.evaluate('state.updaterOpen=false')
                page.wait_for_function("!document.getElementById('check-update').disabled")
                self.assertEqual(page.locator('#check-update').inner_text(),'立即更新')
                page.locator('#check-update').click()
                page.wait_for_function("calls.filter(p=>p==='/api/update/open').length===2")
            finally:browser.close()

    def test_full_workbench_header_stays_quiet_then_shows_ready_button(self):
        from test_asset_retrieval_browser import RetrievalBrowserTests
        fixture=RetrievalBrowserTests();fixture.setUp()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel='msedge',headless=True)
                try:
                    page=browser.new_page(viewport={'width':1440,'height':900});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    state={'status':'downloading','available':True,'downloaded':False,'currentVersion':'1.0.0',
                           'currentReleaseId':'20260101000000','version':'1.0.0','releaseId':'20261109000000'}
                    page.add_init_script('window.FramekeepDesktop={initSettings(){}};window.updateCalls=[]')
                    def respond(route):
                        page.evaluate('(path)=>updateCalls.push(path)',route.request.url.split(fixture.url)[-1])
                        route.fulfill(json=state)
                    page.route('**/api/update**',respond)
                    page.goto(fixture.url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading')
                    page.wait_for_function("()=>updateCalls.includes('/api/update')")
                    self.assertEqual(page.locator('#check-update').inner_text(),'检查更新')
                    self.assertNotIn('正在更新',page.locator('.app-header').inner_text())
                    self.assertFalse(page.locator('#toast').evaluate("e=>e.classList.contains('visible')"))
                    output=ROOT/'build/silent-update';output.mkdir(parents=True,exist_ok=True)
                    page.locator('.app-header').screenshot(path=str(output/'downloading-header.png'))
                    state.update(status='downloaded',downloaded=True)
                    page.wait_for_function("()=>document.getElementById('check-update').textContent==='立即更新'")
                    self.assertIn('新版已下载完成',page.locator('#toast').inner_text())
                    self.assertFalse(page.evaluate("updateCalls.includes('/api/update/open')"))
                    page.locator('.app-header').screenshot(path=str(output/'downloaded-header.png'))
                    page.screenshot(path=str(output/'downloaded-workbench.png'))
                    self.assertEqual(errors,[])
                finally:browser.close()
        finally:fixture.tearDown()

    def test_cached_page_refresh_and_retry_never_flash_header_status(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();page.route('http://framekeep.test/',lambda route:route.fulfill(body='<html></html>',content_type='text/html'))
                page.goto('http://framekeep.test/')
                page.set_content('<header class="app-header"><div class="header-actions"><button>检查更新</button></div></header>')
                page.evaluate('''()=>{
                  window.FRAMEKEEP_SESSION={id:'test',viewBootstrap:{values:{'/api/folders':{rows:[1]}}}};
                  window.FramekeepWorkspace={current:()=> 'selection',refresh:()=>{}};
                  window.headerChanges=[];
                  new MutationObserver(()=>headerChanges.push(document.querySelector('.app-header').textContent)).observe(document.querySelector('.app-header'),{subtree:true,childList:true,attributes:true,characterData:true});
                }''')
                page.add_script_tag(path=str(ROOT/'演示方案/view-cache.js'))
                result=page.evaluate('''async()=>{
                  const cache=FramekeepViewCache;let calls=0;window.failed=true;
                  const fetchLive=async()=>{calls++;await new Promise(resolve=>setTimeout(resolve,30));if(failed)throw Error('offline');return {rows:[2]}};
                  const initial=await cache.read('/api/folders',fetchLive);
                  await new Promise(resolve=>setTimeout(resolve,60));failed=false;
                  await new Promise(resolve=>setTimeout(resolve,3100));
                  const recovered=await cache.read('/api/folders',fetchLive);
                  for(let i=0;i<3;i++){await new Promise(resolve=>setTimeout(resolve,800));await cache.read('/api/folders',fetchLive)}
                  await new Promise(resolve=>setTimeout(resolve,60));
                  return {initial,recovered,calls,changes:headerChanges};
                }''')
                self.assertEqual(result['initial'],{'rows':[1]});self.assertEqual(result['recovered'],{'rows':[2]})
                self.assertGreaterEqual(result['calls'],4);self.assertEqual(result['changes'],[])
            finally:browser.close()


if __name__=='__main__':unittest.main()
