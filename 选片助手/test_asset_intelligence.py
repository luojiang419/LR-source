"""隔离数据验证：当前版本检索、任务恢复、内容归组和向量召回。"""
from contextlib import ExitStack
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from asset_catalog import AssetCatalog
from asset_intelligence import AssetIntelligence
from model_providers import Providers
from retrieval_schema import DIMENSIONS
from retrieval_store import image_query, save_result
from visual_features import APPEARANCE, appearance


def result(color='红色', identity='长袖翻领双胸贴袋衬衫', code='', subjects=1):
    value = {'summary':color+'长袖翻领衬衫', 'attributes':{key:'' for key in DIMENSIONS},
             'keywords':[color, '衬衫', '翻领'], 'visible_text':code, 'sku_candidates':[code] if code else [],
             'product_identity':identity, 'subject_count':subjects, 'confidence':.95}
    value['attributes'].update(category='衬衫', style=identity, color=color, scene='摄影棚')
    return value


class IntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.base = Path(self.tmp.name)
        self.folder = self.base/'photos'/'LC1234';self.folder.mkdir(parents=True)
        for name,color in [('one.jpg','red'),('two.jpg','red'),('three.jpg','blue')]:
            Image.new('RGB',(120,160),color).save(self.folder/name)
        self.catalog = AssetCatalog(self.base/'data');self.catalog.add_root(str(self.folder))
        self.providers = Providers(self.base/'data')
        self.providers.save({'name':'测试视觉','category':'multimodal','protocol':'chat',
                             'url':'http://127.0.0.1:1','model':'fixture','select':True})
        self.service = AssetIntelligence(self.catalog,self.providers,autostart=False)
        self.service.configure({'enabled':True,'auto_group':False})
        self.rows = self.catalog.items()['rows'];self.by_name = {r['name']:r for r in self.rows}

    def tearDown(self):
        self.service.close();self.catalog.close();self.tmp.cleanup()

    def parse(self, values=None):
        values = values or {}
        self.service.discover_tick()
        with patch('asset_intelligence.vision',side_effect=lambda p,image,prompt,schema:json.dumps(values.get(p.get('model'),result()))):
            while self.service.tick():pass

    def seed(self, name, value=None, *, frame_values=None):
        row = self.by_name[name];payload = io.BytesIO();Image.new('RGB',(32,32),'red' if name!='three.jpg' else 'blue').save(payload,'JPEG')
        frames = [{'seconds':i,'result':v} for i,v in enumerate(frame_values or [value or result()])]
        with self.catalog.connect() as c:
            save_result(c,row['id'],row['fingerprint'],'test','fixture',frames,
                        [(0,APPEARANCE,appearance(payload.getvalue()))])
        return row

    def test_end_to_end_task_output_and_keyword_search(self):
        self.parse()
        self.assertEqual(self.service.state()['counts'],{'completed':3})
        self.assertEqual(len(self.service.search(text='红色')['rows']),3)
        self.assertEqual(len(self.catalog.items(text='翻领')['rows']),3)
        self.assertEqual(self.service.state()['coverage'],3)
        self.assertEqual(self.service.state()['groups'][0]['sku'],'LC1234')

    def test_one_character_chinese_query_matches_inside_indexed_words(self):
        self.seed('one.jpg')
        self.assertEqual(len(self.service.search(text='衫')['rows']),1)
        self.assertEqual(len(self.service.search(text='红')['rows']),1)

    def test_multiple_dimensions_must_match_same_frame(self):
        one=self.seed('one.jpg',frame_values=[result('红色'),result('蓝色')])
        with self.catalog.connect() as c:
            c.execute("UPDATE intelligence_dimensions SET value='公园',normalized='公园' WHERE item=? AND frame=1 AND key='scene'",(one['id'],))
        self.assertEqual(self.service.search(facets={'color':'红色','scene':'公园'})['rows'],[])
        self.assertEqual(len(self.service.search(facets={'color':'蓝色','scene':'公园'})['rows']),1)

    def test_same_color_different_styles_requires_review(self):
        self.seed('one.jpg');self.seed('two.jpg',result(identity='圆领套头短袖宽松印花T恤'));self.seed('three.jpg')
        proposal=self.service.propose_group(self.rows[0]['root'],self.rows[0]['folder'])
        self.assertEqual(proposal['status'],'review');self.assertIn('款式',proposal['reason'])

    def test_same_style_different_colors_requires_review(self):
        self.seed('one.jpg');self.seed('two.jpg');self.seed('three.jpg',result('蓝色'))
        self.assertEqual(self.service.propose_group(self.rows[0]['root'],self.rows[0]['folder'])['status'],'review')

    def test_partial_parse_never_auto_groups(self):
        self.service.configure({'auto_group':True});self.seed('one.jpg')
        proposal=self.service.propose_group(self.rows[0]['root'],self.rows[0]['folder'])
        self.assertEqual(proposal['status'],'review');self.assertIn('尚未',proposal['reason'])

    def test_multiple_products_or_sku_evidence_requires_review(self):
        self.seed('one.jpg',result(subjects=2));self.seed('two.jpg');self.seed('three.jpg',result(code='LC9999'))
        proposal=self.service.propose_group(self.rows[0]['root'],self.rows[0]['folder'])
        self.assertEqual(proposal['status'],'review');self.assertIn('多个 SKU',proposal['reason'])

    def test_content_verified_lc_folder_can_auto_assign(self):
        self.service.configure({'auto_group':True})
        for name in self.by_name:self.seed(name)
        self.service.propose_group(self.rows[0]['root'],self.rows[0]['folder'])
        rows=self.catalog.items()['rows'];self.assertTrue(all(r['sku']=='LC1234' for r in rows))
        self.assertTrue(all(r['source']=='vlm_group' for r in rows))
        self.assertEqual(self.service.state()['groups'][0]['status'],'confirmed')
        self.catalog.scan(rows[0]['root'])
        self.assertTrue(all(r['source']=='vlm_group' for r in self.catalog.items()['rows']))
        with self.catalog.connect() as c:c.execute("UPDATE items SET fingerprint='changed' WHERE id=?",(rows[0]['id'],))
        self.assertEqual(self.catalog.item(rows[0]['id'])['sku'],'')
        self.assertEqual(self.service.state()['groups'][0]['status'],'review')

    def test_unrecognized_folder_name_needs_manual_sku(self):
        for name in self.by_name:self.seed(name)
        self.service.sku=type('Rules',(),{'sku_rules':lambda _:{'rules':[{'kind':'prefix','value':'ZZ'}],'length_filter':{'min_exclusive':None,'max_exclusive':None}}})()
        proposal=self.service.propose_group(self.rows[0]['root'],self.rows[0]['folder'])
        self.assertEqual(proposal['status'],'review')

    def test_manual_assignment_not_overwritten_by_automatic_group(self):
        for name in self.by_name:self.seed(name)
        self.catalog.assign(self.rows[0]['id'],'LC7777')
        proposal=self.service.propose_group(self.rows[0]['root'],self.rows[0]['folder'])
        self.assertEqual(proposal['status'],'review')
        self.assertEqual(self.catalog.item(self.rows[0]['id'])['sku'],'LC7777')

    def test_stale_group_signature_rejected(self):
        for name in self.by_name:self.seed(name)
        proposal=self.service.propose_group(self.rows[0]['root'],self.rows[0]['folder'])
        self.catalog.assign(self.rows[0]['id'],'LC7777')
        with self.assertRaisesRegex(ValueError,'变化'):
            self.service.confirm_group(proposal['id'],proposal['signature'],sku='LC1234')

    def test_current_file_version_required_for_text_and_vector_search(self):
        one=self.seed('one.jpg')
        self.assertEqual(len(self.service.search(text='红色')['rows']),1)
        with self.catalog.connect() as c:c.execute("UPDATE items SET fingerprint='new-version' WHERE id=?",(one['id'],))
        self.assertEqual(self.service.search(text='红色')['rows'],[])
        vector=appearance(self.catalog.preview(one['id'],420,read=True))
        self.assertEqual(image_query(self.catalog,vector,APPEARANCE)['rows'],[])

    def test_removed_root_not_searchable(self):
        self.seed('one.jpg');self.catalog.remove(self.rows[0]['root'])
        self.assertEqual(self.service.search(text='红色')['rows'],[])

    def test_pagination_stable_and_bound_to_query(self):
        for name in self.by_name:self.seed(name)
        page=self.service.search(text='红色',limit=1);ids=[page['rows'][0]['id']]
        while page['has_more']:
            page=self.service.search(text='红色',limit=1,cursor=page['next_cursor']);ids.extend(r['id'] for r in page['rows'])
        self.assertEqual(len(set(ids)),3)
        first=self.service.search(text='红色',limit=1)
        with self.assertRaises(ValueError):self.service.search(text='蓝色',cursor=first['next_cursor'])
        self.catalog.assign(self.rows[0]['id'],'LC0001')
        with self.assertRaises(ValueError):self.service.search(text='红色',cursor=first['next_cursor'])

    def test_exact_sku_ranked_before_prefix_and_keywords(self):
        self.catalog.assign(self.by_name['one.jpg']['id'],'LC12')
        self.catalog.assign(self.by_name['two.jpg']['id'],'LC1234')
        self.seed('three.jpg',result(code='LC12'))
        rows=self.service.search(text='LC12')['rows']
        self.assertEqual(rows[0]['name'],'one.jpg');self.assertEqual(rows[0]['priority'],0)

    def test_reference_image_search_uses_real_features(self):
        self.seed('one.jpg');self.seed('three.jpg')
        data=self.service.image_search({'item_id':self.by_name['one.jpg']['id']})
        self.assertEqual(data['rows'][0]['name'],'one.jpg');self.assertAlmostEqual(data['rows'][0]['score'],1,places=4)
        self.assertEqual(data['backend'],APPEARANCE)

    def test_jobs_survive_restart_and_cancelled_work_is_not_requeued(self):
        self.service.discover_tick()
        with self.catalog.connect() as c:c.execute("UPDATE intelligence_jobs SET status='running' WHERE item=?",(self.rows[0]['id'],))
        self.service.close();self.service=AssetIntelligence(self.catalog,self.providers,autostart=False)
        self.assertEqual(self.service.state()['counts'],{'queued':3})
        self.service.control('cancel');self.service.seed_done=False;self.service.seed_cursor=0;self.service.discover_tick()
        self.assertEqual(self.service.state()['counts'],{'cancelled':3})

    def test_pause_cancels_result_commit_and_resume_can_continue(self):
        self.service.discover_tick()
        def inference(*args):
            self.service.configure({'paused':True});return json.dumps(result())
        with patch('asset_intelligence.vision',side_effect=inference):self.service.tick()
        self.assertEqual(self.service.state()['coverage'],0)
        self.assertEqual(self.service.state()['counts'].get('running',0),0)

    def test_provider_and_dimension_change_enqueues_new_version(self):
        self.parse();self.service.configure({'custom_dimensions':['电商卖点']});self.service.discover_tick()
        self.assertEqual(self.service.state()['counts']['queued'],3)

    def test_invalid_model_json_and_injection_query_are_safe(self):
        self.service.discover_tick()
        with patch('asset_intelligence.vision',return_value='not json'):self.service.tick()
        self.assertEqual(self.service.state()['counts']['failed'],1)
        self.assertEqual(self.service.search(text='" OR 1=1 -- %_')['rows'],[])
        self.assertEqual(self.service.state()['coverage'],0)

    def test_builtin_multimodal_enabled_and_selectable(self):
        self.providers.set_builtin(True)
        provider=self.providers.selected('multimodal')
        self.assertEqual(provider['model'],'qwen2.5-vl-7b-q4')
        self.providers.check_inference(provider)

    def test_existing_builtin_ocr_configuration_gains_vlm_on_upgrade(self):
        self.providers.set_builtin(True)
        self.providers.data['providers']=[p for p in self.providers.data['providers'] if p['protocol']!='local' or p['category']!='multimodal']
        self.providers._persist()
        upgraded=Providers(self.base/'data')
        self.assertTrue(upgraded.public()['builtin_enabled'])
        self.assertEqual(upgraded.selected('multimodal')['model'],'qwen2.5-vl-7b-q4')
        self.assertEqual(upgraded.selected('ocr')['model'],'glm-ocr-local')

    def test_online_database_backup_contains_verified_results(self):
        import sqlite3
        from contextlib import closing
        self.seed('one.jpg');saved=self.service.backup()
        with closing(sqlite3.connect(saved['path'])) as c:
            self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0],'ok')
            self.assertEqual(c.execute('SELECT count(*) FROM intelligence_results').fetchone()[0],1)

    def test_video_samples_preserve_timestamps_and_index_each_frame(self):
        import shutil
        import subprocess
        ffmpeg=shutil.which('ffmpeg');probe=shutil.which('ffprobe')
        if not ffmpeg or not probe:self.skipTest('此环境未提供 FFmpeg / ffprobe')
        video=self.folder/'sample.mp4'
        subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i','color=c=red:s=96x96:d=1',
                        '-f','lavfi','-i','color=c=blue:s=96x96:d=1','-filter_complex',
                        '[0:v][1:v]concat=n=2:v=1:a=0','-pix_fmt','yuv420p',str(video)],
                       check=True,capture_output=True,timeout=20)
        self.catalog.scan(self.rows[0]['root']);row=self.catalog.items(media='video')['rows'][0]
        with patch('video_preview.find_tool',side_effect=lambda name:ffmpeg if name=='ffmpeg' else probe):
            frames=self.service._images(row)
        self.assertEqual(len(frames),3)
        self.assertTrue(0<frames[0][0]<frames[1][0]<frames[2][0]<2.1)
        colors=[]
        for _,payload in frames:
            with Image.open(io.BytesIO(payload)) as image:
                r,g,b=image.resize((1,1)).getpixel((0,0));colors.append('red' if r>b else 'blue')
        self.assertEqual(colors[0],'red');self.assertEqual(colors[-1],'blue')

    def test_large_visual_index_uses_bounded_candidates_and_finds_reference(self):
        import numpy as np
        from retrieval_store import store_vector
        row=self.seed('one.jpg');rng=np.random.default_rng(419)
        with self.catalog.connect() as c:
            for number in range(10001):
                item=f'{number+100000:032x}'
                c.execute('''INSERT INTO items(id,root,path,relative,folder,name,extension,size,modified,fingerprint,last_seen,pair_key)
                    VALUES(?,?,?,?,?,?,'.jpg',1,1,'test',1,?)''',
                    (item,row['root'],str(self.folder/(item+'.jpg')),item+'.jpg','/',item+'.jpg',item))
                vector=rng.random(192,dtype=np.float32);vector=vector/np.linalg.norm(vector)
                store_vector(c,item,0,APPEARANCE,'test',vector.tolist())
        response=self.service.image_search({'item_id':row['id'],'threshold':.999,'root':row['root']})
        self.assertTrue(response['approximate']);self.assertFalse(response['incomplete'])
        self.assertLessEqual(response['candidates'],6000)
        self.assertEqual(response['rows'][0]['id'],row['id'])

    def test_fts_upgrade_keeps_results_and_triggers_valid(self):
        from retrieval_store import install
        row=self.seed('one.jpg')
        with self.catalog.connect() as c:
            c.execute('DROP TABLE intelligence_fts')
            c.execute("CREATE VIRTUAL TABLE intelligence_fts USING fts5(item UNINDEXED,terms,tokenize='unicode61')")
            c.execute('INSERT INTO intelligence_fts VALUES(?,?)',(row['id'],'红色 衬衫'))
        with self.catalog.connect() as c:install(c)
        self.assertEqual(len(self.service.search(text='红色')['rows']),1)
        with self.catalog.connect() as c:c.execute('DELETE FROM items WHERE id=?',(row['id'],))
        self.assertEqual(self.service.search(text='红色')['rows'],[])


if __name__=='__main__':unittest.main()
