"""断电恢复：持久记录、旧版半提交和读取失败不得表现为空清单。"""
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from sku_service import SkuService
from test_shooting_documents import ShootingDocumentsTests
from test_sku_workflow import fixture_book


class ShootingRecoveryTests(unittest.TestCase):
    setUp=ShootingDocumentsTests.setUp
    tearDown=ShootingDocumentsTests.tearDown
    prepare=ShootingDocumentsTests.prepare
    rate=ShootingDocumentsTests.rate
    choose=ShootingDocumentsTests.choose

    def reopen(self):
        self.sku.close()
        self.sku=SkuService(self.store,self.providers,archive=self.archive,clock=lambda:self.clock[0],autostart=False)
        self.archive.sku=self.sku

    def test_missing_selection_restores_confirmed_progress_without_rebinding(self):
        p=self.prepare();self.rate(self.records[1]['id']);self.choose(self.records[1]['id'],'SAVED-100')
        binding=self.sku.shooting_context(self.sid)
        # 旧版分段提交留下无 active 的持久库；子进程提交后不走正常关闭。
        subprocess.run([sys.executable,'-c',
            "import sqlite3,os,sys;c=sqlite3.connect(sys.argv[1]);c.execute(\"DELETE FROM settings WHERE key='active'\");c.commit();os._exit(0)",
            str(self.sku.db)],check=True,timeout=15)
        self.reopen()
        state=self.sku.state()
        self.assertIsNotNone(state['project'])
        self.assertEqual(state['project']['id'],p['id'])
        self.assertEqual([(r['sku'],r['status']) for r in state['rows']],[('SAVED-100','已拍摄')])
        self.assertEqual(self.sku.shooting_context(self.sid),binding)
        self.sku.live_arrivals(self.sid,self.records)
        self.assertEqual(len(self.sku.document_inventory()),1)
        self.assertEqual(self.sku.shooting_context(self.sid,self.records[2]['id'])['project'],p['id'])

    def test_imported_binding_repairs_missing_selection_on_new_arrival(self):
        p=self.sku.import_file('plan.xlsx',fixture_book(),self.sid,preferred_scope='模特图')['project']
        with self.sku.connect() as c:c.execute("DELETE FROM settings WHERE key='active'")
        self.sku.live_arrivals(self.sid,self.store.records(self.sid)[:1])
        self.assertIsNotNone(self.sku.project())
        self.assertEqual(self.sku.project()['id'],p['id'])
        self.assertEqual(self.sku.shooting_context(self.sid)['scope'],'模特图')

    def test_existing_view_is_preserved_and_removed_documents_are_not_restored(self):
        p=self.prepare();self.clock[0]+=86400
        self.sku.live_arrivals(self.sid,self.records[2:]);today=self.sku.project()['id']
        self.sku.activate(p['id']);binding=self.sku.shooting_context(self.sid)
        self.reopen()
        self.assertEqual(self.sku.project()['id'],p['id'])
        self.assertEqual(self.sku.shooting_context(self.sid),binding)
        self.sku.manage_document(p['id'],'remove');self.sku.manage_document(today,'remove')
        self.reopen();self.assertIsNone(self.sku.project())

    def test_failure_before_selection_rolls_back_document_and_binding(self):
        self.clock[0]=datetime(2026,11,4,1,tzinfo=timezone.utc).timestamp()
        connect=self.sku.connect
        class FaultConnection:
            def __init__(self,c):self.c=c
            def execute(self,sql,*args):
                if "INTO settings VALUES ('active',?)" in sql:raise OSError('simulated interruption')
                return self.c.execute(sql,*args)
            def __getattr__(self,key):return getattr(self.c,key)
        @contextmanager
        def faulty():
            with connect() as c:yield FaultConnection(c)
        with patch.object(self.sku,'connect',faulty):
            with self.assertRaisesRegex(OSError,'simulated interruption'):
                self.sku.live_arrivals(self.sid,self.store.records(self.sid)[:1])
        with connect() as c:
            for table in ('projects','shooting_bindings','shooting_captures','shooting_stream_queue'):
                self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],0,table)
        self.sku.live_arrivals(self.sid,self.store.records(self.sid)[:1])
        self.assertEqual(len(self.sku.document_inventory()),1)

    def test_killed_process_recovers_wal_progress_queue_and_next_photo(self):
        from asset_archive import AssetArchive
        from catalog_store import Store
        from folder_watch import FolderWatch
        from PIL import Image
        self.prepare();self.rate(self.records[1]['id']);self.choose(self.records[1]['id'],'BEFORE-CRASH')
        pid=self.sku.project()['id'];self.sku.close();self.archive.close()
        marker=self.base/'ready'
        script='''
import sqlite3,sys,time
from pathlib import Path
from catalog_store import Store
from asset_archive import AssetArchive
from model_providers import Providers
from sku_service import SkuService
from test_shooting_documents import ShootingDocumentsTests
data,root,stamp,marker=sys.argv[1:]
store=Store(Path(data),Path(root));archive=AssetArchive(store,autostart=False)
anchors=[sqlite3.connect(Path(data)/name) for name in ('framekeep.sqlite3','sku.sqlite3','asset_archive.sqlite3')]
sku=SkuService(store,Providers(Path(data)),archive=archive,clock=lambda:float(stamp),autostart=False);archive.sku=sku
case=ShootingDocumentsTests();case.store=store;case.sku=sku;case.sid=store.active_id()
records=store.records(case.sid);sku.live_arrivals(case.sid,records[2:])
case.rate(records[2]['id']);case.choose(records[2]['id'],'AT-CRASH')
sku.deliver_events(archive)
with archive.connect() as c:c.execute("UPDATE metadata_jobs SET status='running' WHERE id=(SELECT id FROM metadata_jobs WHERE status='queued' LIMIT 1)")
# 尚未提交的破坏性事务在强制终止后必须回滚。
anchors[1].execute('BEGIN IMMEDIATE');anchors[1].execute('DELETE FROM sku_rows')
Path(marker).write_text('ready')
time.sleep(60)
'''
        process=subprocess.Popen([sys.executable,'-c',script,str(self.store.data),str(self.store.root),str(self.clock[0]),str(marker)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+30
            while not marker.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.05)
            if not marker.exists():
                process.kill();out,err=process.communicate(timeout=10);self.fail((out+err).decode(errors='replace'))
            self.assertTrue(Path(str(self.sku.db)+'-wal').is_file())
            process.kill();process.communicate(timeout=10)
        finally:
            if process.poll() is None:process.kill();process.communicate(timeout=10)
        self.store=Store(self.store.data,self.store.root);self.archive=AssetArchive(self.store,autostart=False)
        self.reopen()
        state=self.sku.state();self.assertEqual(state['project']['id'],pid)
        self.assertEqual({r['sku'] for r in state['rows'] if r['status']=='已拍摄'},{'BEFORE-CRASH','AT-CRASH'})
        with self.archive.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM metadata_jobs WHERE status='running'").fetchone()[0],0)
            self.assertGreater(c.execute("SELECT count(*) FROM metadata_jobs WHERE status='queued'").fetchone()[0],0)
        for owner in (self.store,self.sku,self.archive):
            with owner.connect() as c:self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0],'ok')
        watch=FolderWatch(self.store,autostart=False);watch.on_arrivals=self.sku.live_arrivals
        try:
            watch.start(str(self.photos));watch.tick()
            path=self.photos/'after-crash.JPG';Image.new('RGB',(120,80),'green').save(path)
            watch.tick();watch.tick()
            new=next(r for r in self.store.records(self.sid) if r['jpg']==path.name)
            self.assertEqual(self.sku.shooting_context(self.sid,new['id'])['project'],pid)
            self.rate(new['id']);self.choose(new['id'],'AFTER-CRASH')
            self.assertEqual(len([r for r in self.sku.state()['rows'] if r['status']=='已拍摄']),3)
            self.assertEqual(len(self.sku.document_inventory()),1)
        finally:watch.close()

    def test_browser_restored_progress_and_read_failure_recovers(self):
        from test_sku_input_confirm import InputConfirmTests
        from playwright.sync_api import sync_playwright,expect
        self.prepare();self.rate(self.records[1]['id']);self.choose(self.records[1]['id'],'RESTORED-100')
        with self.sku.connect() as c:c.execute("DELETE FROM settings WHERE key='active'")
        self.reopen()
        out=Path(__file__).resolve().parents[1]/'build/shooting-recovery';out.mkdir(parents=True,exist_ok=True)
        with InputConfirmTests.http(self) as url,sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                failed=[True]
                page.route('**/api/sku',lambda route:route.fulfill(status=503,content_type='application/json',body='{"error":"recovery probe"}') if failed[0] else route.continue_())
                page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading')
                page.locator('#sku-open').click()
                expect(page.locator('#sku-read-error')).to_be_visible()
                page.wait_for_timeout(500)  # 工作区切换动效结束后保存可审阅截图。
                page.screenshot(path=str(out/'read-error.png'))
                failed[0]=False
                expect(page.locator('#sku-rows')).to_contain_text('RESTORED-100',timeout=15000)
                expect(page.locator('#sku-read-error')).to_be_hidden()
                page.wait_for_timeout(350)
                page.screenshot(path=str(out/'restored-progress.png'))
                failed[0]=True
                expect(page.locator('#sku-read-error')).to_be_visible(timeout=15000)
                expect(page.locator('#sku-rows')).to_contain_text('RESTORED-100')
                failed[0]=False;expect(page.locator('#sku-read-error')).to_be_hidden(timeout=15000)
                self.assertFalse(errors,errors)
            finally:browser.close()


if __name__=='__main__':unittest.main()
