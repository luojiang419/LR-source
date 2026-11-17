"""过时搜索中断实际 SQLite 工作，且不跨页面取消。"""
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from asset_catalog import AssetCatalog
from catalog_search_requests import CatalogSearchRequests, SearchCancellation, SearchCancelled, SearchBusy


class CatalogSearchRequestTests(unittest.TestCase):
    def test_newer_request_cancels_only_same_page_and_rejects_late_arrival(self):
        requests=CatalogSearchRequests()
        with requests.begin('page-a',1) as first,requests.begin('page-b',1) as other:
            with requests.begin('page-a',3) as latest:
                self.assertTrue(first());self.assertFalse(other());self.assertFalse(latest())
                with self.assertRaises(SearchCancelled),requests.begin('page-a',2):pass
                self.assertFalse(latest())
            with self.assertRaises(SearchCancelled),requests.begin('page-a',3):pass
        self.assertTrue(all(not entry.active for entry in requests.requests.values()))

    def test_registry_bounds_and_idle_expiry_never_evict_active_request(self):
        requests=CatalogSearchRequests(maximum=2,ttl=5)
        with requests.begin('active',1) as active:
            with requests.begin('idle',1):pass
            with requests.begin('replacement',1):pass
            self.assertEqual(set(requests.requests),{'active','replacement'})
            with requests.begin('replacement',2):
                with self.assertRaises(SearchBusy),requests.begin('overflow',1):pass
            with patch('catalog_search_requests.time.monotonic',return_value=10**12):
                with requests.begin('fresh',1):pass
            self.assertEqual(set(requests.requests),{'active','fresh'});self.assertFalse(active())
        for client,seq in [('bad key',1),('valid','nan'),('valid',-1),('valid',2**53)]:
            with self.assertRaises(ValueError),requests.begin(client,seq):pass

    def test_socket_disconnect_is_cancelled_but_queued_bytes_are_not_consumed(self):
        left,right=socket.socketpair()
        try:
            cancel=SearchCancellation(left);self.assertFalse(cancel())
            right.sendall(b'x');cancel.next_check=0;self.assertFalse(cancel())
            self.assertEqual(left.recv(1),b'x')
            right.close();cancel.next_check=0;self.assertTrue(cancel())
        finally:left.close();right.close()

    def test_new_query_interrupts_long_sql_and_releases_read_transaction(self):
        with tempfile.TemporaryDirectory() as folder:
            catalog=AssetCatalog(Path(folder));requests=CatalogSearchRequests();started=threading.Event();done=threading.Event();errors=[]
            def work():
                try:
                    with requests.begin('page',1) as cancel,catalog._search_connection(cancel) as c:
                        c.execute('BEGIN');started.set()
                        c.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000000) SELECT sum(x) FROM n').fetchone()
                    errors.append('旧 SQL 未取消')
                except SearchCancelled:pass
                except Exception as error:errors.append(repr(error))
                finally:done.set()
            worker=threading.Thread(target=work);worker.start()
            try:
                self.assertTrue(started.wait(3))
                with requests.begin('page',2) as cancel:
                    self.assertTrue(done.wait(3))
                    self.assertEqual(catalog.items(text='new',include_tree=True,cancel=cancel)['total'],0)
                self.assertEqual(errors,[])
                with catalog.connect() as c:self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0],'ok')
            finally:worker.join(5);catalog.close()
