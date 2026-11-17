"""智能搜索：关系反例、自然月、元数据时间优先、分页与存量升级。"""
from datetime import datetime,timezone,timedelta
from pathlib import Path
import json,tempfile,time,unittest
from unittest.mock import patch
from PIL import Image
from asset_catalog import AssetCatalog
from asset_intelligence import AssetIntelligence
from model_providers import Providers
from retrieval_store import save_result
from search_intent import plan,LOCAL_TZ
from smart_query import query
from test_asset_intelligence import result

NOW=datetime(2026,10,29,12,tzinfo=LOCAL_TZ)
def vision(gender='female',category='外套',color='红色',summary='女性身穿红色外套'):
 value=result(color=color);value['summary']=summary;value['attributes'].update(category=category,color=color,people='女性' if gender=='female' else '男性')
 value['subjects']=[{'gender':gender,'clothing':[{'category':category,'color':color,'description':''}]}]
 return value

class IntentTests(unittest.TestCase):
 def test_calendar_boundaries_and_phrasing(self):
  p=plan('帮我搜索昨天拍摄的图片',now=NOW);self.assertEqual((p['dates'][0]['start'],p['dates'][0]['end']),('2026-10-28','2026-10-29'));self.assertEqual(p['residual'],[])
  self.assertEqual(plan('上个月的照片',now=NOW)['dates'][0]['start'],'2026-09-01')
  self.assertEqual(plan('上个月',now=datetime(2026,1,2,tzinfo=LOCAL_TZ))['dates'][0]['start'],'2025-12-01')
  self.assertEqual(plan('上个月',now=datetime(2024,3,2,tzinfo=LOCAL_TZ))['dates'][0]['end'],'2024-03-01')
  self.assertEqual(plan('昨天',now=datetime(2026,10,28,20,tzinfo=timezone.utc))['dates'][0]['start'],'2026-10-28')
  self.assertEqual(plan('2026-09',now=NOW)['dates'][0]['end'],'2026-10-01')
  self.assertEqual(plan('2026年9月1日到10月1日',now=NOW)['dates'][0]['end'],'2026-10-02')
  self.assertFalse(plan('LC1234-12',now=NOW)['recognized'])
  with self.assertRaises(ValueError):plan('2026年2月30日',now=NOW)
 def test_natural_constraints(self):
  p=plan('请帮我找一下上个月的女人穿着蓝色牛仔裤的照片',now=NOW)
  self.assertEqual(p['gender'],'female');self.assertEqual(p['clothes'],[{'category':'jeans','color':'blue'}]);self.assertEqual(p['residual'],[])
  self.assertEqual(plan('find a woman wearing a red coat',now=NOW)['residual'],[])
  self.assertEqual(plan('上个月女人穿红色外套',now=NOW)['residual'],[])
  self.assertEqual(plan('找一下女的身上穿一件红颜色棉袄的照片',now=NOW)['residual'],[])
  self.assertEqual(plan('女人穿着外套是红色的、牛仔裤是蓝色的',now=NOW)['clothes'],[{'category':'coat','color':'red'},{'category':'jeans','color':'blue'}])

class SmartSearchTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.photos=self.base/'photos';self.photos.mkdir()
  for index in range(10):Image.new('RGB',(12,12),'red').save(self.photos/f'{index}.jpg')
  self.catalog=AssetCatalog(self.base/'data');self.catalog.add_root(str(self.photos));self.providers=Providers(self.base/'data')
  self.service=AssetIntelligence(self.catalog,self.providers,autostart=False);self.rows=sorted(self.catalog.items()['rows'],key=lambda r:r['name'])
 def tearDown(self):
  self.service.close();self.catalog.close();self.tmp.cleanup()
 def seed(self,index,values):
  row=self.rows[index]
  frames=[{'seconds':i,'result':value} for i,value in enumerate(values)]
  with self.catalog.connect() as c:save_result(c,row['id'],row['fingerprint'],'fixture','fixture',frames,[])
  return row['id']
 def search(self,text,**kwargs):return query(self.catalog,plan(text,now=NOW),**kwargs)
 def ids(self,text,**kwargs):return {r['id'] for r in self.search(text,**kwargs)['rows']}
 def test_person_garment_color_and_frame_are_not_crossed(self):
  red=self.seed(0,[vision()]);blue=self.seed(1,[vision(category='牛仔裤',color='蓝色',summary='女性穿蓝色牛仔裤')])
  mixed=vision('male');mixed['subjects'].append(vision(category='牛仔裤',color='蓝色')['subjects'][0]);mixed['summary']='男人穿红色外套，女人穿蓝色牛仔裤'
  mixed_id=self.seed(2,[mixed])
  self.seed(3,[vision(color='蓝色',summary='女性穿蓝色外套，背景为红色墙壁')])
  self.seed(4,[vision(color='蓝色'),vision('male')])
  self.assertEqual(self.ids('女人穿着红色外套'),{red})
  self.assertEqual(self.ids('女人穿红色'),{red})
  self.assertEqual(self.ids('女人穿着蓝色牛仔裤'),{blue,mixed_id})
  self.assertEqual(self.ids('女人穿红色外套或者蓝色牛仔裤'),{red,blue,mixed_id})
  both=vision();both['subjects'][0]['clothing'].extend(vision(category='牛仔裤',color='蓝色')['subjects'][0]['clothing'])
  same=self.seed(5,[both]);apart=vision();apart['subjects'].extend(vision(category='牛仔裤',color='蓝色')['subjects']);self.seed(6,[apart])
  self.assertEqual(self.ids('女人穿着红色外套和蓝色牛仔裤'),{same})
 def test_legacy_summary_requires_actual_wearing_evidence(self):
  a=vision();a.pop('subjects');yes=self.seed(0,[a])
  b=vision(summary='一个女人旁边挂着红色外套');b.pop('subjects');self.seed(1,[b])
  inverse=vision(summary='穿红色外套的女性');inverse.pop('subjects');also=self.seed(2,[inverse])
  held=vision(summary='女性手里拿着红色外套，穿蓝色牛仔裤');held.pop('subjects');self.seed(3,[held])
  self.assertEqual(self.ids('女人穿红色外套'),{yes,also})
 def test_time_prefers_capture_and_falls_back_when_stale(self):
  yesterday=datetime(2026,10,28,12,tzinfo=LOCAL_TZ).timestamp()
  with self.catalog.connect() as c:
   c.execute('UPDATE items SET modified=?',(NOW.timestamp(),))
   c.execute('UPDATE items SET modified=? WHERE id IN (?,?)',(yesterday,self.rows[0]['id'],self.rows[1]['id']))
   for index,value in [(0,'2026-09-07T12:00:00'),(2,'2026-10-28T00:00:00+08:00'),(3,'2026-09-30T23:30:00-02:00')]:
    c.execute("UPDATE catalog_metadata SET status='ready',payload=? WHERE item=?",(json.dumps({'capture':{'time':value}}),self.rows[index]['id']))
  self.assertEqual(self.ids('昨天的图片'),{self.rows[1]['id'],self.rows[2]['id']})
  self.assertEqual(self.ids('上个月的图片'),{self.rows[0]['id']})
  with self.catalog.connect() as c:c.execute("UPDATE items SET fingerprint='changed' WHERE id=?",(self.rows[0]['id'],))
  self.assertIn(self.rows[0]['id'],self.ids('昨天'))
 def test_cursor_and_no_model_or_media_access(self):
  first=self.seed(0,[vision()]);second=self.seed(1,[vision(),vision()])
  with patch('asset_intelligence.vision',side_effect=AssertionError('查询不得调用模型')),patch.object(self.service,'_images',side_effect=AssertionError('不得读原图')):
   page=self.search('女人穿红色外套',limit=1);self.assertTrue(page['has_more'])
   other=self.search('女人穿红色外套',limit=1,cursor=page['next_cursor']);self.assertEqual({page['rows'][0]['id'],other['rows'][0]['id']},{first,second})
  with self.catalog.connect() as c:c.execute('UPDATE intelligence_meta SET generation=generation+1 WHERE id=1')
  with self.assertRaises(ValueError):self.search('女人穿红色外套',limit=1,cursor=page['next_cursor'])
  with self.catalog.connect() as c:c.execute('UPDATE items SET online=0 WHERE id=?',(first,))
  self.assertEqual(self.ids('女人穿红色外套'),{second})
 def test_old_results_backfill_in_batches_without_inference(self):
  target=self.seed(0,[vision()])
  with self.catalog.connect() as c:
   c.execute('DELETE FROM smart_frames');c.execute("UPDATE smart_state SET value='0' WHERE key='results_done'");c.execute("DELETE FROM smart_state WHERE key='result_cursor'")
  self.assertFalse(self.search('女人穿红色外套')['index_status']['ready'])
  self.service.search_index.batch(limit=1);self.service.search_index.batch(limit=1)
  self.assertEqual(self.ids('女人穿红色外套'),{target})
  with self.catalog.connect() as c:self.assertTrue(self.service.search_index.status(c)['ready'])

if __name__=='__main__':unittest.main()
