"""当前套、真实 HTTP 三客户端、断网/重启/跨日与 WAL 锁争用验收。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
import copy
import hashlib
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import URLError,HTTPError

from catalog_store import stable_json
from lan_capture_sync import LanCaptureSync,signature,password_key
from lan_sync_data import digest,mapped_id
import test_lan_capture_sync as lan_fixture
Node=lan_fixture.Node
from test_sku_workflow import fixture_book,CODE


class CurrentSetTests(unittest.TestCase):
    setUp=lan_fixture.LanCaptureSyncTests.setUp
    tearDown=lan_fixture.LanCaptureSyncTests.tearDown
    connect=lan_fixture.LanCaptureSyncTests.connect
    peer=lan_fixture.LanCaptureSyncTests.peer

    def test_24000_history_30_current_persistent_hashes_and_unlocked_wal(self):
        sid,record=self.host.photo()
        with self.host.store.connect() as c:
            c.execute("INSERT INTO sessions VALUES ('history',?,'历史','',0,'{}','2026-10-01')",(str(self.base/'never-read'),))
            c.executemany('INSERT INTO captures VALUES (?,?,?,?)', [('history',str(i),stable_json({**record,'id':str(i),'jpg':str(i)+'.JPG','ingested_at':'2026-10-01T00:00:00Z'}),i) for i in range(24000)])
        for i in range(1,30):self.host.photo(str(i)+'.JPG')
        reads=[]
        def counted(path):
            self.assertNotIn('never-read',str(path));reads.append(str(path));return digest(path)
        # 主业务锁被另一线程持有时，同步仍可读；反向阻塞现场提交同样不允许。
        held=threading.Event();release=threading.Event()
        def hold():
            with self.host.store.lock,self.host.sku.lock,self.host.archive.lock:held.set();release.wait(8)
        t=threading.Thread(target=hold);t.start();held.wait(2)
        started=time.perf_counter()
        try:
            with patch('lan_sync_data.digest',side_effect=counted):payload=self.host.sync.manifest()
        finally:release.set();t.join()
        elapsed=time.perf_counter()-started
        self.assertLess(elapsed,2);self.assertEqual(len(payload['tables']['catalog']['captures']),30);self.assertEqual(len(reads),30)
        self.host.sync.data.hash_cache.clear()
        with patch('lan_sync_data.digest',side_effect=AssertionError('重启后不能重复散列')):
            again=self.host.sync.data.snapshot_current(self.host.sync.config['device'],'现场 A',self.host.sync.host_state())[0]
        self.assertEqual(len(again['tables']['catalog']['captures']),30)
        print(f'\n24,000 历史 + 30 当前：{elapsed:.3f}s；历史文件读取 0；重用持久散列 30',flush=True)

    def test_no_watch_raw_missing_and_one_corrupt_transfer_do_not_block_others(self):
        sid,r=self.host.photo()
        self.host.photo('good.JPG','red')
        missing={**r,'id':'missing','jpg':'missing.JPG'};raw={**r,'id':'raw','jpg':None,'raw':'only.CR3'}
        self.host.store.append_live(sid,[missing,raw])
        snapshot=self.host.sync.manifest();self.assertEqual(len(snapshot['skipped']),2)
        self.connect();original=self.receiver.sync.download
        def fail(peer,info,target):
            if target.name=='one.JPG':raise ValueError('同步文件校验失败')
            return original(peer,info,target)
        with patch.object(self.receiver.sync,'download',side_effect=fail):self.receiver.sync.sync_once()
        local=self.host.local_id(sid);self.assertEqual(len(self.receiver.store.records(local)),1)
        self.assertTrue(any('校验失败' in r['reason'] for r in self.receiver.sync.state()['skipped']))
        self.receiver.sync.sync_once();self.assertEqual(len(self.receiver.store.records(local)),2)
        self.host.sync.host_state=lambda:{'watch':{'enabled':False,'session_id':sid}}
        self.receiver.sync.sync_once();self.assertEqual(self.receiver.sync.state()['phase'],'waiting_host')
        self.assertIsNone(self.host.sync.manifest()['set']);self.assertEqual(len(self.receiver.store.records(local)),2)

    def test_midnight_first_arrival_switches_set_and_freezes_old_ratings_and_documents(self):
        clock=[datetime(2026,11,20,23,59,50,tzinfo=timezone(timedelta(hours=8)))]
        self.host.sync.data.clock=lambda:clock[0]
        sid,r=self.host.photo();r=self.host.store.records(sid)[0]
        def arrival(cid,stamp):
            with self.host.store.connect() as c:
                record=json.loads(c.execute('SELECT data FROM captures WHERE session=? AND id=?',(sid,cid)).fetchone()[0]);record['ingested_at']=stamp
                c.execute('UPDATE captures SET data=? WHERE session=? AND id=?',(stable_json(record),sid,cid))
        arrival(r['id'],clock[0].isoformat());self.connect();self.receiver.sync.sync_once()
        old_id=self.host.local_id(sid);old=copy.deepcopy(self.receiver.store.session(old_id));old_record=self.receiver.store.records(old_id)[0];path=Path(old['root'])/old_record['jpg'];old_bytes=path.read_bytes()
        clock[0]+=timedelta(seconds=20);self.receiver.sync.sync_once()
        self.assertEqual(self.receiver.sync.state()['set']['date'],'2026-11-20')
        _,second=self.host.photo('midnight.JPG','lime');arrival(second['id'],clock[0].isoformat());self.receiver.sync.sync_once()
        current=self.host.sync.manifest();self.assertEqual(current['set']['date'],'2026-11-21');self.assertEqual(len(current['tables']['catalog']['captures']),1)
        new_id=self.host.local_id(sid);self.assertNotEqual(new_id,old_id);self.assertEqual(len(self.receiver.store.sessions()),2)
        self.assertEqual(path.read_bytes(),old_bytes)
        self.assertIn('2026-11-21',self.receiver.store.records(new_id)[0]['jpg'])
        source=self.host.store.session(sid);state=copy.deepcopy(source['state']);state['overrides']={r['id']:5};self.host.store.save(sid,source['revision'],state)
        self.receiver.sync.sync_once();self.assertEqual(self.receiver.store.session(old_id)['state'],old['state']);self.assertEqual(path.read_bytes(),old_bytes)
        # 同一天停止再开始（监控 generation 不同）也必须生成独立本地会话。
        self.host.sync.host_state=lambda:{'watch':{'enabled':True,'session_id':sid,'generation':'new-monitor-run'}}
        self.receiver.sync.sync_once();self.assertNotEqual(self.host.local_id(sid),new_id)

    def test_incremental_rows_gap_fallback_and_ocr_revoke(self):
        sid,first=self.host.photo();_,second=self.host.photo('second.JPG','red')
        self.connect();self.receiver.sync.sync_once();initial=self.host.sync.manifest()
        with self.host.sku.connect() as c:c.execute('INSERT INTO ocr_choices VALUES (?,?,?,?,?)',(sid,first['id'],'SKU-1','key',time.time()))
        after=self.host.sync.manifest();response=self.receiver.sync.request_json(self.peer()['address'],f"/sync/changes?set={initial['set']['id']}&after={initial['seq']}&wait=0",token=self.receiver.sync.token)
        change=response['changes'][0]
        self.assertEqual(len(change['tables']['sku']['ocr_choices']),1)
        self.assertEqual(change['tables']['catalog']['captures'],[])
        self.receiver.sync.sync_once();local_sid=self.host.local_id(sid)
        with self.receiver.sku.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM ocr_choices WHERE session_id=?',(local_sid,)).fetchone()[0],1)
        with self.host.sku.connect() as c:c.execute('DELETE FROM ocr_choices WHERE session_id=?',(sid,))
        revoked=self.host.sync.manifest()
        self.receiver.sync.sync_once()
        with self.receiver.sku.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM ocr_choices WHERE session_id=?',(local_sid,)).fetchone()[0],0)
        self.host.photo('third.JPG','blue');third=self.host.sync.manifest()
        self.host.photo('fourth.JPG','yellow');fourth=self.host.sync.manifest()
        with self.host.store.connect() as c:c.execute('DELETE FROM lan_sync_changes WHERE seq=?',(third['seq'],))
        response=self.receiver.sync.request_json(self.peer()['address'],f"/sync/changes?set={initial['set']['id']}&after={revoked['seq']}&wait=0",token=self.receiver.sync.token)
        self.assertTrue(response['set_changed'])
        self.receiver.sync.sync_once();self.assertEqual(len(self.receiver.store.records(local_sid)),4)
        # 长轮询期间主动退出必须及时返回，不等待 25 秒。
        self.receiver.sync.thread.start();time.sleep(.1);started=time.monotonic();self.receiver.sync.disconnect()
        self.assertLess(time.monotonic()-started,2)
        self.receiver.sync.connect_peer({'device':self.host.sync.config['device'],'password':'同步 001Ab!'})
        time.sleep(.15);started=time.monotonic();self.receiver.sync.configure({'enabled':False})
        self.assertLess(time.monotonic()-started,2)

    def test_transfer_ack_tracks_actual_inflight_file_and_bytes(self):
        sid,_=self.host.photo();peer=self.connect();payload=self.host.sync.manifest();info=payload['tables']['catalog']['captures'][0]['sync_files'][0]
        target=Path(self.receiver.sync.config['remote']['root'])/info['relative']
        self.receiver.sync.status.update(pending_files=1,total_files=1);self.receiver.sync.report()
        self.assertEqual(self.host.sync.state()['sending_count'],0)
        entered=threading.Event();resume=threading.Event();request=self.receiver.sync.request
        class PausedResponse:
            def __init__(self,response):self.response=response
            def __enter__(self):return self
            def __exit__(self,*args):self.response.close()
            def read(self,size):
                value=self.response.read(size)
                if value:entered.set();resume.wait(5)
                return value
        def paused(address,path,*args,**kwargs):
            result=request(address,path,*args,**kwargs)
            return PausedResponse(result) if path.startswith('/sync/blob/') else result
        with patch.object(self.receiver.sync,'request',side_effect=paused),ThreadPoolExecutor(max_workers=1) as pool:
            task=pool.submit(self.receiver.sync.download,peer,info,target)
            try:
                self.assertTrue(entered.wait(2));self.assertEqual(self.receiver.sync.status['current_file'],'one.JPG')
                self.receiver.sync.report();self.assertEqual(self.host.sync.state()['sending_count'],1)
            finally:resume.set()
            self.assertTrue(task.result())
        self.assertEqual(self.receiver.sync.status['received_bytes'],info['size'])
        self.assertEqual(self.receiver.sync.status['current_file'],'')
        self.receiver.sync.status.update(seq=payload['seq'],pending_files=0,received_files=1,last_sync=time.time());self.receiver.sync.report()
        self.assertEqual(self.host.sync.state()['sending_count'],0)
        delay=self.host.sync.state()['connected_clients'][0]['delay']
        with patch('lan_capture_sync.time.time',return_value=time.time()+120):self.receiver.sync.report()
        self.assertEqual(self.host.sync.state()['connected_clients'][0]['delay'],delay)

    def test_self_login_and_protocol1_current_only(self):
        sid,_=self.host.photo();peer=self.peer()
        challenge=self.receiver.sync.request_json(peer['address'],'/sync/challenge')
        key=password_key('同步 001Ab!',challenge['salt'])
        body={'nonce':challenge['nonce'],'proof':signature(key,challenge['nonce']+':'+peer['device']),'client_device':peer['device'],'client_name':'self'}
        with self.assertRaises(HTTPError) as error:self.receiver.sync.request_json(peer['address'],'/sync/login',body)
        self.assertEqual(error.exception.code,409);self.assertIn('不能连接本机',error.exception.read().decode())
        challenge=self.receiver.sync.request_json(peer['address'],'/sync/challenge');body.update(nonce=challenge['nonce'],proof=signature(key,challenge['nonce']+':'+peer['device']),client_device='f'*32)
        result=self.receiver.sync.request_json(peer['address'],'/sync/login',body)
        payload=self.receiver.sync.request_json(peer['address'],'/sync/manifest',token=result['token'])
        self.assertEqual(len(payload['tables']['catalog']['sessions']),1)
        self.assertEqual(self.host.sync.state()['connected_clients'][0]['phase'],'outdated')
        self.assertEqual(self.host.sync.state()['sending_count'],0)

    def test_metadata_commit_lock_remains_available_during_slow_hash(self):
        self.host.photo();entered=threading.Event();resume=threading.Event()
        original=digest
        def slow(path):entered.set();resume.wait(5);return original(path)
        with patch('lan_sync_data.digest',side_effect=slow),ThreadPoolExecutor(max_workers=1) as pool:
            task=pool.submit(self.host.sync.manifest);self.assertTrue(entered.wait(2))
            started=time.perf_counter()
            with self.host.archive._metadata_commit_lock():
                with self.host.archive.connect() as c:c.execute("INSERT INTO audit VALUES ('concurrent','test','{}',?)",(time.time(),))
            elapsed=time.perf_counter()-started;resume.set();task.result()
        self.assertLess(elapsed,.5)
        print(f'\n并发元数据提交：{elapsed:.3f}s，无归档锁等待',flush=True)


class MultiClientTests(unittest.TestCase):
    setUp=lan_fixture.LanCaptureSyncTests.setUp
    tearDown=lan_fixture.LanCaptureSyncTests.tearDown
    def test_three_clients_live_delta_offline_60s_and_restarts(self):
        sid,r=self.host.photo();pid=self.host.sku.import_file('plan.xlsx',fixture_book())['project']['id'];self.host.sku.configure(pid,sid,'模特图',True)
        aid=self.host.archive.ensure(sid,r['id'])
        clients=[self.receiver]
        for i in range(2):
            node=Node(self.base/('client-'+str(i)),f'客户端 {i+2}',self.host.sync.discovery_port);self.addCleanup(node.close);clients.append(node)
        for client in clients:
            peer={'device':self.host.sync.config['device'],'name':'现场 A','address':f'http://127.0.0.1:{self.host.sync.server.server_port}','receiving':False,'seen':time.time()}
            client.sync.peers[peer['device']]=peer;client.sync.connect_peer({'device':peer['device'],'password':'同步 001Ab!'})
            client.sync.thread.start()
        def wait(predicate,timeout=2):
            until=time.monotonic()+timeout
            while time.monotonic()<until:
                if predicate():return
                time.sleep(.025)
            self.assertTrue(predicate(),'多端同步没有在期限内完成：'+stable_json([c.sync.state() for c in clients]))
        local=self.host.local_id(sid);wait(lambda:all(len(c.store.records(local))==1 for c in clients),5)
        wait(lambda:self.host.sync.state()['synced_count']==3,3)
        started=time.monotonic();_,second=self.host.photo('live.JPG','red')
        wait(lambda:all(len(c.store.records(local))==2 for c in clients));photo_delay=time.monotonic()-started
        for client in clients:
            for record in client.store.records(local):self.assertEqual((Path(client.store.session(local)['root'])/record['jpg']).read_bytes(),(self.host.photos/Path(record['jpg']).name).read_bytes())
        source=self.host.store.session(sid);state=copy.deepcopy(source['state']);state['overrides']={r['id']:4}
        started=time.monotonic();self.host.store.save(sid,source['revision'],state)
        with self.host.archive.connect() as c:c.execute('UPDATE assets SET sku=?,normalized=? WHERE id=?',(CODE,CODE,aid))
        capture_id=self.host.local_id(r['id'])
        wait(lambda:all(c.store.session(local)['state']['overrides'].get(capture_id)==4 and c.archive.asset(aid)['sku']==CODE for c in clients));metadata_delay=time.monotonic()-started
        # 在 HTTP 请求出口注入真实断网错误；中止已挂起长轮询后保持一分钟。
        victim=clients[-1];original=victim.sync.request
        offline=threading.Event();offline.set()
        def network(*args,**kwargs):
            if offline.is_set():raise URLError('network unreachable')
            return original(*args,**kwargs)
        victim.sync.request=network
        # 让已挂起的请求返回，然后再开始断网计时。
        self.host.photo('offline-start.JPG','blue');wait(lambda:len(clients[0].store.records(local))==3)
        wait(lambda:victim.sync.state()['phase']=='reconnecting',3)
        cutoff=time.monotonic();self.host.photo('offline-later.JPG','orange')
        wait(lambda:all(len(c.store.records(local))==4 for c in clients[:-1]))
        while time.monotonic()-cutoff<60:
            if time.monotonic()-cutoff>31:self.assertEqual(next(c for c in self.host.sync.state()['connected_clients'] if c['device']==victim.sync.config['device'])['phase'],'disconnected')
            time.sleep(.25)
        offline.clear();victim.sync.wake.set();wait(lambda:len(victim.store.records(local))==4,5)
        wait(lambda:self.host.sync.state()['synced_count']==3,3)
        # 主机重启：端口及设备身份保持，token 失效后各客户端重新认证。
        seq=self.host.sync.manifest()['seq'];watch=self.host.sync.host_state;port=self.host.sync.server.server_port
        self.host.sync.close();self.host.sync=LanCaptureSync(self.host.store,self.host.sku,self.host.archive,autostart=False,host_state=watch,discovery_port=self.host.sync.discovery_port)
        self.host.sync._start();self.assertEqual(self.host.sync.server.server_port,port)
        self.host.photo('after-host-restart.JPG','yellow');wait(lambda:all(len(c.store.records(local))==5 for c in clients),10)
        self.assertGreater(self.host.sync.manifest()['seq'],seq)
        # 客户端重启后游标和投影持久恢复，不重复记录。
        client=clients[0];client.sync.close();client.sync=LanCaptureSync(client.store,client.sku,client.archive,autostart=False)
        restored_seq=client.sync.status['seq'];self.assertGreater(restored_seq,0)
        request=client.sync.request
        def resumed(address,path,*args,**kwargs):
            self.assertFalse(path.startswith('/sync/current'),'客户端重启必须从持久游标续接')
            return request(address,path,*args,**kwargs)
        with patch.object(client.sync,'request',side_effect=resumed):client.sync.sync_once()
        self.assertEqual(len(client.store.records(local)),5);self.assertEqual(len(client.store.sessions()),1)
        wait(lambda:self.host.sync.state()['synced_count']==3,3)
        self.assertEqual(self.host.sync.state()['sending_count'],0)
        print(f'\n三客户端：新照片 {photo_delay:.3f}s；改星/款号 {metadata_delay:.3f}s；断网 60s 后补齐；主机/客户端重启后无重复',flush=True)

if __name__=='__main__':unittest.main()
