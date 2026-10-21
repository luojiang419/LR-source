"""补识别中间标签：只读浏览重算、自动归档补偿和人工归属保护。"""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from asset_archive import AssetArchive
from catalog_store import Store
from model_providers import Providers
from sku_service import SkuService
from sku_library import browse,labels
from sku_intervals import partition
import media_engine as media
import server

ROOT=Path(__file__).resolve().parents[1]


class IntervalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.photos=self.base/'photos';self.photos.mkdir()
        self.store=Store(self.base/'data',ROOT);self.sid=self.store.ensure_live_session(self.photos);self.ids=[];self.paths=[]
        for i,name in enumerate(('01-product-a','02-label-a','03-product-c','04-label-c','05-product-b','06-label-b','07-tail')):
            path=self.photos/(name+'.JPG');Image.new('RGB',(300,200),(40+i*20,100,130)).save(path);self.paths.append(path)
            record=media.capture_record(self.photos,path,None,{'DateTimeOriginal':f'2026:10:21 10:00:0{i}','Model':'Test'})
            self.store.append_live(self.sid,[record]);self.ids.append(record['id'])
        self.store.activate(self.sid);self.archive=AssetArchive(self.store,autostart=False);self.providers=Providers(self.store.data)
        self.sku=SkuService(self.store,self.providers,archive=self.archive,autostart=False);self.archive.sku=self.sku

    def tearDown(self):
        self.sku.close();self.archive.close();self.temp.cleanup()

    def mark(self,index,code,created):
        with self.sku.connect() as c:c.execute('INSERT INTO parse_jobs(id,session_id,capture_id,status,created,result) VALUES (?,?,?,?,?,?)',(f'read-{index}-{created}',self.sid,self.ids[index],'completed',created,json.dumps({'codes':[code]})))

    def job(self,code,index,status='matched'):
        return {'id':code,'capture_id':self.ids[index],'session_id':self.sid,'project':'project','scope':'模特图','status':status}

    def automatic(self,code,index):
        job=self.job(code,index);self.archive.reserve(job);self.archive.handle_event(code+'-event',job,code);return job

    def segments(self):
        return {s['job']:s for s in self.archive.stream_state()['segments'] if s['status'] not in ('cancelled','superseded')}

    def drain(self):
        for _ in range(150):
            with self.archive.connect() as c:
                if not c.execute("SELECT 1 FROM metadata_jobs WHERE status='queued' LIMIT 1").fetchone():break
            self.archive.process_one()
        with self.archive.connect() as c:self.assertEqual([dict(r) for r in c.execute("SELECT path,reason FROM metadata_jobs WHERE status='failed'")],[])

    def assert_three(self):
        segments=self.segments();self.assertEqual(set(segments),{'A-100','C-300','B-200'})
        for code,positions in (('A-100',(0,1)),('C-300',(2,3)),('B-200',(4,5))):
            expected={self.archive.ensure(self.sid,self.ids[i]) for i in positions}
            self.assertEqual(set(segments[code]['members']),expected)
            for i in positions:self.assertEqual(self.archive.asset(self.archive.ensure(self.sid,self.ids[i]))['sku'],code)
        self.assertEqual(self.archive.asset(self.archive.ensure(self.sid,self.ids[6]))['sku'],'')

    def test_manual_ocr_repartitions_two_intervals_into_three_without_writing_photos(self):
        before=[hashlib.sha256(path.read_bytes()).hexdigest() for path in self.paths]
        self.archive.start_stream(self.sid,'project','模特图')
        self.mark(1,'A-100',10);self.mark(5,'B-200',20)
        prior=browse(self.sku);self.assertEqual(prior['interval_count'],2)
        self.assertEqual(labels(self.sku,self.sid)['labels'][self.ids[2]],'B-200')
        response={'session_id':self.sid,'capture_id':self.ids[3],'reading':{'codes':['C-300'],'text':'C-300'}}
        with patch.object(self.sku,'_read_capture',return_value=response) as ocr,patch.object(self.providers,'check_inference',side_effect=AssertionError('重划不能重复请求模型')):
            self.sku.parse_capture(self.sid,self.ids[3]);after=browse(self.sku);ocr.assert_called_once()
        self.assertEqual(after['interval_count'],3);self.assertEqual({r['sku']:r['count'] for r in after['groups']},{'A-100':2,'C-300':2,'B-200':2})
        mapping=labels(self.sku,self.sid)['labels'];self.assertEqual([mapping[self.ids[i]] for i in range(6)],['A-100']*2+['C-300']*2+['B-200']*2)
        self.assertNotIn(self.ids[6],mapping)
        self.archive.tick()
        self.assertEqual(before,[hashlib.sha256(path.read_bytes()).hexdigest() for path in self.paths])
        with self.archive.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM metadata_jobs').fetchone()[0],0)

    def test_normal_ocr_can_split_previously_written_automatic_segments_and_retry(self):
        self.archive.start_stream(self.sid,'project','模特图');self.automatic('A-100',1);self.automatic('B-200',5);self.drain()
        response={'session_id':self.sid,'capture_id':self.ids[3],'reading':{'codes':['C-300'],'text':'C-300'}}
        with patch.object(self.sku,'_read_capture',return_value=response) as ocr:
            self.sku.parse_capture(self.sid,self.ids[3]);self.sku.deliver_events(self.archive);ocr.assert_called_once()
        self.drain();segments={s['sku']:s for s in self.segments().values()}
        self.assertEqual(set(segments),{'A-100','C-300','B-200'});self.assertEqual([len(segments[code]['members']) for code in ('A-100','C-300','B-200')],[2,2,2])
        for i in (2,3):self.assertEqual(self.archive.writer.read(self.paths[i])['XMP-fk:SKU'],'C-300')
        middle=segments['C-300'];job=dict(self.job(middle['job'],3),status='ignored')
        self.archive.handle_event('ignore-read-C',job)
        with patch.object(self.sku,'_read_capture',return_value=response):
            self.sku.parse_capture(self.sid,self.ids[3]);self.sku.deliver_events(self.archive)
        self.drain();self.assertEqual(len(self.segments()),3)
        with self.archive.connect() as c:count=c.execute('SELECT count(*) FROM operations').fetchone()[0]
        self.archive.reconcile_ocr('same-reading-replayed',self.sid,self.ids[3])
        with self.archive.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM operations').fetchone()[0],count)

    def test_auto_confirmation_adopts_earlier_readonly_ocr_boundary(self):
        self.archive.start_stream(self.sid,'project','模特图');self.mark(1,'A-100',10);self.mark(5,'B-200',20)
        self.automatic('B-200',5)
        segments={s['sku']:s for s in self.segments().values()}
        self.assertEqual({sku:len(segment['members']) for sku,segment in segments.items()},{'A-100':2,'B-200':4})
        self.mark(3,'C-300',30);self.archive.reconcile_ocr('late-read-C',self.sid,self.ids[3]);self.drain()
        segments={s['sku']:s for s in self.segments().values()}
        self.assertEqual({sku:len(segment['members']) for sku,segment in segments.items()},{'A-100':2,'C-300':2,'B-200':2})

    def test_repeated_sku_remains_separate_end_intervals(self):
        self.mark(1,'A-100',10);self.mark(3,'A-100',30);self.mark(5,'B-200',20)
        result=browse(self.sku);self.assertEqual(result['interval_count'],3)
        self.assertEqual({row['sku']:row['count'] for row in result['groups']},{'A-100':4,'B-200':2})

    def test_queued_auto_assignments_reflow_and_remain_idempotent(self):
        self.archive.start_stream(self.sid,'project','模特图');self.automatic('A-100',1);self.automatic('B-200',5)
        old=self.segments()['B-200']['operation'];self.automatic('C-300',3);self.assert_three()
        with self.archive.connect() as c:
            self.assertFalse(c.execute("SELECT 1 FROM metadata_jobs WHERE operation=? AND status='queued'",(old,)).fetchone())
            count=c.execute('SELECT count(*) FROM operations').fetchone()[0]
        self.archive.reserve(self.job('C-300',3));self.archive.handle_event('duplicate-C',self.job('C-300',3),'C-300')
        with self.archive.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM operations').fetchone()[0],count)
        self.drain();self.assert_three()

    def test_already_written_neighbor_is_corrected_and_restart_does_not_rewrite(self):
        self.archive.start_stream(self.sid,'project','模特图');self.automatic('A-100',1);self.automatic('B-200',5);self.drain()
        unchanged=self.segments()['A-100']['operation'];self.automatic('C-300',3);self.drain();self.assert_three()
        self.assertEqual(self.segments()['A-100']['operation'],unchanged)
        for code,positions in (('A-100',(0,1)),('C-300',(2,3)),('B-200',(4,5))):
            for i in positions:self.assertEqual(self.archive.writer.read(self.paths[i])['XMP-fk:SKU'],code)
        with self.archive.connect() as c:count=c.execute('SELECT count(*) FROM operations').fetchone()[0]
        self.archive.close();self.archive=AssetArchive(self.store,autostart=False);self.archive.sku=self.sku;self.sku.archive=self.archive
        self.archive.reserve(self.job('C-300',3));self.assert_three()
        with self.archive.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM operations').fetchone()[0],count)

    def test_manual_locked_asset_survives_reflow_with_review_notice(self):
        self.archive.start_stream(self.sid,'project','模特图');self.automatic('A-100',1);self.automatic('B-200',5);self.drain()
        preview=self.archive.preview(self.sid,[self.ids[2]],'MANUAL-999','模特图',overwrite=True);self.archive.apply(preview['preview_id']);self.drain()
        self.automatic('C-300',3);self.drain()
        asset=self.archive.asset(self.archive.ensure(self.sid,self.ids[2]));self.assertEqual(asset['sku'],'MANUAL-999');self.assertTrue(asset['locked'])
        self.assertEqual(self.archive.writer.read(self.paths[2])['XMP-fk:SKU'],'MANUAL-999')
        self.assertEqual(self.segments()['C-300']['status'],'needs_review')
        self.assertEqual(self.archive.asset(self.archive.ensure(self.sid,self.ids[3]))['sku'],'C-300')
        self.mark(1,'A-100',10);self.mark(5,'B-200',20);self.mark(3,'C-300',30)
        self.assertNotIn(self.ids[2],labels(self.sku,self.sid)['labels'])

    def test_out_of_order_boundaries_and_cancellation_recompute_members(self):
        self.archive.start_stream(self.sid,'project','模特图')
        for code,index in (('B-200',5),('A-100',1),('C-300',3)):self.archive.reserve(self.job(code,index,'failed'))
        segments=self.segments();self.assertEqual([len(segments[code]['members']) for code in ('A-100','C-300','B-200')],[2,2,2])
        with self.archive.connect() as c:
            boundary=c.execute('SELECT last_boundary FROM streams').fetchone()[0]
            expected=c.execute('SELECT order_key FROM bindings WHERE capture=?',(self.ids[5],)).fetchone()[0]
        self.assertEqual(boundary,expected)
        self.archive.handle_event('cancel-middle',self.job('C-300',3,'ignored'))
        self.assertEqual(set(self.segments()),{'A-100','B-200'});self.assertEqual(len(self.segments()['B-200']['members']),4)

    def test_restart_recovers_committed_reflow_before_reassignment(self):
        self.archive.start_stream(self.sid,'project','模特图');self.automatic('A-100',1);self.automatic('B-200',5)
        with patch.object(self.archive,'apply_segment',side_effect=SystemExit('模拟提交成员后退出')):
            with self.assertRaises(SystemExit):self.archive.reserve(self.job('C-300',3))
        self.archive.close();self.archive=AssetArchive(self.store,autostart=False);self.archive.sku=self.sku;self.sku.archive=self.archive
        self.archive.handle_event('C-after-restart',self.job('C-300',3),'C-300');self.archive.tick();self.drain();self.assert_three()

    def test_camera_folder_and_missing_time_order_are_isolated(self):
        records=[{'id':'a','jpg':'one/a.JPG','cameraKey':'one','time':'2026-10-21T10:00:02'},
                 {'id':'b','jpg':'one/b.JPG','cameraKey':'one','time':'2026-10-21T10:00:01'},
                 {'id':'other','jpg':'one/c.JPG','cameraKey':'two','time':None},
                 {'id':'folder','jpg':'two/d.JPG','cameraKey':'one','time':None}]
        result=partition(records,{'a':'A'});self.assertEqual(result[0]['members'],['b','a'])
        records[0]['time']=None
        result=partition(records,{'a':'A'});self.assertEqual(result[0]['members'],['a']);self.assertEqual(result[0]['order_source'],'catalog_order')

    def test_browser_normal_ocr_updates_labels_and_sku_intervals(self):
        self.mark(1,'A-100',10);self.mark(5,'B-200',20)
        response={'session_id':self.sid,'capture_id':self.ids[3],'reading':{'codes':['C-300'],'text':'C-300'}}
        http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        values={'STORE':self.store,'DATA':self.store.data,'SKU':self.sku,'ARCHIVE':self.archive,'ANALYSIS':None,'MODELS':self.providers,'CATALOG':None,'WATCH':None,'MOUSE':None,'RAWLINK':None,'FILESEARCH':None,'UPDATES':None}
        patches=[patch.object(server,key,value) for key,value in values.items()]
        for item in patches:item.start()
        threading.Thread(target=http.serve_forever,daemon=True).start()
        try:
            with patch.object(self.sku,'_read_capture',return_value=response) as ocr,sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                try:
                    page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.locator('[data-view="single"]').click()
                    page.wait_for_function('()=>document.getElementById("watch-browser-count").textContent.includes("2 段")')
                    page.locator('[data-watch-photo="'+self.ids[3]+'"]').click();page.locator('#asset-parse-sku').click()
                    page.wait_for_function('()=>document.getElementById("watch-browser-count").textContent.includes("3 段")')
                    self.assertEqual(page.locator('[data-watch-photo="'+self.ids[3]+'"] .watch-sku-label').inner_text(),'SKU:C-300')
                    page.locator('#sku-database-open').click();page.wait_for_function('()=>document.getElementById("sku-library-count").textContent.includes("3 个区间")')
                    self.assertEqual(page.locator('#sku-database-list [data-db-sku]').count(),3)
                    page.locator('#sku-database-list [data-db-sku="C300"]').click();page.wait_for_function('()=>document.querySelectorAll("#sku-database-grid .catalog-card").length===2')
                    page.wait_for_function('()=>[...document.querySelectorAll("#sku-database-grid img")].every(img=>img.naturalWidth>0)')
                    out=ROOT/'build/sku-intervals';out.mkdir(exist_ok=True,parents=True);page.screenshot(path=str(out/'three-intervals.png'))
                    ocr.assert_called_once();self.assertFalse(errors,errors)
                finally:browser.close()
        finally:
            http.shutdown();http.server_close()
            for item in reversed(patches):item.stop()
