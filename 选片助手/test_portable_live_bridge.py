import time
import unittest

from archive_capture_service import ArchiveCaptureService
import test_archive_pipeline as pipeline


class PortableLiveBridgeTests(unittest.TestCase):
    setUp = pipeline.PipelineTests.setUp
    add = pipeline.PipelineTests.add
    rate = pipeline.PipelineTests.rate
    drain = pipeline.PipelineTests.drain
    recognize = pipeline.PipelineTests.recognize

    def tearDown(self):
        if hasattr(self, 'capture'):
            self.capture.close()
        self.catalog.close()
        pipeline.PipelineTests.tearDown(self)

    def test_live_two_stars_publish_complete_metadata_and_keep_job_valid(self):
        self.capture = ArchiveCaptureService(self.store, self.archive, self.catalog, self.sku, autostart=False)
        self.add('IMG_0001.JPG')
        label = self.add('IMG_0002.JPG')
        self.rate(label)
        self.sku.tick()
        self.drain()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.capture.tick()
            dataset = self.capture.dataset_for_path(self.paths[0])
            if dataset and len([r for r in dataset.records() if r.get('sku')]) == 2:
                with dataset.connect() as c:
                    if not c.execute("SELECT 1 FROM tasks WHERE status IN ('queued','running')").fetchone():
                        break
            time.sleep(.02)
        self.assertTrue((self.live / '.shiying/dataset.json').is_file())
        self.assertEqual([r['sku'] for r in dataset.records()], ['TEMP-NEW-900', 'TEMP-NEW-900'])
        self.assertEqual(len(dataset.groups()), 1)
        for path in self.paths:
            metadata = self.archive.writer.read_asset(path)
            self.assertEqual(metadata['XMP-fk:SKU'], 'TEMP-NEW-900')
            self.assertTrue(metadata.get('XMP-fk:DecisionId'))
        with self.sku.connect() as c:
            job = dict(c.execute("SELECT * FROM jobs WHERE status IN ('matched','confirmed') LIMIT 1").fetchone())
        self.assertIsNotNone(self.sku._valid(job), '额外字段补写后仍须能沿版本链验证原拍摄作业')

    def ready(self):
        self.capture=ArchiveCaptureService(self.store,self.archive,self.catalog,self.sku,autostart=False)
        self.add('IMG_0001.JPG');label=self.add('IMG_0002.JPG');self.rate(label);self.sku.tick();self.drain()
        for _ in range(30):
            self.capture.tick()
            dataset=self.capture.dataset_for_path(self.paths[0])
            with dataset.connect() as c:
                pending=c.execute("SELECT count(*) FROM tasks WHERE status IN ('queued','running')").fetchone()[0]
            if not pending and len(dataset.groups()):break
            time.sleep(.02)
        return label,dataset

    def finish(self,dataset):
        for _ in range(40):
            self.capture.tick()
            with dataset.connect() as c:
                pending=c.execute("SELECT count(*) FROM tasks WHERE status IN ('queued','running')").fetchone()[0]
                failed=[dict(r) for r in c.execute("SELECT kind,reason FROM tasks WHERE status='failed'")]
            if not pending:
                self.assertFalse(failed,failed);return
        self.fail('桥接任务未完成')

    def test_archive_correction_syncs_same_source_document_without_other_copy(self):
        import shutil
        label,dataset=self.ready()
        other=self.final/'副本';other.mkdir()
        target=other/self.paths[0].name;shutil.copy2(self.paths[0],target)
        sid=self.store.ensure_live_session(other)
        original=self.store.records(self.sid)[0]
        self.store.append_live(sid,[original]);self.archive.ensure(sid,original['id'],inherit=False)
        before=target.read_bytes()
        with self.catalog.connect() as c:item=c.execute('SELECT id FROM items WHERE path=?',(str(self.paths[0]),)).fetchone()[0]
        operation=self.capture.confirm(item,'LC777777')['operation_id'];self.finish(dataset)
        with self.sku.connect() as c:self.assertEqual(c.execute('SELECT sku FROM ocr_choices WHERE session_id=? AND capture_id=?',(self.sid,label)).fetchone()[0],'LC777777')
        self.sku.tick();self.capture.tick()
        self.assertEqual(target.read_bytes(),before)
        asset=self.archive.asset(self.archive.ensure(self.sid,original['id']))
        self.assertEqual(self.capture.view_selection(self.sid,[asset])[0]['sku'],'LC777777')
        self.capture.undo(self.catalog.tree()['roots'][0]['id'],'',operation);self.finish(dataset)
        with self.sku.connect() as c:self.assertEqual(c.execute('SELECT sku FROM ocr_choices WHERE session_id=? AND capture_id=?',(self.sid,label)).fetchone()[0],'TEMP-NEW-900')

    def test_undo_initial_confirmation_does_not_auto_resurrect_source_choice(self):
        label,dataset=self.ready()
        with dataset.connect() as c:operation=c.execute("SELECT id FROM history WHERE kind='live_confirm' ORDER BY created DESC LIMIT 1").fetchone()[0]
        root=next(r['id'] for r in self.catalog.tree()['roots'] if r['path']==str(self.live))
        self.capture.undo(root,'',operation);self.finish(dataset)
        self.sku.tick();self.capture.tick()
        with self.sku.connect() as c:self.assertIsNone(c.execute('SELECT sku FROM ocr_choices WHERE session_id=? AND capture_id=?',(self.sid,label)).fetchone())
        self.assertFalse(any(r.get('sku') for r in dataset.records()))

    def test_native_queue_is_merged_and_source_freezes_without_waiting_all_writes(self):
        self._native_queue_freeze(False)

    def test_archive_executes_while_native_metadata_writes_are_pending(self):
        self._native_queue_freeze(True)

    def _native_queue_freeze(self,execute):
        from folder_archive_jobs import FolderArchiveJobs
        from metadata_writer import sha
        self.capture=ArchiveCaptureService(self.store,self.archive,self.catalog,self.sku,autostart=False)
        self.add('IMG_0001.JPG');label=self.add('IMG_0002.JPG');self.rate(label);self.sku.tick()
        for _ in range(20):
            self.capture.tick()
            dataset=self.capture.dataset_for_path(self.paths[0])
            if dataset and len(dataset.groups()):break
            time.sleep(.02)
        before={str(path):sha(path) for path in self.paths}
        with self.archive.connect() as c:self.assertTrue(c.execute("SELECT 1 FROM metadata_jobs WHERE status='queued'").fetchone())
        with dataset.connect() as c:self.assertTrue(c.execute("SELECT 1 FROM tasks WHERE status='waiting_native'").fetchone())
        until=time.monotonic()+10
        while time.monotonic()<until:
            with self.catalog.connect() as c:root=c.execute('SELECT id FROM roots WHERE path=?',(str(self.live),)).fetchone()
            if root:break
            time.sleep(.02)
        jobs=FolderArchiveJobs(self.store,self.archive,self.catalog)
        try:
            job=jobs.prepare({'root_id':root['id'],'folder':'','route':'folders','target_path':str(self.final),'preprocess':True})
            until=time.monotonic()+20
            while time.monotonic()<until:
                state=jobs.state(job['id'])
                if state['status']=='ready':break
                if state['status']=='failed':self.fail(state.get('error'))
                time.sleep(.03)
            self.assertEqual(state['status'],'ready',state)
            self.archive.process_one();self.capture.tick()
            self.assertEqual({str(path):sha(path) for path in self.paths},before)
            with self.assertRaisesRegex(ValueError,'归档计划'):
                with self.catalog.connect() as c:item=c.execute('SELECT id FROM items WHERE path=?',(str(self.paths[0]),)).fetchone()[0]
                self.capture.confirm(item,'LC999999')
            if execute:
                jobs.start(job['id'])
                until=time.monotonic()+30
                while time.monotonic()<until:
                    state=jobs.state(job['id'])
                    if state['status'] in ('completed','failed'):break
                    time.sleep(.03)
                self.assertEqual(state['status'],'completed',state)
                target=self.capture.dataset_for_path(self.final)
                self.assertEqual(len([r for r in target.records() if r.get('sku')]),2)
                for r in target.records():
                    self.assertEqual(self.archive.writer.read_asset(target.root/r['relative_path'])['XMP-fk:SKU'],'TEMP-NEW-900')
            else:jobs.cancel(job['id'])
            self.assertFalse(dataset.meta('archive_freeze'))
            self.drain();self.finish(dataset)
            self.assertTrue(all(r.get('write_state')=='verified' for r in dataset.records()))
        finally:jobs.close()

    def test_boundary_and_all_members_are_portable_before_ocr_finishes(self):
        from portable_capture_store import Dataset,DatasetBusy
        self.capture=ArchiveCaptureService(self.store,self.archive,self.catalog,self.sku,autostart=False)
        self.add('IMG_0001.JPG');label=self.add('IMG_0002.JPG');self.rate(label)
        with self.sku.connect() as c:job=dict(c.execute("SELECT * FROM jobs WHERE status='queued' LIMIT 1").fetchone())
        self.archive.start_stream(self.sid,job['project'],job['scope']);self.archive.reserve(job)
        self.capture.tick()
        dataset=self.capture.dataset_for_path(self.paths[0])
        other=Dataset(self.live,self.base/'reader')
        try:
            other.refresh()
            self.assertEqual(len(other.records()),2)
            self.assertEqual(other.groups()[0]['status'],'pending')
            self.assertTrue(any(r.get('boundary') and r.get('boundary_token')==job['id'] for r in other.records()))
            self.assertFalse(any(r.get('sku') for r in other.records()))
            root=next(r['id'] for r in self.catalog.tree()['roots'] if r['path']==str(self.live))
            self.capture.edit(root,enabled=False)
            with self.assertRaises(DatasetBusy):self.capture.require_live(self.sid)
            other.acquire()
        finally:other.close()


if __name__ == '__main__':
    unittest.main()
