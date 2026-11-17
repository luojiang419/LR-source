"""全页缓存恢复、写后失效、会话隔离、脚本包和慢接口下的实际交互。"""
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit
from urllib.request import urlopen,Request
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from workbench_views import WorkbenchViews,view_key,PATHS
from workbench_assets import WorkbenchAssets
import test_asset_retrieval_browser as fixtures
from inventory_service import InventoryService
from test_inventory import record
from test_sku_workflow import fixture_book
import server

ROOT=Path(__file__).resolve().parents[1]


class ViewSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.db=self.base/'db';self.db.touch()
        self.views=WorkbenchViews(self.base,self.db)

    def tearDown(self):self.tmp.cleanup()

    def test_restart_scope_and_credentials_and_mutation_epoch(self):
        value={'rows':[{'sku':'LC123'}],'request_token':'old-token'}
        self.views.remember('/api/sku/library?fresh=0&sku=A',value,'one',0)
        value['rows'].clear()
        views=WorkbenchViews(self.base,self.db)
        self.assertEqual(views.bootstrap('two')['values'],{})
        saved=views.bootstrap('one')['values']['/api/sku/library?sku=A']
        self.assertEqual(saved['rows'][0]['sku'],'LC123');self.assertNotIn('request_token',saved)
        views.invalidate();views.remember('/api/sku',{'stale':True},'one',0)
        self.assertFalse(WorkbenchViews(self.base,self.db).bootstrap('one')['values'])

    def test_corrupt_foreign_bounded_and_failed_queries(self):
        self.views.folder.mkdir();path=self.views.folder/'views.json';path.write_text('[]')
        self.assertFalse(WorkbenchViews(self.base,self.db).values)
        for n in range(50):self.views.remember('/api/sku/library?sku='+str(n),{'rows':[]},'',0)
        self.assertLessEqual(len(self.views.values),32)
        before=self.views.values.copy()
        self.views.remember('/api/sku',{'error':'busy'},'',0);self.views.remember('/api/sku',{'loading':True},'',0)
        self.assertEqual(before,self.views.values)
        value=json.loads(path.read_bytes());value['identity']='other';path.write_text(json.dumps(value))
        self.assertFalse(WorkbenchViews(self.base,self.db).values)

    def test_bundle_order_and_source_invalidation_and_image_versions(self):
        assets=WorkbenchAssets(ROOT/'演示方案');body,etag=assets.read()
        self.assertLess(body.index(b'window.FramekeepViewCache='),body.index(b'function partialBody'))
        self.assertEqual(assets.read(),(body,etag))
        self.assertEqual(view_key('/api/sku/library?client=x&fresh=1&sku=A'),'/api/sku/library?sku=A')
        self.assertIsNone(view_key('/api/update'))
        self.assertNotEqual(self.views.image_key('/api/preview/a/b?v=one&size=420'),self.views.image_key('/api/preview/a/b?v=two&size=420'))
        self.assertIsNone(self.views.image_key('/api/preview/a/b?v=one&size=2048'))

    def test_inventory_updates_preserve_other_pages_and_old_reads_do_not_publish(self):
        self.views.remember('/api/sku',{'rows':[1]},'one',0)
        self.views.remember('/api/inventory/records',{'rows':[1]},'one',0)
        self.views.invalidate('/api/inventory/usb')
        self.assertEqual(self.views.epoch,0)
        self.views.invalidate('/api/inventory/delete')
        self.views.remember('/api/inventory/records',{'rows':['old']},'one',0)
        values=self.views.bootstrap('one')['values']
        self.assertIn('/api/sku',values);self.assertNotIn('/api/inventory/records',values)

    def test_normal_session_save_keeps_restart_views_but_rejects_old_publish(self):
        self.views.remember('/api/sku/library',{'rows':['saved']},'one',0)
        self.views.remember('/api/parsing',{'jobs':['saved']},'one',0)
        self.views.invalidate('/api/session/patch')
        self.views.remember('/api/parsing',{'jobs':['late']},'one',0)
        values=WorkbenchViews(self.base,self.db).bootstrap('one')['values']
        self.assertEqual(values['/api/sku/library']['rows'],['saved']);self.assertEqual(values['/api/parsing']['jobs'],['saved'])


class InstantWorkbenchBrowserTests(unittest.TestCase):
    def setUp(self):
        fixtures.RetrievalBrowserTests.setUp(self)
        self.inventory=InventoryService(self.store.data,autostart=False)
        self.inventory.usb=lambda body=None:{'phase':'idle','auto':False,'devices':[],'message':'测试不连接手机'}
        self.inventory.receive(record());self.stack.enter_context(patch.object(server,'INVENTORY',self.inventory))
        self.sku.import_file('task.xlsx',fixture_book(),preferred_scope='模特图')
        from sku_library_index import for_sku
        for_sku(self.sku).refresh()

    def tearDown(self):
        self.inventory.close();fixtures.RetrievalBrowserTests.tearDown(self)

    def test_bundle_http_and_all_pages_restore_before_slow_readers(self):
        # 使用真实业务接口登记快照；重新创建浏览器后，所有这些读取仍挂起。
        paths=['/api/sku','/api/parsing','/api/providers','/api/inventory/records?mode=&query=&offset=0&limit=60',
               '/api/intelligence/search-state','/api/intelligence/facets?key=',
               '/api/sku/library?q=&sort=name&root=&folder=&sku=&bin_id=&offset=0&limit=120&fresh=0']
        for path in paths:
            with urlopen(self.url+path,timeout=10) as response:self.assertEqual(response.status,200)
        # 首款图片请求的筛选键会改变，再把真实首款页保存。
        with urlopen(self.url+paths[-1]) as response:data=json.load(response)
        if data['groups']:
            with urlopen(self.url+paths[-1].replace('sku=&','sku='+data['groups'][0]['normalized']+'&')) as response:response.read()
        with urlopen(self.url+'/workbench.js') as response:etag=response.headers['ETag'];self.assertGreater(len(response.read()),10000)
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as response:urlopen(Request(self.url+'/workbench.js',headers={'If-None-Match':etag}))
        self.assertEqual(response.exception.code,304)
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();errors=[];held=[];page.on('pageerror',lambda e:errors.append(str(e)))
                def block(route):
                    if route.request.method=='GET' and urlsplit(route.request.url).path in PATHS:held.append(route)
                    else:route.continue_()
                page.route('**/api/**',block)
                page.goto(self.url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading')
                for name in ('selection','progress','archive','inventory','sku-database','retrieval','analysis','settings'):
                    page.evaluate('name=>FramekeepWorkspace.show(name)',name)
                    page.wait_for_function('name=>FramekeepWorkspace.current()===name',arg=name,timeout=500)
                page.evaluate("FramekeepWorkspace.show('progress')")
                page.wait_for_function("()=>document.querySelectorAll('#sku-rows tr').length>1",timeout=1500)
                page.evaluate("FramekeepWorkspace.show('inventory')")
                page.wait_for_function("()=>document.getElementById('inv-total').textContent==='1 条'",timeout=1500)
                self.assertTrue(held);self.assertFalse(errors,errors)
            finally:browser.close()

    def test_inventory_rows_do_not_wait_for_connection_and_quick_input_focus(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();held=[]
                page.route('**/api/inventory/state',lambda route:held.append(route))
                page.goto(self.url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading')
                page.evaluate("FramekeepWorkspace.show('inventory')")
                page.wait_for_function("()=>document.getElementById('inv-total').textContent==='1 条'",timeout=1500)
                page.route('**/api/intelligence/search-state',lambda route:held.append(route))
                page.evaluate('()=>{FramekeepRetrieval.openQuick()}')
                page.wait_for_function("()=>document.activeElement?.classList.contains('retrieval-query')",timeout=800)
                page.keyboard.type('LC123');self.assertEqual(page.locator('#quick-search-dialog .retrieval-query').input_value(),'LC123')
            finally:browser.close()

    def test_selection_shows_cached_picture_then_upgrades_without_blocking(self):
        import base64,io
        from PIL import Image
        def jpeg(size,color):
            image=io.BytesIO();Image.new('RGB',size,color).save(image,format='JPEG');return image.getvalue()
        small=jpeg((80,40),'red');large=jpeg((160,80),'blue')
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();page.goto(self.url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading')
                held=[];pattern='**/api/preview/cache-test/photo*'
                page.route(pattern,lambda route:held.append(route))
                page.evaluate("payload=>{FramekeepWorkspace.show('selection');FramekeepViewCache.images.set('/api/preview/cache-test/photo?size=420&v=one',payload);const image=document.createElement('img');image.id='cache-picture';image.src='/api/preview/cache-test/photo?full=1&v=one';document.getElementById('selection-page').append(image)}",'data:image/jpeg;base64,'+base64.b64encode(small).decode())
                page.wait_for_function("()=>document.getElementById('cache-picture').naturalWidth===80",timeout=1000)
                self.assertTrue(held)
                page.evaluate("FramekeepWorkspace.show('inventory')")
                page.unroute(pattern);page.route(pattern,lambda route:route.fulfill(body=large,content_type='image/jpeg'))
                page.evaluate("FramekeepWorkspace.show('selection')")
                page.wait_for_function("()=>document.getElementById('cache-picture').naturalWidth===160",timeout=1500)
            finally:browser.close()

    def test_background_rows_update_and_connection_settings_keep_user_draft(self):
        with urlopen(self.url+'/api/inventory/records?mode=&query=&offset=0&limit=60') as response:response.read()
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();page.goto(self.url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading')
                page.evaluate("FramekeepWorkspace.show('inventory')")
                page.wait_for_function("()=>document.getElementById('inv-total').textContent==='1 条'")
                self.inventory.receive(record('out'))
                page.evaluate('()=>{FramekeepWorkspace.refresh()}')
                page.wait_for_function("()=>document.getElementById('inv-total').textContent==='2 条'",timeout=5000)
                held=[];page.route('**/api/inventory/state',lambda route:held.append(route))
                page.locator('#inv-config-open').click();page.locator('#inv-port').fill('8799')
                page.wait_for_function("()=>document.getElementById('inv-config').open")
                with page.expect_response('**/api/inventory/state'):
                    routes=held[:];held.clear()
                    for route in routes:route.continue_()
                    page.unroute('**/api/inventory/state')
                page.evaluate('()=>new Promise(requestAnimationFrame)')
                self.assertEqual(page.locator('#inv-port').input_value(),'8799')
                page.locator('#inv-config-save').click()
                page.wait_for_function("()=>document.getElementById('inv-config-status').textContent==='已保存'")
                self.assertEqual(self.inventory.state()['port'],8799)
            finally:browser.close()

    def test_all_settings_sections_are_immediate_and_keep_unsaved_rules(self):
        with urlopen(self.url+'/api/providers') as response:response.read()
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();page.goto(self.url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading')
                page.locator('#app-settings-open').click()
                sections=('models','sku-rules','files','streams','remote','quick','preview','table','library','database','general')
                self.assertEqual(page.locator('.app-settings-nav button').count(),len(sections))
                for section in sections:
                    page.locator('[data-settings-section="'+section+'"]').click()
                    self.assertTrue(page.locator('#settings-'+section).is_visible())
                    self.assertEqual(page.locator('.app-settings-section:visible').count(),1)
                page.locator('[data-settings-section="sku-rules"]').click();page.locator('#sku-rule-add').click()
                page.get_by_label('款号匹配文字',exact=True).last.fill('DRAFT')
                page.locator('[data-settings-section="models"]').click();page.locator('[data-settings-section="sku-rules"]').click()
                self.assertEqual(page.get_by_label('款号匹配文字',exact=True).last.input_value(),'DRAFT')
            finally:browser.close()

    def test_cached_retrieval_settings_open_while_read_is_pending_and_save_with_fresh_token(self):
        for path in ('/api/intelligence/search-state','/api/intelligence/state'):
            with urlopen(self.url+path) as response:response.read()
        self.stack.enter_context(patch.object(server,'TOKEN','fresh-workbench-token'))
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();held=[]
                page.route('**/api/intelligence/state',lambda route:held.append(route))
                page.goto(self.url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading')
                page.evaluate('()=>{FramekeepRetrieval.settings()}')
                page.wait_for_selector('#retrieval-settings[open]',timeout=1000)
                page.locator('#retrieval-settings [data-config="quick_blur"]').evaluate("node=>{node.value='17';node.dispatchEvent(new Event('input',{bubbles:true}))}")
                page.locator('#retrieval-settings [data-config="save"]').click()
                page.wait_for_function("()=>document.querySelector('#retrieval-settings .retrieval-message').textContent.includes('已保存')")
                self.assertEqual(self.service.config['quick_blur'],17)
                self.assertTrue(held)
            finally:browser.close()

    def test_live_timestamps_do_not_create_refresh_loop_and_explicit_refresh_stays_fresh(self):
        original=server.parsing_state;calls=[]
        def tick():
            calls.append(1);return {**original(),'changing_clock':len(calls)}
        self.stack.enter_context(patch.object(server,'parsing_state',side_effect=tick))
        with urlopen(self.url+'/api/parsing') as response:response.read()
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();page.goto(self.url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading')
                with page.expect_response('**/api/parsing'):page.evaluate("FramekeepWorkspace.show('analysis')")
                page.wait_for_timeout(600)
                self.assertLessEqual(len(calls),3,'时间字段变化不能触发逐帧读取循环')
                result=page.evaluate("""async()=>{let calls=0;const fetch=async()=>({rows:[],value:++calls});const cache=FramekeepViewCache;
                  await cache.read('/api/sku/library?sku=REFRESH_TEST',fetch);
                  await cache.read('/api/sku/library?sku=REFRESH_TEST&fresh=1',fetch);
                  const result=await cache.read('/api/sku/library?sku=REFRESH_TEST',fetch);
                  cache.invalidate('/api/session/patch');
                  const afterSave=await Promise.race([cache.read('/api/sku/library?sku=REFRESH_TEST',()=>new Promise(()=>{})),new Promise(resolve=>setTimeout(()=>resolve({value:'timeout'}),300))]);
                  return {calls,value:result.value,afterSave:afterSave.value}}""")
                self.assertEqual(result,{'calls':2,'value':2,'afterSave':2})
            finally:browser.close()

    def test_large_inventory_images_use_persistent_thumbnails_and_preserve_full_zoom(self):
        import base64,hashlib,io,re
        from PIL import Image
        output=io.BytesIO();Image.effect_noise((1800,1400),70).convert('RGB').save(output,format='JPEG',quality=92)
        original=output.getvalue();self.assertGreater(len(original),128*1024)
        item=record();item['product']=base64.b64encode(original).decode();item['product_sha']=hashlib.sha256(original).hexdigest();self.inventory.receive(item)
        thumbnail=self.inventory.image(item['id'],'product',420)
        with Image.open(io.BytesIO(thumbnail)) as image:self.assertLessEqual(max(image.size),420)
        self.assertEqual(self.inventory.image(item['id'],'product'),original)
        with patch('inventory_service.Image.open',side_effect=AssertionError('重读缩略图不能再解码原图')):
            self.assertEqual(self.inventory.image(item['id'],'product',420),thumbnail)
        url=self.url+'/api/inventory/image?id='+item['id']+'&kind=product'
        with urlopen(url+'&size=420') as response:
            self.assertEqual(response.read(),thumbnail);self.assertIn('immutable',response.headers['Cache-Control'])
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(reduced_motion='reduce');held=[];page.route(re.compile(re.escape(url)+'$'),lambda route:held.append(route))
                page.goto(self.url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading');page.evaluate("FramekeepWorkspace.show('inventory')")
                page.locator('[data-inv-photo="'+url.removeprefix(self.url)+'"]').click()
                page.wait_for_function("()=>document.getElementById('inv-photo-image').naturalWidth===420",timeout=1500)
                page.locator('#inv-photo-plus').click();self.assertEqual(page.locator('#inv-photo-scale').inner_text(),'150%')
                self.assertTrue(held);before=page.locator('#inv-photo-image').bounding_box()
                for route in held:route.continue_()
                page.wait_for_function("()=>document.getElementById('inv-photo-image').naturalWidth===1800",timeout=2000)
                after=page.locator('#inv-photo-image').bounding_box();self.assertLess(abs(before['width']-after['width']),1)
                self.assertEqual(page.locator('#inv-photo-scale').inner_text(),'150%')
            finally:browser.close()

    def test_selection_preview_does_not_shadow_media_module_with_archive_filter(self):
        import media_engine as media
        folder=self.base/'LC1234';photo=folder/'衬衫正面.jpg'
        sid=self.store.ensure_live_session(folder);record=media.capture_record(folder,photo,None,{})
        self.store.append_live(sid,[record]);self.store.activate(sid)
        with urlopen(self.url+'/api/catalog/bootstrap?media=image') as response:self.assertIn('tree',json.load(response))
        with urlopen(self.url+'/api/preview/'+sid+'/'+record['id']+'?size=480&v='+record['fingerprint']) as response:
            self.assertEqual(response.status,200);self.assertEqual(response.headers['Content-Type'],'image/jpeg');self.assertTrue(response.read().startswith(b'\xff\xd8'))

    def test_task_preview_uses_current_version_without_decoding_unrelated_captures(self):
        import media_engine as media
        folder=self.base/'LC1234';photo=folder/'衬衫正面.jpg';sid=self.store.ensure_live_session(folder)
        record=media.capture_record(folder,photo,None,{});self.store.append_live(sid,[record])
        unrelated=json.dumps({'id':'unrelated','fingerprint':'unrelated','jpg':'unrelated.jpg','fileCount':1})
        with self.store.connect() as c:c.execute('INSERT INTO captures(session,id,data,position) VALUES(?,?,?,?)',(sid,'unrelated',unrelated,999))
        with self.sku.connect() as c:c.execute('INSERT INTO jobs(id,project,session_id,capture_id,fingerprint,scope,status,due,created) VALUES(?,?,?,?,?,?,?,?,?)',('preview-version',self.sku.project()['id'],sid,record['id'],'old-job-fingerprint','模特图','completed',0,0))
        decode=json.loads
        def selected_only(value,*args,**kwargs):
            if value==unrelated:raise AssertionError('任务状态不能解析无关照片')
            return decode(value,*args,**kwargs)
        with patch.object(self.sku,'full_auto_state',return_value={}),patch('catalog_store.json.loads',side_effect=selected_only):data=self.sku.task_state()
        row=next(job for job in data['jobs'] if job['capture_id']==record['id'])
        self.assertEqual(row['fingerprint'],'old-job-fingerprint')
        self.assertIn('&v='+record['fingerprint'],row['preview'])
        with urlopen(self.url+row['preview']) as response:self.assertEqual(response.status,200)

    def test_readonly_selection_post_preserves_real_session_startup_snapshots(self):
        import media_engine as media
        folder=self.base/'LC1234';sid=self.store.ensure_live_session(folder)
        record=media.capture_record(folder,folder/'衬衫正面.jpg',None,{})
        self.store.append_live(sid,[record]);self.store.activate(sid)
        with urlopen(self.url+'/api/bootstrap') as response:response.read()
        with urlopen(self.url+'/api/parsing') as response:response.read()
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();held=[];page.route('**/api/parsing',lambda route:held.append(route))
                page.goto(self.url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading')
                self.assertIn('/api/parsing',page.evaluate('Object.keys(FRAMEKEEP_SESSION.viewBootstrap.values)'))
                page.evaluate("data=>FramekeepIO.api('/api/archive/selection',data)",{'session_id':sid,'capture_ids':[record['id']]})
                result=page.evaluate("async()=>Promise.race([FramekeepIO.api('/api/parsing'),new Promise(resolve=>setTimeout(()=>resolve({timeout:true}),300))])")
                self.assertIn('ocr',result)
                self.assertIn('/api/parsing',server.workbench_views().bootstrap(sid)['values'])
            finally:browser.close()


if __name__=='__main__':unittest.main()
