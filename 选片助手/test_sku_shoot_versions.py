"""重复拍摄按永久标签分版本，保留文档原款号和可迁移的稳定占号。"""
import copy
from contextlib import ExitStack
import hashlib
from http.server import ThreadingHTTPServer
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
import server
import test_ocr_metadata as fixture
from sku_metadata_backup import export_metadata,import_metadata,_canonical
from sku_service import SkuService
from sku_match import normalize
from sku_presentation import gallery


class ShootVersionTests(unittest.TestCase):
    setUp=fixture.OcrMetadataTests.setUp
    tearDown=fixture.OcrMetadataTests.tearDown
    parse=fixture.OcrMetadataTests.parse
    choose=fixture.OcrMetadataTests.choose

    def two_shoots(self):
        self.sku.set_sku_rules([{'kind':'contains','value':'LC'}])
        first=self.parse()['auto_applied']
        second=self.sku.parse_capture(self.sid,self.ids[3])['auto_applied']
        self.assertEqual(first['sku'],fixture.CODE)
        self.assertEqual(second['sku'],fixture.CODE+'-A')
        return second

    def test_first_repeat_and_retry_preserve_two_intervals_original_plan_and_current_image(self):
        second=self.two_shoots()
        self.assertEqual(second['base_sku'],fixture.CODE);self.assertEqual(second['shoot_version'],'A')
        with self.archive.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM assets WHERE sku=?',(fixture.CODE,)).fetchone()[0],2)
            self.assertEqual(c.execute('SELECT count(*) FROM assets WHERE sku=?',(fixture.CODE+'-A',)).fetchone()[0],2)
        row=next(r for r in self.sku.state()['rows'] if r['sheet']=='模特图' and r['sku']==fixture.CODE)
        self.assertEqual(row['shoot_sku'],fixture.CODE+'-A')
        self.assertEqual(row['sku_image']['capture_id'],self.ids[2])
        self.assertEqual(row['completion']['capture_id'],self.ids[3])
        self.assertFalse([r for r in self.sku.rows(self.pid) if r['sku']==fixture.CODE+'-A'])
        self.assertEqual(self.choose(cid=self.ids[3])['sku'],fixture.CODE+'-A')
        self.assertEqual(self.choose(fixture.CODE+'-A',cid=self.ids[3])['sku'],fixture.CODE+'-A')
        self.assertEqual(gallery(self.sku,self.pid,fixture.CODE)['total'],2)
        self.assertIn(fixture.CODE,self.sku.ocr_details(self.sid,self.ids[3])['text'])
        with self.sku.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM sku_shoot_versions').fetchone()[0],2)

    def test_restart_clear_and_restar_keep_reserved_version_and_metadata(self):
        self.two_shoots()
        self.sku.close();self.sku=SkuService(self.store,self.providers,archive=self.archive,autostart=False,require_sku_choice=True,recognizer=lambda *_:copy.deepcopy(self.reading));self.archive.sku=self.sku
        session=self.store.session(self.sid);old=copy.deepcopy(session['state']);state=copy.deepcopy(old);state['overrides'][self.ids[3]]=0
        self.store.save(self.sid,session['revision'],state);self.sku.on_save(self.sid,old,state);self.sku.deliver_events(self.archive)
        self.assertFalse(self.sku.ocr_details(self.sid,self.ids[3])['has_ocr'])
        session=self.store.session(self.sid);old=copy.deepcopy(session['state']);state=copy.deepcopy(old);state['overrides'][self.ids[3]]=2
        self.store.save(self.sid,session['revision'],state);self.sku.on_save(self.sid,old,state)
        result=self.sku.parse_capture(self.sid,self.ids[3])['auto_applied']
        self.assertEqual(result['sku'],fixture.CODE+'-A')
        for _ in range(50):
            with self.archive.connect() as c:pending=c.execute("SELECT 1 FROM metadata_jobs WHERE status='queued'").fetchone()
            if not pending:break
            self.archive.process_one()
        with self.archive.connect() as c:self.assertEqual(c.execute("SELECT reason FROM metadata_jobs WHERE status='failed'").fetchall(),[])
        self.assertEqual(self.archive.writer.read(self.photos/'2.JPG')['XMP-fk:SKU'],fixture.CODE+'-A')
        self.assertEqual(self.archive.writer.read(self.photos/'3.JPG')['XMP-fk:SKU'],fixture.CODE+'-A')

    def test_failed_confirmation_does_not_consume_a_version(self):
        self.sku.set_sku_rules([]);self.parse();self.choose()
        self.sku.parse_capture(self.sid,self.ids[3])
        with self.sku.connect() as c:c.execute("CREATE TRIGGER reject_version BEFORE INSERT ON jobs BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaisesRegex(Exception,'test failure'):self.choose(cid=self.ids[3])
        with self.sku.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM sku_shoot_versions').fetchone()[0],1)
            c.execute('DROP TRIGGER reject_version')
        self.assertEqual(self.choose(cid=self.ids[3])['sku'],fixture.CODE+'-A')

    def test_existing_original_codes_and_exhausted_letters_are_protected(self):
        self.parse();self.choose()
        self.archive.apply(self.archive.preview(self.sid,[self.ids[2]],fixture.CODE+'-A','模特图',self.pid)['preview_id'])
        self.sku.parse_capture(self.sid,self.ids[3])
        self.assertEqual(self.choose(cid=self.ids[3])['sku'],fixture.CODE+'-B')
        with self.sku.connect() as c:
            for index in range(1,27):
                if index==2:continue
                value=fixture.CODE+'-'+chr(64+index)
                c.execute('INSERT INTO sku_shoot_versions VALUES (?,?,?,?,?,?)',('reserved-'+str(index),normalize(fixture.CODE),fixture.CODE,value,index,0))
        from sku_shoot_versions import allocate
        with self.sku.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(ValueError,'A–Z'):allocate(c,self.archive,'new-asset',self.sid,'new-capture',fixture.CODE,0)

    def test_versions_backup_roundtrip_and_old_backup_compatibility(self):
        self.two_shoots();backup=export_metadata(self.sku)['backup']
        self.assertEqual(len(backup['payload']['databases']['sku']['sku_shoot_versions']),2)
        from asset_archive import AssetArchive
        from catalog_store import Store
        from model_providers import Providers
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder,fixture.ROOT);archive=AssetArchive(store,autostart=False);sku=SkuService(store,Providers(store.data),archive=archive,autostart=False);archive.sku=sku
            try:
                import_metadata(sku,_canonical(backup))
                with sku.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM sku_shoot_versions').fetchone()[0],2)
                self.assertEqual(sku.ocr_details(self.sid,self.ids[3])['base_sku'],fixture.CODE)
            finally:sku.close();archive.close()
        legacy=copy.deepcopy(backup);legacy['payload']['databases']['sku'].pop('sku_shoot_versions');legacy['sha256']=hashlib.sha256(_canonical(legacy['payload'])).hexdigest()
        from sku_metadata_backup import _decode
        _decode(_canonical(legacy))

    def test_third_shoot_uses_b_and_same_asset_across_sessions_keeps_its_version(self):
        self.two_shoots()
        from sku_shoot_versions import allocate
        with self.sku.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            result=allocate(c,self.archive,'third-label',self.sid,'third-capture',fixture.CODE,0)
            self.assertEqual(result['sku'],fixture.CODE+'-B')
            self.assertEqual(allocate(c,self.archive,'third-label','another-session','another-capture',fixture.CODE,1),result)

    def test_automatic_version_limit_is_reported_in_capture_feedback(self):
        self.sku.set_sku_rules([{'kind':'contains','value':'LC'}])
        from sku_shoot_versions import VersionLimitError
        with patch.object(self.sku,'choose_ocr_sku',side_effect=VersionLimitError('此款重拍版本 A–Z 已用完')):
            with self.assertRaisesRegex(VersionLimitError,'A–Z'):self.parse()
        from capture_parsing import capture_status
        status=capture_status(self.store,self.sku,self.archive,None,self.sid,self.ids[1])['ocr']
        self.assertEqual(status['status'],'review');self.assertIn('A–Z',status['reason'])

    def test_browser_repeat_shows_confirmed_version_and_progress_without_changing_plan(self):
        self.sku.set_sku_rules([{'kind':'contains','value':'LC'}]);self.parse()
        values=dict(STORE=self.store,DATA=self.store.data,MODELS=self.providers,SKU=self.sku,ARCHIVE=self.archive,CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None)
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        with ExitStack() as stack:
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1580,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>window.Framekeep&&window.FramekeepIO')
                        page.evaluate('(cid)=>Framekeep.browsePhoto(cid)',self.ids[3]);page.locator('#sku-asset-panel').evaluate('e=>e.open=true')
                        page.locator('#asset-parse-sku').click()
                        page.wait_for_function('(code)=>document.querySelector("#asset-ocr-feedback").dataset.status==="completed"&&document.querySelector("#asset-ocr-feedback").textContent.includes(code)',arg=fixture.CODE+'-A')
                        page.wait_for_function('()=>document.querySelectorAll("#asset-ocr-codes [aria-pressed=true]").length===1')
                        out=fixture.ROOT/'build/sku-shoot-versions';out.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(out/'repeat-confirmed.png'),full_page=True)
                        page.locator('#asset-parse-sku').click()
                        page.wait_for_function('(code)=>document.querySelector("#asset-message").textContent.includes("保留已有拍摄版本")&&document.querySelector("#asset-message").textContent.includes(code)',arg=fixture.CODE+'-A')
                        page.locator('#sku-open').click()
                        page.wait_for_function('(code)=>document.querySelector("#sku-rows").textContent.includes(code)',arg=fixture.CODE+'-A')
                        page.screenshot(path=str(out/'repeat-progress.png'),full_page=True)
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
