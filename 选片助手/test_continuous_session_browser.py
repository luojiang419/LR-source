"""完整轻量目录与大目录增量：现场不再依赖当前分页。"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import urlopen
import test_session_browser as fixtures


class ContinuousSessionTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.SessionBrowserTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_full_catalog_ignores_old_page_offset_and_reads_records_once(self):
        import server
        f=self.f
        with patch.object(server,'STORE',f.store),patch.object(server,'ARCHIVE',None),patch.object(server,'PREVIEWS',None),patch.object(server,'DIRECT',None),patch.object(f.store,'records',wraps=f.store.records) as reads,patch.object(server.session_browser,'read',side_effect=AssertionError('现场不可再使用当前页读取')):
            session,records=server.browse_state(f.sid,{'offset':1920,'limit':240,'q':'old query'})
        self.assertEqual(len(records),2500);self.assertEqual(records[0]['id'],'P00000');self.assertEqual(records[-1]['id'],'P02499')
        self.assertEqual(reads.call_count,1);self.assertTrue(session['browser']['continuous']);self.assertFalse(session['browser'].get('paged'))

    def test_large_live_delta_keeps_continuous_identity_without_full_reload(self):
        import server
        f=self.f;watch=SimpleNamespace(delta=lambda sid,generation,after:['P02499'])
        with ExitStack() as stack:
            for key,value in dict(STORE=f.store,DATA=f.store.data,ARCHIVE=None,PREVIEWS=None,DIRECT=None,WATCH=watch).items():stack.enter_context(patch.object(server,key,value))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start();stack.callback(http.server_close);stack.callback(http.shutdown)
            with patch.object(f.store,'records',wraps=f.store.records) as reads:
                with urlopen(f'http://127.0.0.1:{http.server_port}/api/state?session_id={f.sid}&live_after=1&live_generation=test') as response:data=json.load(response)
            self.assertTrue(data['catalog_delta']);self.assertEqual([row['id'] for row in data['catalog']],['P02499']);self.assertEqual(data['session']['browser'],{'continuous':True,'total':2500})
            self.assertEqual(reads.call_args.kwargs['ids'],['P02499'])


if __name__=='__main__':unittest.main()
