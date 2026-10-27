from http.server import ThreadingHTTPServer
from pathlib import Path
import hashlib
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import Store,folder_name
from folder_watch import FolderWatch
from model_providers import Providers
import media_engine as media

ROOT=Path(__file__).resolve().parents[1]


class FolderContextTests(unittest.TestCase):
    def test_root_drive_and_network_share_names_are_never_empty(self):
        for value,expected in [('Z:\\','Z:'),(r'\\192.168.1.25\摄影共享',r'\\192.168.1.25\摄影共享'),(r'\\NAS\照片\2026-10-22','2026-10-22')]:
            self.assertEqual(folder_name(value),expected)
        with self.store.connect() as c:c.execute("UPDATE sessions SET root=?,name='' WHERE id=?",(r'\\192.168.1.25\摄影共享',self.ids[0]))
        self.assertEqual(self.store.session(self.ids[0])['name'],r'\\192.168.1.25\摄影共享')
        self.assertEqual(next(row['name'] for row in self.store.sessions() if row['id']==self.ids[0]),r'\\192.168.1.25\摄影共享')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.store=Store(self.base/'data',ROOT)
        self.folders=[];self.ids=[]
        for name in ('a-当前','b-监控','c-备用'):
            folder=self.base/name;folder.mkdir();image=folder/'001.JPG';Image.new('RGB',(600,400),(90,130,100)).save(image)
            (folder/'001.xmp').write_text('原始侧车内容',encoding='utf8')
            sid=self.store.ensure_live_session(folder);self.store.append_live(sid,[media.capture_record(folder,image,None,{})])
            cid=self.store.records(sid)[0]['id'];self.store.save(sid,0,{'overrides':{cid:4}})
            self.folders.append(folder);self.ids.append(sid)
        self.store.activate(self.ids[0]);self.before=self.files()

    def tearDown(self):self.temp.cleanup()

    def files(self):return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for f in self.folders for p in f.rglob('*') if p.is_file()}

    def test_detach_restart_fallback_and_readd_preserve_photos_and_ratings(self):
        a,b,c=self.ids
        self.assertEqual(self.store.remove_session(b),a)
        self.assertEqual([s['id'] for s in Store(self.store.data,ROOT).sessions()],[a,c])
        with self.assertRaises(ValueError):self.store.activate(b)
        self.assertEqual(self.store.remove_session(a),c)
        self.assertIsNone(self.store.remove_session(c));self.assertIsNone(self.store.session())
        self.assertEqual(Store(self.store.data,ROOT).sessions(),[])
        with patch('catalog_store.threading.Thread'),patch('media_engine.find_exiftool',return_value=Path('unused')):
            job=self.store.scan(self.folders[0]);self.store._scan(job,self.folders[0],True)
        self.assertEqual(self.store.jobs[job]['status'],'completed',self.store.jobs[job])
        self.assertEqual(self.store.manifest()['captures'][0]['rating'],4)
        self.assertEqual(self.files(),self.before)

    def test_remove_during_scan_cannot_restore_folder_or_replace_old_index(self):
        sid=self.ids[0];original=self.store.records(sid)
        Image.new('RGB',(600,400)).save(self.folders[0]/'002.JPG');before=self.files()
        def metadata(paths,exe,**kwargs):
            self.store.remove_session(sid)
            return [{} for _ in paths]
        with patch('catalog_store.threading.Thread'),patch('media_engine.find_exiftool',return_value=Path('unused')),patch('media_engine.read_metadata',side_effect=metadata):
            job=self.store.scan(self.folders[0]);self.store._scan(job,self.folders[0],True)
        self.assertEqual(self.store.jobs[job]['status'],'cancelled')
        self.assertNotIn(sid,[s['id'] for s in self.store.sessions()])
        self.assertEqual(self.store.records(sid),original)
        self.assertEqual(self.store.folder_state()['scans'],[])
        self.assertEqual(self.files(),before)

    def test_browser_menu_open_remove_current_last_restart_and_readd(self):
        import server
        watch=FolderWatch(self.store,autostart=False);watch.start(str(self.folders[1]))
        seen=[];patches=[patch.object(server,k,v) for k,v in [('STORE',self.store),('DATA',self.store.data),('WATCH',watch),('MODELS',Providers(self.store.data)),('MOUSE',None),('SKU',None),('ARCHIVE',None),('ANALYSIS',None),('RAWLINK',None),('FILESEARCH',None)]]
        patches.append(patch('file_reveal.os.startfile',side_effect=seen.append))
        for p in patches:p.start()
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                try:
                    page=browser.new_page(viewport={'width':1440,'height':1050},reduced_motion='reduce');errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    url=f'http://127.0.0.1:{http.server_port}';page.goto(url,wait_until='domcontentloaded');page.locator('#folder-list .folder-row').first.wait_for()
                    self.assertEqual(page.request.post(url+'/api/folders/remove',data={'session_id':self.ids[0]}).status,403)
                    def row(sid):return page.locator('#folder-list .folder-row[data-session="'+sid+'"]')
                    b=row(self.ids[1]);b.click(button='right');self.assertTrue(page.evaluate('()=>Framekeep.remoteTarget().paused'))
                    page.keyboard.press('ArrowDown');self.assertEqual(page.locator(':focus').get_attribute('data-folder-action'),'remove')
                    page.keyboard.press('Escape');self.assertFalse(page.evaluate('()=>Framekeep.remoteTarget().paused'))
                    b.focus();page.keyboard.press('Shift+F10');page.locator('[data-folder-action="open"]').click()
                    page.wait_for_timeout(100);self.assertEqual(seen,[str(self.folders[1])]);self.assertEqual(self.store.active_id(),self.ids[0])
                    b.locator('xpath=..').locator('.folder-parent').click(button='right')
                    out=ROOT/'build/folder-context';out.mkdir(exist_ok=True);page.screenshot(path=str(out/'folder-menu.png'))
                    page.locator('[data-folder-action="remove"]').click();row(self.ids[1]).wait_for(state='detached')
                    self.assertFalse(watch.state()['enabled']);self.assertEqual(page.evaluate('()=>FRAMEKEEP_SESSION.id'),self.ids[0])
                    # 保存队列中尚未提交的最新评级应在移除当前目录前落库。
                    page.locator('#stars [data-rating="5"]').click()
                    for sid in (self.ids[0],self.ids[2]):
                        row(sid).click(button='right');page.locator('[data-folder-action="remove"]').click()
                        page.wait_for_function('(id)=>FRAMEKEEP_SESSION.id!==id',arg=sid)
                    self.assertIsNone(page.evaluate('()=>FRAMEKEEP_SESSION.id'));self.assertEqual(page.locator('#photo-grid [data-photo]').count(),0)
                    page.reload(wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading');self.assertEqual(page.locator('#folder-list .folder-row').count(),0)
                    self.assertTrue(page.locator('#catalog-empty').is_visible());self.assertTrue(page.locator('#canon-tether-open').is_enabled())
                    page.locator('#folder-watch').click();page.locator('#watch-path').fill(str(self.folders[0]));page.locator('#watch-start').click()
                    row(self.ids[0]).wait_for(state='attached');page.wait_for_function('(id)=>FRAMEKEEP_SESSION.id===id',arg=self.ids[0])
                    self.assertEqual(page.locator('#stars .on').count(),5)
                    self.assertEqual(self.files(),self.before);self.assertFalse(errors,errors)
                    page.screenshot(path=str(out/'readded-folder.png'))
                finally:browser.close()
        finally:
            watch.close();http.shutdown();http.server_close()
            for p in reversed(patches):p.stop()


if __name__=='__main__':unittest.main()
