"""真实侧车读取、四项描述元数据写回、竞争保护与持久恢复。"""
import hashlib
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import patch

from PIL import Image
from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from catalog_store import Store
from folder_watch import FolderWatch
from metadata_writer import sha
import media_engine as media
import photo_metadata
from rating_xmp import sidecar_metadata,sidecar_rating

ROOT=Path(__file__).resolve().parents[1]
XMP='''<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/">
<rdf:RDF><rdf:Description xmp:Rating="4" xmp:Label="Red" crs:Exposure2012="1.25"><dc:title><rdf:Alt><rdf:li xml:lang="x-default">原始标题</rdf:li><rdf:li xml:lang="fr">Titre conserve</rdf:li></rdf:Alt></dc:title><dc:description><rdf:Alt><rdf:li xml:lang="x-default">原始说明</rdf:li></rdf:Alt></dc:description><dc:subject><rdf:Bag><rdf:li>原始关键词</rdf:li></rdf:Bag></dc:subject></rdf:Description></rdf:RDF></x:xmpmeta>'''


class XmpSidecarSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.photos=self.base/'照片';self.photos.mkdir()
        self.photo=self.photos/'样片.JPG';Image.new('RGB',(80,60),'orange').save(self.photo)
        self.store=Store(self.base/'data',ROOT);self.archive=AssetArchive(self.store,autostart=False)
        self.catalog=AssetCatalog(self.store.data);self.catalog.add_root(str(self.photos));self.item=self.catalog.items()['rows'][0]['id']
        self.side=self.photo.with_suffix('.xmp');self.side.write_text(XMP,encoding='utf8')
        with Image.open(self.photo) as image:self.pixels=image.tobytes()

    def tearDown(self):self.catalog.close();self.archive.close();self.temp.cleanup()

    def edit(self,**overrides):
        current=photo_metadata.read(self.archive,self.catalog,self.item)
        values={key:current[key] for key in ('title','description','keywords','label')};values.update(overrides)
        return photo_metadata.save(self.archive,self.catalog,self.item,values,current['token'])

    def finish(self,result):
        for _ in range(12):
            self.archive.process_one();state=self.archive.operation(result['operation']['id'])
            if all(job['status'] not in ('queued','running') for job in state['jobs']):return state
        self.fail('元数据队列未结束')

    def assert_applied(self,result):
        state=self.finish(result);self.assertEqual([job['status'] for job in state['jobs']],['applied'],state)

    def test_read_jpeg_full_name_priority_zero_keywords_and_language(self):
        extra=Path(str(self.photo)+'.XMP');extra.write_text(XMP.replace('Rating="4"','Rating="0"').replace('原始标题','完整文件名侧车'),encoding='utf8')
        value=photo_metadata.read(self.archive,self.catalog,self.item)
        self.assertEqual(value['title'],'完整文件名侧车');self.assertEqual(value['keywords'],['原始关键词'])
        self.assertEqual(sidecar_rating(self.photo),0)
        self.assertEqual(self.archive.writer.read_asset(self.photo)['XMP-xmp:Rating'],0)
        self.assertEqual(media.read_metadata([self.photo],media.find_exiftool(ROOT))[0]['Title'],'完整文件名侧车')

    def test_real_four_fields_multiline_and_clear_preserve_pixels_develop_and_language(self):
        extra=Path(str(self.photo)+'.xmp');extra.write_bytes(self.side.read_bytes())
        result=self.edit(title='新的标题',description='第一行\n第二行：测试',keywords=['服装','秋季','服装'],label='蓝色');self.assert_applied(result)
        for target in (self.photo,self.side,extra):
            actual=self.archive.writer.read(target,True)
            self.assertEqual(actual['XMP-dc:Title'],'新的标题');self.assertEqual(actual['XMP-dc:Description'],'第一行\n第二行：测试')
            self.assertEqual(actual['XMP-dc:Subject'],['服装','秋季']);self.assertEqual(actual['XMP-xmp:Label'],'蓝色')
            if target!=self.photo:
                self.assertEqual(actual['XMP-crs:Exposure2012'],1.25);self.assertEqual(actual['XMP-dc:Title-fr'],'Titre conserve')
                self.assertEqual(actual['XMP-xmp:Rating'],4)
        self.assert_applied(self.edit(title='',description='',keywords=[],label=''))
        value=photo_metadata.read(self.archive,self.catalog,self.item)
        for key in ('title','description','label'):self.assertEqual(value[key],'')
        self.assertEqual(value['keywords'],[])
        with Image.open(self.photo) as image:self.assertEqual(image.tobytes(),self.pixels)

    def test_new_sidecar_and_two_queued_edits_keep_latest(self):
        self.side.unlink();first=self.edit(title='第一次');second=self.edit(title='最终标题')
        self.assert_applied(first);self.assert_applied(second)
        self.assertEqual(sidecar_metadata(self.photo)['Title'],'最终标题')
        self.assertTrue(Path(str(self.photo)+'.xmp').is_file())

    def test_descriptive_undo_restores_each_target_and_rejects_later_changes(self):
        result=self.edit(title='可撤销的修改',keywords=['临时关键词']);self.assert_applied(result)
        self.assert_applied(self.archive.revert(result['operation']['id']))
        current=photo_metadata.read(self.archive,self.catalog,self.item)
        self.assertEqual(current['title'],'原始标题');self.assertEqual(current['keywords'],['原始关键词'])
        first=self.edit(title='稍后会再次修改');self.assert_applied(first)
        self.assert_applied(self.edit(title='更晚的修改'))
        state=self.finish(self.archive.revert(first['operation']['id']))
        self.assertEqual(state['jobs'][0]['status'],'failed')
        self.assertEqual(photo_metadata.read(self.archive,self.catalog,self.item)['title'],'更晚的修改')

    def test_stale_editor_and_changed_sidecar_after_enqueue_are_not_overwritten(self):
        current=photo_metadata.read(self.archive,self.catalog,self.item);self.side.write_text(XMP.replace('原始标题','外部修改'),encoding='utf8')
        with self.assertRaisesRegex(ValueError,'已被修改'):photo_metadata.save(self.archive,self.catalog,self.item,current,current['token'])
        result=self.edit(title='不应覆盖');self.side.write_text(XMP.replace('原始标题','再次外部修改'),encoding='utf8')
        before=sha(self.photo);state=self.finish(result)
        self.assertEqual(state['jobs'][0]['status'],'failed');self.assertEqual(sha(self.photo),before)
        self.assertEqual(sidecar_metadata(self.photo)['Title'],'再次外部修改')

    def test_restart_recovers_after_primary_commit_before_sidecars(self):
        result=self.edit(title='重启后继续')
        with patch.object(self.archive.writer,'commit_sidecars',side_effect=RuntimeError('模拟中断')):
            self.assertEqual(self.finish(result)['jobs'][0]['status'],'failed')
        self.assertEqual(sidecar_metadata(self.photo)['Title'],'原始标题')
        self.archive.close();self.archive=AssetArchive(self.store,autostart=False)
        self.archive.retry(result['operation']['id']);self.assert_applied(result)
        self.assertEqual(sidecar_metadata(self.photo)['Title'],'重启后继续')

    def test_invalid_sidecar_and_entities_are_not_consumed_or_overwritten(self):
        self.side.write_text('<!DOCTYPE x [<!ENTITY bad "expanded">]><x>&bad;</x>',encoding='utf8')
        self.assertEqual(sidecar_metadata(self.photo),{})
        self.side.write_bytes(b'not xml');before=sha(self.photo)
        with self.assertRaises(ValueError):self.edit(title='不应写入')
        self.assertEqual(sha(self.photo),before);self.assertEqual(self.side.read_bytes(),b'not xml')

    def test_sidecar_only_watch_change_refreshes_rating_without_preview_identity_change(self):
        now=[0.];watch=FolderWatch(self.store,clock=lambda:now[0],autostart=False)
        self.store.on_external_metadata=self.archive.ratings.observe_sidecars
        try:
            sid=watch.start(str(self.photos))['session_id']
            for _ in range(3):watch.tick();now[0]+=2
            before=self.store.records(sid)[0];self.archive.ratings.seed_session(sid)
            digest=sha(self.photo);self.side.write_text(XMP.replace('Rating="4"','Rating="0"').replace('原始标题','外部改标题'),encoding='utf8')
            for _ in range(3):watch.tick();now[0]+=2
            after=self.store.records(sid)[0]
            self.assertEqual(after['sidecarMetadata']['Title'],'外部改标题');self.assertEqual(after['liveRating'],0)
            self.assertEqual(self.archive.ratings.get([self.photo]),0)
            self.assertEqual(after['fingerprint'],before['fingerprint']);self.assertNotEqual(after['sidecarFingerprint'],before['sidecarFingerprint'])
            self.assertEqual(sha(self.photo),digest)
        finally:watch.close()

    def test_sku_changes_and_revert_update_existing_sidecars_without_losing_descriptions(self):
        extra=Path(str(self.photo)+'.xmp');extra.write_bytes(self.side.read_bytes())
        first=photo_metadata.assign_sku(self.archive,self.catalog,self.item,'款号-100');self.assert_applied(first)
        second=photo_metadata.assign_sku(self.archive,self.catalog,self.item,'款号-200');self.assert_applied(second)
        for path in (self.photo,self.side,extra):
            actual=self.archive.writer.read(path,True);self.assertEqual(actual['XMP-fk:SKU'],'款号-200')
            subjects=actual['XMP-dc:Subject'];subjects=subjects if isinstance(subjects,list) else [subjects]
            self.assertNotIn('SKU:款号-100',subjects)
        self.assert_applied(self.archive.revert(second['operation']['id']))
        for path in (self.photo,self.side,extra):self.assertEqual(self.archive.writer.read(path)['XMP-fk:SKU'],'款号-100')
        self.assertEqual(photo_metadata.read(self.archive,self.catalog,self.item)['title'],'原始标题')

    def test_png_metadata_uses_sidecar_and_keeps_original_bytes(self):
        path=self.photos/'透明.png';Image.new('RGBA',(24,24),(20,50,100,100)).save(path);digest=sha(path)
        self.catalog.scan(self.catalog.state()['roots'][0]['id']);item=next(row['id'] for row in self.catalog.items()['rows'] if row['path']==str(path))
        current=photo_metadata.read(self.archive,self.catalog,item)
        self.assert_applied(photo_metadata.save(self.archive,self.catalog,item,dict(current,title='PNG 标题',keywords=['透明']),current['token']))
        self.assertEqual(sha(path),digest);self.assertEqual(sidecar_metadata(path)['Title'],'PNG 标题')

    @unittest.skipUnless((ROOT/'build/raw-reconcile/r6ii-6402.CR3').is_file(),'公开 RAW 样片未准备')
    def test_real_raw_four_fields_preserve_complete_raw_bytes(self):
        path=self.photos/'原始.CR3';shutil.copy2(ROOT/'build/raw-reconcile/r6ii-6402.CR3',path)
        digest=sha(path);path.with_suffix('.xmp').write_text(XMP,encoding='utf8')
        self.catalog.scan(self.catalog.state()['roots'][0]['id']);item=next(row['id'] for row in self.catalog.items()['rows'] if row['path']==str(path))
        current=photo_metadata.read(self.archive,self.catalog,item)
        self.assert_applied(photo_metadata.save(self.archive,self.catalog,item,dict(current,title='RAW 标题',description='保留 RAW',label='Green',keywords=['RAW']),current['token']))
        self.assertEqual(sha(path),digest)
        self.assertEqual(photo_metadata.read(self.archive,self.catalog,item)['title'],'RAW 标题')


class XmpMetadataBrowserTests(unittest.TestCase):
    setUp=XmpSidecarSyncTests.setUp
    tearDown=XmpSidecarSyncTests.tearDown

    def test_auto_save_four_fields_switch_and_themes(self):
        from contextlib import ExitStack
        from http.server import ThreadingHTTPServer
        import threading
        from playwright.sync_api import sync_playwright
        from reference_library import ReferenceLibrary
        from model_providers import Providers
        import server
        other=self.photos/'另一张.JPG';Image.new('RGB',(80,60),'blue').save(other)
        self.catalog.scan(self.catalog.state()['roots'][0]['id']);library=ReferenceLibrary(self.store.data)
        self.archive.thread.start();self.catalog.metadata.start()
        try:
            with ExitStack() as stack:
                for name,value in dict(STORE=self.store,DATA=self.store.data,ARCHIVE=self.archive,CATALOG=self.catalog,REFERENCES=library,
                    MODELS=Providers(self.store.data),SKU=None,MOUSE=None,WATCH=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None,
                    UPDATES=None,STORAGE=None,COMPONENTS=None,DRIVER=None).items():stack.enter_context(patch.object(server,name,value))
                http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
                threading.Thread(target=http.serve_forever,daemon=True).start()
                try:
                    with sync_playwright() as pw:
                        browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                        try:
                            page=browser.new_page(viewport={'width':1440,'height':1050},reduced_motion='reduce');errors=[]
                            page.on('pageerror',lambda error:errors.append(str(error)))
                            page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                            page.locator('#asset-archive-open').click();page.wait_for_selector('.catalog-thumb[data-preview-state=ready]')
                            page.locator('.catalog-card').filter(has=page.locator('img[alt="样片.JPG"]')).dblclick()
                            editor=page.locator('.asset-viewer-metadata')
                            page.wait_for_function("()=>document.querySelector('.asset-viewer-metadata input[name=title]')?.value==='原始标题'")
                            editor.locator('[name=title]').fill('浏览器自动保存')
                            editor.locator('[name=description]').fill('第一行\n第二行')
                            editor.locator('[name=keywords]').fill('人像，服装\n秋季')
                            editor.locator('[name=label]').fill('紫色')
                            page.wait_for_function("()=>document.querySelector('.asset-viewer-metadata [data-status]')?.textContent==='已自动保存元数据'",timeout=30000)
                            actual=photo_metadata.read(self.archive,self.catalog,self.item)
                            self.assertEqual(actual['description'],'第一行\n第二行');self.assertEqual(actual['keywords'],['人像','服装','秋季']);self.assertEqual(actual['label'],'紫色')
                            out=ROOT/'build/xmp-sidecar-metadata';out.mkdir(parents=True,exist_ok=True)
                            for theme in ('dark','light','pure-white','hardcore'):
                                page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                                editor.scroll_into_view_if_needed();page.screenshot(path=str(out/f'editor-{theme}.png'))
                                page.set_viewport_size({'width':375,'height':950})
                                self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                                page.set_viewport_size({'width':1440,'height':1050})
                            editor.locator('[name=title]').fill('切图前的修改')
                            # 第一张按修改时间排列，样片位于第二张，立即切回第一张。
                            page.locator('#asset-viewer-prev').click()
                            page.wait_for_function("()=>document.querySelector('.asset-viewer-metadata input[name=title]')?.value===''")
                            page.wait_for_function("async()=>{const r=await fetch('/api/catalog/metadata/"+self.item+"');return (await r.json()).title==='切图前的修改'}",timeout=30000)
                            self.assertNotIn('Title',sidecar_metadata(other))
                            page.locator('#asset-viewer-close').click()
                            self.assertFalse(errors,errors)
                        finally:browser.close()
                finally:http.shutdown();http.server_close()
        finally:library.close()


if __name__=='__main__':unittest.main()
