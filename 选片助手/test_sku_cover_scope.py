"""未拍摄封面隐藏、当前 SKU 组图/分页/保存隔离，保留原表参考图。"""
from contextlib import ExitStack,contextmanager
import copy
from http.server import ThreadingHTTPServer
import json,threading,unittest,urllib.request,urllib.error
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from sku_presentation import gallery,preferences,select_images
from sku_match import normalize
import test_ocr_metadata as fixture
import server


class CoverScopeTests(unittest.TestCase):
    setUp=fixture.OcrMetadataTests.setUp
    tearDown=fixture.OcrMetadataTests.tearDown
    parse=fixture.OcrMetadataTests.parse
    choose=fixture.OcrMetadataTests.choose
    other='LC7877496-P804'

    def shoot_two(self):
        self.parse();self.choose()
        self.sku.parse_capture(self.sid,self.ids[3]);self.choose(self.other,self.ids[3])

    @contextmanager
    def http(self):
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        values=dict(STORE=self.store,DATA=self.store.data,MODELS=self.providers,SKU=self.sku,ARCHIVE=self.archive,CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,CANON=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None)
        with ExitStack() as stack:
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:yield f'http://127.0.0.1:{http.server_port}'
            finally:http.shutdown();http.server_close()

    def test_pending_rows_have_no_cover_but_keep_original_reference_images(self):
        state=self.sku.state()
        self.assertTrue(all(row['sku_image'] is None for row in state['rows']))
        self.assertTrue(next(row for row in state['rows'] if row['sheet']=='模特图' and row['sku']==fixture.CODE)['images'])
        self.assertEqual(gallery(self.sku,self.pid,fixture.CODE)['images'],[])
        self.parse();self.choose()
        chosen=gallery(self.sku,self.pid,fixture.CODE)['images'][0]
        state=select_images(self.sku,self.pid,{normalize(fixture.CODE):chosen['key']},fixture.CODE)
        pending=next(row for row in state['rows'] if row['sheet']=='细节图' and row['sku']==fixture.CODE)
        self.assertEqual(pending['status'],'未拍摄');self.assertIsNone(pending['sku_image']);self.assertTrue(pending['images'])

    def test_group_and_images_are_limited_to_requested_sku_and_foreign_save_is_rejected(self):
        self.shoot_two();data=gallery(self.sku,self.pid,fixture.CODE)
        self.assertEqual([g['normalized'] for g in data['groups']],[normalize(fixture.CODE)])
        self.assertEqual({im['capture_id'] for im in data['images']},{self.ids[0]})
        foreign=gallery(self.sku,self.pid,self.other)['images'][0]
        before=preferences(self.sku,self.pid)
        with self.assertRaises(ValueError):select_images(self.sku,self.pid,{normalize(fixture.CODE):foreign['key']},fixture.CODE)
        with self.assertRaises(ValueError):select_images(self.sku,self.pid,{normalize(self.other):foreign['key']},fixture.CODE)
        self.assertEqual(preferences(self.sku,self.pid),before)

    def test_all_pages_remain_in_current_sku_even_with_other_skus_in_library(self):
        self.shoot_two()
        rows=[{'session_id':self.sid,'capture_id':'a'+str(i),'normalized':normalize(fixture.CODE),'sku':fixture.CODE,'name':f'a{i}.JPG','preview':'/api/preview/test/a'+str(i),'source':'interval'} for i in range(135)]
        rows += [dict(rows[0],capture_id='b'+str(i),normalized=normalize(self.other),sku=self.other,name=f'b{i}.JPG') for i in range(90)]
        with patch('sku_library.browse',return_value={'rows':rows}):pages=[gallery(self.sku,self.pid,fixture.CODE,offset,60) for offset in (0,60,120)]
        self.assertEqual([len(p['images']) for p in pages],[60,60,15])
        self.assertEqual([p['total'] for p in pages],[135,135,135])
        self.assertEqual({im['normalized'] for p in pages for im in p['images']},{normalize(fixture.CODE)})
        self.assertTrue(all(len(p['groups'])==1 for p in pages));self.assertFalse(pages[-1]['has_more'])

    def test_clear_rating_removes_cover_and_rejects_old_selection(self):
        self.parse();self.choose();image=gallery(self.sku,self.pid,fixture.CODE)['images'][0]
        select_images(self.sku,self.pid,{normalize(fixture.CODE):image['key']},fixture.CODE)
        session=self.store.session(self.sid);before=copy.deepcopy(session['state']);after=copy.deepcopy(before);after['overrides'][self.ids[1]]=0
        self.store.save(self.sid,session['revision'],after);self.sku.on_save(self.sid,before,after);self.sku.deliver_events(self.archive)
        state=self.sku.state();self.assertTrue(all(row['sku_image'] is None for row in state['rows'] if row['sku']==fixture.CODE))
        self.assertEqual(gallery(self.sku,self.pid,fixture.CODE)['images'],[])
        with self.assertRaises(ValueError):select_images(self.sku,self.pid,{normalize(fixture.CODE):image['key']},fixture.CODE)

    def test_saved_cover_reassigned_to_another_sku_is_not_displayed(self):
        self.parse();self.choose();self.sku.parse_capture(self.sid,self.ids[3]);self.choose(cid=self.ids[3])
        image=next(im for im in gallery(self.sku,self.pid,fixture.CODE)['images'] if im['capture_id']==self.ids[2])
        select_images(self.sku,self.pid,{normalize(fixture.CODE):image['key']},fixture.CODE)
        self.choose(self.other,self.ids[3])
        row=next(r for r in self.sku.state()['rows'] if r['sheet']=='模特图' and r['sku']==fixture.CODE)
        self.assertEqual(row['status'],'已拍摄');self.assertEqual(row['sku_image']['capture_id'],self.ids[0])
        data=gallery(self.sku,self.pid,fixture.CODE)
        self.assertNotEqual(data['groups'][0]['selected_key'],image['key'])

    def test_http_gallery_requires_current_sku(self):
        with self.http() as url:
            with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(url+'/api/sku/gallery?project_id='+self.pid,timeout=10)
            self.assertEqual(caught.exception.code,400)

    def test_browser_pending_reference_column_and_locked_single_sku_dialog(self):
        with self.http() as url,sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1580,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading');page.locator('#sku-open').click()
                page.wait_for_selector('#sku-rows [data-sku-row]')
                self.assertEqual(page.locator('#sku-rows [data-sku-cover]').count(),0)
                self.assertGreater(page.locator('#sku-rows [data-sku-image] img').count(),0)
                out=fixture.ROOT/'build/sku-cover-scope';out.mkdir(parents=True,exist_ok=True)
                page.locator('#sku-dialog').screenshot(path=str(out/'pending-no-cover.png'))
                self.shoot_two()
                page.wait_for_function('()=>document.querySelectorAll("#sku-rows [data-sku-cover]").length===2')
                cover=page.locator('#sku-rows [data-sku-cover="'+normalize(fixture.CODE)+'"]')
                with page.expect_response('**/api/sku/gallery?**') as response:cover.click()
                data=response.value.json();self.assertEqual({im['normalized'] for im in data['images']},{normalize(fixture.CODE)})
                page.wait_for_selector('#sku-cover-groups [data-cover-key]')
                self.assertEqual(page.locator('#sku-cover-group option').count(),1);self.assertTrue(page.locator('#sku-cover-group').is_disabled())
                self.assertEqual(page.locator('#sku-cover-group').input_value(),normalize(fixture.CODE))
                self.assertEqual(page.locator('#sku-cover-groups [data-cover-sku="'+normalize(self.other)+'"]').count(),0)
                self.assertNotIn(self.other,page.locator('#sku-cover-groups').inner_text())
                before=preferences(self.sku,self.pid);page.locator('#sku-cover-groups [data-cover-key]').first.click();page.locator('#sku-cover-cancel').click()
                self.assertEqual(preferences(self.sku,self.pid),before)
                cover.click();page.wait_for_selector('#sku-cover-groups [data-cover-key]')
                page.wait_for_function('()=>[...document.querySelectorAll("#sku-cover-groups img")].every(i=>i.complete&&i.naturalWidth>0)')
                page.locator('#sku-cover-dialog').screenshot(path=str(out/'single-sku-gallery.png'))
                page.locator('#sku-cover-groups [data-cover-key]').first.click()
                with page.expect_response('**/api/sku/cover') as response:page.locator('#sku-cover-save').click()
                self.assertEqual(response.value.status,200)
                self.assertEqual({row['status'] for row in self.sku.state()['rows'] if row['sheet']=='细节图'},{'未拍摄'})
                page.wait_for_selector('#sku-cover-dialog',state='hidden');page.locator('[data-progress-sheet="细节图"]').click()
                page.wait_for_function('()=>document.querySelectorAll("#sku-rows [data-sku-cover]").length===0')
                self.assertEqual(errors,[])
            finally:browser.close()


if __name__=='__main__':unittest.main()
