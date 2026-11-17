"""索引失效、入口成本、取消与重启日期兼容。"""
from datetime import datetime
import json,unittest
from unittest.mock import patch
from catalog_search_requests import SearchCancellation,SearchCancelled
from search_intent import LOCAL_TZ
import test_smart_search as fixtures
vision=fixtures.vision

class LifecycleTests(unittest.TestCase):
 setUp=fixtures.SmartSearchTests.setUp
 tearDown=fixtures.SmartSearchTests.tearDown
 seed=fixtures.SmartSearchTests.seed
 search=fixtures.SmartSearchTests.search
 ids=fixtures.SmartSearchTests.ids

 def test_replaced_or_deleted_metadata_invalidates_old_postings(self):
  item=self.seed(0,[vision()]);changed=vision(category='牛仔裤',color='蓝色',summary='女性穿蓝色牛仔裤')
  with self.catalog.connect() as c:
   c.execute('UPDATE intelligence_results SET frames=?,created=created+1 WHERE item=?',(json.dumps([{'seconds':0,'result':changed}]),item))
  self.assertEqual(self.ids('女人穿红色外套'),set());self.service.search_index.batch()
  self.assertEqual(self.ids('女人穿蓝色牛仔裤'),{item})
  with self.catalog.connect() as c:
   c.execute('DELETE FROM intelligence_results WHERE item=?',(item,))
   self.assertEqual(c.execute("SELECT count(*) FROM smart_fts WHERE smart_fts MATCH 'genderfemale'").fetchone()[0],0)
   self.assertEqual(c.execute('SELECT count(*) FROM smart_wear').fetchone()[0],0)
  self.assertEqual(self.ids('女人穿蓝色牛仔裤'),set())

 def test_cancelled_request_does_not_run_database_query(self):
  cancel=SearchCancellation();cancel.event.set()
  with patch.object(self.catalog,'connect',side_effect=AssertionError('已取消请求不能进入查询')):
   with self.assertRaises(SearchCancelled):self.search('女人穿红色外套',cancel=cancel)

 def test_lightweight_search_state_does_not_compute_full_statistics(self):
  with patch.object(self.service,'state',side_effect=AssertionError('不能统计整库')),patch.object(self.service.clip,'available',side_effect=AssertionError('不能探测模型文件')):
   state=self.service.search_state();self.assertTrue(state['lightweight']);self.assertEqual(len(state['roots']),1)

 def test_legacy_capture_dates_stay_correct_during_restart_backfill(self):
  row=self.rows[0]
  with self.catalog.connect() as c:
   c.execute('UPDATE items SET modified=? WHERE id=?',(datetime(2026,10,28,12,tzinfo=LOCAL_TZ).timestamp(),row['id']))
   payload={'capture':{'time':'2026-09-07T12:00:00'},'stamp':[row['fingerprint'],'fixture']}
   c.execute("UPDATE catalog_metadata SET status='queued',payload=? WHERE item=?",(json.dumps(payload),row['id']))
   c.execute('DELETE FROM smart_dates');c.execute("UPDATE smart_state SET value='0' WHERE key='dates_done'");c.execute("DELETE FROM smart_state WHERE key='date_cursor'")
  self.assertNotIn(row['id'],self.ids('昨天'));self.service.search_index.batch()
  self.assertIn(row['id'],self.ids('上个月'));self.assertNotIn(row['id'],self.ids('昨天'))

 def test_worker_start_is_idempotent(self):
  self.service.search_index.start();thread=self.service.search_index.thread
  self.service.search_index.start();self.assertIs(self.service.search_index.thread,thread)

if __name__=='__main__':unittest.main()
