"""现场积压不阻塞新快门、真实只读 SMB 与接收端断开回归。"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
from PIL import Image
from catalog_store import stable_json
import media_engine as media
import test_lan_capture_sync as fixture


class FastPreviewTests(unittest.TestCase):
    setUp=fixture.LanCaptureSyncTests.setUp
    tearDown=fixture.LanCaptureSyncTests.tearDown
    def tearDown(self):
        for client in getattr(self,'extra_clients',[]):client.close()
        fixture.LanCaptureSyncTests.tearDown(self)

    peer=fixture.LanCaptureSyncTests.peer
    connect=fixture.LanCaptureSyncTests.connect

    def test_smb_three_clients_766_backlog_does_not_delay_new_shutter(self):
        sid,record=self.host.photo();source=self.host.photos/'one.JPG'
        rows=[]
        for i in range(1,766):
            name=f'old-{i:04d}.JPG';(self.host.photos/name).write_bytes(source.read_bytes())
            rows.append({**record,'id':f'old-{i:04d}','jpg':name,'name':name})
        self.host.store.append_live(sid,rows)
        self.host.sync.share.enabled=True
        self.host.sync.share.describe(self.host.photos)
        until=time.monotonic()+8
        while not self.host.sync.share.ready and time.monotonic()<until:time.sleep(.05)
        self.assertTrue(self.host.sync.share.ready,self.host.sync.share.error)
        share=self.host.sync.share.name
        unc=Path(chr(92)*2+'127.0.0.1'+chr(92)+share)
        self.assertEqual((unc/'one.JPG').read_bytes(),source.read_bytes())
        with self.assertRaises(OSError):(unc/'must-not-write.jpg').write_bytes(b'forbidden')
        clients=[self.receiver];self.extra_clients=[]
        for i in range(2):
            client=fixture.Node(self.base/str(i),f'fast client {i}',self.host.sync.discovery_port);clients.append(client);self.extra_clients.append(client)
        release=threading.Event();blocked=threading.Event();patches=[]
        try:
            for client in clients:
                original=client.sync.download
                def slow(peer,info,target,_download=original):
                    if threading.current_thread().name.startswith('lan-download'):
                        blocked.set();release.wait(12)
                    return _download(peer,info,target)
                p=patch.object(client.sync,'download',side_effect=slow);p.start();patches.append(p)
                peer=self.peer();client.sync.peers[peer['device']]=peer;client.sync.connect_peer({'device':peer['device'],'password':'同步 001Ab!'})
                client.sync.thread.start()
            self.assertTrue(blocked.wait(15),stable_json([c.sync.state() for c in clients]))
            local=self.host.local_id(sid)
            started=time.monotonic();new_file=self.host.photos/'NEW-SHUTTER.JPG'
            Image.effect_noise((3000,2000),80).convert('RGB').save(new_file,quality=95)
            new=media.capture_record(self.host.photos,new_file,None,{})
            self.host.store.append_live(sid,[new])
            self.assertGreater(new_file.stat().st_size,3000000)
            until=started+2
            def received(client):return any(Path(r['jpg']).name=='NEW-SHUTTER.JPG' for r in client.store.records(local))
            while time.monotonic()<until and not all(received(c) for c in clients):time.sleep(.02)
            self.assertTrue(all(received(c) for c in clients),'新照片被 766 张历史积压阻塞')
            elapsed=time.monotonic()-started;self.assertLess(elapsed,2)
            for client in clients:
                self.assertGreater(client.sync.status['pending_files'],0)
                self.assertEqual(client.sync.confirmed['seq'],0,'预览不能确认尚未传完的完整批次')
                record=next(r for r in client.store.records(local) if Path(r['jpg']).name=='NEW-SHUTTER.JPG')
                path=Path(client.store.session(local)['root'])/record['jpg']
                self.assertEqual(path.read_bytes(),(self.host.photos/'NEW-SHUTTER.JPG').read_bytes())
                self.assertEqual(client.sync.status['transport'],'SMB')
            print(f'\n766 张积压 / 三接收端，SMB {new_file.stat().st_size/1000000:.2f} MB 新快门 {elapsed:.3f}s 可见，原批次仍未确认',flush=True)
        finally:
            release.set()
            for p in patches:p.stop()
        until=time.monotonic()+45
        while time.monotonic()<until and not all(len(c.store.records(local))==767 for c in clients):time.sleep(.1)
        self.assertTrue(all(len(c.store.records(local))==767 for c in clients))
        # 停止后快通道不能继续投影，已收文件保留。
        for client in clients:client.sync.disconnect()
        self.host.photo('AFTER-DISCONNECT.JPG','blue');time.sleep(.5)
        self.assertTrue(all(len(c.store.records(local))==767 for c in clients))

    def test_unavailable_smb_falls_back_to_verified_http(self):
        self.host.photo();peer=self.connect();payload=self.host.sync.manifest()
        payload['share']={'name':'Framekeep_aaaaaaaa_bbbbbbbb'}
        path,info=self.receiver.sync.batch_files(self.receiver.sync.config['remote'],payload)[0]
        with patch.object(self.receiver.sync.shared_reader,'copy',return_value=False):
            self.assertTrue(self.receiver.sync.download(peer,info,path))
        self.assertEqual(path.read_bytes(),(self.host.photos/'one.JPG').read_bytes())
        self.assertEqual(self.receiver.sync.status['transport'],'HTTP')

    def test_native_shared_preview_precedes_host_catalog_ingestion(self):
        sid,_=self.host.photo();self.host.sync.share.enabled=True;self.host.sync.share.describe(self.host.photos)
        until=time.monotonic()+8
        while not self.host.sync.share.ready and time.monotonic()<until:time.sleep(.05)
        self.assertTrue(self.host.sync.share.ready,self.host.sync.share.error)
        self.connect();self.receiver.sync.config['remote']['share']={'name':self.host.sync.share.name,'prefix':''}
        self.receiver.sync.live_supported=False;self.receiver.sync.thread.start()
        until=time.monotonic()+8
        while time.monotonic()<until and self.receiver.sync.direct.state()['notification_status']!='active':time.sleep(.02)
        self.assertEqual(self.receiver.sync.direct.state()['notification_status'],'active')
        time.sleep(.1);started=time.monotonic();path=self.host.photos/'UNINDEXED.JPG';Image.new('RGB',(800,600),'orange').save(path)
        until=started+2
        while time.monotonic()<until and (self.receiver.sync.direct.state().get('receiving') or {}).get('name')!='UNINDEXED.JPG':time.sleep(.01)
        state=self.receiver.sync.direct.state();self.assertEqual((state.get('receiving') or {}).get('name'),'UNINDEXED.JPG')
        source=self.receiver.sync.direct.source_path(state['generation'],state['receiving']['key'])
        self.assertEqual(source.read_bytes(),path.read_bytes())
        self.assertEqual(len(self.host.store.records(sid)),1,'快图不能依赖主机先完成入库')
        self.assertFalse(any(Path(r['jpg']).name=='UNINDEXED.JPG' for r in self.receiver.store.records(self.host.local_id(sid))))
        print(f'\nSMB 原生目录通知先于主机入库：{time.monotonic()-started:.3f}s',flush=True)
        self.receiver.sync.disconnect();self.assertIsNone(self.receiver.sync.direct.source_path(state['generation'],state['receiving']['key']))

    def test_disconnect_does_not_wait_for_blocked_manifest_or_apply_late_response(self):
        self.host.photo();self.connect();entered=threading.Event();release=threading.Event();request=self.receiver.sync.request_json
        def slow(address,path,*args,**kwargs):
            if path=='/sync/current':entered.set();release.wait(8);return self.host.sync.manifest()
            return request(address,path,*args,**kwargs)
        with patch.object(self.receiver.sync,'request_json',side_effect=slow),ThreadPoolExecutor(max_workers=1) as pool:
            task=pool.submit(self.receiver.sync.sync_once)
            try:
                self.assertTrue(entered.wait(2));started=time.monotonic();self.receiver.sync.disconnect()
                self.assertLess(time.monotonic()-started,1.5);self.assertFalse(self.receiver.sync.receiving)
                # 即使立即重新联机，旧响应也不能写入新连接。
                self.receiver.sync.connect_peer({'device':self.host.sync.config['device'],'password':'同步 001Ab!'})
            finally:release.set()
            task.result()
        self.assertEqual(self.receiver.store.sessions(),[])

    def test_manual_shared_root_follows_host_folder_change(self):
        peer={'share':{'name':'d','prefix':'套图拍摄/2026-11-20'}}
        first=self.receiver.sync.resolve_share(peer,{'set':{'root':'D:/套图拍摄/2026-11-20'},'share':{'name':'Framekeep_aaaaaaaa_bbbbbbbb'}})
        self.assertEqual(first['name'],'d')
        self.assertEqual(first['prefix'],'套图拍摄/2026-11-20')
        second=self.receiver.sync.resolve_share(peer,{'set':{'root':'D:/套图拍摄/2026-11-21'}})
        self.assertEqual(second['prefix'],'套图拍摄/2026-11-21')
        self.assertIsNone(self.receiver.sync.resolve_share(peer,{'set':{'root':'E:/其他照片'}}))

    def test_latest_photo_compares_timezone_instants_and_never_regresses(self):
        peer={'device':'a'*32};payload={'set':{'id':'b'*16},'tables':{'catalog':{'captures':[
            {'id':'old','position':0,'sync_files':[{'kind':'jpg'}],'data':stable_json({'time':'2026-11-20T14:20:00+08:00'})},
            {'id':'new','position':1,'sync_files':[{'kind':'jpg'}],'data':stable_json({'ingested_at':'2026-11-20T06:52:00+00:00'})}]}}}
        self.receiver.sync.record_latest(payload,peer)
        self.assertEqual(self.receiver.sync.status['latest_photo'],'2026-11-20T06:52:00+00:00')
        payload['tables']['catalog']['captures']=payload['tables']['catalog']['captures'][:1]
        self.receiver.sync.record_latest(payload,peer)
        self.assertEqual(self.receiver.sync.status['latest_photo'],'2026-11-20T06:52:00+00:00')

    def test_legacy_host_share_preview_without_committing_baseline(self):
        sid,_=self.host.photo();self.connect();self.receiver.sync.live_supported=False
        self.receiver.sync.config['remote']['share']={'name':'legacy','prefix':'photos'}
        with patch.object(self.receiver.sync.shared_reader,'copy',return_value=False):self.receiver.sync.preview_once()
        self.assertEqual(len(self.receiver.store.records(self.host.local_id(sid))),1)
        self.assertEqual(self.receiver.sync.confirmed['seq'],0)
        self.assertIsNone(self.receiver.sync.data.receipt(self.host.sync.config['device']))
        self.assertTrue(self.receiver.sync.status['latest_capture_id'])

    def test_live_snapshot_ignores_main_manifest_lock_and_keeps_cursor(self):
        sid,_=self.host.photo();self.connect()
        with self.host.sync.manifest_lock:
            started=time.monotonic();self.receiver.sync.preview_once()
            self.assertLess(time.monotonic()-started,2)
        self.assertEqual(len(self.receiver.store.records(self.host.local_id(sid))),1)
        self.assertIsNone(self.receiver.sync.data.receipt(self.host.sync.config['device']))
        self.assertEqual(self.receiver.sync.confirmed['seq'],0)

if __name__=='__main__':unittest.main()
