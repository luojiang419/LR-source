"""同款不同色标签缺色号时保持独立款号、清单、图片及手动改号。"""
import copy
import unittest
from unittest.mock import patch

from sku_library import browse,labels
from sku_presentation import gallery,save_preferences
from sku_match import normalize
from sku_service import SkuService
from sku_workbook import inspect_workbook
import test_ocr_metadata as fixture
import test_ocr_without_document as no_document


class DuplicateLabelTests(unittest.TestCase):
    setUp=fixture.OcrMetadataTests.setUp
    tearDown=fixture.OcrMetadataTests.tearDown
    parse=fixture.OcrMetadataTests.parse
    choose=fixture.OcrMetadataTests.choose

    def automatic_labels(self):
        self.sku.set_sku_rules([{'kind':'contains','value':'LC'}])
        first=self.parse()['auto_applied'];second=self.sku.parse_capture(self.sid,self.ids[3])['auto_applied']
        self.assertEqual((first['sku'],second['sku']),(fixture.CODE,fixture.CODE+'-A'))
        return second

    def test_automatic_duplicate_has_independent_rows_gallery_and_export(self):
        self.automatic_labels();state=self.sku.state()
        shot=[r for r in state['rows'] if r['status']=='已拍摄']
        self.assertEqual({r['sku'] for r in shot},{fixture.CODE,fixture.CODE+'-A'})
        self.assertEqual({r['completion']['capture_id'] for r in shot},{self.ids[1],self.ids[3]})
        self.assertEqual(labels(self.sku,self.sid)['labels'],dict(zip(self.ids,[fixture.CODE]*2+[fixture.CODE+'-A']*2)))
        for code,cid in ((fixture.CODE,self.ids[0]),(fixture.CODE+'-A',self.ids[2])):
            images=gallery(self.sku,self.pid,code)
            self.assertEqual(images['total'],1);self.assertEqual(images['images'][0]['capture_id'],cid)
        groups=browse(self.sku)['groups'];self.assertEqual({g['sku'] for g in groups},{fixture.CODE,fixture.CODE+'-A'})
        book=inspect_workbook(self.sku.export(self.pid),allow_empty=True)
        self.assertEqual({r['sku'] for s in book for r in s['rows'] if r['baseline']=='已拍摄'},{fixture.CODE,fixture.CODE+'-A'})
        self.assertEqual(self.sku.ocr_details(self.sid,self.ids[3])['base_sku'],fixture.CODE)
        self.assertIn(fixture.CODE,self.sku.ocr_details(self.sid,self.ids[3])['text'])

    def test_manual_color_correction_only_changes_its_own_interval(self):
        self.automatic_labels();row=next(r for r in self.sku.state()['rows'] if r['sku']==fixture.CODE+'-A')
        self.sku.rename_shooting_row(self.pid,row['id'],row['sku'],fixture.CODE+'-BLUE')
        mapping=labels(self.sku,self.sid)['labels']
        self.assertEqual(mapping,dict(zip(self.ids,[fixture.CODE]*2+[fixture.CODE+'-BLUE']*2)))
        with self.archive.connect() as c:
            for code in (fixture.CODE,fixture.CODE+'-BLUE'):
                self.assertEqual(c.execute('SELECT count(*) FROM assets WHERE sku=?',(code,)).fetchone()[0],2)
        self.assertEqual({r['sku'] for r in self.sku.state()['rows'] if r['status']=='已拍摄'},{fixture.CODE,fixture.CODE+'-BLUE'})
        for _ in range(50):
            with self.archive.connect() as c:pending=c.execute("SELECT 1 FROM metadata_jobs WHERE status='queued'").fetchone()
            if not pending:break
            self.archive.process_one()
        for index,code in enumerate([fixture.CODE]*2+[fixture.CODE+'-BLUE']*2):
            self.assertEqual(self.archive.writer.read(self.photos/f'{index}.JPG')['XMP-fk:SKU'],code)

    def test_repeat_and_restart_keep_same_suffix_and_row_count(self):
        self.automatic_labels();before={r['id'] for r in self.sku.rows(self.pid)}
        self.assertEqual(self.sku.parse_capture(self.sid,self.ids[3]).get('auto_applied'),None)
        self.assertEqual(self.choose(cid=self.ids[3])['sku'],fixture.CODE+'-A')
        self.sku.close();self.sku=SkuService(self.store,self.providers,archive=self.archive,autostart=False,require_sku_choice=True,
            recognizer=lambda *_:copy.deepcopy(self.reading));self.archive.sku=self.sku
        self.assertEqual(self.choose(cid=self.ids[3])['sku'],fixture.CODE+'-A')
        self.assertEqual({r['id'] for r in self.sku.rows(self.pid)},before)

    def test_old_base_cover_cannot_reuse_another_variant_label(self):
        self.automatic_labels()
        save_preferences(self.sku,self.pid,{'headers':{},'images':{normalize(fixture.CODE):{
            'key':'capture:'+self.sid+':'+self.ids[3],'kind':'capture','session_id':self.sid,'capture_id':self.ids[3]}}})
        first=next(r for r in self.sku.state()['rows'] if r['sku']==fixture.CODE and r['sheet']=='模特图')
        self.assertEqual(first['sku_image']['capture_id'],self.ids[0])

    def test_legacy_full_auto_also_assigns_suffix_to_duplicate_labels(self):
        self.sku.require_sku_choice=False;self.sku.set_full_auto(True,self.sid)
        for _ in range(4):self.sku.tick()
        self.assertEqual({labels(self.sku,self.sid)['labels'][cid] for cid in (self.ids[1],self.ids[3])},{fixture.CODE,fixture.CODE+'-A'})
        with self.sku.connect() as c:
            choices=c.execute('SELECT sku FROM ocr_choices WHERE session_id=?',(self.sid,)).fetchall()
            self.assertEqual({r[0] for r in choices},{fixture.CODE,fixture.CODE+'-A'})
        self.assertEqual({r['sku'] for r in self.sku.state()['rows'] if r['status']=='已拍摄'},{fixture.CODE,fixture.CODE+'-A'})

    def test_legacy_commit_failure_rolls_back_suffix_and_choice(self):
        self.sku.require_sku_choice=False;self.sku.set_full_auto(True,self.sid);self.sku._queue_full_auto()
        with self.sku.connect() as c:job=dict(c.execute('SELECT * FROM jobs WHERE capture_id=?',(self.ids[1],)).fetchone())
        target=next(r['id'] for r in self.sku.rows(self.pid,'模特图') if r['sku']==fixture.CODE)
        with patch('sku_service.record_ocr',side_effect=RuntimeError('测试提交失败')):
            with self.assertRaisesRegex(RuntimeError,'测试提交失败'):
                self.sku._finish(job,'matched',{'starred_label':True,'codes':[fixture.CODE]},[target],'match',defer_archive=True)
        with self.sku.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM sku_shoot_versions').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM ocr_choices').fetchone()[0],0)


class NoDocumentDuplicateLabelsTests(unittest.TestCase):
    setUp=no_document.NoDocumentTests.setUp
    tearDown=no_document.NoDocumentTests.tearDown

    def test_no_document_rule_keeps_each_same_code_label_as_its_own_row(self):
        records=self.store.records(self.sid);session=self.store.session(self.sid)
        state=copy.deepcopy(session['state']);state['overrides']={r['id']:2 for r in records}
        self.store.save(self.sid,session['revision'],state)
        self.sku.set_sku_rules([{'kind':'contains','value':'FREE'}])
        codes=[]
        for record in records:codes.append(self.sku.parse_capture(self.sid,record['id'])['auto_applied']['sku'])
        self.assertEqual(codes,['FREE-100','FREE-100-A','FREE-100-B'])
        rows=[r for r in self.sku.state()['rows'] if r['status']=='已拍摄']
        self.assertEqual({r['sku'] for r in rows},set(codes));self.assertEqual(len(rows),3)
        for code,record in zip(codes,records):
            self.assertEqual(gallery(self.sku,self.sku.project()['id'],code)['images'][0]['capture_id'],record['id'])


if __name__=='__main__':unittest.main()
