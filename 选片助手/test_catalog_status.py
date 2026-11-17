"""慢统计不阻塞轮询，不重复启动，失败保留快照并可恢复。"""
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from catalog_status import CatalogStatus


class CatalogStatusTests(unittest.TestCase):
    def test_slow_refresh_single_flight_preserves_last_success_and_recovers(self):
        entered=threading.Event();release=threading.Event()
        value={'roots':[{'id':'root'}],'counts':{'total':7},'metadata':{'ready':3}}
        def slow():
            entered.set()
            if not release.wait(5):raise RuntimeError('测试未释放')
            return value
        catalog=SimpleNamespace(state=Mock(side_effect=slow))
        previews=SimpleNamespace(archive_state=Mock(return_value={'ready':4,'total':7}))
        progress=CatalogStatus(catalog)
        try:
            first=progress.snapshot(previews)
            self.assertFalse(first['initialized']);self.assertTrue(entered.wait(2))
            for _ in range(20):self.assertFalse(progress.snapshot(previews)['initialized'])
            self.assertEqual(catalog.state.call_count,1)
        finally:
            release.set();progress.worker.join(5);progress.preview_worker.join(5)
        saved=progress.snapshot(previews)
        self.assertTrue(saved['initialized']);self.assertEqual(saved['previews']['ready'],4)
        catalog.state.side_effect=RuntimeError('模拟数据库忙')
        with patch('catalog_status.time.monotonic',return_value=progress.updated+3):
            self.assertEqual(progress.snapshot(previews)['counts']['total'],7)
            progress.worker.join(5)
        failed=progress.snapshot(previews)
        self.assertEqual(failed['counts']['total'],7);self.assertIn('模拟数据库忙',failed['status_error'])
        catalog.state.side_effect=lambda:{**value,'counts':{'total':8}}
        with patch('catalog_status.time.monotonic',return_value=progress.updated+3):
            progress.snapshot(previews);progress.worker.join(5)
        self.assertEqual(progress.snapshot(previews)['counts']['total'],8)
        self.assertEqual(progress.snapshot(previews)['status_error'],'')

    def test_decode_progress_restores_while_metadata_statistics_are_blocked(self):
        entered=threading.Event();release=threading.Event()
        def slow():
            entered.set();release.wait(5)
            return {'roots': [], 'counts': {}, 'metadata': {}}
        catalog=SimpleNamespace(state=slow)
        previews=SimpleNamespace(archive_state=Mock(return_value={'ready':17,'total':31,'pending':14}))
        progress=CatalogStatus(catalog)
        try:
            progress.snapshot(previews);self.assertTrue(entered.wait(1))
            progress.preview_worker.join(1)
            restored=progress.snapshot(previews)
            self.assertFalse(restored['initialized'])
            self.assertTrue(restored['previews_initialized'])
            self.assertEqual(restored['previews']['ready'],17)
            previews.archive_state.assert_called_once_with(persisted=True)
            previews.archive_state.side_effect=RuntimeError('模拟队列统计忙')
            with patch('catalog_status.time.monotonic',return_value=progress.preview_updated+3):
                progress.snapshot(previews);progress.preview_worker.join(1)
            failed=progress.snapshot(previews)
            self.assertEqual(failed['previews']['ready'],17)
            self.assertIn('模拟队列统计忙',failed['preview_status_error'])
            previews.archive_state.side_effect=None
            previews.archive_state.return_value={'ready':18,'total':31,'pending':13}
            with patch('catalog_status.time.monotonic',return_value=progress.preview_updated+3):
                progress.snapshot(previews);progress.preview_worker.join(1)
            self.assertEqual(progress.snapshot(previews)['previews']['ready'],18)
            self.assertEqual(progress.snapshot(previews)['preview_status_error'],'')
        finally:release.set();progress.close()


if __name__=='__main__':unittest.main()
