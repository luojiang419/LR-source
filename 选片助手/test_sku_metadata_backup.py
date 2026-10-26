"""隔离资料库迁移、失败回滚和真实浏览器备份交互。"""
import base64
from contextlib import ExitStack
import copy
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from asset_archive import AssetArchive
from catalog_store import Store
from model_providers import Providers
from sku_service import SkuService
from sku_library import browse
from sku_library_management import manage
from sku_metadata_backup import export_metadata, import_metadata, _canonical
import test_ocr_metadata as fixtures

ROOT = fixtures.ROOT


class MetadataBackupTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.OcrMetadataTests();self.f.setUp();f = self.f
        f.parse();f.choose('A-100');f.sku.parse_capture(f.sid, f.ids[3]);f.choose('B-200', f.ids[3])
        for _ in range(20):
            if not f.archive.process_one():break
        group = manage(f.sku, f.archive, {'action':'create_bin','name':'春季拍摄'})['bin_id']
        manage(f.sku, f.archive, {'action':'move','sku':'A-100','bin_id':group})
        manage(f.sku, f.archive, {'action':'remove','sku':'B-200'})
        self.output = export_metadata(f.sku);self.content = _canonical(self.output['backup'])
        self.target_temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.target_temp.name)/'data', ROOT)
        self.providers = Providers(self.store.data)
        self.archive = AssetArchive(self.store, autostart=False)
        self.sku = SkuService(self.store, self.providers, archive=self.archive, autostart=False, require_sku_choice=True)
        self.archive.sku = self.sku

    def tearDown(self):
        self.sku.close();self.archive.close();self.target_temp.cleanup();self.f.tearDown()

    def changed(self, edit):
        backup = copy.deepcopy(self.output['backup']);edit(backup)
        backup['sha256'] = hashlib.sha256(_canonical(backup['payload'])).hexdigest()
        return _canonical(backup)

    def test_manual_stars_and_explicit_clear_export_import_and_xmp(self):
        paths=[self.f.photos/f'{i}.JPG' for i in range(2)]
        for path,value in zip(paths,(5,0)):self.f.archive.ratings.set([path],value)
        content=_canonical(export_metadata(self.f.sku)['backup'])
        import_metadata(self.sku,content)
        for _ in range(20):
            if not self.archive.ratings.sync.process_one():break
        from rating_xmp import sidecar_rating
        for path,value in zip(paths,(5,0)):
            self.assertEqual(self.archive.ratings.get([path]),value)
            self.assertEqual(sidecar_rating(path),value)
            self.assertEqual(self.archive.writer.read(path)['XMP-xmp:Rating'],value)
        self.assertTrue(import_metadata(self.sku,content)['already_imported'])

    def test_roundtrip_all_skus_groups_removed_ocr_documents_and_restart(self):
        result = import_metadata(self.sku, self.content)
        self.assertGreater(result['records'], 20);self.assertEqual(result['projects'], 1)
        for bin_id in ('', 'trash', self.output['backup']['payload']['databases']['sku']['library_bins'][0]['id']):
            expected = browse(self.f.sku, bin_id=bin_id);actual = browse(self.sku, bin_id=bin_id)
            for key in ('groups','roots','total','interval_count','intervals'):
                self.assertEqual(actual[key], expected[key], key)
        self.assertEqual(browse(self.sku, search='COTTON')['total'], 2)
        self.assertFalse(self.sku.project(self.f.pid)['enabled'])
        self.assertEqual((self.sku.root/self.f.pid/'source.xlsx').read_bytes(), (self.f.sku.root/self.f.pid/'source.xlsx').read_bytes())
        self.assertEqual(self.providers.public()['providers'], [])
        with self.store.connect() as c:
            self.assertFalse(c.execute("SELECT 1 FROM settings WHERE key='active'").fetchone())
        reopened = SkuService(self.store, self.providers, archive=self.archive, autostart=False, require_sku_choice=True)
        try:self.assertEqual(browse(reopened)['total'], 2)
        finally:reopened.close()
        second = export_metadata(self.sku)
        self.assertEqual(len(second['backup']['payload']['databases']['catalog']['captures']), 4)
        self.assertTrue(import_metadata(self.sku, self.content)['already_imported'])

    def test_import_suspends_pending_writes_and_ocr_without_modifying_photos(self):
        before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in self.f.photos.glob('*.JPG')]
        def pending(backup):
            tables = backup['payload']['databases']
            for row in tables['archive']['metadata_jobs']:row['status'] = 'running'
            for row in tables['sku']['parse_jobs']:row['status'] = 'queued'
        import_metadata(self.sku, self.changed(pending))
        with self.archive.connect() as c:
            self.assertFalse(c.execute("SELECT 1 FROM metadata_jobs WHERE status IN ('queued','running')").fetchone())
            self.assertFalse(c.execute("SELECT 1 FROM streams WHERE state='active'").fetchone())
        with self.sku.connect() as c:
            self.assertFalse(c.execute("SELECT 1 FROM parse_jobs WHERE status IN ('queued','running')").fetchone())
        self.archive.tick();self.sku.tick()
        self.assertEqual(before, [hashlib.sha256(p.read_bytes()).hexdigest() for p in self.f.photos.glob('*.JPG')])

    def test_export_includes_more_than_one_browser_page_and_offline_archive(self):
        f = self.f
        record = f.store.records(f.sid)[0]
        with f.store.connect() as c:
            c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('archive_session:'+f.sid, 'true'))
            for i in range(250):
                item = dict(record, id='backup-'+str(i), jpg='offline-'+str(i)+'.JPG')
                c.execute('INSERT INTO captures VALUES (?,?,?,?)', (f.sid, item['id'], json.dumps(item), i+10))
        with f.archive.connect() as c:
            for i in range(250):
                aid = 'backup-asset-'+str(i)
                c.execute("INSERT INTO assets(id,sku,normalized,created) VALUES (?,'BULK-900','BULK900',0)", (aid,))
                c.execute('INSERT INTO catalog_links VALUES (?,?,?)', (f.sid, 'backup-'+str(i), aid))
                c.execute("INSERT INTO assignments VALUES (?,0,'{}',0)", (aid,))
        offline = f.photos.with_name('photos-offline');f.photos.rename(offline)
        try:
            self.assertEqual(browse(f.sku, code='BULK-900')['total'], 250)
            output = export_metadata(f.sku)
            import_metadata(self.sku, _canonical(output['backup']))
            result = browse(self.sku, code='BULK-900')
            self.assertEqual(result['total'], 250);self.assertEqual(len(result['rows']), 120)
            self.assertTrue(result['has_more']);self.assertEqual(result['rows'][0]['source'], 'archive')
            self.assertTrue(all(row['offline'] for row in result['rows']))
        finally:offline.rename(f.photos)

    def test_corrupt_version_path_and_unknown_sql_columns_rejected(self):
        bad = copy.deepcopy(self.output['backup']);bad['payload']['databases']['sku']['library_bins'][0]['name'] = '损坏'
        with self.assertRaisesRegex(ValueError, '校验'):import_metadata(self.sku, _canonical(bad))
        bad = copy.deepcopy(self.output['backup']);bad['version'] = 99
        with self.assertRaisesRegex(ValueError, '版本'):import_metadata(self.sku, _canonical(bad))
        with self.assertRaisesRegex(ValueError, '路径'):
            import_metadata(self.sku, self.changed(lambda b:b['payload']['documents'].update({'../escape':{}})))
        with self.assertRaisesRegex(ValueError, '字段'):
            import_metadata(self.sku, self.changed(lambda b:b['payload']['databases']['sku']['projects'][0].update({'evil";DROP TABLE projects;--':'bad'})))
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)
        self.assertEqual(list(self.sku.root.iterdir()), [])

    def test_conflict_rolls_back_all_databases_and_preserves_local_data(self):
        import_metadata(self.sku, self.content)
        with self.sku.connect() as c:c.execute("UPDATE library_bins SET name='本机修改'")
        def conflict(backup):
            session = copy.deepcopy(backup['payload']['databases']['catalog']['sessions'][0]);session['id'] = 'must-roll-back'
            backup['payload']['databases']['catalog']['sessions'].append(session)
            backup['payload']['databases']['sku']['library_bins'][0]['name'] = '不同备份'
        with self.assertRaisesRegex(ValueError, '冲突'):import_metadata(self.sku, self.changed(conflict))
        self.assertIsNone(self.store.session('must-roll-back'))
        with self.sku.connect() as c:self.assertEqual(c.execute('SELECT name FROM library_bins').fetchone()[0], '本机修改')

    def test_merge_independent_local_session_and_document_failure_rolls_back(self):
        local = Path(self.target_temp.name)/'local-photos';local.mkdir()
        sid = self.store.ensure_live_session(local)
        path = self.sku.root/self.f.pid/'source.xlsx';path.parent.mkdir();path.write_bytes(b'local-document')
        with self.assertRaisesRegex(ValueError, '文档'):import_metadata(self.sku, self.content)
        self.assertIsNone(self.store.session(self.f.sid));self.assertEqual(path.read_bytes(), b'local-document')
        path.unlink();path.parent.rmdir()
        import_metadata(self.sku, self.content)
        self.assertIsNotNone(self.store.session(sid));self.assertEqual(browse(self.sku)['total'], 2)

    def test_browser_collapse_persistence_export_import_and_http_auth(self):
        from playwright.sync_api import sync_playwright
        import server
        http = ThreadingHTTPServer(('127.0.0.1',0), server.Handler);http.daemon_threads = True
        values = dict(STORE=self.store, DATA=self.store.data, MODELS=self.providers, SKU=self.sku, ARCHIVE=self.archive,
                      CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,CANON=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None)
        out = ROOT/'build/sku-metadata-acceptance';out.mkdir(parents=True, exist_ok=True)
        fixture = out/'source.framekeep.json';fixture.write_bytes(self.content)
        with ExitStack() as stack:
            for key, value in values.items():stack.enter_context(patch.object(server, key, value))
            threading.Thread(target=http.serve_forever, daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe', headless=True)
                    try:
                        page = browser.new_page(viewport={'width':1680,'height':1050}, accept_downloads=True, reduced_motion='reduce')
                        errors = [];page.on('pageerror', lambda e:errors.append(str(e)))
                        url = f'http://127.0.0.1:{http.server_port}';page.goto(url, wait_until='networkidle')
                        self.assertEqual(page.request.post(url+'/api/sku/metadata/import', data={'content_base64':base64.b64encode(self.content).decode()}).status, 403)
                        page.locator('#app-settings-open').click();page.locator('[data-settings-section=database]').click()
                        page.locator('#sku-metadata-file').set_input_files(str(fixture))
                        page.wait_for_function("()=>document.getElementById('sku-metadata-message').textContent.includes('导入完成')")
                        page.locator('#settings-database').scroll_into_view_if_needed()
                        page.locator('.app-settings-grid').evaluate("e=>e.scrollTo({top:e.scrollHeight,behavior:'instant'})")
                        page.screenshot(path=str(out/'settings.png'))
                        page.locator('#sku-metadata-file').set_input_files(str(fixture))
                        page.wait_for_function("()=>document.getElementById('sku-metadata-message').textContent.includes('已经导入')")
                        with page.expect_download() as download:page.locator('#sku-metadata-export').click()
                        exported = json.loads(Path(download.value.path()).read_text(encoding='utf8'))
                        self.assertEqual(exported['schema'], 'framekeep.sku-metadata')
                        self.assertEqual(len(exported['payload']['databases']['catalog']['captures']), 4)
                        page.locator('#sku-database-open').click();page.wait_for_selector('[data-db-sku]')
                        page.locator('#sku-database-search').fill('COTTON')
                        page.wait_for_function("()=>document.getElementById('sku-database-title').textContent.includes('COTTON')")
                        self.assertEqual(page.locator('#sku-database-grid .catalog-card').count(), 2)
                        page.locator('#sku-database-search').fill('')
                        page.wait_for_function("()=>document.getElementById('sku-database-title').textContent==='选择一个 SKU'")
                        expanded = page.locator('#sku-database-grid').bounding_box()['width']
                        for kind in ('bins','list'):
                            page.locator('#sku-toggle-'+kind).click();self.assertEqual(page.locator('#sku-toggle-'+kind).get_attribute('aria-expanded'), 'false')
                        self.assertGreater(page.locator('#sku-database-grid').bounding_box()['width'], expanded+300)
                        page.reload(wait_until='networkidle');page.locator('#sku-database-open').click()
                        self.assertEqual(page.locator('#sku-toggle-list').get_attribute('aria-expanded'), 'false')
                        page.locator('#sku-toggle-bins').focus();page.keyboard.press('Enter');page.locator('#sku-toggle-list').click()
                        page.locator('[data-db-sku]').first.click()
                        page.wait_for_selector('#sku-database-grid img')
                        for theme in ('dark','light','pure-white','hardcore'):
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)', theme)
                            for width in (1680,1024,600,375):
                                page.set_viewport_size({'width':width,'height':1050})
                                for kind in ('expanded','collapsed'):
                                    if kind=='collapsed':
                                        page.locator('#sku-toggle-bins').click();page.locator('#sku-toggle-list').click()
                                    self.assertTrue(page.evaluate('()=>document.documentElement.scrollWidth<=innerWidth'), (theme,width,kind))
                                page.locator('#sku-toggle-bins').click();page.locator('#sku-toggle-list').click()
                            page.set_viewport_size({'width':1680,'height':1050});page.screenshot(path=str(out/f'database-{theme}.png'))
                        self.assertEqual(errors, [])
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__ == '__main__':unittest.main()
