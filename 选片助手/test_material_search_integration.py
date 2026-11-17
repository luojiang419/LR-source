"""真实复制图片、原项目独立库与正式资产检索页面的端到端验收。"""
import base64
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import expect,sync_playwright

import server
from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from archive_visits import ArchiveVisits
from asset_intelligence import AssetIntelligence
from catalog_store import Store
from model_providers import Providers
from reference_library import ReferenceLibrary
from sku_service import SkuService

ROOT=Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('FRAMEKEEP_TEST_REAL_CLIP')=='1','设置 FRAMEKEEP_TEST_REAL_CLIP=1 运行真实模型验收')
class MaterialSearchIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='lr-materialsearch-');self.base=Path(self.tmp.name)
        self.out=ROOT/'build/archive-image-search-integration';self.out.mkdir(parents=True,exist_ok=True)
        self.photos=self.base/'photos';self.photos.mkdir();self.sources={}
        originals=sorted((ROOT/'演示方案/assets').glob('*.jpg'))
        for number,position in enumerate((0,1,4,8,12,16)):
            path=originals[position];folder=self.photos/('A' if number<3 else 'B/deep');folder.mkdir(parents=True,exist_ok=True)
            target=folder/path.name;shutil.copy2(path,target);self.sources[str(path)]=hashlib.sha256(path.read_bytes()).hexdigest()
        shutil.copy2(ROOT/'品牌资源/SHIYING-logo原始设计.png',self.photos/'logo.png')
        self.query=next((self.photos/'A').glob('*.jpg'))
        self.store=Store(self.base/'data',ROOT);self.catalog=AssetCatalog(self.store.data);self.catalog.visits=ArchiveVisits(self.catalog)
        self.library=ReferenceLibrary(self.store.data,catalog=self.catalog,autostart=False)
        self.rid=self.library.add_root(str(self.photos))['root_id']
        self.providers=Providers(self.store.data);self.archive=AssetArchive(self.store,autostart=False)
        self.sku=SkuService(self.store,self.providers,autostart=False)
        self.service=AssetIntelligence(self.catalog,self.providers,self.sku,autostart=False)
        self.service.quick_index.build()
        self.stack=ExitStack()
        values=dict(STORE=self.store,CATALOG=self.catalog,ARCHIVE=self.archive,SKU=self.sku,MODELS=self.providers,
                    INTELLIGENCE=self.service,LANWORKSPACE=None,DATA=self.store.data,WATCH=None,MOUSE=None,ANALYSIS=None,
                    RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None,CANON=None,PREVIEWS=None,REFERENCES=self.library)
        for name,value in values.items():self.stack.enter_context(patch.object(server,name,value))
        self.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);self.http.daemon_threads=True
        threading.Thread(target=self.http.serve_forever,daemon=True).start();self.url=f'http://127.0.0.1:{self.http.server_port}'

    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.stack.close();self.service.close();self.sku.close()
        self.archive.close();self.library.close();self.catalog.close();self.tmp.cleanup()

    def wait_index(self,count,timeout=90,failed=0):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            state=self.service.visual.state()
            if state.get('phase')=='failed':self.fail(state)
            if state.get('indexed')==count and state.get('phase')=='idle':
                self.assertEqual(state.get('failed'),failed,state);return state
            time.sleep(.2)
        self.fail(state)

    def visual_rows(self):
        with closing(sqlite3.connect(self.store.data/'visual-search/assets.db')) as c:
            return c.execute('SELECT id,path,checksum FROM image ORDER BY path').fetchall()

    def wait_visible_photos(self,page,count):
        page.wait_for_function('''expected=>{
          const images=[...document.querySelectorAll('#asset-retrieval-page .retrieval-card img')];
          return images.length===expected&&images.every(image=>{
            const box=image.closest('.retrieval-preview'),scanner=box.querySelector('.retrieval-preview-scanner');
            return image.complete&&image.naturalWidth>0&&box.dataset.previewState==='ready'
              &&Number(getComputedStyle(image).opacity)>.99&&Number(getComputedStyle(scanner).opacity)<.01;
          });
        }''',arg=count)

    def test_copied_photos_browser_and_incremental_lifecycle(self):
        report={'copied_images':7,'checks':[]};errors=[];http_errors=[]
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':1000})
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('response',lambda response:http_errors.append((response.status,response.url)) if response.status>=400 and '/api/' in response.url else None)
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-retrieval-open').click()
                panel=page.locator('#asset-retrieval-page');expect(panel).to_be_visible()
                page.locator('.retrieval-actions-menu>summary').click()
                page.locator('[data-workspace-heading="retrieval"] [data-action="settings"]').click()
                expect(page.locator('#retrieval-settings')).to_be_visible()
                page.locator('[data-config="image_refresh"]').click()
                state=self.wait_index(7);report['initial_state']=state
                page.wait_for_function('()=>document.querySelector("[data-image-index-status]").textContent.includes("已建库 7")')
                page.screenshot(path=str(self.out/'index-ready.png'))
                page.locator('[data-config="close"]').click()
                with page.expect_response(lambda response:'/api/intelligence/image' in response.url and response.request.method=='POST',timeout=60000) as query:
                    panel.locator('.retrieval-upload').set_input_files(str(self.query))
                response=query.value;self.assertEqual(response.status,200,response.text());result=response.json()
                fronts={'XSY_0929.jpg','XSY_0930.jpg','XSY_0933.jpg'}
                self.assertEqual(response.request.post_data_json['threshold'],.85)
                self.assertEqual(response.request.post_data_json['match_mode'],'frame')
                self.assertEqual(result['match_mode'],'frame');self.assertEqual(result['threshold_applied'],.85)
                self.assertEqual({row['name'] for row in result['rows']},fronts)
                self.assertEqual(result['rows'][0]['path'],str(self.query));self.assertGreater(result['rows'][0]['semantic_score'],.99)
                self.assertTrue(all(row['score']>=.85 and row['detail'] for row in result['rows']))
                expect(panel.locator('.retrieval-card')).to_have_count(3);self.wait_visible_photos(page,3)
                panel.locator('.retrieval-card').first.click()
                with page.expect_response(lambda r:'/api/intelligence/image' in r.url and r.request.method=='POST') as similar:
                    panel.locator('[data-detail="similar"]').click()
                self.assertEqual({row['name'] for row in similar.value.json()['rows']},fronts)
                panel.locator('.retrieval-card').first.click();panel.locator('[data-detail="preview"]').click()
                expect(page.locator('#asset-image-viewer')).to_be_visible()
                page.wait_for_function('()=>{const i=document.getElementById("asset-viewer-image");return i&&!i.hidden&&i.complete&&i.naturalWidth>0&&i.getBoundingClientRect().width>0}')
                page.screenshot(path=str(self.out/'image-search-fullscreen.png'))
                page.locator('#asset-viewer-close').click();expect(page.locator('#asset-image-viewer')).to_be_hidden()
                self.wait_visible_photos(page,3)
                page.screenshot(path=str(self.out/'image-search-frame-match.png'))
                report['search']={'top':result['rows'][0]['name'],'score':result['rows'][0]['score'],'semantic_score':result['rows'][0]['semantic_score'],'match_mode':result['match_mode'],'elapsed_ms':result['elapsed_ms'],'returned':len(result['rows'])}
                report['checks']+=['settings build','all unvisited subdirectories indexed','upload search','all result thumbnails','selected result similarity search','full-screen preview']
                def choose_mode(mode):
                    with page.expect_response(lambda r:'/api/intelligence/image' in r.url and r.request.method=='POST') as changed:
                        panel.locator('.reference-match-mode').select_option(mode)
                    value=changed.value.json();self.assertEqual(changed.value.status,200)
                    self.assertEqual(changed.value.request.post_data_json['match_mode'],mode)
                    return value
                self.assertEqual(panel.locator('.reference-match-mode').input_value(),'frame')
                content=choose_mode('semantic');self.assertEqual(len(content['rows']),6)
                expect(panel.locator('.retrieval-card')).to_have_count(6);self.wait_visible_photos(page,6)
                page.screenshot(path=str(self.out/'image-search-content-mode.png'))
                slider=panel.locator('.reference-threshold');self.assertEqual(slider.input_value(),'85')
                with page.expect_response(lambda r:'/api/intelligence/image' in r.url and r.request.method=='POST') as relaxed:
                    slider.evaluate("e=>{e.value='50';e.dispatchEvent(new Event('input',{bubbles:true}))}")
                self.assertEqual(relaxed.value.request.post_data_json['threshold'],.5)
                expect(panel.locator('.retrieval-card')).to_have_count(7)
                expect(panel.locator('.reference-help')).to_contain_text('可能包含画面差异')
                with page.expect_response(lambda r:'/api/intelligence/image' in r.url and r.request.method=='POST') as strict:
                    slider.evaluate("e=>{e.value='85';e.dispatchEvent(new Event('input',{bubbles:true}))}")
                self.assertEqual(strict.value.request.post_data_json['threshold'],.85)
                expect(panel.locator('.retrieval-card')).to_have_count(6)
                precision=choose_mode('frame');self.assertEqual({row['name'] for row in precision['rows']},fronts)
                expect(panel.locator('.retrieval-card')).to_have_count(3)
                report['relevance']={'default_threshold':.85,'default_match_mode':'frame','front_without_hands':sorted(fronts),'content_mode_returned':6,'lower_threshold_requeries':True}
                for label,path,expected in (
                    ('unindexed_front_without_hands',ROOT/'演示方案/assets/XSY_0931.jpg',fronts),
                    ('hands_query',self.photos/'B/deep/XSY_0937.jpg',{'XSY_0937.jpg'}),
                    ('side_query',self.photos/'B/deep/XSY_0945.jpg',{'XSY_0945.jpg'}),
                    ('logo_query',self.photos/'logo.png',{'logo.png'}),
                    ('unrelated_screenshot',ROOT/'演示方案/AI选片工作台.png',set())):
                    with page.expect_response(lambda r:'/api/intelligence/image' in r.url and r.request.method=='POST') as queried:
                        panel.locator('.retrieval-upload').set_input_files(str(path))
                    value=queried.value.json();self.assertEqual(queried.value.status,200)
                    names={row['name'] for row in value['rows']};self.assertEqual(names,expected,(label,value))
                    expect(panel.locator('.retrieval-card')).to_have_count(len(expected))
                    if expected:self.wait_visible_photos(page,len(expected))
                    report['relevance'][label]={'returned':len(expected),'names':sorted(names)}
                    page.screenshot(path=str(self.out/('relevance-'+label+'.png')))
                with page.expect_response(lambda r:'/api/intelligence/image' in r.url and r.request.method=='POST'):
                    panel.locator('.retrieval-upload').set_input_files(str(self.query))
                expect(panel.locator('.retrieval-card')).to_have_count(3);self.wait_visible_photos(page,3)
                page.screenshot(path=str(self.out/'image-search-results.png'))
                page.screenshot(path=str(self.out/'image-search-frame-match.png'))
                report['checks']+=['default frame mode excludes hands and side poses','explicit content mode preserves broad garment retrieval','relaxing and restoring content threshold requery correctly',
                                   'unindexed front view retrieves only frontal views without hands','hand query does not globally ban hands','side query finds the side pose','logo query retrieves logo only','unmatched query returns no filler results']
                with self.catalog.connect() as c:
                    report['metadata_items']=c.execute('SELECT count(*) FROM items').fetchone()[0]
                    report['metadata_vectors']=c.execute('SELECT count(*) FROM intelligence_vectors').fetchone()[0]
                self.assertEqual(report['metadata_items'],0);self.assertEqual(report['metadata_vectors'],0)
                # 同时覆盖已在归档页面访问、已经有元数据资产 ID 的图片。
                self.catalog.visits.touch(self.rid,'A')
                until=time.monotonic()+15
                while self.catalog.visits.state()['running'] and time.monotonic()<until:time.sleep(.02)
                self.assertFalse(self.catalog.visits.state()['running']);self.assertFalse(self.catalog.visits.state()['error'])
                self.service.quick_index.build()
                with page.expect_response(lambda r:'/api/intelligence/image' in r.url and r.request.method=='POST') as known:
                    panel.locator('[data-reference="generate"]').click()
                rows=known.value.json()['rows'];self.assertEqual({row['name'] for row in rows},fronts)
                self.assertTrue(all(not row['id'].startswith('material:') for row in rows))
                expect(panel.locator('.retrieval-card')).to_have_count(3);self.wait_visible_photos(page,3)
                panel.locator('.retrieval-card').first.click()
                with page.expect_response(lambda r:'/api/intelligence/image' in r.url and r.request.method=='POST') as known_similar:
                    panel.locator('[data-detail="similar"]').click()
                self.assertEqual({row['name'] for row in known_similar.value.json()['rows']},fronts)
                report['checks'].append('existing metadata asset ids use the same frame matching and visible previews')
                self.assertFalse(errors,errors);self.assertFalse(http_errors,http_errors)
            finally:browser.close()
        before=self.visual_rows()
        self.service.visual.configure({'image_index_paused':True})
        new=self.photos/'B/deep/new.jpg';shutil.copy2(sorted((ROOT/'演示方案/assets').glob('*.jpg'))[20],new)
        self.service.visual.configure({'image_index_action':'refresh'});time.sleep(1)
        self.assertEqual(self.visual_rows(),before)
        self.service.visual.configure({'image_index_paused':False,'image_index_action':'refresh'})
        self.wait_index(8)
        self.assertTrue(all(row in self.visual_rows() for row in before))
        report['checks']+=['pause blocks new image','resume only indexes new image','existing image ids and hashes preserved']
        automatic=self.photos/'auto.jpg';shutil.copy2(self.query,automatic)
        self.wait_index(9,timeout=15)
        auto_results=self.service.image_search({'image':base64.b64encode(self.query.read_bytes()).decode()})
        old_id=next(row['id'] for row in auto_results['rows'] if row['path']==str(automatic))
        automatic.unlink();self.wait_index(8,timeout=15)
        replacement=self.photos/'replacement.jpg';shutil.copy2(self.query,replacement);self.wait_index(9,timeout=15)
        with self.assertRaises(ValueError):self.catalog.item(old_id)
        replacement.unlink();self.wait_index(8,timeout=15)
        report['checks'].append('native file notifications automatically add and remove images')
        report['checks'].append('reused upstream integer ids cannot show an older result as another file')
        self.service.visual.stop_worker();self.service.visual.sync();self.wait_index(8)
        self.assertGreaterEqual(self.service.visual.state()['reused'],8)
        report['checks'].append('restart reuses all unchanged images')
        self.assertTrue(new.resolve().is_relative_to(self.base.resolve()));new.unlink()
        self.service.visual.configure({'image_index_action':'refresh'});self.wait_index(7)
        report['checks'].append('deleted copy removed from visual index')
        broken=self.photos/'broken.jpg';broken.write_bytes(b'invalid copied image fixture')
        self.service.visual.configure({'image_index_action':'refresh'});self.wait_index(7,failed=1)
        shutil.copy2(self.query,broken)
        self.service.visual.configure({'image_index_action':'retry'});self.wait_index(8)
        self.assertTrue(broken.resolve().is_relative_to(self.base.resolve()));broken.unlink()
        self.service.visual.configure({'image_index_action':'refresh'});self.wait_index(7)
        report['checks']+=['corrupt image reported without blocking other files','retry recovers repaired image']
        import cv2
        video=self.photos/'B/deep/clip.mp4'
        pixels=cv2.imread(str(self.query));size=(pixels.shape[1]//2*2,pixels.shape[0]//2*2)
        writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*'mp4v'),4,size)
        self.assertTrue(writer.isOpened())
        pixels=cv2.resize(pixels,size)
        for _ in range(8):writer.write(pixels)
        writer.release()
        self.service.visual.configure({'image_index_action':'refresh'});self.wait_index(8)
        video_result=self.service.image_search({'image':base64.b64encode(self.query.read_bytes()).decode(),'media':'video'})
        self.assertTrue(video_result['rows']);hit=video_result['rows'][0]
        self.assertEqual(hit['path'],str(video));self.assertIn('clip',hit)
        self.assertTrue(self.catalog.preview(hit['id'],420,read=True,frame=hit['timestamp_ms']).startswith(b'\xff\xd8'))
        self.assertTrue(video.resolve().is_relative_to(self.base.resolve()));video.unlink()
        self.service.visual.configure({'image_index_action':'refresh'});self.wait_index(7)
        report['checks'].append('original video frame store and image-to-video match preview')
        with self.catalog.connect() as c:
            report['metadata_items_after_manual_visit']=c.execute('SELECT count(*) FROM items').fetchone()[0]
            report['metadata_vectors_after_manual_visit']=c.execute('SELECT count(*) FROM intelligence_vectors').fetchone()[0]
        self.assertEqual(report['metadata_items_after_manual_visit'],3);self.assertEqual(report['metadata_vectors_after_manual_visit'],0)
        with closing(sqlite3.connect(self.store.data/'visual-search/assets.db')) as c:
            report['visual_tables']=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        self.assertEqual(report['visual_tables'],['image','image_features','video','video_features'])
        self.library.remove_root(self.rid)
        result=self.service.image_search({'image':base64.b64encode(self.query.read_bytes()).decode()})
        self.assertEqual(result['rows'],[])
        report['checks'].append('removed archive root immediately excluded')
        self.assertTrue(all(hashlib.sha256(Path(path).read_bytes()).hexdigest()==value for path,value in self.sources.items()))
        report.update(originals_unchanged=True,page_errors=errors,http_errors=http_errors)
        (self.out/'browser-lifecycle-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')


if __name__=='__main__':unittest.main()
