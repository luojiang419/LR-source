"""仅 OCR 场景下提交与自动确认不执行目录归档/重读原片。"""
import unittest
from unittest.mock import patch
import test_ocr_metadata as fixture


class OcrLatencyTests(unittest.TestCase):
    setUp=fixture.OcrMetadataTests.setUp
    tearDown=fixture.OcrMetadataTests.tearDown
    parse=fixture.OcrMetadataTests.parse
    choose=fixture.OcrMetadataTests.choose

    def test_first_two_star_queues_before_archive_folder_scan(self):
        import copy
        session=self.store.session(self.sid);old=copy.deepcopy(session['state'])
        old['overrides']={};state=copy.deepcopy(old);state['overrides']={self.ids[1]:2}
        # 未导入文档、已有整批照片的普通选片，与现场逐图到达不同。
        with self.sku.connect() as c:
            c.execute('DELETE FROM projects');c.execute('DELETE FROM shooting_bindings');c.execute("DELETE FROM settings WHERE key='active'")
        self.store.save(self.sid,session['revision'],state)
        with patch.object(self.archive,'start_stream',side_effect=AssertionError('提交不能扫描照片目录')):
            self.sku.on_save(self.sid,old,state)
        with self.sku.connect() as c:
            self.assertEqual(c.execute('SELECT status FROM jobs').fetchone()[0],'queued')
            self.assertEqual(c.execute('SELECT count(*) FROM shooting_stream_queue').fetchone()[0],1)

    def test_auto_confirmation_uses_registered_versions_without_rehashing_files(self):
        self.parse()
        for cid in self.ids:self.archive.ensure(self.sid,cid)
        with patch('asset_archive.sha',side_effect=AssertionError('确认不能重新读取原片')):
            self.choose()
        with self.archive.connect() as c:
            jobs=c.execute("SELECT status FROM metadata_jobs").fetchall()
        self.assertTrue(jobs);self.assertTrue(all(r[0]=='queued' for r in jobs))

    def test_deferred_hash_still_rejects_external_changes_before_write(self):
        from pathlib import Path
        from PIL import Image
        self.parse();self.choose()
        with self.archive.connect() as c:job=dict(c.execute("SELECT * FROM metadata_jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone())
        photo=Path(job['path']);Image.new('RGB',(160,120),'red').save(photo);changed=photo.read_bytes()
        self.archive.process_one()
        with self.archive.connect() as c:status=c.execute('SELECT status FROM metadata_jobs WHERE id=?',(job['id'],)).fetchone()[0]
        self.assertEqual(status,'failed');self.assertEqual(photo.read_bytes(),changed)

    def test_deferred_confirmation_survives_service_restart_before_archival(self):
        from sku_service import SkuService
        self.parse();details=self.sku.ocr_details(self.sid,self.ids[1])
        with patch.object(self.sku,'deliver_events',side_effect=AssertionError('即时记账不应同步归档')):
            result=self.sku.choose_ocr_sku(self.sid,self.ids[1],fixture.CODE,details['reading_key'],details['image_key'],automatic=True,defer_archive=True)
        self.assertTrue(result['archive_pending'])
        self.assertTrue(any(r['status']=='已拍摄' for r in self.sku.state()['rows']))
        self.sku.close()
        self.sku=SkuService(self.store,self.providers,autostart=False,archive=self.archive,require_sku_choice=True)
        self.archive.sku=self.sku;self.sku.deliver_events(self.archive)
        self.assertEqual(self.archive.query(fixture.CODE)['total'],2)
        with self.sku.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM archive_events WHERE delivered=0').fetchone()[0],0)

    def test_recovery_does_not_hold_sku_lock_while_waiting_for_archive(self):
        self.parse();self.choose()
        def repair(*args):self.assertFalse(self.sku.lock._is_owned())
        with patch.object(self.archive,'repair_segment_jobs',side_effect=repair) as call:
            self.assertTrue(self.sku._queue_confirmed_interval(self.sid,self.ids[1],only_missing=True))
            call.assert_called_once()

    def test_automatic_result_keeps_queued_document_after_view_switch(self):
        self.parse();old=self.sku.project()['id']
        with self.sku.connect() as c:
            c.execute('DELETE FROM shooting_bindings');c.execute("DELETE FROM settings WHERE key='active'")
        new=self.sku._automatic_project(self.sid,start_stream=False)['id'];self.assertNotEqual(old,new)
        self.sku.set_sku_rules([{'kind':'prefix','value':'LC'}])
        self.sku.apply_sku_rules(self.sid,self.ids[1],[self.reading],project_id=old,scope='模特图',defer_archive=True)
        with self.sku.connect() as c:
            projects={r[0] for r in c.execute("SELECT project FROM jobs WHERE status='confirmed' AND capture_id=?",(self.ids[1],))}
        self.assertEqual(projects,{old})

    def test_deferred_confirmation_commits_before_full_history_projection(self):
        self.parse();details=self.sku.ocr_details(self.sid,self.ids[1])
        with patch.object(self.sku,'_refresh_progress',side_effect=AssertionError('确认提交不能重算全目录')):
            self.sku.choose_ocr_sku(self.sid,self.ids[1],fixture.CODE,details['reading_key'],details['image_key'],automatic=True,defer_archive=True)
        with self.sku.connect() as c:
            row=c.execute("SELECT * FROM sku_rows WHERE status='已拍摄'").fetchone()
            self.assertIsNotNone(row)
            self.assertTrue(c.execute('SELECT 1 FROM jobs WHERE id=? AND status=\'confirmed\'',(row['completion_job'],)).fetchone())

    def test_automatic_confirmation_holds_file_stable_between_detail_checks(self):
        import threading
        self.parse();self.sku.set_sku_rules([{'kind':'prefix','value':'LC'}])
        original=self.sku.ocr_details;started=threading.Event();finished=threading.Event();worker=[]
        path=self.photos/'1.JPG'
        def change():
            started.set()
            with self.archive.writer.file_lock(path):finished.set()
        def details(*args):
            result=original(*args)
            if not worker:
                thread=threading.Thread(target=change,daemon=True);worker.append(thread);thread.start()
                self.assertTrue(started.wait(1));self.assertFalse(finished.wait(.03))
            return result
        with patch.object(self.sku,'ocr_details',side_effect=details):
            self.sku.apply_sku_rules(self.sid,self.ids[1],[self.reading],defer_archive=True)
        worker[0].join(2);self.assertTrue(finished.is_set())

if __name__=='__main__':unittest.main()
