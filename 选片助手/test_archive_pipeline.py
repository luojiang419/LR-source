"""二星无需表格、现场写入、同名最终拷贝监控及永久身份链路验收。"""
import copy
from datetime import datetime,timezone,timedelta
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import threading
from zipfile import ZipFile

from PIL import Image
from playwright.sync_api import sync_playwright
from unittest.mock import patch
from archive_pipeline import ArchivePipeline
from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from catalog_store import Store
from folder_watch import FolderWatch
from model_providers import Providers
from raw_reconcile import RawReconcile
from sku_service import SkuService
from sku_library import browse
from sku_workbook import inspect_workbook
import media_engine as media
import server
from test_sku_workflow import fixture_book,reading

ROOT=Path(__file__).resolve().parents[1]


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.live=self.base/'现场'/'CARD-001';self.live.mkdir(parents=True);self.final=self.base/'最终拷贝';self.final.mkdir()
        self.store=Store(self.base/'data',ROOT);self.sid=self.store.ensure_live_session(self.live);self.store.activate(self.sid)
        with self.store.connect() as c:c.execute('INSERT INTO settings VALUES (?,?)',('shooting_session:'+self.sid,str(self.live)))
        self.providers=Providers(self.store.data);provider=self.providers.save({'name':'test OCR','category':'ocr','protocol':'chat','url':'http://127.0.0.1:1/v1','model':'mock'})['providers'][0];self.providers.select(provider['id'])
        self.archive=AssetArchive(self.store,autostart=False);self.calls=[]
        self.sku=SkuService(self.store,self.providers,archive=self.archive,autostart=False,recognizer=self.recognize);self.sku.DEBOUNCE=0;self.archive.sku=self.sku
        self.link=RawReconcile(self.archive,autostart=False);self.catalog=AssetCatalog(self.store.data);self.now=100.
        self.pipeline=ArchivePipeline(self.catalog,self.archive,self.sku,self.link,autostart=False,clock=lambda:self.now)
        self.store.on_catalog=lambda sid:(self.pipeline.changed(sid),self.link.request(sid))
        self.ids=[];self.paths=[]

    def tearDown(self):
        self.pipeline.close();self.link.close();self.sku.close();self.archive.close();self.temp.cleanup()

    def recognize(self,provider,image):
        self.calls.append(provider['id']);return reading('TEMP-NEW-900')

    def add(self,name,rating=0):
        path=self.live/name;exif=Image.Exif();exif[36867]=f'2026:10:21 10:00:0{len(self.ids)}';exif[272]='TestCam';exif[42033]='123456';Image.new('RGB',(180,240),(60+len(self.ids)*20,100,150)).save(path,exif=exif)
        metadata={'Model':'TestCam','SerialNumber':'123456','DateTimeOriginal':f'2026:10:21 10:00:0{len(self.ids)}','Rating':rating}
        record=media.capture_record(self.live,path,None,metadata);self.store.append_live(self.sid,[record]);self.ids.append(record['id']);self.paths.append(path);return record['id']

    def rate(self,cid,value=2,legacy_ignore=True):
        session=self.store.session(self.sid);state=copy.deepcopy(session['state']);state.setdefault('overrides',{})[cid]=value
        if legacy_ignore:state.setdefault('skuMarkers',{})[cid]={'ignore':True}
        self.store.save(self.sid,session['revision'],state);self.sku.on_save(self.sid,session['state'],state)

    def drain(self):
        for _ in range(160):
            self.archive.tick();self.link.tick()
            with self.archive.connect() as c:
                pending=c.execute("SELECT 1 FROM metadata_jobs WHERE status IN ('queued','running') LIMIT 1").fetchone()
                events=c.execute('SELECT count(*) FROM event_receipts').fetchone()[0]
            if not pending and not self.link.pending:break

    def identify(self,external=False):
        self.add('IMG_0001.JPG');cid=self.add('IMG_0002.JPG',2 if external else 0)
        if not external:self.rate(cid)
        self.sku.tick();self.drain();return cid

    def test_real_two_star_without_excel_creates_date_document_and_embeds_sku(self):
        self.identify();self.assertEqual(len(self.calls),1)
        project=self.sku.project();expected=datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')+'.xlsx'
        self.assertEqual(project['name'],expected);output=self.sku.export(project['id']);self.assertEqual(output.name,expected)
        sheets=inspect_workbook(output);self.assertEqual(sheets[0]['rows'][0]['sku'],'TEMP-NEW-900');self.assertEqual(sheets[0]['rows'][0]['baseline'],'已拍摄')
        for path in self.paths:self.assertEqual(self.archive.writer.read(path)['XMP-fk:SKU'],'TEMP-NEW-900')
        self.sku.request_catalog(self.sid);self.sku.tick();self.assertEqual(len(self.calls),1)
        self.assertEqual(browse(self.sku)['groups'][0]['count'],2)

    def test_manual_mode_pause_and_restart_recovery_do_not_duplicate_ocr(self):
        self.sku.configure_tasks(mode='manual');self.add('IMG_0001.JPG');cid=self.add('IMG_0002.JPG');self.rate(cid)
        self.sku.tick();self.assertEqual(self.calls,[])
        self.sku.configure_tasks(mode='auto',paused=True);self.sku.tick();self.assertEqual(self.calls,[])
        self.sku.configure_tasks(paused=False);self.sku.tick();self.drain();self.assertEqual(len(self.calls),1)
        self.sku.close();self.sku=SkuService(self.store,self.providers,archive=self.archive,autostart=False,recognizer=self.recognize);self.sku.DEBOUNCE=0;self.archive.sku=self.sku;self.pipeline.sku=self.sku
        self.pipeline.changed(self.sid);self.sku.tick();self.drain();self.assertEqual(len(self.calls),1)

    def test_same_name_without_capture_identity_never_writes_final_sku(self):
        self.identify();destination=self.final/self.live.name;destination.mkdir()
        for path in self.paths:Image.new('RGB',(180,240),'orange').save(destination/path.name)
        before={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in destination.glob('*.JPG')}
        self.catalog.add_root(str(self.final));self.pipeline.tick();self.now+=1;self.pipeline.tick();self.drain()
        self.assertTrue(all(hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest for path,digest in before.items()))
        with self.archive.connect() as c:self.assertTrue(c.execute("SELECT count(*) FROM raw_pairs WHERE status='unmatched'").fetchone()[0])

    def test_incomplete_copy_is_not_written_and_removed_root_stays_disabled(self):
        self.identify();rid=self.catalog.add_root(str(self.final))['root_id'];self.pipeline.tick();destination=self.final/self.live.name;destination.mkdir()
        path=destination/self.paths[0].name;path.write_bytes(self.paths[0].read_bytes()[:200])
        self.now+=20;self.pipeline.tick();self.now+=1;self.pipeline.tick()
        self.assertEqual(len(self.store.records(self.store.ensure_live_session(self.final))),0)
        shutil.copy2(self.paths[0],path);self.now+=1;self.pipeline.tick();self.now+=1;self.pipeline.tick();self.drain()
        self.assertTrue(any(row['provenance']=='archive' for row in browse(self.sku)['rows']))
        self.catalog.remove(rid);self.pipeline.tick()
        self.assertFalse(any(row['provenance']=='archive' for row in browse(self.sku)['rows']))

    def test_external_rating_change_existing_capture_enqueues_once(self):
        self.add('IMG_0001.JPG');cid=self.add('IMG_0002.JPG')
        record=next(r for r in self.store.records(self.sid) if r['id']==cid);metadata={'Model':'TestCam','SerialNumber':'123456','DateTimeOriginal':'2026:10:21 10:00:01','Rating':2}
        self.archive.writer.run('-overwrite_original','-XMP-xmp:Rating=2',self.paths[1])
        updated=media.capture_record(self.live,self.paths[1],None,metadata);self.store.append_live(self.sid,[updated]);self.sku.tick();self.drain()
        self.assertEqual(len(self.calls),1);self.assertEqual(next(r for r in self.store.records(self.sid) if r['id']==cid)['liveRating'],2)
        with self.archive.connect() as c:
            fingerprints=[dict(row) for row in c.execute('SELECT * FROM file_fingerprints')]
        self.assertTrue(fingerprints);self.assertTrue(all(row['filename'] and row['sha'] and row['identity_json'] for row in fingerprints))

    def test_browser_two_star_without_document_and_automatic_copy_sync(self):
        self.add('IMG_0001.JPG');label=self.add('IMG_0002.JPG');self.catalog.add_root(str(self.final))
        # 真实后台线程与浏览器点击，验证前端默认 skuMarkers 不会再吞掉二星。
        self.pipeline.clock=__import__('time').monotonic
        self.sku.thread.start();self.archive.thread.start();self.link.thread.start();self.pipeline.thread.start()
        http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        values={'STORE':self.store,'DATA':self.store.data,'SKU':self.sku,'ARCHIVE':self.archive,'RAWLINK':self.link,'CATALOG':self.catalog,'MODELS':self.providers,'WATCH':None,'MOUSE':None,'ANALYSIS':None,'FILESEARCH':None,'UPDATES':None}
        patches=[patch.object(server,key,value) for key,value in values.items()]
        for item in patches:item.start()
        threading.Thread(target=http.serve_forever,daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                try:
                    page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                    page.locator('[data-focus="'+label+'"]').first.click();page.locator('#stars [data-rating="2"]').click()
                    page.wait_for_function('()=>document.querySelectorAll(".watch-sku-label").length===2')
                    self.assertEqual(len(self.calls),1)
                    page.wait_for_function('async()=>{const data=await (await fetch("/api/sku")).json();return data.project?.name.endsWith(".xlsx")&&data.rows.some(row=>row.status==="已拍摄")}')
                    destination=self.final/self.live.name;destination.mkdir()
                    for path in self.paths:shutil.copy2(path,destination/path.name)
                    page.locator('#sku-database-open').click()
                    page.wait_for_selector('#sku-database-list [data-db-sku="TEMPNEW900"]');page.locator('#sku-database-list [data-db-sku="TEMPNEW900"]').click()
                    try:page.wait_for_function('()=>document.querySelectorAll("#sku-database-grid [data-provenance=archive]").length===2')
                    except Exception:
                        with self.archive.connect() as c:
                            evidence={name:[dict(row) for row in c.execute('SELECT * FROM '+name)] for name in ('raw_session_work','raw_pairs','audit')}
                        out=ROOT/'build/archive-pipeline';out.mkdir(parents=True,exist_ok=True);(out/'browser-failure.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf8');raise
                    self.assertEqual(page.locator('#sku-database-grid [data-provenance="onsite"]').count(),2)
                    page.wait_for_function('()=>[...document.querySelectorAll("#sku-database-grid img")].every(img=>img.naturalWidth>0)')
                    out=ROOT/'build/archive-pipeline';out.mkdir(exist_ok=True,parents=True);page.screenshot(path=str(out/'browser-sources.png'))
                    self.assertEqual(len(self.calls),1);self.assertFalse(errors,errors)
                finally:browser.close()
        finally:
            self.pipeline.close();self.link.close();self.sku.close();self.archive.close();http.shutdown();http.server_close()
            for item in reversed(patches):item.stop()

    def test_external_file_two_star_triggers_without_session_override(self):
        self.identify(external=True);self.assertEqual(len(self.calls),1)
        self.assertEqual(self.store.session(self.sid)['state'].get('overrides',{}),{})
        self.assertEqual(self.archive.writer.read(self.paths[0])['XMP-fk:SKU'],'TEMP-NEW-900')

    def test_real_folder_watch_reads_xmp_two_stars_and_runs_ocr(self):
        watch=FolderWatch(self.store,autostart=False,clock=lambda:self.now)
        try:
            watch.start(str(self.live))
            for i,name in enumerate(('IMG_0001.JPG','IMG_0002.JPG')):
                path=self.live/name;exif=Image.Exif();exif[272]='TestCam';exif[42033]='123456';exif[36867]=f'2026:10:21 10:00:0{i}'
                Image.new('RGB',(180,240),(60+i*30,100,150)).save(path,exif=exif)
            self.archive.writer.run('-overwrite_original','-XMP-xmp:Rating=2',self.live/'IMG_0002.JPG')
            watch.tick();self.now+=1;watch.tick();self.sku.tick();self.drain()
            self.assertEqual(len(self.calls),1)
            self.assertEqual(self.archive.writer.read(self.live/'IMG_0001.JPG')['XMP-fk:SKU'],'TEMP-NEW-900')
            self.assertEqual(self.store.session(self.sid)['state'].get('overrides',{}),{})
        finally:watch.close()

    def test_inserted_sku_is_added_to_extra_sheet_preserving_original_parts(self):
        source=fixture_book();self.sku.import_file('导入计划.xlsx',source,self.sid);pid=self.sku.project()['id'];self.sku.configure(pid,self.sid,'模特图')
        self.identify();project=self.sku.project(pid);self.assertIn('临时插拍',[s['name'] for s in project['metadata']['sheets']])
        path=self.sku.export(pid);sheets=inspect_workbook(path);extra=next(s for s in sheets if s['name']=='临时插拍');self.assertEqual(extra['rows'][0]['sku'],'TEMP-NEW-900')
        import io
        with ZipFile(io.BytesIO(source)) as old,ZipFile(path) as new:
            for name in ('xl/media/image1.png','xl/cellimages.xml','xl/_rels/cellimages.xml.rels'):self.assertEqual(old.read(name),new.read(name))
            self.assertEqual(old.read('xl/worksheets/sheet1.xml').count(b'DISPIMG'),new.read('xl/worksheets/sheet1.xml').count(b'DISPIMG'))
        self.assertEqual(next(s for s in sheets if s['name']=='模特图')['rows'][0]['baseline'],'未拍摄')

    def test_two_star_near_plan_sku_is_added_as_observed_without_marking_original(self):
        self.sku.import_file('导入计划.xlsx',fixture_book(),self.sid);pid=self.sku.project()['id'];self.sku.configure(pid,self.sid,'模特图')
        self.sku.recognizer=lambda provider,image:reading('LC7877496-P906')
        self.identify();state=self.sku.state();self.assertEqual(state['jobs'][0]['status'],'matched')
        extra=next(row for row in state['rows'] if row['sheet']=='临时插拍')
        self.assertEqual(extra['sku'],'LC7877496-P906');self.assertEqual(extra['status'],'已拍摄')
        self.assertEqual(next(row for row in state['rows'] if row['sku']=='LC7877496-P905' and row['sheet']=='模特图')['status'],'未拍摄')
        self.drain();self.assertEqual(self.archive.query('LC7877496-P906')['total'],2)
        output=self.sku.export(pid);sheet=next(s for s in inspect_workbook(output) if s['name']=='临时插拍')
        self.assertEqual(sheet['rows'][0]['baseline'],'已拍摄')

    def test_top_level_archive_watches_later_copy_and_lists_both_sources(self):
        self.identify();self.catalog.add_root(str(self.final));self.pipeline.tick()
        destination=self.final/self.live.name;destination.mkdir()
        for path in self.paths:shutil.copy2(path,destination/path.name)
        self.now+=20;self.pipeline.tick();self.now+=1;self.pipeline.tick();self.drain()
        for path in destination.glob('*.JPG'):self.assertEqual(self.archive.writer.read(path)['XMP-fk:SKU'],'TEMP-NEW-900')
        result=browse(self.sku,code='TEMP-NEW-900');self.assertEqual(result['total'],4)
        self.assertEqual({r['provenance_label'] for r in result['rows']},{'现场拍摄','最终归档'})
        with self.archive.connect() as c:
            sources={r['asset'] for r in c.execute('SELECT asset FROM catalog_links WHERE session=?',(self.sid,))}
            targets={r['asset'] for r in c.execute('SELECT asset FROM catalog_links WHERE session!=?',(self.sid,))}
        self.assertEqual(sources,targets)

    def test_archive_copy_can_arrive_before_source_is_recognized(self):
        self.add('IMG_0001.JPG');cid=self.add('IMG_0002.JPG');destination=self.final/self.live.name;destination.mkdir()
        for path in self.paths:shutil.copy2(path,destination/path.name)
        self.catalog.add_root(str(self.final));self.pipeline.tick();self.now+=1;self.pipeline.tick();self.drain()
        self.rate(cid);self.sku.tick();self.drain()
        for path in destination.glob('*.JPG'):self.assertEqual(self.archive.writer.read(path)['XMP-fk:SKU'],'TEMP-NEW-900')

    def test_original_large_jpg_without_embedded_id_matches_camera_and_folder(self):
        self.add('IMG_0001.JPG');label=self.add('IMG_0002.JPG');destination=self.final/self.live.name;destination.mkdir()
        for path in self.paths:shutil.copy2(path,destination/path.name)
        # 最终拷贝的相机 EXIF 不依赖小 JPG 后写入的自有元数据。
        for i,path in enumerate(destination.glob('*.JPG')):
            self.archive.writer.run('-overwrite_original','-Model=TestCam','-SerialNumber=123456',path)
        self.rate(label);self.sku.tick();self.drain();self.catalog.add_root(str(self.final))
        self.pipeline.tick();self.now+=1;self.pipeline.tick();self.drain()
        for path in destination.glob('*.JPG'):self.assertEqual(self.archive.writer.read(path)['XMP-fk:SKU'],'TEMP-NEW-900')

    def test_move_and_external_pixel_edit_retains_identity_and_versions(self):
        self.identify();destination=self.final/self.live.name;destination.mkdir();path=destination/self.paths[0].name;shutil.copy2(self.paths[0],path)
        self.catalog.add_root(str(self.final));self.pipeline.tick();self.now+=1;self.pipeline.tick();self.drain()
        meta=self.archive.writer.read(path);old_path=str(path);moved=destination/'编辑后';moved.mkdir();new=moved/'retouched.JPG'
        with Image.open(path) as image:
            image=image.copy();image.putpixel((0,0),(255,0,0));image.save(new)
        fields={key:meta['XMP-fk:'+key] for key in ('AssetId','FileId','SKU','ShootType','ProjectId','SegmentId','MetadataRevision')}
        for key,value in fields.items():self.archive.writer.run('-overwrite_original','-XMP-fk:'+key+'='+str(value),new)
        path.unlink();self.now+=20;self.pipeline.tick();self.now+=1;self.pipeline.tick();self.drain()
        result=browse(self.sku,code='TEMP-NEW-900');self.assertTrue(any(row['name']=='retouched.JPG' for row in result['rows']))
        with self.archive.connect() as c:
            self.assertEqual(c.execute('SELECT status FROM locations WHERE path=?',(old_path,)).fetchone()[0],'offline')
            fid=str(meta['XMP-fk:FileId']).replace('-','');self.assertGreaterEqual(c.execute('SELECT count(*) FROM versions WHERE file=?',(fid,)).fetchone()[0],2)
