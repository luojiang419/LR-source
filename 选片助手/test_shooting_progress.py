import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile
from xml.etree import ElementTree as E

import test_sku_workflow as fixture
from sku_progress import capture_time,statistics
from sku_service import SkuService
from sku_workbook import inspect_workbook,export_workbook,N


class TimeTests(unittest.TestCase):
    def test_exif_subsecond_offset_and_missing(self):
        stamp,source=capture_time({'captureIdentity':{'DateTimeOriginal':'2026:10:20 15:16:17','SubSecTimeOriginal':'123456789','OffsetTimeOriginal':'+08:00'}})
        self.assertEqual(stamp,'2026-10-20T15:16:17.123456789+08:00');self.assertEqual(source,'EXIF.DateTimeOriginal')
        self.assertEqual(capture_time({'captureIdentity':{'SubSecDateTimeOriginal':'2026:10:20 15:16:17.007Z'}})[0],'2026-10-20T15:16:17.007Z')
        self.assertEqual(capture_time({'time':None,'missingTime':True}),('','missing'))
        self.assertEqual(capture_time({'captureIdentity':{'DateTimeOriginal':'2026:02:31 15:16:17'}}),('','missing'))
    def test_unique_sku_counts_per_sheet(self):
        rows=[{'sheet':'a','normalized':'SKU1','status':s} for s in ['已拍摄','未拍摄']]+[{'sheet':'b','normalized':'SKU1','status':'未拍摄'}]
        result=statistics(rows,[{'name':'a'},{'name':'b'}]);self.assertEqual(result[0],{'sheet':'a','total':1,'shot':1,'pending':0,'rows':2});self.assertEqual(result[1]['shot'],0)


class ProgressTests(unittest.TestCase):
    setUp=fixture.SkuWorkflowTests.setUp
    tearDown=fixture.SkuWorkflowTests.tearDown
    rate=fixture.SkuWorkflowTests.rate
    tick=fixture.SkuWorkflowTests.tick
    def stamp(self,index,value,offset=''):
        with self.store.connect() as c:
            row=c.execute('SELECT data FROM captures WHERE session=? AND id=?',(self.sid,self.cids[index])).fetchone();record=json.loads(row[0])
            record['captureIdentity']={'SubSecDateTimeOriginal':value,'OffsetTimeOriginal':offset};record['time']=None;record['missingTime']=True
            c.execute('UPDATE captures SET data=? WHERE session=? AND id=?',(json.dumps(record),self.sid,self.cids[index]))
    def own(self):return next(r for r in self.sku.state()['rows'] if r['sheet']=='模特图' and r['excel_row']==2)
    def test_photo_time_frozen_not_model_clock_and_export_column(self):
        self.stamp(0,'2026:10:20 15:16:17.123456','+08:00');self.rate(2);self.clock[0]+=10000;self.tick()
        row=self.own();self.assertEqual(row['completed_at'],'2026-10-20T15:16:17.123456+08:00');self.assertEqual(row['completion']['capture_id'],self.cids[0])
        progress=self.sku.state()['progress'];self.assertEqual(next(s for s in progress if s['sheet']=='模特图')['shot'],1);self.assertEqual(next(s for s in progress if s['sheet']=='细节图')['shot'],0)
        with ZipFile(self.sku.export(self.pid)) as z:
            cells={c.get('r'):''.join(c.itertext()) for c in E.fromstring(z.read('xl/worksheets/sheet1.xml')).findall('m:sheetData/m:row/m:c',N)}
        self.assertEqual(cells['E1'],'拍摄完成时间');self.assertEqual(cells['E2'],row['completed_at']);self.assertEqual(cells['D2'],'已拍摄')
    def test_latest_valid_capture_and_revoke_falls_back_other_evidence(self):
        self.stamp(0,'2026:10:20 15:00:00+08:00');self.stamp(1,'2026:10:20 09:00:00Z')
        self.rate(2);self.tick();self.rate(2,self.cids[1]);self.tick();self.assertEqual(self.own()['completed_at'],'2026-10-20T09:00:00Z')
        self.rate(0,self.cids[1]);self.tick();self.assertEqual(self.own()['completed_at'],'2026-10-20T15:00:00+08:00')
        self.rate(0);self.tick();self.assertEqual(self.own()['completed_at'],'');self.assertEqual(self.own()['status'],'未拍摄')
    def test_missing_time_does_not_invent_completion_or_erase_known(self):
        self.stamp(0,'2026:10:20 15:00:00');self.rate(2);self.tick();self.rate(2,self.cids[1]);self.tick()
        self.assertEqual(self.own()['completed_at'],'2026-10-20T15:00:00')
        self.rate(0);self.tick();self.assertEqual(self.own()['completed_at'],'');self.assertEqual(self.own()['status'],'已拍摄');self.assertEqual(self.own()['completion']['capture_id'],self.cids[1])
    def test_review_and_restart_preserve_capture_time(self):
        self.stamp(0,'2026:10:20 15:00:00.001');self.sku.recognizer=lambda p,b:fixture.reading(confidence=.5)
        self.rate(2);self.tick();job=self.sku.state()['jobs'][0];self.assertEqual(self.own()['completed_at'],'')
        self.sku.review(job['id'],'confirm',self.own()['id']);self.assertEqual(self.own()['completed_at'],'2026-10-20T15:00:00.001')
        self.sku.close();self.sku=SkuService(self.store,self.providers,autostart=False);self.assertEqual(self.own()['completed_at'],'2026-10-20T15:00:00.001')
    def test_old_schema_migration_backfills_valid_evidence(self):
        self.stamp(0,'2026:10:20 15:00:00.123');self.rate(2);self.tick();self.sku.close()
        with self.sku.connect() as c:
            p=self.sku.project(self.pid);p['metadata'].pop('progress_version')
            c.execute('UPDATE projects SET metadata=? WHERE id=?',(json.dumps(p['metadata']),self.pid))
            for table,columns in [('sku_rows',['baseline_completed_at','completed_at','completion_job','source_values']),('jobs',['captured_at','capture_time_source'])]:
                for column in columns:c.execute('ALTER TABLE '+table+' DROP COLUMN '+column)
        self.sku=SkuService(self.store,self.providers,autostart=False);self.sku.export(self.pid)
        self.assertEqual(self.own()['completed_at'],'2026-10-20T15:00:00.123');self.assertEqual(self.sku.project()['metadata']['progress_version'],1)


class WorkbookProgressTests(unittest.TestCase):
    def test_adjacent_time_column_reimport_and_protect_original_parts(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.xlsx';source.write_bytes(fixture.fixture_book());out=Path(folder)/'output.xlsx';plans=inspect_workbook(source)
            self.assertEqual([p['time_letter'] for p in plans],['E','D'])
            export_workbook(source,out,plans,{('模特图',2):'已拍摄'},{('模特图',2):'2026-10-20T15:00:00.123+08:00'})
            with ZipFile(source) as a,ZipFile(out) as b:
                for name in a.namelist():
                    if not name.startswith('xl/worksheets/'):self.assertEqual(a.read(name),b.read(name))
            reread=inspect_workbook(out);self.assertEqual(reread[0]['time_col'],5);self.assertEqual(reread[0]['rows'][0]['baseline_completed_at'],'2026-10-20T15:00:00.123+08:00')
    def test_occupied_status_neighbor_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.xlsx'
            with ZipFile(__import__('io').BytesIO(fixture.fixture_book())) as a,ZipFile(source,'w') as b:
                for info in a.infolist():
                    data=a.read(info.filename)
                    if info.filename=='xl/worksheets/sheet1.xml':data=data.replace('备注'.encode(),'拍摄状态'.encode())+b'';data=data.replace(b'</row>',b'<c r="D1" t="inlineStr"><is><t>occupied</t></is></c></row>',1)
                    b.writestr(info,data)
            with self.assertRaisesRegex(ValueError,'已有内容'):inspect_workbook(source)


if __name__=='__main__':unittest.main()
