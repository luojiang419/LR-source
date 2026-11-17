"""历史ready不能冒充实际缓存；释放、外删、配对版本与重启的真实文件回归。"""
from pathlib import Path
from unittest.mock import patch
import unittest

from PIL import Image
from preview_cache import cache_for,cache_name,PreviewWarmer
import test_archive_decode_pause as fixture


class ArchiveCacheCoverageTests(unittest.TestCase):
    setUp=fixture.ArchiveDecodePauseTests.setUp
    tearDown=fixture.ArchiveDecodePauseTests.tearDown
    state=fixture.ArchiveDecodePauseTests.state

    def cached(self):
        while self.worker.process_one():pass
        self.assertEqual(self.state()['ready'],4)
        return cache_for(self.catalog.previews)

    def test_eviction_reduces_actual_ready_without_automatically_requeueing(self):
        index=self.cached()
        with index.lock,index.connect() as c:
            name=c.execute('SELECT name FROM previews WHERE edge=2048 LIMIT 1').fetchone()[0]
            self.assertTrue(index._remove(c,name))
        after=self.state()
        self.assertEqual((after['total'],after['ready'],after['uncached'],after['pending']),(4,3,1,0))
        with self.cache.read_connection() as c:self.assertEqual(c.execute("SELECT count(*) FROM archive_jobs WHERE state='ready'").fetchone()[0],4)
        self.worker.reconcile_archive();self.worker.reconcile_thread.join(.1)
        self.assertFalse(self.worker.process_one())
        self.assertEqual(self.state()['uncached'],1)

    def test_external_file_removal_is_detected_without_a_database_change(self):
        index=self.cached()
        with index.read_connection() as c:row=dict(c.execute('SELECT * FROM previews WHERE edge=420 LIMIT 1').fetchone())
        (index.folder/row['name']).unlink()
        after=self.state();self.assertEqual(after['ready'],3);self.assertEqual(after['thumbnails_ready'],3)
        self.assertEqual(after['uncached'],1)
        with index.read_connection() as c:self.assertTrue(c.execute('SELECT 1 FROM previews WHERE name=?',(row['name'],)).fetchone())

    def test_current_pair_is_not_counted_twice_by_obsolete_historical_jobs(self):
        self.cached()
        with self.cache.connect() as c:
            row=dict(c.execute('SELECT * FROM archive_jobs LIMIT 1').fetchone())
            c.execute('''INSERT INTO archive_jobs(identity,root_id,pair_key,record,state,small_ready)
                         VALUES (?,?,?,?,?,?)''',('obsolete:'+row['identity'],row['root_id'],row['pair_key'],row['record'],'ready',1))
        self.assertEqual(self.state()['total'],4)
        self.assertEqual(self.state()['ready'],4)

    def test_size_corruption_is_rechecked_even_if_directory_time_does_not_change(self):
        index=self.cached()
        with index.read_connection() as c:name=c.execute('SELECT name FROM previews WHERE edge=2048 LIMIT 1').fetchone()[0]
        (index.folder/name).write_bytes(b'broken')
        self.worker.coverage.last_check=0
        self.assertEqual((self.state()['ready'],self.state()['uncached']),(3,1))

    def test_old_pair_representatives_and_old_versions_are_not_extra_materials(self):
        self.cached()
        raw=self.base/'photos/0.CR3';raw.write_bytes(b'raw-pair-placeholder')
        self.catalog.scan(self.root)
        while self.worker.process_one():pass
        self.assertEqual((self.state()['total'],self.state()['ready']),(4,4))
        with self.cache.read_connection() as c:self.assertEqual(c.execute("SELECT count(*) FROM archive_jobs WHERE state='ready'").fetchone()[0],5)
        raw.unlink();self.catalog.scan(self.root)
        self.catalog.queue_previews(self.catalog.pending_preview_changes())
        self.assertEqual((self.state()['total'],self.state()['ready']),(4,4))

    def test_restart_preserves_cache_missing_status_and_reuses_surviving_files(self):
        index=self.cached()
        with index.lock,index.connect() as c:
            name=c.execute('SELECT name FROM previews WHERE edge=2048 LIMIT 1').fetchone()[0];index._remove(c,name)
        files={p.name:p.stat().st_mtime_ns for p in index.folder.glob('*.jpg')}
        self.worker.close();self.worker=PreviewWarmer(self.cache,'unused',catalog=self.catalog,autostart=False)
        self.catalog.on_previews=self.worker.enqueue
        self.worker.reconcile_archive()
        self.worker.reconcile_thread.join(.2)
        self.assertEqual((self.state()['ready'],self.state()['uncached'],self.state()['pending']),(3,1,0))
        self.assertEqual(files,{p.name:p.stat().st_mtime_ns for p in index.folder.glob('*.jpg')})

    def test_unchanged_snapshot_avoids_reenumerating_files(self):
        self.cached()
        with patch('archive_cache_coverage.os.scandir',side_effect=AssertionError('版本未变化不得重复扫磁盘')):
            self.assertEqual(self.state()['ready'],4)

    def test_statistics_never_open_business_database_write_connections(self):
        self.cached()
        with patch.object(self.catalog,'connect',side_effect=AssertionError('统计不应打开业务写连接')):
            self.assertEqual(self.worker.archive_state()['ready'],4)

    def test_changed_source_is_not_counted_using_an_old_cached_version(self):
        self.cached();self.catalog.on_previews=None
        Image.new('RGB',(80,60),'green').save(self.base/'photos/0.jpg')
        self.catalog.scan(self.root)
        value=self.state();self.assertEqual((value['ready'],value['pending']),(3,1))
        self.catalog.on_previews=self.worker.enqueue
        self.catalog.queue_previews(self.catalog.pending_preview_changes())
        self.assertEqual(self.state()['ready'],3)
        self.assertTrue(self.worker.process_one());self.assertEqual(self.state()['ready'],4)

    def test_released_cache_is_regenerated_only_after_explicit_preview_request(self):
        index=self.cached()
        with index.lock,index.connect() as c:
            row=dict(c.execute('SELECT * FROM previews WHERE edge=2048 LIMIT 1').fetchone());index._remove(c,row['name'])
        self.assertEqual(self.state()['uncached'],1)
        import json
        with self.cache.read_connection() as c:
            record=next(json.loads(r[0]) for r in c.execute('SELECT record FROM archive_jobs') if json.loads(r[0])['fingerprint']==row['fingerprint'])
        self.worker.enqueue(record['_source_root'],[record]);self.assertTrue(self.worker.process_one())
        self.assertEqual((self.state()['ready'],self.state()['uncached']),(4,0))


if __name__=='__main__':unittest.main()
