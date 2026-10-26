"""搜索目录全量匹配、文件夹分组和无变化时的原片访问控制。"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from catalog_store import Store
from model_providers import Providers
from reference_library import ReferenceLibrary
import media_engine as media
import test_archive_pipeline as fixtures

ROOT=Path(__file__).resolve().parents[1]


class SearchFolderTests(unittest.TestCase):
    def test_folder_matches_cover_all_pages_and_do_not_access_originals(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir()
            for group,names in [('甲',[f'A001-{i:03}.jpg' for i in range(125)]),('乙/深层',['A001-other.jpg']),('无匹配',['B002.jpg'])]:
                target=photos/group;target.mkdir(parents=True)
                for index,name in enumerate(names):
                    path=target/name;Image.new('RGB',(8,8),'red').save(path);os.utime(path,(1000+index if group=='甲' else 1,)*2)
            catalog=AssetCatalog(base/'data');catalog.add_root(str(photos))
            with patch.object(Path,'stat',side_effect=AssertionError('查询不应访问源文件')):
                result=catalog.items(text='A001');tree=catalog.tree(text='A001',media='image')
                self.assertEqual(result['total'],126);self.assertEqual(len(result['rows']),120)
                self.assertEqual({row['folder'] for row in result['rows']},{'甲'})
                self.assertEqual({row['folder']:row['count'] for row in tree['roots'][0]['folders']},{'甲':125,'乙/深层':1})
                self.assertFalse(catalog.tree(text='A001',media='video')['roots'])
                self.assertFalse(catalog.tree(text='no-match')['roots'])


class IdlePipelineTests(unittest.TestCase):
    setUp=fixtures.PipelineTests.setUp
    tearDown=fixtures.PipelineTests.tearDown
    recognize=fixtures.PipelineTests.recognize

    def signal(self):
        return SimpleNamespace(handle=1,changed=lambda:False,close=lambda:None)

    def test_no_changes_for_one_hour_do_not_rescan_or_read_originals(self):
        root=self.catalog.add_root(str(self.final))['root_id']
        with patch('archive_pipeline.DirectorySignal',return_value=self.signal()):self.pipeline.tick()
        with patch.object(media,'discover',side_effect=AssertionError('空闲不应遍历原片')),patch('archive_pipeline.files_ready',side_effect=AssertionError('空闲不应解码')):
            for seconds in (15,30,60,300,900,1800,3600):self.now=100+seconds;self.pipeline.tick()
        self.assertEqual(self.pipeline.last_scan[root],100)

    def test_restart_unchanged_jpeg_skips_decode_and_exif(self):
        photo=self.final/'known.JPG';Image.new('RGB',(50,40),'green').save(photo)
        sid=self.store.ensure_live_session(self.final);self.store.append_live(sid,[media.capture_record(self.final,photo,None,{})])
        self.catalog.add_root(str(self.final))
        with patch('archive_pipeline.DirectorySignal',return_value=self.signal()),patch('archive_pipeline.files_ready',side_effect=AssertionError('已知照片不应重复解码')),patch.object(media,'read_metadata',side_effect=AssertionError('已知照片不应重读 EXIF')):
            self.pipeline.tick()
        self.assertFalse(self.pipeline.pending)

    def test_notifications_manual_refresh_and_fallback_still_scan(self):
        root=self.catalog.add_root(str(self.final))['root_id'];signal=self.signal()
        with patch('archive_pipeline.DirectorySignal',return_value=signal):self.pipeline.tick()
        with patch.object(self.pipeline,'_scan',wraps=self.pipeline._scan) as scan:
            signal.changed=lambda:True;self.now+=2;self.pipeline.tick();self.assertEqual(scan.call_count,1)
            signal.changed=lambda:False;self.catalog.scan(root);self.now+=2;self.pipeline.tick();self.assertEqual(scan.call_count,2)
            self.now+=21601;self.pipeline.tick();self.assertEqual(scan.call_count,3)

    def test_unavailable_notifications_use_bounded_polling(self):
        self.catalog.add_root(str(self.final));signal=self.signal();signal.handle=None
        with patch('archive_pipeline.DirectorySignal',return_value=signal),patch.object(self.pipeline,'_scan',wraps=self.pipeline._scan) as scan:
            self.pipeline.tick()
            for _ in range(9):self.now+=30;self.pipeline.tick()
            self.assertEqual(scan.call_count,1)
            self.now+=31;self.pipeline.tick();self.assertEqual(scan.call_count,2)

    def test_sidecar_notification_updates_metadata_without_decoding_unchanged_jpeg(self):
        photo=self.final/'known.JPG';Image.new('RGB',(50,40),'green').save(photo)
        sid=self.store.ensure_live_session(self.final);self.store.append_live(sid,[media.capture_record(self.final,photo,None,{})])
        self.catalog.add_root(str(self.final));signal=self.signal()
        with patch('archive_pipeline.DirectorySignal',return_value=signal):self.pipeline.tick()
        photo.with_suffix('.xmp').write_text('<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:xmp="http://ns.adobe.com/xap/1.0/"><rdf:RDF><rdf:Description xmp:Rating="4" /></rdf:RDF></x:xmpmeta>',encoding='utf8')
        with patch('archive_pipeline.files_ready',side_effect=AssertionError('仅侧车变化不应重解图片')):
            signal.changed=lambda:True;self.now+=2;self.pipeline.tick()
            signal.changed=lambda:False;self.now+=1;self.pipeline.tick()
        self.assertEqual(self.store.records(sid)[0]['existingRating'],4)

    def test_removing_and_readding_root_reconnects_without_old_retry_delay(self):
        root=self.catalog.add_root(str(self.final))['root_id']
        with patch('archive_pipeline.DirectorySignal',side_effect=lambda *_:self.signal()) as watches:
            self.pipeline.tick();self.catalog.remove(root);self.pipeline.tick()
            self.assertNotIn(root,self.pipeline.watch_attempts)
            self.catalog.add_root(str(self.final));self.pipeline.tick()
            self.assertEqual(watches.call_count,2)


class SearchFolderBrowserTests(unittest.TestCase):
    def test_global_search_tree_groups_navigation_clear_and_stale_responses(self):
        import server
        with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
            base=Path(folder);photos=base/'归档'
            for group,name,color in [('甲','A001-one.JPG','red'),('甲','A001-two.JPG','orange'),('乙/子目录','A001-three.JPG','blue'),('无匹配','B002.JPG','green')]:
                target=photos/group;target.mkdir(parents=True,exist_ok=True);Image.new('RGB',(80,60),color).save(target/name)
            store=Store(base/'data',ROOT);catalog=AssetCatalog(store.data);catalog.add_root(str(photos))
            archive=AssetArchive(store,autostart=False);library=ReferenceLibrary(store.data)
            for name,value in dict(STORE=store,DATA=store.data,CATALOG=catalog,ARCHIVE=archive,REFERENCES=library,MODELS=Providers(store.data),
                SKU=None,MOUSE=None,WATCH=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,STORAGE=None,COMPONENTS=None,DRIVER=None).items():stack.enter_context(patch.object(server,name,value))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1600,'height':1000},reduced_motion='reduce');errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                        page.wait_for_selector('#catalog-tree [data-folder="甲"]')
                        entered=threading.Event();release=threading.Event();original_items=catalog.items
                        def slow_items(*args,**kwargs):
                            if len(args)>1 and args[1]=='甲' and not release.is_set():entered.set();release.wait(10)
                            return original_items(*args,**kwargs)
                        with patch.object(catalog,'items',side_effect=slow_items),patch.object(catalog,'tree',wraps=catalog.tree) as tree_reads:
                            try:
                                page.locator('#catalog-tree [data-folder="甲"]').click();self.assertTrue(entered.wait(3))
                                self.assertEqual(page.locator('#catalog-tree [data-folder="甲"]').get_attribute('aria-selected'),'true')
                                self.assertEqual(page.locator('#catalog-grid').get_attribute('aria-busy'),'true')
                                self.assertEqual(tree_reads.call_count,0,'点击目录不应等待整棵树重新查询')
                                page.locator('#catalog-tree [data-folder="乙/子目录"]').click()
                                page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===1&&!document.getElementById('catalog-grid').hasAttribute('aria-busy')")
                                release.set();page.wait_for_timeout(150)
                                self.assertEqual(page.locator('#catalog-tree [data-folder="乙/子目录"]').get_attribute('aria-selected'),'true')
                                self.assertEqual(page.locator('#catalog-grid .catalog-card').count(),1)
                            finally:release.set()
                        page.locator('#catalog-tree [data-folder="甲"]').click()
                        page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===2")
                        tree_node=page.locator('#catalog-tree [data-folder="甲"]').element_handle()
                        page.wait_for_timeout(2200)
                        self.assertTrue(tree_node.evaluate('(node)=>node.isConnected'),'统计未变化时应保留目录节点和焦点')
                        with patch.object(catalog,'items',side_effect=ValueError('模拟读取失败')):
                            page.locator('#catalog-tree [data-folder="乙/子目录"]').click()
                            page.wait_for_function("()=>document.getElementById('catalog-summary').textContent.includes('目录读取失败')")
                        page.locator('#catalog-tree [data-folder="乙/子目录"]').click()
                        page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===1")
                        page.locator('#catalog-tree [data-folder="甲"]').click()
                        page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===2")
                        search=page.locator('#catalog-search');search.fill('A001')
                        page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===3&&document.querySelectorAll('#catalog-tree .catalog-search-result').length===2")
                        self.assertNotIn('无匹配',page.locator('#catalog-tree').inner_text())
                        self.assertEqual(page.locator('#catalog-tree details').count(),0,'搜索结果应直接列出命中文件夹，不展开目录树')
                        self.assertEqual(page.locator('.catalog-folder-group>header strong').all_text_contents(),['子目录','甲'])
                        self.assertTrue(page.locator('#catalog-tree [data-folder="乙/子目录"]').is_visible())
                        page.locator('#catalog-tree [data-folder="乙/子目录"]').click()
                        page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===1")
                        self.assertEqual(page.locator('#catalog-tree .catalog-search-result').count(),2)
                        page.locator('#catalog-all-search-folders').click();page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===3")
                        cards=page.locator('#catalog-grid .catalog-card');names=cards.locator('img').evaluate_all('(nodes)=>nodes.map(node=>node.alt)')
                        cards.first.dblclick();page.wait_for_selector('#asset-viewer-title')
                        self.assertEqual(page.locator('#asset-viewer-title').inner_text(),names[0]);page.locator('#asset-viewer-next').click()
                        self.assertEqual(page.locator('#asset-viewer-title').inner_text(),names[1]);page.locator('#asset-viewer-close').click()
                        out=ROOT/'build/archive-search-nas-idle';out.mkdir(parents=True,exist_ok=True)
                        for theme in ('dark','light','pure-white','hardcore'):
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme);page.screenshot(path=str(out/f'search-{theme}.png'))
                            page.set_viewport_size({'width':375,'height':950});self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'));page.set_viewport_size({'width':1600,'height':1000})
                        toggle=page.locator('.catalog-folder-toggle').first;control_id=toggle.get_attribute('aria-controls')
                        toggle.click();self.assertEqual(toggle.get_attribute('aria-expanded'),'false')
                        self.assertFalse(page.locator('#'+control_id).is_visible())
                        page.wait_for_timeout(2200);self.assertEqual(toggle.get_attribute('aria-expanded'),'false')
                        visible=page.locator('#catalog-grid .catalog-card:visible');self.assertEqual(visible.count(),2)
                        visible.first.dblclick();self.assertEqual(page.locator('#asset-viewer-position').inner_text(),'1 / 2');page.locator('#asset-viewer-close').click()
                        toggle.press('Enter');self.assertEqual(toggle.get_attribute('aria-expanded'),'true')
                        toggle.press('Space');self.assertEqual(toggle.get_attribute('aria-expanded'),'false')
                        page.screenshot(path=str(out/'search-collapsed.png'))
                        toggle.click()
                        search.fill('');page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===2")
                        self.assertTrue(page.locator('#catalog-tree [data-folder="甲"]').evaluate('(node)=>node.classList.contains("active")'))
                        self.assertFalse(page.locator('#catalog-all-search-folders').is_visible())
                        entered=threading.Event();release=threading.Event();original=catalog.tree
                        def delayed(text='',media=''):
                            if text=='A001':entered.set();release.wait(10)
                            return original(text,media)
                        with patch.object(catalog,'tree',side_effect=delayed):
                            search.fill('A001');self.assertTrue(entered.wait(5));search.fill('B002')
                            page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===1&&document.getElementById('catalog-tree').textContent.includes('无匹配')")
                            release.set();page.wait_for_timeout(200)
                            self.assertNotIn('子目录',page.locator('#catalog-tree').inner_text())
                        search.fill('A001');page.wait_for_selector('.catalog-folder-toggle')
                        page.locator('button[aria-controls="'+control_id+'"]').click()
                        page.reload(wait_until='domcontentloaded');page.locator('#asset-archive-open').click();page.locator('#catalog-search').fill('A001')
                        saved=page.locator('button[aria-controls="'+control_id+'"]');saved.wait_for()
                        self.assertEqual(saved.get_attribute('aria-expanded'),'false')
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close();library.close();archive.close()


if __name__=='__main__':unittest.main()
