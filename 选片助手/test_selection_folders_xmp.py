"""隔离目录树、XMP 互通、连续更新、离线及重启验收。"""
from pathlib import Path
import hashlib
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from asset_archive import AssetArchive
from catalog_store import Store
from selection_folders import tree,files
from rating_xmp import sidecar_rating
import media_engine as media

ROOT=Path(__file__).resolve().parents[1]


class SelectionFoldersXmpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.photos=self.base/'拍摄';(self.photos/'日期'/'子目录').mkdir(parents=True)
        self.store=Store(self.base/'data',ROOT);self.sid=self.store.ensure_live_session(self.photos)
        self.paths=[];self.ids=[]
        for i,name in enumerate(('根.JPG','日期/一.JPG','日期/子目录/二.JPG')):
            path=self.photos/name;Image.new('RGB',(240,160),(30+i*60,100,140)).save(path)
            record=media.capture_record(self.photos,path,None,{})
            self.store.append_live(self.sid,[record]);self.paths.append(path);self.ids.append(record['id'])
        self.store.activate(self.sid)
        self.store.save(self.sid,0,{'view':'grid','overrides':dict.fromkeys(self.ids,0)})
        self.archive=AssetArchive(self.store,autostart=False);self.archive.ratings.seed_session(self.sid)

    def tearDown(self):self.archive.close();self.temp.cleanup()

    def drain(self):
        for _ in range(20):
            if not self.archive.ratings.sync.process_one():break
        self.assertEqual(self.archive.ratings.sync.state()['failed'],0,self.archive.ratings.sync.state())

    def test_tree_counts_direct_files_and_removed_sessions(self):
        root=tree(self.store)['roots'][0]
        self.assertEqual(root['folders'],{'':3,'日期':2,'日期/子目录':1})
        self.assertEqual([r['id'] for r in files(self.store,self.sid,'日期')['rows']],[self.ids[1]])
        with self.assertRaises(ValueError):files(self.store,self.sid,'../outside')
        self.store.remove_session(self.sid);self.assertFalse(tree(self.store)['roots'])
        with self.assertRaises(ValueError):files(self.store,self.sid)

    def test_real_exiftool_sidecar_embedded_update_clear_and_preserve(self):
        path=self.paths[0];sidecar=path.with_suffix('.xmp')
        self.archive.writer.run('-XMP-dc:Description=保留的说明',sidecar)
        extra=Path(str(path)+'.xmp');extra.write_bytes(sidecar.read_bytes())
        with Image.open(path) as im:pixels=im.tobytes()
        for rating in (1,5,2,0):
            self.archive.ratings.set([path],rating);self.drain()
            self.assertEqual(self.archive.ratings.get([path]),rating)
            for target in (path,sidecar,extra):self.assertEqual(self.archive.writer.read(target)['XMP-xmp:Rating'],rating)
            self.assertEqual(self.archive.writer.read(sidecar,True)['XMP-dc:Description'],'保留的说明')
            self.assertEqual(sidecar_rating(path),rating)
            self.assertEqual(media.read_metadata([path],media.find_exiftool(ROOT))[0]['Rating'],rating)
        with Image.open(path) as im:self.assertEqual(im.tobytes(),pixels)

    def test_latest_revision_restart_offline_retry_and_raw_untouched(self):
        raw=self.photos/'原图.CR3';raw.write_bytes(b'RAW sensor fixture')
        digest=hashlib.sha256(raw.read_bytes()).hexdigest()
        self.archive.ratings.set([raw],1);self.archive.ratings.set([raw],4);self.archive.ratings.set([raw],0)
        self.archive.close();self.archive=AssetArchive(self.store,autostart=False);self.drain()
        self.assertEqual(sidecar_rating(raw),0);self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(),digest)
        missing=self.photos/'离线.JPG';self.archive.ratings.set([missing],3)
        self.archive.ratings.sync.process_one();self.assertEqual(self.archive.ratings.sync.state()['failed'],1)
        self.assertEqual(self.archive.ratings.get([missing]),3)
        Image.new('RGB',(32,32),'red').save(missing)
        self.archive.ratings.sync.retry();self.drain();self.assertEqual(sidecar_rating(missing),3)

    def test_invalid_sidecar_not_replaced_and_existing_zero_is_read(self):
        path=self.paths[0];sidecar=path.with_suffix('.xmp');sidecar.write_bytes(b'not valid XMP')
        self.archive.ratings.set([path],2)
        for _ in range(8):self.archive.ratings.sync.process_one()
        self.assertEqual(self.archive.ratings.sync.state()['failed'],1)
        self.assertEqual(sidecar.read_bytes(),b'not valid XMP')
        self.assertIsNone(sidecar_rating(path))
        sidecar.write_text('<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:xmp="http://ns.adobe.com/xap/1.0/"><rdf:RDF><rdf:Description xmp:Rating="0" /></rdf:RDF></x:xmpmeta>',encoding='utf8')
        self.assertEqual(sidecar_rating(path),0)
        record=media.capture_record(self.photos,path,None,{'Rating':5})
        self.assertEqual(record['existingRating'],0);self.assertEqual(record['liveRating'],0)

    def test_background_write_updated_during_flight_keeps_latest_rating(self):
        self.archive.close();self.archive=AssetArchive(self.store,autostart=True)
        started=threading.Event();resume=threading.Event();writer=self.archive.writer.write_rating
        def delayed(path,value):
            if value==1:started.set();resume.wait(5)
            return writer(path,value)
        with patch.object(self.archive.writer,'write_rating',side_effect=delayed):
            self.archive.ratings.set([self.paths[0]],1)
            self.assertTrue(started.wait(5))
            self.archive.ratings.set([self.paths[0]],5);self.archive.ratings.set([self.paths[0]],0)
            resume.set();deadline=time.monotonic()+15
            while time.monotonic()<deadline and self.archive.ratings.sync.state()['pending']:time.sleep(.05)
            self.assertEqual(self.archive.ratings.sync.state()['pending'],0)
        self.assertEqual(sidecar_rating(self.paths[0]),0)
        self.assertEqual(self.archive.writer.read(self.paths[0])['XMP-xmp:Rating'],0)

    def test_browser_tree_filter_files_fold_and_themes(self):
        import server
        values={'STORE':self.store,'DATA':self.store.data,'ARCHIVE':self.archive,'WATCH':None,'SKU':None,'MOUSE':None,'CATALOG':None,'MODELS':None,'ANALYSIS':None,'RAWLINK':None,'FILESEARCH':None}
        patches=[patch.object(server,k,v) for k,v in values.items()]
        for p in patches:p.start()
        http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        threading.Thread(target=http.serve_forever,daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                try:
                    page=browser.new_page(viewport={'width':1680,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                    toggle=page.locator('#selection-folders-toggle');self.assertEqual(toggle.get_attribute('aria-expanded'),'false')
                    toggle.click();page.wait_for_function('()=>document.querySelectorAll("#selection-folder-tree details").length===3')
                    self.assertEqual(page.locator('#selection-folder-tree .catalog-drive-folders > details').count(),1)
                    # 目录树只展示文件夹；点击目录由右侧缩略图栏显示照片。
                    page.evaluate('()=>document.querySelectorAll("#selection-folder-tree details").forEach(d=>d.open=true)')
                    page.get_by_role('treeitem',name='日期 · 2',exact=True).click()
                    page.wait_for_function('()=>Framekeep.getSnapshot().count===2')
                    page.wait_for_function('()=>document.querySelectorAll("#watch-thumbnails [data-watch-photo]").length===2')
                    self.assertEqual(page.locator('#selection-folder-tree .selection-tree-file').count(),0)
                    page.evaluate('()=>document.querySelectorAll("#selection-folder-tree details").forEach(d=>d.open=true)')
                    page.get_by_role('treeitem',name='子目录 · 1',exact=True).click()
                    page.wait_for_function('()=>document.querySelectorAll("#watch-thumbnails [data-watch-photo]").length===1')
                    self.assertNotIn('二.JPG',page.locator('#selection-folder-tree').inner_text())
                    page.wait_for_function('(id)=>Framekeep.remoteTarget().photoId===id',arg=self.ids[2])
                    self.assertEqual(page.evaluate('()=>Framekeep.getSnapshot().count'),1)
                    out=ROOT/'build/selection-folders-xmp';out.mkdir(parents=True,exist_ok=True)
                    for theme in ('dark','light','pure-white','hardcore'):
                        page.evaluate('(t)=>FramekeepTheme.set(t)',theme)
                        for width in (1680,1024,600,375):
                            page.set_viewport_size({'width':width,'height':1000})
                            self.assertFalse(page.evaluate('()=>document.documentElement.scrollWidth>innerWidth'),(theme,width))
                        page.set_viewport_size({'width':1680,'height':1000});page.screenshot(path=str(out/(theme+'.png')))
                    toggle.click();self.assertFalse(page.locator('#selection-folders').is_visible())
                    page.reload(wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading');self.assertEqual(toggle.get_attribute('aria-expanded'),'false')
                    self.assertEqual(page.evaluate('()=>Framekeep.getSnapshot().count'),1)
                    self.assertFalse(errors,errors)
                finally:browser.close()
        finally:
            http.shutdown();http.server_close()
            for p in reversed(patches):p.stop()


if __name__=='__main__':unittest.main()
