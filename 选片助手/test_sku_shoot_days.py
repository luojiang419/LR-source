import copy
import json
import unittest

import test_ocr_metadata as fixture
import test_archive_capture_service as archive_fixture
from sku_shoot_versions import allocate,record_day
from sku_match import normalize


class ShootDayTests(unittest.TestCase):
    setUp=fixture.OcrMetadataTests.setUp
    tearDown=fixture.OcrMetadataTests.tearDown
    parse=fixture.OcrMetadataTests.parse
    choose=fixture.OcrMetadataTests.choose

    def day(self,ids,day):
        with self.store.connect() as c:
            for cid in ids:
                row=c.execute('SELECT data FROM captures WHERE session=? AND id=?',(self.sid,cid)).fetchone()
                value=json.loads(row[0]);value['time']=day+'T10:00:03'
                c.execute('UPDATE captures SET data=? WHERE session=? AND id=?',(json.dumps(value),self.sid,cid))

    def allocate(self,asset,cid,code=fixture.CODE):
        with self.sku.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            return allocate(c,self.archive,asset,self.sid,cid,code,0)

    def test_each_shoot_day_starts_with_literal_code_and_owns_its_a_suffix(self):
        self.parse();self.assertEqual(self.choose()['sku'],fixture.CODE)
        self.assertEqual(self.allocate('old-repeat',self.ids[0])['sku'],fixture.CODE+'-A')
        self.day(self.ids[2:],'2026-10-22')
        self.sku.parse_capture(self.sid,self.ids[3])
        self.assertEqual(self.choose(cid=self.ids[3])['sku'],fixture.CODE)
        self.assertEqual(self.allocate('today-repeat',self.ids[2])['sku'],fixture.CODE+'-A')
        self.assertEqual(self.allocate('today-repeat',self.ids[2])['sku'],fixture.CODE+'-A')

    def test_history_documents_choices_and_assets_do_not_allocate_a_suffix(self):
        self.day([self.ids[2]],'2026-10-20')
        self.archive.apply(self.archive.preview(self.sid,[self.ids[2]],fixture.CODE,'模特图',self.pid)['preview_id'])
        with self.sku.connect() as c:
            c.execute('INSERT INTO ocr_choices VALUES(?,?,?,?,?)',('history','old',fixture.CODE,'historical-reading',0))
            c.execute('UPDATE sku_rows SET sku=?,normalized=? WHERE id=(SELECT id FROM sku_rows LIMIT 1)',(fixture.CODE+'-A',normalize(fixture.CODE+'-A')))
        self.parse();self.assertEqual(self.choose()['sku'],fixture.CODE)

    def test_only_identical_literal_ocr_code_counts_as_duplicate(self):
        self.parse();self.choose()
        other=fixture.CODE.replace('-','')
        self.reading['text']=other;self.reading['codes']=[other]
        self.sku.parse_capture(self.sid,self.ids[3])
        self.assertEqual(self.choose(other,cid=self.ids[3])['sku'],other)

    def test_unknown_capture_date_is_not_assumed_to_be_current_day(self):
        self.parse();self.choose()
        with self.store.connect() as c:
            r=self.store.records(self.sid,[self.ids[2]])[0];r['time']=None
            c.execute('UPDATE captures SET data=? WHERE session=? AND id=?',(json.dumps(r),self.sid,self.ids[2]))
        self.assertEqual(self.allocate('unknown-day',self.ids[2])['sku'],fixture.CODE)

    def test_explicit_day_and_china_timezone_midnight_are_respected(self):
        self.assertEqual(record_day({'time':'2026-11-09T18:00:00+00:00'}),'2026-11-10')
        self.assertEqual(record_day({'shoot_date':'2026-11-09','time':'2026-11-09T18:00:00+00:00'}),'2026-11-09')
        self.assertEqual(record_day({'time':'invalid'},'session:one'),'session:one')

    def test_unclosed_yesterday_photos_cannot_enter_today_interval(self):
        from sku_intervals import partition
        records=[{'id':'old','jpg':'old.jpg','time':'2026-11-09T23:00:00','cameraKey':'camera'},
                 {'id':'today','jpg':'today.jpg','time':'2026-11-10T10:00:00','cameraKey':'camera'},
                 {'id':'label','jpg':'label.jpg','time':'2026-11-10T10:00:01','cameraKey':'camera'}]
        self.assertEqual(partition(records,{'label':{}})[0]['members'],['today','label'])


class ArchiveShootDayTests(unittest.TestCase):
    setUp=archive_fixture.ArchiveCaptureTests.setUp
    tearDown=archive_fixture.ArchiveCaptureTests.tearDown
    client=archive_fixture.ArchiveCaptureTests.client
    item=archive_fixture.ArchiveCaptureTests.item
    drain=archive_fixture.ArchiveCaptureTests.drain

    def test_history_group_cannot_make_today_first_ocr_code_a_variant(self):
        self.api.rate(self.item(2),2,correction=True)
        self.api.confirm(self.item(2),'LC7832821-P305',automatic=True)
        with self.dataset.connect() as c:
            for row in c.execute('SELECT id,data FROM records').fetchall():
                r=json.loads(row['data'])
                if r['relative_path'] in ('IMG_0003.JPG','IMG_0004.JPG'):
                    r['shoot_date']='2026-11-10';r['time']=r['time'].replace('2026-11-09','2026-11-10')
                    c.execute('UPDATE records SET data=? WHERE id=?',(json.dumps(r),row['id']))
        self.api.rate(self.item(4),2,correction=True)
        self.assertEqual(self.api.confirm(self.item(4),'LC7832821-P305',automatic=True)['sku'],'LC7832821-P305')

    def test_same_day_identical_ocr_still_gets_a_suffix(self):
        self.api.rate(self.item(2),2,correction=True)
        self.api.confirm(self.item(2),'LC7832821-P305',automatic=True)
        self.api.rate(self.item(4),2,correction=True)
        self.assertEqual(self.api.confirm(self.item(4),'LC7832821-P305',automatic=True)['sku'],'LC7832821-P305-A')

    def test_manual_literal_stays_unchanged_and_later_same_day_ocr_sees_confirmed_code(self):
        self.api.rate(self.item(2),2,correction=True)
        self.api.confirm(self.item(2),'LC7832821-P305')
        self.api.rate(self.item(4),2,correction=True)
        self.assertEqual(self.api.confirm(self.item(4),'LC7832821-P305',automatic=True)['sku'],'LC7832821-P305-A')
