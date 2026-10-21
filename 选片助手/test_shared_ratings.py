"""归档人工评级、身份共享与真实浏览器回归。"""
import hashlib
import json
import shutil
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright

import server
from asset_archive import AssetArchive
import test_archive_pipeline as pipeline_tests
from test_archive_pipeline import ROOT
import test_raw_reconcile as raw_tests
from test_raw_reconcile import SAMPLE


class SharedRatingTests(unittest.TestCase):
    setUp = pipeline_tests.PipelineTests.setUp
    tearDown = pipeline_tests.PipelineTests.tearDown
    add = pipeline_tests.PipelineTests.add
    rate = pipeline_tests.PipelineTests.rate
    drain = pipeline_tests.PipelineTests.drain
    identify = pipeline_tests.PipelineTests.identify
    recognize = pipeline_tests.PipelineTests.recognize

    def copies(self):
        destination=self.final/self.live.name;destination.mkdir()
        for path in self.paths:shutil.copy2(path,destination/path.name)
        self.catalog.add_root(str(self.final));self.pipeline.tick();self.now+=1;self.pipeline.tick();self.drain()
        return destination

    def test_live_to_archive_manual_change_clear_and_restart(self):
        self.identify()
        self.archive.ratings.set([self.paths[0]],4)
        self.link.request(self.sid);self.drain()
        destination=self.copies();target=destination/self.paths[0].name
        rows=self.archive.ratings.decorate(self.catalog.items()['rows'])
        self.assertEqual(next(r['rating'] for r in rows if r['name']==target.name),4)
        self.assertEqual(self.archive.writer.read(target)['XMP-xmp:Rating'],4)
        self.archive.ratings.set([target],5)
        self.assertEqual(self.archive.ratings.get([self.paths[0]]),5)
        self.link.request(self.sid);self.drain()
        self.assertEqual(self.archive.writer.read(target)['XMP-xmp:Rating'],5)
        self.archive.ratings.set([target],0)
        self.link.request(self.sid);self.drain()
        self.assertEqual(self.archive.writer.read(target)['XMP-xmp:Rating'],0)
        self.catalog.scan(self.catalog.state()['roots'][0]['id'])
        reopened=AssetArchive(self.store,autostart=False)
        try:self.assertEqual(reopened.ratings.get([self.paths[0]]),0)
        finally:reopened.close()

    def test_pair_sharing_without_same_name_cross_directory_and_read_only(self):
        self.add('IMG_0001.JPG')
        raw=self.live/'IMG_0001.CR3';raw.write_bytes(b'pair-fixture')
        other=self.final/'IMG_0001.JPG';Image.new('RGB',(50,80),'red').save(other)
        hashes={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.paths[0],raw,other)}
        self.archive.ratings.set([raw],3)
        self.assertEqual(self.archive.ratings.get([self.paths[0]]),3)
        self.assertIsNone(self.archive.ratings.get([other]))
        self.archive.ratings.set([self.paths[0]],0)
        self.assertEqual(self.archive.ratings.get([raw]),0)
        for invalid in (-2,6,1.2,True,'3'):
            with self.assertRaises(ValueError):self.archive.ratings.set([raw],invalid)
        self.assertEqual(hashes,{p:hashlib.sha256(p.read_bytes()).hexdigest() for p in hashes})

    def test_settings_save_from_stale_window_does_not_restore_old_rating(self):
        cid=self.add('IMG_0001.JPG');record=self.store.records(self.sid)[0]
        before=self.store.session(self.sid);self.archive.ratings.set([self.paths[0]],4)
        stale={'overrides':{cid:1},'view':'list'}
        self.archive.ratings.saved(self.sid,before,stale,{cid:1})
        self.assertEqual(self.archive.ratings.get([self.paths[0]]),4,self.link.state())
        self.archive.ratings.saved(self.sid,before,{'overrides':{cid:0}},{cid:4})
        self.assertEqual(self.archive.ratings.get(self.archive.ratings.paths(self.sid,record)),0)

    def test_archive_manual_rating_before_link_is_kept_after_matching(self):
        self.identify();destination=self.final/self.live.name;destination.mkdir()
        for path in self.paths:shutil.copy2(path,destination/path.name)
        target=destination/self.paths[0].name
        self.archive.ratings.set([target],5)
        self.catalog.add_root(str(self.final));self.pipeline.tick();self.now+=1;self.pipeline.tick();self.drain()
        self.assertEqual(self.archive.ratings.get([self.paths[0]]),5)
        self.assertEqual(self.archive.writer.read(target)['XMP-xmp:Rating'],5)

    def test_archive_rating_links_unrated_source_without_sku(self):
        self.add('IMG_0001.JPG');destination=self.final/self.live.name;destination.mkdir()
        target=destination/self.paths[0].name;shutil.copy2(self.paths[0],target)
        self.archive.ratings.set([target],4)
        self.catalog.add_root(str(self.final));self.pipeline.tick();self.now+=1;self.pipeline.tick();self.drain()
        self.assertEqual(self.archive.ratings.get([self.paths[0]]),4)
        self.assertEqual(self.archive.writer.read(target)['XMP-xmp:Rating'],4)

    def test_browser_archive_edit_badges_keyboard_and_live_reuse(self):
        cid=self.add('IMG_0001.JPG');self.add('IMG_0002.JPG')
        (self.live/'IMG_0001.CR3').write_bytes(b'paired-thumbnail-fixture')
        self.catalog.add_root(str(self.live))
        self.archive.ratings.set([self.paths[0]],3)
        values={'STORE':self.store,'DATA':self.store.data,'SKU':self.sku,'ARCHIVE':self.archive,'RAWLINK':self.link,'CATALOG':self.catalog,'MODELS':self.providers,'WATCH':None,'MOUSE':None,'ANALYSIS':None,'FILESEARCH':None,'UPDATES':None}
        http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        patches=[patch.object(server,key,value,create=key=='UPDATES') for key,value in values.items()]
        for item in patches:item.start()
        threading.Thread(target=http.serve_forever,daemon=True).start()
        out=ROOT/'build/shared-ratings';out.mkdir(parents=True,exist_ok=True)
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                try:
                    page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];requests=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    page.on('request',lambda request:requests.append(request.post_data_json) if request.url.endswith('/api/session') else None)
                    page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                    self.assertEqual(page.locator('#selection-tab').inner_text(),'现场拍摄')
                    self.assertFalse(page.locator('#open-folder').is_visible());self.assertFalse(page.locator('#threshold').is_visible())
                    page.locator('#asset-archive-open').click()
                    card=page.locator('#catalog-grid .catalog-card').filter(has=page.locator('img[alt="IMG_0001.JPG"]'))
                    card.wait_for();self.assertEqual(card.locator('.photo-rating-badge').inner_text(),'★★★');card.click()
                    page.locator('[data-catalog-rating="5"]').click()
                    page.wait_for_function('()=>document.getElementById("catalog-rating-status").textContent==="已保存 5 星"')
                    self.assertEqual(card.locator('.photo-rating-badge').inner_text(),'★★★★★')
                    self.assertEqual(page.locator('#catalog-selected-rating').inner_text(),'★★★★★')
                    for theme in ('dark','light','pure-white','hardcore'):
                        page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                        for width,height in ((1680,1050),(1000,680)):
                            page.set_viewport_size({'width':width,'height':height})
                            box=card.locator('.photo-rating-badge').bounding_box();thumb=card.locator('.catalog-thumb').bounding_box()
                            self.assertLessEqual(box['x']+box['width'],thumb['x']+thumb['width']);self.assertLessEqual(box['y']+box['height'],thumb['y']+thumb['height'])
                            badge=card.locator('.photo-format-badge').bounding_box();self.assertLessEqual(badge['y']+badge['height'],box['y'])
                    page.set_viewport_size({'width':1680,'height':1050});page.screenshot(path=str(out/'archive-five-stars.png'))
                    page.locator('#selection-tab').click()
                    self.assertEqual(page.evaluate('(cid)=>Framekeep.photoItems().find(i=>i.photo.id===cid).rating',cid),5)
                    page.locator('#asset-archive-open').click();card.click();card.focus();page.keyboard.press('0')
                    page.wait_for_function('()=>document.getElementById("catalog-rating-status").textContent==="已清除星级"')
                    self.assertEqual(card.locator('.photo-rating-badge').count(),0)
                    page.reload();page.locator('#asset-archive-open').click();card.wait_for();self.assertEqual(card.locator('.photo-rating-badge').count(),0)
                    self.assertEqual(self.archive.ratings.get([self.paths[0]]),0)
                    page.locator('#selection-tab').click();page.evaluate('(cid)=>Framekeep.browsePhoto(cid)',cid);page.keyboard.press('3');page.evaluate('()=>FramekeepIO.flush()')
                    self.assertEqual(page.evaluate('(cid)=>Framekeep.photoItems().find(i=>i.photo.id===cid).rating',cid),3)
                    self.assertEqual(self.archive.ratings.get([self.paths[0]]),3,requests)
                    exported=page.evaluate('()=>FramekeepIO.api("/api/export",{selected_only:false})')
                    self.assertEqual(next(r['rating'] for r in exported['captures'] if r['id']==cid),3)
                    selected=page.evaluate('()=>FramekeepIO.api("/api/export",{selected_only:true})');self.assertEqual(selected['applyPolicy']['mode'],'selected-only')
                    page.locator('#asset-archive-open').click();card.wait_for();self.assertEqual(card.locator('.photo-rating-badge').inner_text(),'★★★')
                    self.assertFalse(errors,errors)
                finally:browser.close()
        finally:
            http.shutdown();http.server_close()
            for item in reversed(patches):item.stop()


@unittest.skipUnless(SAMPLE.is_file(),'公开 RAW 测试样片缺失')
class RawCaptureRatingTests(unittest.TestCase):
    setUp=raw_tests.RawWorkflowTests.setUp
    tearDown=raw_tests.RawWorkflowTests.tearDown
    scan=raw_tests.RawWorkflowTests.scan
    finish=raw_tests.RawWorkflowTests.finish

    def test_paired_and_raw_only_live_capture_share_ratings(self):
        shutil.copy2(self.target,self.jpg/self.target.name)
        with self.store.connect() as c:c.execute('INSERT INTO settings VALUES (?,?)',('shooting_session:'+self.sid,str(self.jpg)))
        import media_engine as media
        self.store.append_live(self.sid,[media.capture_record(self.jpg,self.jpg/self.target.name,self.jpg/'IMG_0346.JPG',self.meta)]);self.link.process_session(self.sid)
        sources=self.link.sources();self.assertTrue(any(json.loads(s['record']).get('raw') for s in sources))
        self.assertEqual(self.link.state(self.sid)['pairs'],[])
        target_sid=self.scan(self.raw);self.assertEqual(self.finish(target_sid)['status'],'applied')
        self.assertEqual(self.archive.writer.read(self.target)['XMP-xmp:Rating'],4)
        (self.jpg/'IMG_0346.JPG').unlink();self.store.append_live(self.sid,[media.capture_record(self.jpg,self.jpg/self.target.name,None,self.meta)]);self.link.process_session(self.sid)
        self.archive.ratings.set([self.target],5);self.link.process_session(self.sid)
        self.assertEqual(self.finish(target_sid)['status'],'applied')
        self.assertEqual(self.link.state(target_sid)['pairs'][0]['source_filename'],'IMG_0346.CR3')
        self.assertEqual(self.archive.writer.read(self.target)['XMP-xmp:Rating'],5)
        self.assertEqual(self.archive.ratings.get([self.jpg/self.target.name]),5)


if __name__=='__main__':unittest.main()
