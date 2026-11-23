"""首显必须在可见窗口内发生，包含默认网格、现场恢复和切到接收端的长轮询。"""
from contextlib import closing
import json
import os
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright

from direct_channel import DirectImageChannel
from direct_jpeg import DirectJpegFeed
from eos_monitor import EosMonitor
from lan_sync_share import TemporaryCaptureShare
from test_jpeg_receiving_preview import application_runtime, camera_jpeg, configure


class FirstFrameTests(unittest.TestCase):
    def test_share_preparation_retries_same_directory_after_cooldown(self):
        share = TemporaryCaptureShare('a'*32)
        share.enabled = True
        try:
            with patch.object(share, '_powershell', side_effect=[OSError('busy'), None, None]) as prepare:
                self.assertIsNone(share.describe('photos'))
                share.worker.join(2)
                self.assertIsNone(share.describe('photos'))
                self.assertEqual(prepare.call_count, 1)
                with patch('lan_sync_share.time.monotonic', return_value=share.retry_at+1):
                    share.describe('photos')
                share.worker.join(2)
                self.assertEqual(share.describe('photos'), {'name':share.name})
                share.close()
        finally:
            # 夹具未创建实际的 Windows 共享。
            share.owned.clear()

    def test_restore_rebinds_image_channel_after_session_is_already_open(self):
        with application_runtime() as (server, base, photos, store, watch, sid, url), closing(DirectImageChannel(watch, url)) as channel, patch.object(server, 'DIRECT', channel), sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=True)
            try:
                page = browser.new_page()
                page.goto(url, wait_until='domcontentloaded')
                page.wait_for_function('()=>window.FramekeepReceiving&&!FramekeepBoot.loading&&FRAMEKEEP_SESSION.id')
                # 正式会话已打开，迟到的启动恢复只应补回独立通道。
                page.evaluate('''c=>{delete FRAMEKEEP_SESSION.directPreview;document.dispatchEvent(new Event('framekeep:preview-channel'));
                    return FramekeepIO.restoreStartup({session:{directPreview:c}})}''', channel.public())
                with watch.lock:
                    (photos/'LATE-CHANNEL.JPG').write_bytes(camera_jpeg())
                    self.check_frame(page,'LATE-CHANNEL.JPG')
                    image_url=page.locator('.receiving-photo-preview img').get_attribute('src')
                    self.assertTrue(image_url.startswith(channel.origin+'/'))
            finally:
                browser.close()

    def test_eos_callback_from_idle_paints_before_folder_sidebar_and_follows_burst(self):
        payload = camera_jpeg()
        with application_runtime() as (server, base, photos, store, watch, sid, url), closing(DirectImageChannel(watch, url)) as channel, patch.object(server, 'DIRECT', channel), patch.object(server, 'CANON', EosMonitor(store, watch)), patch.dict(os.environ, FRAMEKEEP_DESKTOP_TOKEN='first-frame-fixture'), sync_playwright() as pw:
            watch.stop()
            folder = base / 'EOS'
            folder.mkdir()
            browser = pw.chromium.launch(channel='msedge', headless=True)
            try:
                page = browser.new_page()
                page.goto(url, wait_until='domcontentloaded')
                page.wait_for_function('()=>window.FramekeepReceiving&&!FramekeepBoot.loading')
                admission = []
                page.evaluate('''()=>{FramekeepIO.refreshFolders=()=>new Promise(resolve=>{window.finishFolders=resolve})}''')
                page.route('**/api/state?session_id=*', lambda route: admission.append(route))
                elapsed = []
                for index in range(5):
                    path = folder / f'EOS_{index}.JPG'
                    request = Request(url+'/api/desktop/canon', json.dumps({'paths':[str(path)]}).encode(),
                                      {'Content-Type':'application/json', 'X-Framekeep-Desktop':'first-frame-fixture'})
                    started = time.perf_counter()
                    path.write_bytes(payload)
                    with urlopen(request, timeout=3) as response:
                        result = json.load(response)
                        self.assertTrue(result['accepted'])
                    page.wait_for_function('''name=>{
                        const image=document.querySelector('.receiving-photo-preview img')||document.querySelector('#photos [data-loupe] img');
                        return image?.alt===name&&image.naturalWidth===3600&&image.getBoundingClientRect().height>0;
                    }''', arg=path.name, timeout=950)
                    page.evaluate('()=>new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done)))')
                    elapsed.append(round((time.perf_counter()-started)*1000))
                    if index == 0:
                        self.assertNotEqual(page.evaluate('FRAMEKEEP_SESSION.id'), result['session_id'])
                        for route in admission:
                            route.continue_()
                        page.unroute('**/api/state?session_id=*')
                        page.wait_for_function('id=>FRAMEKEEP_SESSION.id===id', arg=result['session_id'])
                print('EOS 文件落盘到画面（毫秒）', elapsed, flush=True)
                page.evaluate('window.finishFolders?.()')
            finally:
                browser.close()

    def test_visible_grid_and_resumed_selection_receive_without_catalog(self):
        payload = camera_jpeg()
        with application_runtime() as (server, base, photos, store, watch, sid, url), closing(DirectImageChannel(watch, url)) as channel, patch.object(server, 'DIRECT', channel), sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=True)
            try:
                page = browser.new_page()
                page.goto(url, wait_until='domcontentloaded')
                page.wait_for_function('()=>window.FramekeepReceiving&&!FramekeepBoot.loading&&FramekeepWatch.getFollow()')
                for scenario in ('grid', 'paused'):
                    with self.subTest(scenario=scenario), watch.lock:
                        page.locator('[data-view=grid]').click()
                        if scenario == 'paused':
                            page.evaluate('Framekeep.setInteractionPaused(true)')
                        name = scenario + '.JPG'
                        (photos / name).write_bytes(payload)
                        if scenario == 'paused':
                            page.wait_for_timeout(200)
                            self.assertFalse(page.locator('.receiving-photo-preview img').is_visible())
                            page.evaluate('Framekeep.setInteractionPaused(false)')
                        self.check_frame(page, name)
                        page.evaluate('FramekeepReceiving.dismiss()')
            finally:
                browser.close()

    @staticmethod
    def check_frame(page, name):
        page.wait_for_function('''name=>{
            const image=document.querySelector('.receiving-photo-preview img');
            return image?.alt===name && image.naturalWidth===3600 && image.getBoundingClientRect().height>0
        }''', arg=name, timeout=800)
        page.evaluate('()=>new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done)))')

    def test_switching_role_replaces_wait_on_old_feed(self):
        payload = camera_jpeg()
        with application_runtime() as (server, base, photos, store, watch, sid, url), closing(DirectImageChannel(watch, url)) as channel, patch.object(server, 'DIRECT', channel), closing(DirectJpegFeed(store, autostart=False)) as receiver, sync_playwright() as pw:
            feed = [watch.direct]
            channel.feed_provider = lambda: feed[0]
            browser = pw.chromium.launch(channel='msedge', headless=True)
            try:
                page = browser.new_page()
                page.goto(url, wait_until='domcontentloaded')
                page.wait_for_function('()=>window.FramekeepReceiving&&!FramekeepBoot.loading')
                page.locator('[data-view=single]').click()
                page.wait_for_timeout(200)
                shared = base / 'shared'
                shared.mkdir()
                configure(receiver, shared, sid, 'lan-session')
                path = shared / 'SWITCHED.JPG'
                path.write_bytes(payload)
                receiver.announce(path)
                feed[0] = receiver
                # 通过实际同步设置的状态刷新通知页面角色已变。
                page.route('**/api/lan-sync', lambda route: route.fulfill(json={'enabled':True,'receiving':True,'peer':{'name':'拍摄主机'}}))
                page.locator('[data-settings-section="sync"]').dispatch_event('click')
                page.wait_for_function('()=>window.FramekeepLanSync?.receiving()')
                self.check_frame(page, path.name)
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
