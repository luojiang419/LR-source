"""大目录分页、跨批分组/评级及局部保存保护。"""
from datetime import datetime,timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from catalog_store import Store,stable_json
from session_browser import count,patch_state,read


class SessionBrowserTests(unittest.TestCase):
    def test_rating_seed_checkpoint_survives_restart_and_invalidates_changed_paths(self):
        from asset_archive import AssetArchive
        from asset_catalog import pair_key
        self.store.save(self.sid,0,{'overrides':{'P00003':4}})
        archive=AssetArchive(self.store,autostart=False);self.addCleanup(archive.close)
        archive.ratings.seed_session(self.sid)
        resumed=AssetArchive(self.store,autostart=False);self.addCleanup(resumed.close)
        with patch.object(self.store,'iter_records',side_effect=AssertionError('未变化的历史评级不能每次启动全量恢复')):
            resumed.ratings.seed_session(self.sid)
        self.assertEqual(resumed.ratings.get([self.photos/'P00003.JPG']),4)
        changed=dict(self.records[3],jpg='新位置/P00003.JPG')
        with self.store.connect() as c:c.execute('UPDATE captures SET data=? WHERE session=? AND id=?',(stable_json(changed),self.sid,'P00003'))
        resumed.ratings.seed_session(self.sid)
        with resumed.connect() as c:
            paths=[r[0] for r in c.execute('SELECT path_key FROM rating_capture_paths WHERE session=? AND capture=?',(self.sid,'P00003'))]
        self.assertEqual(paths,[pair_key(self.photos/changed['jpg'])])
        self.assertEqual(resumed.ratings.get([self.photos/changed['jpg']]),4)
        resumed.ratings.invalidate_seeds()
        with patch.object(resumed.ratings,'_seed_large',wraps=resumed.ratings._seed_large) as seed:
            resumed.ratings.seed_session(self.sid);seed.assert_called_once()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.photos=self.root/'photos';self.photos.mkdir()
        self.store=Store(self.root/'data',Path(__file__).resolve().parents[1]);self.sid=self.store.ensure_live_session(self.photos)
        stamp=datetime(2026,10,23,10)
        self.records=[{'id':f'P{i:05d}','time':(stamp+timedelta(seconds=i)).isoformat(),'cameraKey':'camera','camera':'camera',
            'missingTime':False,'raw':None,'jpg':('子目录/' if i>=2400 else '')+f'P{i:05d}.JPG','existingRating':0,'fileCount':1,'fingerprint':f'hash-{i}','name':f'P{i:05d}'} for i in range(2500)]
        with self.store.connect() as db:db.executemany('INSERT INTO captures VALUES (?,?,?,?)',[(self.sid,r['id'],stable_json(r),i) for i,r in enumerate(self.records)])

    def test_first_page_and_last_page_are_bounded_without_false_tail_recommendation(self):
        original=self.store.records;calls=[]
        def bounded(sid=None,ids=None):
            self.assertIsNotNone(ids,'分页不能解码全部照片 JSON');calls.append(len(ids));return original(sid,ids)
        with patch.object(self.store,'records',side_effect=bounded):
            session,rows=read(self.store,self.sid)
            self.assertEqual(len(rows),240);self.assertEqual(session['browser']['total'],2500);self.assertEqual(session['browser']['picked'],1)
            self.assertTrue(all(r['liveRating']==0 for r in rows));self.assertEqual(rows[-1]['browserGroupSize'],2500)
            session,rows=read(self.store,self.sid,{'offset':2400})
            self.assertEqual(len(rows),100);self.assertEqual(rows[-1]['id'],'P02499');self.assertEqual(rows[-1]['liveRating'],1)
            self.assertEqual(rows[0]['browserGroup'],1);self.assertAlmostEqual(rows[0]['browserGap'],1,places=2)
        self.assertTrue(all(n<=240 for n in calls))

    def test_search_sort_filter_and_counts_cover_the_entire_directory(self):
        with self.store.connect() as db:db.execute('UPDATE sessions SET state=? WHERE id=?',(stable_json({'overrides':{'P00003':4,'P02490':5}}),self.sid))
        session,rows=read(self.store,self.sid,{'sort':'rating-desc'})
        self.assertEqual([r['id'] for r in rows[:2]],['P02490','P00003']);self.assertEqual(session['browser']['picked'],3)
        session,rows=read(self.store,self.sid,{'filter':'picked'})
        self.assertEqual(session['browser']['matched'],3);self.assertEqual(len(rows),3)
        session,rows=read(self.store,self.sid,{'q':'P02490'})
        self.assertEqual([r['id'] for r in rows],['P02490']);self.assertEqual(session['browser']['total'],2500)
        session,rows=read(self.store,self.sid,{'folder':'子目录'})
        self.assertEqual(len(rows),100);self.assertEqual(session['browser']['matched'],100)

    def test_cross_page_patch_preserves_unseen_ratings_and_checks_revision(self):
        _,state,revision=patch_state(self.store,self.sid,0,{'overrides':{'P00003':4},'skuMarkers':{'P00003':{'scope':'细节图'}}})
        _,state,revision=patch_state(self.store,self.sid,revision,{'overrides':{'P02490':5}})
        self.assertEqual(state['overrides'],{'P00003':4,'P02490':5});self.assertIn('P00003',state['skuMarkers'])
        with self.assertRaisesRegex(ValueError,'其他窗口'):patch_state(self.store,self.sid,0,{'overrides':{'P02490':0}})
        self.assertEqual(self.store.session(self.sid)['state']['overrides']['P02490'],5)
        _,state,_=patch_state(self.store,self.sid,revision,{'overrides':{'P00003':None}})
        self.assertEqual(state['overrides'],{'P02490':5})

    def test_around_locator_and_saved_selection_resume_in_correct_page(self):
        session,rows=read(self.store,self.sid,{'around':'P02490'})
        self.assertEqual(session['browser']['offset'],2400);self.assertIn('P02490',{r['id'] for r in rows})
        patch_state(self.store,self.sid,0,{'selectedId':'P02490'})
        session,_=read(self.store,self.sid);self.assertEqual(session['browser']['offset'],2400)

    def test_compare_tail_reads_real_group_end_instead_of_page_end(self):
        session,rows=read(self.store,self.sid,{'tail_capture':'P00003'})
        self.assertEqual([r['id'] for r in rows],['P02498','P02499'])

    def test_incremental_append_keeps_pair_identity_and_full_snapshot(self):
        first=dict(self.records[3],raw='P00003.CR3',fingerprint='paired',fileCount=2)
        result=self.store.append_live(self.sid,[first]);self.assertFalse(result['added']);self.assertEqual(count(self.store,self.sid),2500)
        import hashlib
        records=self.store.records(self.sid)
        expected=hashlib.sha256(stable_json([(r['id'],r['fingerprint'],r['time']) for r in records]).encode()).hexdigest()
        self.assertEqual(self.store.session(self.sid)['snapshot'],expected)

    def test_index_tracks_direct_inserts_deletes_and_changed_time(self):
        read(self.store,self.sid)
        with self.store.connect() as db:
            db.execute('DELETE FROM captures WHERE session=? AND id=?',(self.sid,'P00003'))
            changed=dict(self.records[240],missingTime=True)
            db.execute('UPDATE captures SET data=? WHERE session=? AND id=?',(stable_json(changed),self.sid,changed['id']))
        session,rows=read(self.store,self.sid,{'around':'P00240'})
        row=next(r for r in rows if r['id']=='P00240');self.assertGreater(row['browserGroup'],1);self.assertEqual(session['browser']['total'],2499)

    def test_simultaneous_cold_pages_and_group_tail_keep_consistent_groups(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            result=list(pool.map(lambda i:read(self.store,self.sid,{'offset':i*240}),range(4)))
        self.assertEqual([s['browser']['offset'] for s,_ in result],[0,240,480,720])
        self.assertTrue(all(r['browserGroup']==1 for _,rows in result for r in rows))

    def test_shared_asset_rating_cache_matches_related_locations_and_invalidates(self):
        from asset_archive import AssetArchive,uid
        archive=AssetArchive(self.store,autostart=False);self.addCleanup(archive.close)
        archive.ratings.seed_session(self.sid)
        aid,fid=uid(),uid();local=self.photos/'P00003.JPG';alias=self.root/'other/P00003.JPG'
        with archive.connect() as db:
            db.execute('INSERT INTO assets(id,created) VALUES (?,0)',(aid,))
            db.execute('INSERT INTO files(id,asset,component,sha) VALUES (?,?,?,?)',(fid,aid,'jpg','hash'))
            db.execute('INSERT INTO locations VALUES (?,?,0,?)',(str(alias),fid,'online'))
        archive.ratings.set([str(alias)],4)
        with archive.connect() as db:db.execute('INSERT INTO catalog_links VALUES (?,?,?)',(self.sid,'P00003',aid))
        session,rows=read(self.store,self.sid,{'filter':'picked'},archive)
        self.assertEqual(next(r for r in rows if r['id']=='P00003')['liveRating'],4)
        from asset_catalog import pair_key
        with archive.connect() as db:
            db.execute('DELETE FROM locations WHERE path=?',(str(alias),))
            db.execute('INSERT INTO catalog_links VALUES (?,?,?)',('old-session','old-capture',aid))
            db.execute('INSERT INTO rating_capture_paths VALUES (?,?,?)',('old-session','old-capture',pair_key(str(alias))))
        _,rows=read(self.store,self.sid,{'filter':'picked'},archive)
        self.assertEqual(next(r for r in rows if r['id']=='P00003')['liveRating'],4)
        archive.ratings.set([str(alias)],5)
        session,rows=read(self.store,self.sid,{'sort':'rating-desc'},archive)
        self.assertEqual(rows[0]['id'],'P00003');self.assertEqual(rows[0]['liveRating'],5)
        session,_=read(self.store,self.sid,archive=archive);self.assertEqual(session['browser']['picked'],2)

    def test_rule_reset_is_global_even_when_other_page_was_not_loaded(self):
        with self.store.connect() as db:db.execute('UPDATE sessions SET state=? WHERE id=?',(stable_json({'protectManual':False,'overrides':{'P00003':4,'P02490':5}}),self.sid))
        _,state,_=patch_state(self.store,self.sid,0,{'threshold':8})
        self.assertEqual(state['overrides'],{})

    def test_current_page_labels_do_not_compute_full_photo_intervals(self):
        from asset_archive import AssetArchive,uid
        from types import SimpleNamespace
        from sku_library import labels
        archive=AssetArchive(self.store,autostart=False);self.addCleanup(archive.close)
        aid=uid()
        with archive.connect() as db:
            db.execute('INSERT INTO assets(id,created,sku) VALUES (?,0,?)',(aid,'TEST-123'))
            db.execute('INSERT INTO catalog_links VALUES (?,?,?)',(self.sid,'P00003',aid))
        with patch('sku_library.interval_rows',side_effect=AssertionError('分页标签不得完整展开区间')):
            result=labels(SimpleNamespace(archive=archive),self.sid,['P00003','P02490'])
            self.assertEqual(result['labels'],{'P00003':'TEST-123'})
            self.assertEqual(labels(SimpleNamespace(archive=archive),self.sid,[])['labels'],{})

    def test_only_rating_changes_queue_scoped_raw_work(self):
        import server
        from unittest.mock import Mock
        raw=Mock()
        with patch.object(server,'STORE',self.store),patch.object(server,'ARCHIVE',None),patch.object(server,'SKU',None),patch.object(server,'RAWLINK',raw):
            first=server.save_session_state({'session_id':self.sid,'revision':0,'changes':{'selectedId':'P00003'}},partial=True)
            raw.request.assert_not_called()
            server.save_session_state({'session_id':self.sid,'revision':first['revision'],'changes':{'overrides':{'P00003':4}}},partial=True)
            raw.request.assert_called_once_with(self.sid,capture_ids=['P00003'])

    def test_scoped_raw_jobs_merge_resume_and_keep_full_discovery_priority(self):
        from asset_archive import AssetArchive
        from raw_reconcile import RawReconcile
        archive=AssetArchive(self.store,autostart=False);self.addCleanup(archive.close)
        raw=RawReconcile(archive,autostart=False)
        raw.request(self.sid,capture_ids=['P00003']);raw.request(self.sid,capture_ids=['P00240']);raw.close()
        resumed=RawReconcile(archive,autostart=False);self.addCleanup(resumed.close)
        with patch.object(resumed,'process_session') as process:
            resumed.tick();process.assert_called_once_with(self.sid,['P00003','P00240'])
        resumed.request(self.sid,discover=True);resumed.request(self.sid,capture_ids=['P00003'])
        with patch.object(resumed,'process_session') as process,patch.object(resumed,'discover_raws') as discover:
            resumed.tick();discover.assert_called_once_with(self.sid);process.assert_called_once_with(self.sid,None)

    def test_scoped_source_index_never_reads_whole_catalog(self):
        from asset_archive import AssetArchive
        from raw_reconcile import RawReconcile
        archive=AssetArchive(self.store,autostart=False);self.addCleanup(archive.close);archive.ratings.seed_session(self.sid)
        raw=RawReconcile(archive,autostart=False);self.addCleanup(raw.close)
        original=self.store.records
        def bounded(sid=None,ids=None):
            self.assertIsNotNone(ids);return original(sid,ids)
        with patch.object(self.store,'records',side_effect=bounded),patch.object(raw,'enrich',side_effect=lambda sid,records:records):
            raw.request(self.sid,capture_ids=['P00003']);raw.tick()
        with archive.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM raw_sources').fetchone()[0],1)

    def test_view_preference_revision_does_not_reseed_large_rating_index(self):
        from asset_archive import AssetArchive
        archive=AssetArchive(self.store,autostart=False);self.addCleanup(archive.close)
        archive.ratings.seed_session(self.sid);patch_state(self.store,self.sid,0,{'selectedId':'P00003'})
        with patch.object(self.store,'iter_records',side_effect=AssertionError('偏好保存不应重新登记所有评级')):archive.ratings.seed_session(self.sid)

    def test_changed_photo_path_reseeds_rating_index_without_snapshot_change(self):
        from asset_archive import AssetArchive
        from asset_catalog import pair_key
        archive=AssetArchive(self.store,autostart=False);self.addCleanup(archive.close)
        archive.ratings.seed_session(self.sid)
        record=dict(self.records[3],jpg='新位置/P00003.JPG')
        with self.store.connect() as db:db.execute('UPDATE captures SET data=? WHERE session=? AND id=?',(stable_json(record),self.sid,record['id']))
        archive.ratings.seed_session(self.sid)
        with archive.connect() as db:paths=[r[0] for r in db.execute('SELECT path_key FROM rating_capture_paths WHERE session=? AND capture=?',(self.sid,record['id']))]
        self.assertEqual(paths,[pair_key(self.photos/record['jpg'])])

    def test_naive_local_and_explicit_timezone_times_have_same_sort_epoch(self):
        from datetime import timezone
        local=datetime.now().replace(microsecond=0)
        aware=local.astimezone(timezone(timedelta(hours=-6))).isoformat()
        with self.store.connect() as db:
            for i,value in enumerate((local.isoformat(),aware)):
                record=dict(self.records[i],time=value)
                db.execute('UPDATE captures SET data=? WHERE session=? AND id=?',(stable_json(record),self.sid,record['id']))
            epochs=[r[0] for r in db.execute('SELECT epoch FROM browser_captures WHERE session=? AND id IN (?,?) ORDER BY id',(self.sid,'P00000','P00001'))]
        self.assertAlmostEqual(epochs[0],epochs[1],places=2);self.assertAlmostEqual(epochs[0],local.timestamp(),places=2)


if __name__=='__main__':unittest.main()
