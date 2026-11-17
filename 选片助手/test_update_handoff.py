"""更新交接恢复、实例绑定及浏览器保存/重试边界。"""
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch,Mock

from playwright.sync_api import sync_playwright
from update_client import UpdateClient
from updater import write_json

ROOT=Path(__file__).resolve().parents[1]


class UpdateHandoffTests(unittest.TestCase):
    def test_file_writers_must_finish_before_close_returns(self):
        from asset_archive import AssetArchive
        from rating_sync import RatingSync
        for service,old_timeout in ((AssetArchive,3),(RatingSync,5)):
            with self.subTest(service=service.__name__):
                instance=service.__new__(service);instance.closed=threading.Event();instance.wake=threading.Event()
                if service is AssetArchive:
                    instance.metadata_thread=None;instance.backfill_thread=None;instance.writer=Mock()
                writing=threading.Event();release=threading.Event();closed=threading.Event()
                def write():writing.set();release.wait()
                instance.thread=threading.Thread(target=write);instance.thread.start();writing.wait(1)
                closer=threading.Thread(target=lambda:(instance.close(),closed.set()));closer.start()
                try:self.assertFalse(closed.wait(old_timeout+.2),'仍在写入时不能完成关闭')
                finally:release.set();closer.join(2);instance.thread.join(2)
                self.assertTrue(closed.is_set())

    def test_adopts_only_handoff_bound_to_this_engine_and_desktop(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'FRAMEKEEP_DESKTOP_PID':'12345'}):
            base=Path(folder);root=base/'app';root.mkdir();data=base/'data';session=data/'maintenance/updates/test';session.mkdir(parents=True)
            settings={'root':str(root),'session':str(session),'parentPid':12345,'enginePid':os.getpid()}
            write_json(session/'session.json',settings);write_json(session/'ready.json',{'pid':os.getpid()})
            handoff=data/'maintenance/updates/handoff.json'
            for changes in ({'enginePid':-1},{'parentPid':1},{'root':str(base/'other')}):
                client=UpdateClient(root,data,8769);write_json(handoff,{**settings,**changes})
                self.assertFalse(client.state().get('ready',False));self.assertIsNone(client.session)
            write_json(handoff,settings);client=UpdateClient(root,data,8769)
            self.assertTrue(client.state()['ready']);self.assertEqual(client.session,session)
            write_json(session/'cancel.json',{'cancelled':True});self.assertFalse(client.state()['ready'])

    def test_browser_recovers_poll_failure_and_waits_instead_of_cancelling(self):
        self.browser_case('retry')

    def test_native_fallback_saves_before_exit_without_page_poll(self):
        self.browser_case('native')

    def test_save_failure_keeps_app_and_cancels_transaction(self):
        self.browser_case('save-failure')

    def browser_case(self,mode):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();page.set_content('<div class="header-actions"></div>')
                page.evaluate('''mode=>{
                  window.mode=mode;window.calls=[];window.polls=0;window.attempts=0;
                  window.Framekeep={notify:message=>calls.push('notice:'+message)};
                  window.FramekeepIO={flush:async()=>calls.push('save'),api:async(path)=>{
                    if(path==='/api/update'){polls++;if(mode==='retry'&&polls===1)throw Error('offline');
                      return {ready:true,available:true,status:'checked',currentVersion:'1',currentReleaseId:'1'};}
                    if(path==='/api/update/cancel')calls.push('cancel');return {};
                  }};
                  window.FramekeepAssetViewer={flush:async()=>{calls.push('metadata');if(mode==='save-failure')throw Error('保存失败')}};
                  window.__TAURI__={core:{invoke:async command=>{calls.push(command);attempts++;
                    if(mode==='retry'&&attempts===1)throw Error('UPDATE_WAIT:cache migration');window.closedForUpdate=true;}}};
                }''',mode)
                page.add_script_tag(path=str(ROOT/'演示方案/update.js'))
                if mode=='retry':page.locator('#check-update').click()
                else:page.evaluate('FramekeepUpdate.prepare()')
                if mode=='save-failure':
                    page.wait_for_function("calls.includes('cancel')")
                    self.assertFalse(page.evaluate('!!window.closedForUpdate'))
                    self.assertNotIn('close_for_update',page.evaluate('calls'))
                else:
                    page.wait_for_function('window.closedForUpdate===true',timeout=10000)
                    calls=page.evaluate('calls');self.assertNotIn('cancel',calls)
                    self.assertLess(calls.index('save'),calls.index('metadata'))
                    self.assertLess(calls.index('metadata'),calls.index('close_for_update'))
                    if mode=='retry':self.assertGreaterEqual(page.evaluate('polls'),3)
                    else:self.assertEqual(page.evaluate('polls'),0)
            finally:browser.close()


if __name__=='__main__':unittest.main()
