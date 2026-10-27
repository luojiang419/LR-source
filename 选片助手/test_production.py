import copy
import importlib.util
import json
import tempfile
import threading
import urllib.request
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
from PIL import Image
import media_engine as media
from catalog_store import Store

ROOT=Path(__file__).resolve().parents[1]

class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.photos=self.base/'photos';self.photos.mkdir();self.store=Store(self.base/'data',ROOT)
        for i in range(4):Image.new('RGB',(100,80),(80+i*10,90,100)).save(self.photos/f'photo{i}.JPG')
    def tearDown(self):self.temp.cleanup()
    def metadata(self,paths,exe,**kwargs):return [{'DateTimeOriginal':f'2026:10:17 10:00:{i:02d}','Model':'TestCamera','Orientation':1,'ImageWidth':100,'ImageHeight':80} for i,_ in enumerate(paths)]
    def scan(self):
        with patch('catalog_store.threading.Thread'),patch('media_engine.find_exiftool',return_value=Path('test.exe')),patch('media_engine.read_metadata',side_effect=self.metadata):
            job=self.store.scan(self.photos);self.store._scan(job,self.photos,True)
        self.assertEqual(self.store.jobs[job]['status'],'completed',self.store.jobs[job]);return self.store.session()
    def test_multiple_folders_keep_ratings_rules_and_active_session_separate(self):
        first=self.scan();cid=self.store.records()[0]['id']
        self.store.save(first['id'],first['revision'],{'overrides':{cid:5},'keep':1,'threshold':4})
        other=self.base/'another-task';other.mkdir()
        Image.new('RGB',(100,80)).save(other/'photo0.JPG')
        with patch('catalog_store.threading.Thread'),patch('media_engine.find_exiftool',return_value=Path('test')),patch('media_engine.read_metadata',side_effect=self.metadata):
            job=self.store.scan_many([str(other)],activate=False)[0]
            self.store._scan(job,other,True,False)
        self.assertEqual(self.store.active_id(),first['id'])
        second=self.store.jobs[job]['session_id'];self.store.activate(second)
        cid2=self.store.records()[0]['id'];self.store.save(second,0,{'overrides':{cid2:2},'keep':2,'threshold':9})
        self.assertEqual(cid,cid2) # 同名照片也必须按 session 隔离。
        reopened=Store(self.base/'data',ROOT)
        reopened.activate(first['id']);self.assertEqual(reopened.manifest()['captures'][0]['rating'],5)
        self.assertEqual(reopened.session()['state']['threshold'],4)
        reopened.activate(second);self.assertEqual(reopened.manifest()['captures'][0]['rating'],2)
        self.assertEqual(reopened.session()['state']['keep'],2)
        self.assertEqual(reopened.manifest(sid=first['id'])['captures'][0]['rating'],5)
        self.assertEqual(sorted(s['capture_count'] for s in reopened.sessions()),[1,4])

    def test_batch_queue_deduplicates_and_cancelled_task_does_not_block_next(self):
        other=self.base/'missing'
        with patch('catalog_store.threading.Thread') as threads:
            ids=self.store.scan_many([str(self.photos),str(self.photos),str(other)])
            self.assertEqual(ids[0],ids[1]);self.assertEqual(threads.call_count,1)
            self.assertEqual(self.store.jobs[ids[0]]['status'],'queued')
            self.store.cancel_scan(ids[0]);self.store._scan_worker()
        self.assertEqual(self.store.jobs[ids[0]]['status'],'cancelled')
        self.assertEqual(self.store.jobs[ids[-1]]['status'],'failed')
        self.assertIn('无法读取文件夹',self.store.jobs[ids[-1]]['error'])
        self.assertFalse(self.store.scan_worker_running);self.assertIsNone(self.store.session())

    def test_cancel_during_metadata_preserves_existing_index_and_manual_rating(self):
        original=self.scan();cid=self.store.records()[0]['id'];self.store.save(original['id'],0,{'overrides':{cid:5}})
        Image.new('RGB',(120,80)).save(self.photos/'photo0.JPG')
        def cancelled(paths,exe,**kwargs):
            self.store.cancel_scan(job);raise media.ScanCancelled()
        with patch('catalog_store.threading.Thread'),patch('media_engine.find_exiftool',return_value=Path('test')),patch('media_engine.read_metadata',side_effect=cancelled):
            job=self.store.scan(self.photos);self.store._scan(job,self.photos,True)
        self.assertEqual(self.store.jobs[job]['status'],'cancelled')
        self.assertEqual(self.store.session()['snapshot'],original['snapshot'])
        self.assertEqual(self.store.session()['state']['overrides'][cid],5)

    def test_blank_path_rejected_before_enqueuing_any_task(self):
        for folders in [[],[''],['   '],[str(self.photos),''],['""']]:
            with self.subTest(folders=folders),self.assertRaises(ValueError):self.store.scan_many(folders)
        self.assertFalse(self.store.jobs)

    def test_metadata_process_is_terminated_when_cancelled(self):
        import subprocess
        process=MagicMock()
        process.__enter__.return_value=process
        cancel=threading.Event()
        calls=0
        def blocked(**kwargs):
            nonlocal calls
            calls+=1
            if calls==1:cancel.set();raise subprocess.TimeoutExpired('test',.3)
            return b'',b''
        process.communicate.side_effect=blocked
        with patch('media_engine.subprocess.Popen',return_value=process),self.assertRaises(media.ScanCancelled):
            media.read_metadata([self.photos/'photo0.JPG'],Path('test'),cancel=cancel)
        process.kill.assert_called_once()

    def test_actual_catalog_and_formal_manifest(self):
        session=self.scan();self.assertEqual(len(self.store.records()),4);m=self.store.manifest();self.assertFalse(m['demo']);self.assertEqual(m['version'],2);self.assertEqual(m['source']['root'],str(self.photos));self.assertEqual(len(m['captures']),1)
    def test_manual_rating_survives_restart_and_revision_conflict(self):
        s=self.scan();rid=self.store.records()[0]['id'];revision=self.store.save(s['id'],0,{'overrides':{rid:5},'keep':1,'threshold':4})
        self.assertEqual(revision,1);reopened=Store(self.base/'data',ROOT);self.assertEqual(reopened.session()['state']['overrides'][rid],5)
        with self.assertRaisesRegex(ValueError,'其他窗口'):self.store.save(s['id'],0,{'overrides':{}})
    def test_metadata_cache_and_changed_file_invalidates_decision(self):
        s=self.scan();rid=self.store.records()[0]['id'];self.store.save(s['id'],0,{'overrides':{rid:5}})
        with patch('media_engine.read_metadata',side_effect=AssertionError('should use cache')):
            with patch('catalog_store.threading.Thread'),patch('media_engine.find_exiftool',return_value=Path('unused')):
                job=self.store.scan(self.photos);self.store._scan(job,self.photos,True)
        self.assertEqual(self.store.jobs[job]['status'],'completed');self.assertEqual(self.store.session()['state']['overrides'][rid],5)
        Image.new('RGB',(120,80)).save(self.photos/'photo0.JPG');self.scan();self.assertNotIn(rid,self.store.session()['state']['overrides'])
    def test_identical_save_retry_is_idempotent(self):
        s=self.scan();state={'overrides':{},'keep':1,'threshold':4}
        self.assertEqual(self.store.save(s['id'],0,state),1);self.assertEqual(self.store.save(s['id'],0,state),1)
    def test_raw_jpg_pair_and_duplicate_raw_not_dropped(self):
        (self.photos/'photo0.CR3').write_bytes(b'raw');(self.photos/'photo0.DNG').write_bytes(b'raw2')
        _,shots=media.discover(self.photos);self.assertEqual(len(shots),5);self.assertEqual(sum(1+bool(pair) for _,pair in shots),6)
    def test_relative_path_escape_rejected(self):
        for path in ['../secret','/absolute','C:/absolute','folder/../evil','folder\\evil','']:
            with self.subTest(path=path),self.assertRaises((ValueError,FileNotFoundError)):media.resolved_file(self.photos,path)
    def test_subsecond_timezone_and_missing_time(self):
        self.assertEqual(media.parse_time({'SubSecDateTimeOriginal':'2026:10:17 10:00:00.123+08:00'}),'2026-10-17T10:00:00.123000+08:00');self.assertIsNone(media.parse_time({}))
        p=self.photos/'photo0.JPG';r=media.capture_record(self.photos,p,None,{})
        self.assertTrue(r['missingTime']);self.assertIsNone(r['time'])
    def test_real_exiftool_unicode_filename_and_subsecond(self):
        exif=Image.Exif();exif[272]='Test';exif[34665]={36867:'2026:10:17 10:00:00',37521:'85'}
        path=self.photos/'中文-é.JPG';Image.new('RGB',(100,80)).save(path,exif=exif)
        rows=media.read_metadata([path],media.find_exiftool(ROOT));self.assertEqual(len(rows),1);self.assertNotIn('Error',rows[0]);self.assertIn('10:00:00.85',rows[0]['SubSecDateTimeOriginal'])
    def test_missing_time_records_kept_in_time_rules(self):
        with patch.object(self,'metadata',return_value=[{}]*4):self.scan()
        self.assertEqual(len(self.store.manifest()['captures']),4)
    def test_preview_cache_and_image_orientation(self):
        s=self.scan();r=self.store.records()[0];path=media.preview(r,self.photos,self.base/'cache',Path('unused'),480)
        self.assertTrue(path.is_file());self.assertEqual(path,media.preview(r,self.photos,self.base/'cache',Path('unused'),480))
        with Image.open(path) as im:self.assertEqual(im.size,(100,80))
    def test_old_clients_cannot_restore_ai_selection(self):
        s=self.scan()
        with self.assertRaisesRegex(ValueError,'已移除'):self.store.save(s['id'],0,{'overrides':{},'analysisMode':'ai','activeAiRunId':'old'})

class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from lupa import LuaRuntime
        cls.LuaRuntime=LuaRuntime
    def setUp(self):
        self.lua=self.LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute('package.path='+json.dumps(str(ROOT/'FramekeepBridge.lrplugin'/'?.lua').replace('\\','/'))+'..";"..package.path')
        self.lua.execute('''photos={};writeCount=0;messages={}
        function addPhoto(path,rating)
          local p={path=path,rating=rating};function p:getRawMetadata(k) if k=='path' then return self.path else return self.rating end end;function p:setRawMetadata(k,v) self.rating=v end;photos[string.lower(path)]=p;return p
        end
        catalog={};function catalog:findPhotoByPath(path) return photos[string.lower(path)] end;function catalog:withWriteAccessDo(name,fn,opts) writeCount=writeCount+1;fn() end
        function catalog:getAllPhotos() local r={};for _,p in pairs(photos) do table.insert(r,p) end;return r end
        function catalog:batchGetRawMetadata(items,fields) local r={};for _,p in ipairs(items) do r[p]={path=p.path} end;return r end
        function catalog:getTargetPhoto() return nil end
        prefs={};modules={LrApplication={activeCatalog=function() return catalog end},LrDialogs={confirm=function()return 'ok'end,message=function(...)table.insert(messages,{...})end},LrFileUtils={},LrPathUtils={child=function(root,part)return root..'/'..part end},LrTasks={pcall=pcall},LrHttp={},LrPrefs={prefsForPlugin=function()return prefs end}}
        function import(name) return modules[name] end
        ''')
        self.json=self.lua.eval("(require('Json'))");self.bridge=self.lua.eval("(require('BridgeCore'))")
    def manifest(self,captures=None):return {'schema':'framekeep.selection','version':2,'demo':False,'source':{'root':'X:/photos'},'applyPolicy':{'mode':'selected-only','clearUnlisted':False},'captures':captures or [{'relativePaths':['a.CR3','a.JPG'],'rating':1}]}
    def decoded(self,value):return self.json.decode(json.dumps(value,ensure_ascii=True))
    def test_lua_files_compile(self):
        for path in (ROOT/'FramekeepBridge.lrplugin').glob('*.lua'):self.lua.execute('assert(load(...))',path.read_text(encoding='utf8'))
    def test_json_unicode_surrogates_and_roundtrip(self):
        value=self.json.decode('{"path":"\\u4e2d\\u6587\\ud83d\\udcf7","rating":5,"demo":false}')
        self.assertEqual(value['path'],'中文📷');self.assertEqual(json.loads(self.json.encode(value))['rating'],5)
    def test_json_rejects_duplicate_trailing_and_bad_numbers(self):
        for text in ['{"a":1,"a":2}','[1,]','01','NaN','1e999','"\\ud800"','{} extra']:
            with self.subTest(text=text),self.assertRaises(Exception):self.json.decode(text)
    def test_reject_demo_traversal_duplicates_and_rating(self):
        bad=[];m=self.manifest();m['demo']=True;bad.append(m)
        for path in ['../a.CR3','C:/evil.CR3','/evil','sub/../../a','sub\\a']:
            m=self.manifest([{'relativePaths':[path],'rating':1}]);bad.append(m)
        bad.append(self.manifest([{'relativePaths':['a.CR3','A.CR3'],'rating':1}]))
        bad.append(self.manifest([{'relativePaths':['a.CR3'],'rating':6}]))
        for m in bad:
            with self.subTest(m=m),self.assertRaises(Exception):self.bridge.validate(self.decoded(m))
    def test_sdk_rating_protection_pair_companion_and_undo(self):
        photo=self.lua.globals().addPhoto('X:/photos/a.CR3',0);plan=self.bridge.plan(self.decoded(self.manifest()))
        self.assertEqual(plan['matched'],1);self.assertEqual(plan['companions'],1);self.assertEqual(len(plan['changes']),1)
        result=self.bridge.apply(plan);self.assertEqual(result['changed'],1);self.assertEqual(photo['rating'],1);self.assertEqual(self.lua.globals().writeCount,1)
        self.bridge.undo();self.assertIsNone(photo['rating'])
        photo['rating']=5;plan=self.bridge.plan(self.decoded(self.manifest()));self.assertEqual(plan['protected'],1);self.assertEqual(len(plan['changes']),0)
    def test_changes_after_preview_and_after_apply_protected(self):
        p=self.lua.globals().addPhoto('X:/photos/a.CR3',0);plan=self.bridge.plan(self.decoded(self.manifest()));p['rating']=3
        result=self.bridge.apply(plan);self.assertEqual(result['conflicts'],1);self.assertEqual(p['rating'],3)
        p['rating']=0;self.bridge.apply(self.bridge.plan(self.decoded(self.manifest())));p['rating']=4;self.bridge.undo();self.assertEqual(p['rating'],4)
    def test_missing_capture_does_not_write_catalog(self):
        plan=self.bridge.plan(self.decoded(self.manifest()));self.assertEqual(plan['missing'],1);self.assertEqual(len(plan['changes']),0)

    def test_drive_unc_alias_exact_path_matching(self):
        p=self.lua.globals().addPhoto('//server/share/photos/a.CR3',0)
        manifest=self.manifest();manifest['source']['pathAliases']=['//server/share/photos']
        plan=self.bridge.plan(self.decoded(manifest));self.assertEqual(plan['matched'],1);self.assertEqual(plan['missing'],0)
        self.bridge.apply(plan);self.assertEqual(p['rating'],1)

    def test_reconnect_after_first_service_failure(self):
        self.lua.execute('''gets=0;sleeps=0
        modules.LrHttp.get=function(url,headers,timeout)
          gets=gets+1
          if gets==1 then return nil end
          if url:match('/hello$') then return '{"schema":"framekeep.bridge","token":"test"}' end
          return '{"job":null}'
        end
        modules.LrTasks.sleep=function(seconds) sleeps=sleeps+1;if sleeps>=2 then require('BridgeCore').stop() end end
        ''')
        self.bridge.connect(True);self.assertEqual(self.lua.globals().gets,3);self.assertEqual(self.lua.globals().sleeps,2)

    def test_catalog_diagnosis_finds_same_folder_on_other_share(self):
        self.lua.globals().addPhoto('Y:/2026/2026-10-14/a.CR3',0)
        self.lua.globals().addPhoto('Y:/2026/2026-10-16/a.CR3',0)
        self.lua.execute('modules.LrHttp.get=function()return nil end')
        manifest=self.manifest();manifest['source']['folderName']='2026-10-14'
        dirs=self.bridge.diagnose(self.decoded(manifest));self.assertEqual(len(dirs),1);self.assertEqual(dirs[1]['path'],'Y:/2026/2026-10-14');self.assertEqual(dirs[1]['matchedPaths'],1)

    def test_process_maps_unique_source_but_not_other_date(self):
        p=self.lua.globals().addPhoto('Y:/2026/2026-10-14/a.CR3',0)
        other=self.lua.globals().addPhoto('Y:/2026/2026-10-16/a.CR3',0)
        self.lua.execute('modules.LrHttp.get=function()return nil end')
        manifest=self.manifest();manifest['source']['folderName']='2026-10-14'
        status,result=self.bridge.process(self.decoded(manifest));self.assertEqual(status,'applied');self.assertEqual(result['changed'],1);self.assertEqual(p['rating'],1);self.assertEqual(other['rating'],0)

class FormalHttpTests(unittest.TestCase):
    def setUp(self):
        import server
        self.server_module=server;self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.photos=self.base/'照片';self.photos.mkdir()
        Image.new('RGB',(100,80)).save(self.photos/'test.JPG')
        self.store=Store(self.base/'data',ROOT)
        with patch('catalog_store.threading.Thread'),patch('media_engine.find_exiftool',return_value=Path('unused')),patch('media_engine.read_metadata',return_value=[{'DateTimeOriginal':'2026:10:17 10:00:00','Model':'Test'}]):
            job=self.store.scan(self.photos);self.store._scan(job,self.photos,True)
        self.patches=[patch.object(server,'STORE',self.store),patch.object(server,'DATA',self.base/'data'),patch.object(server,'BRIDGE_LAST_SEEN',None),patch.object(server,'BRIDGE_DIAGNOSTIC',None)]
        for p in self.patches:p.start()
        self.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);self.http.daemon_threads=True
        threading.Thread(target=self.http.serve_forever,daemon=True).start();self.url=f'http://127.0.0.1:{self.http.server_port}'
    def tearDown(self):
        self.http.shutdown();self.http.server_close()
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def get(self,path,headers=None):
        with urllib.request.urlopen(urllib.request.Request(self.url+path,headers=headers or {})) as r:return json.load(r)
    def post(self,path,body,bridge=False):
        key='X-Framekeep-Bridge' if bridge else 'X-Framekeep-Token';value=self.server_module.BRIDGE_TOKEN if bridge else self.server_module.TOKEN
        req=urllib.request.Request(self.url+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json',key:value},method='POST')
        with urllib.request.urlopen(req) as r:return json.load(r)
    def test_formal_export_and_saved_state_revision(self):
        s=self.store.session();cid=self.store.records()[0]['id'];r=self.post('/api/session',{'session_id':s['id'],'revision':0,'state':{'overrides':{cid:5}}})
        self.assertEqual(r['revision'],1);m=self.post('/api/export',{'selected_only':True});self.assertEqual(m['version'],2);self.assertFalse(m['demo']);self.assertEqual(m['captures'][0]['rating'],5)
        with self.assertRaises(urllib.error.HTTPError) as e:self.post('/api/session',{'session_id':s['id'],'revision':0,'state':{'overrides':{}}})
        self.assertEqual(e.exception.code,409)
    def test_desktop_health_and_shutdown_requires_instance_secret(self):
        health=self.get('/api/health');self.assertEqual(health['application'],'framekeep');self.assertFalse(health['busy']);self.assertGreater(health['process_id'],0)
        with patch.dict('os.environ',{'FRAMEKEEP_DESKTOP_TOKEN':'isolated-desktop-secret'}):
            with self.assertRaises(urllib.error.HTTPError) as error:self.post('/api/desktop/shutdown',{})
            self.assertEqual(error.exception.code,403)
            stopped=threading.Event()
            with patch.object(self.http,'shutdown',side_effect=stopped.set):
                request=urllib.request.Request(self.url+'/api/desktop/shutdown',data=b'{}',headers={'Content-Type':'application/json','X-Framekeep-Desktop':'isolated-desktop-secret'},method='POST')
                with urllib.request.urlopen(request) as response:self.assertTrue(json.load(response)['ok'])
                self.assertTrue(stopped.wait(1))
        self.assertEqual(self.get('/api/health')['application'],'framekeep')
    def test_bridge_queue_ack_and_heartbeat(self):
        job=self.post('/api/bridge',{});hello=self.get('/bridge/hello');self.assertEqual(hello['schema'],'framekeep.bridge')
        queued=self.get('/bridge/next',{'X-Framekeep-Bridge':hello['token']})['job'];self.assertEqual(queued['id'],job['id']);self.assertEqual(len(queued['manifest']['captures']),1)
        self.post('/bridge/result',{'id':job['id'],'status':'applied','result':{'changed':1}},bridge=True)
        status=self.get('/api/bridge/status');self.assertTrue(status['connected']);self.assertEqual(status['latest_job']['status'],'applied');self.assertEqual(self.get('/bridge/next',{'X-Framekeep-Bridge':hello['token']})['job'],None)
    def test_bridge_diagnostics_and_superseded_jobs(self):
        first=self.post('/api/bridge',{});second=self.post('/api/bridge',{})
        self.assertNotEqual(first['id'],second['id'])
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT status FROM bridge_jobs WHERE id=?',(first['id'],)).fetchone()[0],'superseded')
        diagnostic={'sourceFolder':'2026-10-14','directories':[{'path':'Y:/2026-10-14','count':1701,'matchedPaths':1}]}
        self.post('/bridge/diagnostics',diagnostic,bridge=True);self.assertEqual(self.get('/api/bridge/status')['diagnostic'],diagnostic)
    def test_preview_photo_route_and_no_raw_button_for_jpeg(self):
        state=self.get('/api/state');photo=state['catalog'][0];self.assertNotIn('previewRaw',photo)
        with urllib.request.urlopen(self.url+photo['preview']) as r:self.assertEqual(r.headers.get_content_type(),'image/jpeg');self.assertTrue(r.read().startswith(b'\xff\xd8'))
    def test_batch_folder_api_cancel_and_scoped_export(self):
        first=self.store.session();other=self.base/'second';other.mkdir();Image.new('RGB',(100,80)).save(other/'test.JPG')
        with patch.object(self.store,'scan_worker_running',True):
            ids=self.post('/api/folders',{'folders':[str(other),str(self.base/'missing')]})['scan_ids']
        self.assertEqual(len(ids),2)
        state=self.get('/api/folders');self.assertEqual(state['sessions'][0]['capture_count'],1)
        self.assertEqual(self.post('/api/scan/cancel',{'scan_id':ids[1]})['status'],'cancelled')
        with patch('media_engine.find_exiftool',return_value=Path('unused')),patch('media_engine.read_metadata',return_value=[{}]):self.store._scan_worker()
        second=self.store.jobs[ids[0]]['session_id'];self.post('/api/activate',{'session_id':second})
        scoped=self.get('/api/state?session_id='+first['id']);self.assertEqual(scoped['session']['id'],first['id'])
        manifest=self.post('/api/export',{'session_id':first['id']});self.assertEqual(manifest['source']['sessionId'],first['id'])
        with self.assertRaises(urllib.error.HTTPError):self.post('/api/folders',{'folders':['']})

    def test_formal_auth_and_private_paths(self):
        for path in ['/../api%20key.md','/.framekeep/framekeep.sqlite3','/assets/']:
            with self.subTest(path=path),self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(self.url+path)
            self.assertIn(e.exception.code,(400,403))
        req=urllib.request.Request(self.url+'/api/export',data=b'{}',headers={'Content-Type':'application/json'},method='POST')
        with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(req)
        self.assertEqual(e.exception.code,403)

    def test_retired_ai_routes_and_state_do_not_expose_runs(self):
        for path in ['/api/analyze','/api/reviews']:
            with self.subTest(path=path),self.assertRaises(urllib.error.HTTPError) as error:self.post(path,{})
            self.assertEqual(error.exception.code,410)
        with self.assertRaises(urllib.error.HTTPError) as error:self.get('/api/runs/old')
        self.assertEqual(error.exception.code,410)
        state=self.get('/api/state')
        self.assertNotIn('latest_run',state);self.assertNotIn('latest_successful_run',state)
        with urllib.request.urlopen(self.url+'/assets/demo-data.js') as response:self.assertNotIn(b'activeAiRun',response.read())

    def test_assistant_config_saved_sanitized_and_failed_save_preserves_previous(self):
        config=self.base/'助手配置.md';config.write_text('https://example.invalid/v1\nsk-test-only-secret\ngpt-test',encoding='utf8')
        with patch.dict('os.environ',{},clear=False):
            result=self.post('/api/config',{'path':str(config)})
            self.assertTrue(result['configured']);self.assertEqual(result['model'],'gpt-test')
            self.assertEqual(self.get('/api/config'),result);self.assertNotIn('sk-test-only-secret',json.dumps(result))
            self.assertEqual((self.base/'data/config-location').read_text(encoding='utf8'),str(config))
            bad=self.base/'invalid.md';bad.write_text('not valid',encoding='utf8')
            with self.assertRaises(urllib.error.HTTPError) as error:self.post('/api/config',{'path':str(bad)})
            self.assertEqual(error.exception.code,400);self.assertEqual(self.get('/api/config'),result)
            with self.assertRaises(urllib.error.HTTPError) as error:self.post('/api/config',{'path':str(self.base/'missing.md')})
            self.assertEqual(error.exception.code,400);self.assertEqual(self.get('/api/config'),result)

if __name__=='__main__':unittest.main()
