"""正式参考库：后台索引、授权、分页、持久偏好与真实 LAN HTTP。"""
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler
from unittest.mock import patch

from PIL import Image
from reference_library import ReferenceLibrary, normalize_address


def available_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class ReferenceTests(unittest.TestCase):
    def test_all_roots_search_paginates_filters_hidden_and_deduplicates_overlaps(self):
        from reference_api import local_get
        extra=self.base/'other';extra.mkdir()
        for index in range(65):(extra/f'global-{index:03d}.JPG').write_bytes((self.photos/'one.JPG').read_bytes())
        root=self.library.add_root(extra)['root_id'];self.ready(self.library,root)
        duplicate=self.library.add_root(self.photos/'sub')['root_id'];self.ready(self.library,duplicate)
        self.assertEqual(self.library.search_all('two')['total'],1)
        first=local_get(self.library,'search',{'q':'global','cache_generation':self.library.cache.generation})
        self.assertEqual(first['total'],65);self.assertEqual(len(first['rows']),60)
        second=self.library.search_all('global',first['next_cursor']);self.assertEqual(len(second['rows']),5)
        self.assertFalse({row['id'] for row in first['rows']} & {row['id'] for row in second['rows']})
        self.assertEqual({row['root'] for row in first['rows']},{root})
        self.library.set_root_visibility(root,False)
        self.assertEqual(self.library.search_all('global')['total'],0)
        with self.assertRaisesRegex(ValueError,'范围已变化'):self.library.search_all('global',first['next_cursor'])
        with self.assertRaisesRegex(ValueError,'拍摄任务已切换'):self.library.search_all('global',generation=self.library.cache.generation+1)
        self.library.set_root_visibility(root,True);original=self.library.items
        def offline(root_id,*args,**kwargs):
            if root_id==root:raise ValueError('模拟位置离线')
            return original(root_id,*args,**kwargs)
        with patch.object(self.library,'items',side_effect=offline):
            result=self.library.search_all('two')
        self.assertEqual(result['total'],1);self.assertIn('模拟位置离线',result['errors'][0])

    def test_existing_everything_coverage_is_ready_without_directory_walk(self):
        from types import SimpleNamespace
        from reference_library import ReferenceCatalog
        other=self.base/'indexed-share';other.mkdir()
        self.library.search=SimpleNamespace(_request=lambda query:{'available':True,'paths':[str(other/'one.JPG')],'total':486901})
        with patch.object(ReferenceCatalog,'_media_paths',side_effect=AssertionError('已覆盖目录不应再遍历')):
            root=self.library.add_root(other)['root_id'];self.ready(self.library,root)
        self.assertEqual(self.library._root(root)['index_source'],'everything')
        self.assertEqual(self.library._root(root)['progress']['percent'],100)
        self.assertEqual(self.library._root(root)['count'],486901)
        self.assertEqual(self.library.catalog(root).items()['total'],0)

    def test_everything_metadata_search_pages_beyond_2048_without_file_probes(self):
        from types import SimpleNamespace
        with self.library.lock:next(row for row in self.library.config['roots'] if row['id']==self.root)['native_complete']=False
        paths=[str(self.photos/f'match-{index:04d}.JPG') for index in range(2055)]
        calls=[]
        def request(query):
            calls.append(query);offset=query['offset'];page=paths[offset:offset+2048]
            return {'available':True,'paths':page,'records':[{'path':path,'size':100,'modified_ns':1700000000000000000} for path in page],
                    'has_more':offset+2048<len(paths),'total':len(paths)}
        self.library.search=SimpleNamespace(_request=request)
        with patch('reference_library.os.scandir',side_effect=AssertionError('Everything 搜索不应遍历目录')),patch.object(self.library,'resolve',wraps=self.library.resolve) as resolved:
            result=self.library.items(self.root,text='match')
            self.assertEqual(resolved.call_count,0)
        self.assertEqual([query['offset'] for query in calls],[0,2048])
        self.assertEqual(result['total'],2055);self.assertEqual(len(result['rows']),60)
        second=self.library.items(self.root,text='match',cursor=result['next_cursor'])
        self.assertFalse({row['id'] for row in result['rows']} & {row['id'] for row in second['rows']})
        with self.library.lock:
            next(row for row in self.library.config['roots'] if row['id']==self.root)['index_source']='everything'
        self.library.search=SimpleNamespace(_request=lambda query:{'available':True,'paths':[]})
        self.assertEqual(self.library.items(self.root,text='match')['total'],0)
        self.library.search=SimpleNamespace(_request=lambda query:{'available':False,'message':'Everything 未运行'})
        cached=self.library.items(self.root,text='match');self.assertEqual(cached['total'],2055)
        self.assertTrue(cached['incomplete']);self.assertIn('已缓存',cached['note'])

    def test_real_index_progress_counts_files_and_finishes_at_100(self):
        from reference_library import ReferenceCatalog
        for index in range(260):
            (self.photos/f'extra-{index}.JPG').write_bytes(b'index-only')
        events=[];original=ReferenceCatalog.scan_with_progress
        def observe(catalog, root, report, expected_total=0):
            def received(phase, done, total):
                events.append((phase,done,total));report(phase,done,total)
            return original(catalog,root,received,expected_total)
        with patch.object(ReferenceCatalog,'scan_with_progress',observe):
            self.library.rescan(self.root);self.ready(self.library,self.root)
        self.assertTrue(any(phase=='indexing' and done==128 for phase,done,total in events))
        self.assertTrue(any(phase=='indexing' and done==256 for phase,done,total in events))
        self.assertEqual(events[-1],('completed',263,263))
        progress=self.library.roots()['roots'][0]['progress']
        self.assertEqual(progress,{'phase':'completed','done':263,'total':263,'percent':100,
                                  'basis':'files','directories_done':2,'directories_total':2})
        self.assertEqual(self.library.public_config()['roots'][0]['progress'],progress)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='reference-')
        self.base = Path(self.temp.name)
        self.photos = self.base/'photos'
        self.photos.mkdir()
        self.libraries = []
        self.library = self.make_library('data')
        Image.new('RGB', (320, 200), 'teal').save(self.photos/'one.JPG')
        (self.photos/'one.CR3').write_bytes(b'paired-raw')
        (self.photos/'sub').mkdir()
        Image.new('RGB', (30, 20), 'red').save(self.photos/'sub/two.JPG')
        self.root = self.library.add_root(self.photos, '参考资料')['root_id']
        self.ready(self.library, self.root)

    def make_library(self, folder):
        service = ReferenceLibrary(self.base/folder)
        self.libraries.append(service)
        return service

    def ready(self, service, root):
        until = time.monotonic()+15
        while service._root(root)['status'] in ('queued', 'scanning'):
            if time.monotonic()>until:
                self.fail('后台索引超时')
            time.sleep(.02)
        self.assertEqual(service._root(root)['status'], 'ready', service._root(root))

    def tearDown(self):
        for service in reversed(self.libraries):
            service.close()
        self.temp.cleanup()

    def test_batch_add_reports_partial_failure_and_deduplicates_existing_roots(self):
        from reference_api import local_post
        second=self.base/'second';second.mkdir()
        result=local_post(self.library,'add-roots',{'entries':[{'path':str(second),'name':'第二参考盘','visible':False},{'path':str(self.base/'missing')},{'path':str(self.photos)}]})
        self.assertEqual(len(result['added']),2);self.assertEqual(len(result['errors']),1)
        self.assertEqual(len(self.library.public_config()['roots']),2)
        self.ready(self.library,result['added'][0]['root_id']);self.ready(self.library,self.root)
        self.assertEqual(self.library._root(result['added'][0]['root_id'])['name'],'第二参考盘')
        self.assertTrue(self.library._root(result['added'][0]['root_id'])['hidden'])
        self.assertEqual([row['id'] for row in self.library.sources()['devices'][0]['roots']],[self.root])
        local_post(self.library,'add-roots',{'entries':[{'path':str(second),'visible':True}]})
        self.assertFalse(self.library._root(result['added'][0]['root_id'])['hidden'])
        self.ready(self.library,result['added'][0]['root_id'])
        for entries in [None,[],[{}],[{'path':str(second),'name':123}],[{'path':str(second),'visible':'false'}],[{'path':str(second)}]*51]:
            with self.assertRaises(ValueError):local_post(self.library,'add-roots',{'entries':entries})

    def test_visibility_persists_without_revoking_access_or_deleting_photos(self):
        from reference_api import local_post
        key=self.library.create_key('设备',[self.root]);secret=key['api_key']
        local_post(self.library,'root-visibility',{'id':self.root,'visible':False})
        self.assertEqual(self.library.sources()['devices'][0]['roots'],[])
        self.assertTrue(self.library.public_config()['roots'][0]['hidden'])
        allowed=self.library.authorize(secret);self.assertEqual(len(self.library.roots(allowed)['roots']),1)
        self.assertEqual(self.library.items(self.root,allowed=allowed)['total'],1)
        restarted=self.make_library('data');self.assertEqual(restarted.sources()['devices'][0]['roots'],[])
        local_post(restarted,'root-visibility',{'id':self.root,'visible':True})
        self.assertEqual(len(restarted.sources()['devices'][0]['roots']),1)
        with self.assertRaises(ValueError):local_post(restarted,'root-visibility',{'id':self.root,'visible':'false'})
        restarted.remove_root(self.root);self.assertTrue((self.photos/'one.JPG').is_file())
        self.assertEqual(restarted.sources()['devices'][0]['roots'],[])
        restarted.add_root(self.photos);self.ready(restarted,self.root)
        self.assertEqual(len(restarted.sources()['devices'][0]['roots']),1)

    def test_background_pairing_preview_and_overlapping_roots(self):
        self.assertEqual(self.library._root(self.root)['count'], 2)
        self.assertEqual(self.library.items(self.root)['total'], 1)
        self.assertEqual(self.library.folders(self.root)['folders'][0]['folder'], 'sub')
        with patch('media_engine.find_exiftool', side_effect=AssertionError('配对 JPG 无需 RAW 工具')):
            raw = self.library.preview(self.root, 'one.JPG', 240)
        with Image.open(io.BytesIO(raw)) as image:
            self.assertEqual(image.size, (240, 150))
        self.assertEqual(self.library.catalog(self.root).previews, self.library.data/'catalog_previews')
        second = self.library.add_root(self.photos/'sub')['root_id']
        self.ready(self.library, second)
        self.assertEqual(self.library.items(second)['total'], 1)
        self.assertNotEqual(self.library.catalog(self.root).db, self.library.catalog(second).db)

    def test_key_encryption_assignment_revocation_and_path_boundary(self):
        key = self.library.create_key('摄影工作站', [self.root])
        self.assertEqual(self.library.reveal(key['id']), key['api_key'])
        self.assertNotIn(key['api_key'], self.library.config_path.read_text('utf8'))
        self.assertEqual(self.library.authorize(key['api_key']), {self.root})
        self.assertNotIn(str(self.photos), json.dumps(self.library.roots([self.root])))
        for path in ('../one.JPG', '/one.JPG', 'C:/Windows/a.jpg', 'sub/../../one.JPG'):
            with self.assertRaises(ValueError):
                self.library.preview(self.root, path)
        with self.assertRaises(PermissionError):
            self.library.items(self.root, allowed=[])
        with self.assertRaises(ValueError):
            self.library.preview(self.root, 'not-an-image.txt')
        second = self.library.add_root(self.photos/'sub')['root_id']
        self.ready(self.library, second)
        self.library.assign_key(key['id'], [second])
        with self.assertRaises(PermissionError):
            self.library.items(self.root, allowed=self.library.authorize(key['api_key']))
        self.library.disable_key(key['id'])
        with self.assertRaises(PermissionError):
            self.library.authorize(key['api_key'])

    def test_keys_can_be_created_before_directories_and_assigned_or_cleared_later(self):
        from reference_api import local_post, resource
        library = self.make_library('unassigned')
        key = local_post(library, 'create-key', {'name': '先建密钥'})
        empty = local_post(library, 'create-key', {'name': '空目录密钥', 'roots': []})
        for created in (key, empty):
            self.assertEqual(library.authorize(created['api_key']), set())
            self.assertEqual(resource(library, 'roots', {}, set())['roots'], [])
            self.assertEqual(library.reveal(created['id']), created['api_key'])
        library.close();self.libraries.remove(library)
        library = self.make_library('unassigned')
        self.assertEqual(library.authorize(key['api_key']), set())
        root = library.add_root(self.photos)['root_id']
        self.ready(library, root)
        self.assertEqual(resource(library, 'roots', {}, library.authorize(key['api_key']))['roots'], [])
        with self.assertRaises(PermissionError):
            library.items(root, allowed=library.authorize(key['api_key']))
        local_post(library, 'assign-key', {'id': key['id'], 'roots': [root]})
        self.assertEqual(library.items(root, allowed=library.authorize(key['api_key']))['total'], 1)
        for invalid in ('', {}, ['missing'], [None]):
            with self.subTest(roots=invalid):
                with self.assertRaises(ValueError):
                    library.create_key('无效目录', invalid)
                with self.assertRaises(ValueError):
                    library.assign_key(key['id'], invalid)
                self.assertEqual(library.authorize(key['api_key']), {root})
        local_post(library, 'assign-key', {'id': key['id'], 'roots': []})
        self.assertEqual(library.authorize(key['api_key']), set())
        with self.assertRaises(PermissionError):
            library.preview(root, 'one.JPG', allowed=library.authorize(key['api_key']))

    def test_reference_cache_task_cleanup_and_local_reveal_validation(self):
        library = self.library
        library.sync_task('shoot-1', 'project-1')
        first = {'device':'local', 'root':self.root, 'relative':'one.JPG', 'name':'one.JPG'}
        second_path = self.photos/'second.JPG';second_path.write_bytes((self.photos/'one.JPG').read_bytes())
        second = {**first, 'relative':'second.JPG', 'name':'second.JPG'}
        with patch.object(library, 'preview', return_value=b'\xff\xd8test') as preview:
            for item in (first, first, second):
                library.client_resource('local', 'preview', {'root':self.root, 'file':item['relative'], 'size':420, 'v':'version'})
            self.assertEqual(preview.call_count, 2)
        # 刚固定就立即清理时，参考集合可能尚未保存，仍必须删掉该图的临时副本。
        library.clear_reference(first)
        with library.cache.connect() as connection:
            self.assertEqual([row['relative'] for row in connection.execute('SELECT relative FROM entries')], ['second.JPG'])
        library.save_preferences({'references':[first, second]})
        library.save_preferences({'references':[second]})
        with library.cache.connect() as connection:
            self.assertEqual([row['relative'] for row in connection.execute('SELECT relative FROM entries')], ['second.JPG'])
        with patch('file_reveal.reveal_path') as reveal:
            library.reveal_reference('local', self.root, 'one.JPG')
            reveal.assert_called_once_with(self.photos/'one.JPG')
            for device, relative in (('peer', 'one.JPG'), ('local', '../outside.JPG')):
                with self.assertRaises(ValueError):
                    library.reveal_reference(device, self.root, relative)
            self.assertEqual(reveal.call_count, 1)
        self.assertFalse(library.sync_task('shoot-1', 'project-1')['changed'])
        generation = library.cache.generation
        self.assertTrue(library.sync_task('shoot-1', 'project-2')['changed'])
        self.assertEqual(library.preferences()['references'], [])
        self.assertFalse(list(library.cache.folder.glob('*.webp')))
        self.assertFalse(list(library.cache.folder.glob('*.jpg')))
        with self.assertRaises(ValueError):
            library.save_preferences({'references':[first], 'cache_generation':generation})
        self.assertTrue(second_path.exists());self.assertTrue((self.photos/'one.JPG').exists())

    def test_remote_search_and_previews_are_cached_until_task_changes(self):
        from reference_api import local_get
        self.library.configure_sharing(True, available_port())
        key = self.library.create_key('缓存客户端', [self.root])
        client = self.make_library('cached-client')
        peer = client.add_peer('资料机', self.library.public_config()['endpoint'], key['api_key'])['id']
        client.sync_task('shoot-1', None)
        with patch.object(client, 'remote', wraps=client.remote) as request:
            items = {'device':peer, 'root':self.root, 'q':'one'}
            image = {'device':peer, 'root':self.root, 'file':'one.JPG', 'size':240, 'v':'version'}
            original = local_get(client, 'library/items', items)
            preview = local_get(client, 'library/preview', image)
            self.library.configure_sharing(False, self.library.public_config()['sharing']['port'])
            self.assertEqual(local_get(client, 'library/items', items), original)
            self.assertEqual(local_get(client, 'library/preview', image), preview)
            self.assertEqual(request.call_count, 2)
        client.sync_task('shoot-2', None)
        with self.assertRaises(ValueError):
            local_get(client, 'library/preview', image)

    def test_cursor_paging_literal_search_and_cache_restart(self):
        raw = (self.photos/'one.JPG').read_bytes()
        for index in range(131):
            (self.photos/f'Look {index:03d}.JPG').write_bytes(raw)
        self.library.rescan(self.root)
        self.ready(self.library, self.root)
        all_rows, cursor = [], ''
        while True:
            result = self.library.items(self.root, cursor=cursor)
            all_rows.extend(result['rows'])
            if not result['has_more']:
                break
            cursor = result['next_cursor']
        self.assertEqual(len(all_rows), 132)
        self.assertEqual(len({row['id'] for row in all_rows}), 132)
        self.assertEqual(self.library.items(self.root, text='Look 01')['total'], 10)
        self.assertEqual(self.library.items(self.root, text='%')['total'], 0)
        self.assertEqual(self.library.items(self.root, text='two')['total'], 1)
        ref = {'device': 'local', 'root': self.root, 'relative': 'one.JPG', 'name': 'one.JPG'}
        self.library.save_preferences({'references': [ref, ref], 'index': 9, 'collapsed': True, 'split_enabled': True})
        self.library.close();self.libraries.remove(self.library)
        self.library = self.make_library('data')
        saved = self.library.preferences()
        self.assertEqual(len(saved['references']), 1)
        self.assertEqual(saved['index'], 0)
        self.assertTrue(saved['collapsed'] and saved['split_enabled'])
        self.assertEqual(self.library.items(self.root)['total'], 132)

    def test_everything_candidates_are_scoped_and_persisted(self):
        from types import SimpleNamespace
        with self.library.lock:next(row for row in self.library.config['roots'] if row['id']==self.root)['native_complete']=False
        fresh = self.photos/'fresh.JPG';fresh.write_bytes((self.photos/'one.JPG').read_bytes())
        outside = self.base/'fresh.JPG';outside.write_bytes(fresh.read_bytes())
        self.library.search = SimpleNamespace(_request=lambda query: {'available': True, 'paths': [fresh, outside]})
        result = self.library.items(self.root, text='fresh')
        self.assertEqual(result['total'], 1)
        self.assertIn('Everything', result['engine'])
        self.library.search = SimpleNamespace(_request=lambda query: {'available': False, 'message': '未启动'})
        self.assertEqual(self.library.items(self.root, text='fresh')['total'], 1)

    def test_browse_fills_new_folder_before_full_rescan(self):
        nested = self.photos/'new-folder';nested.mkdir()
        (nested/'new.JPG').write_bytes((self.photos/'one.JPG').read_bytes())
        self.assertEqual(self.library.items(self.root, folder='new-folder')['total'], 1)

    def test_external_preview_cache_cannot_be_shared(self):
        from preview_cache import _LOCATIONS
        cache = self.base/'cache';cache.mkdir()
        with patch.dict(_LOCATIONS, {self.library.data: cache}):
            with self.assertRaises(ValueError):
                self.library.add_root(cache)
            root = self.library.add_root(self.base)['root_id']
            self.ready(self.library, root)
            self.assertNotIn('cache', [row['folder'] for row in self.library.folders(root)['folders']])

    def test_interrupted_background_scan_resumes_on_restart(self):
        from reference_library import ReferenceCatalog
        entered = threading.Event()
        def interrupted(catalog, root, excluded=(), scan_metadata=None, on_directory_progress=None):
            entered.set()
            self.library.stopping.wait(3)
            raise OSError('扫描停止')
            yield
        with patch.object(ReferenceCatalog, '_media_paths', interrupted):
            root = self.library.add_root(self.photos/'sub')['root_id']
            self.assertTrue(entered.wait(3))
            self.library.close();self.libraries.remove(self.library)
            self.assertEqual(self.library._root(root)['status'], 'queued')
        self.library = self.make_library('data')
        self.ready(self.library, root)
        self.assertEqual(self.library.items(root)['total'], 1)

    def test_internal_data_excluded_and_removing_root_does_not_remove_files(self):
        root = self.library.add_root(self.base, '总目录')['root_id']
        Image.new('RGB', (10, 10)).save(self.library.data/'secret.JPG')
        self.ready(self.library, root)
        self.assertFalse(any(row['folder']=='data' for row in self.library.folders(root)['folders']))
        self.assertEqual(self.library.items(root, text='secret')['total'], 0)
        with self.assertRaises(ValueError):
            self.library.preview(root, 'data/secret.JPG')
        key = self.library.create_key('调用方', [self.root])
        self.library.remove_root(self.root)
        self.assertTrue((self.photos/'one.JPG').exists())
        self.assertEqual(self.library.authorize(key['api_key']), set())

    def test_address_normalization(self):
        for value in ('192.168.1.20:8770', 'http://192.168.1.20:8770/api/library/v1/', '192.168.1.20/api'):
            self.assertEqual(normalize_address(value), 'http://192.168.1.20:8770/api/library/v1')
        for value in ('8.8.8.8:8770', 'localhost:8770', 'http://user:pwd@192.168.1.20', '192.168.1.20/bad', '192.168.1.20?file=a', '192.168.1.20:0'):
            with self.assertRaises(ValueError):
                normalize_address(value)

    def test_real_lan_http_scopes_peer_lifecycle_and_read_only_routes(self):
        port = available_port()
        self.library.configure_sharing(True, port)
        key = self.library.create_key('客户端', [self.root])
        addresses = self.library.public_config()['sharing']['addresses']
        endpoint = next((value for value in addresses if '127.0.0.1' not in value), addresses[0])
        opener = build_opener(ProxyHandler({}))
        def request(route, token=key['api_key'], method='GET', origin=None,accept=None):
            headers = {'Authorization': 'Bearer '+token}
            if accept:headers['Accept']=accept
            if origin:
                headers['Origin'] = origin
            return opener.open(Request(endpoint+'/'+route, headers=headers, method=method), timeout=3)
        with request('roots') as response:
            result = json.load(response)
        self.assertEqual(result['roots'][0]['id'], self.root)
        self.assertNotIn(str(self.photos), json.dumps(result))
        with request('preview?'+urlencode({'root': self.root, 'file': 'one.JPG'})) as response:
            self.assertTrue(response.read().startswith(b'\xff\xd8'))
            self.assertEqual(response.headers.get_content_type(),'image/jpeg')
        with request('preview?'+urlencode({'root':self.root,'file':'one.JPG'}),accept='image/webp,image/jpeg') as response:
            self.assertEqual(response.headers.get_content_type(),'image/webp')
            self.assertTrue(response.read().startswith(b'RIFF'))
        for route, token, method, status in [('roots', 'bad', 'GET', 401), ('config', key['api_key'], 'GET', 404), ('roots', key['api_key'], 'POST', 405)]:
            with self.assertRaises(HTTPError) as error:
                request(route, token, method)
            self.assertEqual(error.exception.code, status)
            error.exception.close()
        with self.assertRaises(HTTPError) as error:
            request('roots', origin='http://example.test')
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        client = self.make_library('client')
        peer = client.add_peer('参考工作站', endpoint, key['api_key'])
        self.assertEqual(client.add_peer('改名', endpoint, key['api_key'])['id'], peer['id'])
        remote = client.sources()['devices'][1]
        self.assertEqual(remote['roots'][0]['id'], self.root)
        self.assertEqual(client.remote(client.peer(peer['id']), 'items', {'root': self.root})['total'], 1)
        self.assertNotIn(key['api_key'], client.config_path.read_text('utf8'))
        with socket.socket() as busy:
            busy.bind(('0.0.0.0', 0));busy.listen()
            with self.assertRaises(ValueError):
                self.library.configure_sharing(True, busy.getsockname()[1])
        self.assertEqual(self.library.shared.server_port, port)
        self.library.disable_key(key['id'])
        self.assertIn('密钥', client.sources()['devices'][1]['error'])
        self.library.configure_sharing(False, port)
        self.assertFalse(self.library.public_config()['sharing']['running'])
        self.assertIn('离线', client.sources()['devices'][1]['error'])
        client.remove_peer(peer['id'])
        self.assertEqual(len(client.sources()['devices']), 1)


if __name__=='__main__':
    unittest.main()
