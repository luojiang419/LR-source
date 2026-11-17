"""后台消费者阻塞、重试与合并不应阻塞连续照片及监控状态。"""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
from catalog_notifications import CatalogNotifications
from catalog_store import Store
from folder_watch import FolderWatch

ROOT=Path(__file__).resolve().parents[1]


class CatalogNotificationTests(unittest.TestCase):
    def test_blocked_consumer_does_not_block_state_or_next_arrival(self):
        entered=threading.Event();release=threading.Event();calls=[]
        def consume(sid,records):
            calls.extend(r['jpg'] for r in records);entered.set();release.wait(3)
        queue=CatalogNotifications(consume)
        try:
            with tempfile.TemporaryDirectory() as temp:
                base=Path(temp);photos=base/'photos';photos.mkdir();store=Store(base/'data',ROOT)
                store.on_live_catalog=queue.request;watch=FolderWatch(store,autostart=False)
                try:
                    sid=watch.start(str(photos))['session_id']
                    with patch('media_engine.read_metadata',side_effect=lambda paths,*a,**kw:[{} for _ in paths]):
                        for name in ('one.JPG','two.JPG'):
                            Image.new('RGB',(80,60)).save(photos/name)
                            tick=threading.Thread(target=watch.tick,daemon=True);tick.start();tick.join(1)
                            self.assertFalse(tick.is_alive(),'后台通知阻塞了照片入库')
                            self.assertTrue(entered.wait(1))
                            states=[];reader=threading.Thread(target=lambda:states.append(watch.state()),daemon=True)
                            reader.start();reader.join(.3);self.assertFalse(reader.is_alive(),'后台通知占有监控状态锁')
                        self.assertEqual(states[0]['arrivals'],2)
                        self.assertEqual(len(store.records(sid)),2)
                    release.set();self.assertTrue(queue.wait_idle());self.assertEqual(calls,['one.JPG','two.JPG'])
                finally:release.set();watch.close()
        finally:release.set();queue.close()

    def test_merge_latest_record_and_bound_to_full_notification(self):
        calls=[];queue=CatalogNotifications(lambda *args:calls.append(args),autostart=False)
        try:
            queue.request('one',[{'id':'a','fingerprint':'old'}]);queue.request('one',[{'id':'a','fingerprint':'new'}])
            queue.request('large',[{'id':str(i)} for i in range(queue.MAX_RECORDS+1)])
            queue.request('large',[{'id':'later'}]);queue.request('one',[{'id':'b'}])
            queue.thread.start();self.assertTrue(queue.wait_idle())
            self.assertEqual(calls,[('one',[{'id':'a','fingerprint':'new'},{'id':'b'}]),('large',None)])
        finally:queue.close()

    def test_failure_retries_full_state_without_losing_later_requests(self):
        calls=[]
        def consume(sid,records):
            calls.append((sid,records))
            if len(calls)==1:
                queue.request(sid,[{'id':'later'}]);raise RuntimeError('temporary busy')
        queue=CatalogNotifications(consume,autostart=False)
        try:
            queue.request('one',[{'id':'a'}]);queue.thread.start();self.assertTrue(queue.wait_idle())
            self.assertEqual(calls,[('one',[{'id':'a'}]),('one',None)]);self.assertEqual(queue.error,'')
        finally:queue.close()

    def test_sku_notification_does_not_wait_for_business_lock(self):
        from sku_service import SkuService
        # 不启动模型/数据库，复现通知在另一线程持有业务锁时的行为。
        sku=SkuService.__new__(SkuService);sku.lock=threading.RLock();sku.catalog_lock=threading.Lock();sku.catalog_pending=set()
        with sku.lock:
            thread=threading.Thread(target=lambda:sku.request_catalog('session'),daemon=True)
            thread.start();thread.join(.3);self.assertFalse(thread.is_alive())
        self.assertEqual(sku.catalog_pending,{'session'})


if __name__=='__main__':unittest.main()
