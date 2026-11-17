"""归档添加后无人浏览也自动生成；验证恢复、失败上限和本地尺寸复用。"""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
from asset_catalog import AssetCatalog,_id
import media_engine as media
from preview_cache import PreviewWarmer,cache_for,cache_name,WARM_WORKERS
from reference_library import ReferenceLibrary


class ArchiveBackgroundPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.photos=self.base/'photos';self.photos.mkdir()
        for number in range(8):
            folder=self.photos/f'未访问/{number%3}/更深目录';folder.mkdir(parents=True,exist_ok=True)
            Image.new('RGB',(1600,1000),(number*20,90,120)).save(folder/f'{number}.jpg')
        self.catalog=AssetCatalog(self.base/'data');self.cache=cache_for(self.base/'data/previews')
        self.worker=PreviewWarmer(self.cache,'unused',catalog=self.catalog)
        self.catalog.on_previews=self.worker.enqueue
        self.library=ReferenceLibrary(self.base/'data',warmer=self.worker,catalog=self.catalog)

    def tearDown(self):
        self.library.close();self.worker.close();self.catalog.close();self.temp.cleanup()

    def wait(self,condition):
        until=time.monotonic()+20
        while not condition():
            if time.monotonic()>until:self.fail('后台预览未自动完成')
            time.sleep(.02)

    def add(self):
        root=self.library.add_root(self.photos)['root_id']
        self.wait(lambda:self.library._root(root)['status']=='ready')
        return root

    def test_unvisited_subfolders_complete_in_parallel_without_full_source_hash(self):
        active=peak=0;target=min(4,WARM_WORKERS);lock=threading.Lock();overlap=threading.Event();original=media._jpeg_source
        def render(*args):
            nonlocal active,peak
            with lock:
                active+=1;peak=max(peak,active)
                if active>=target:overlap.set()
            try:
                if not overlap.wait(5):raise AssertionError('后台解码没有实际并行')
                return original(*args)
            finally:
                with lock:active-=1
        with patch.object(media,'_jpeg_source',side_effect=render),patch.object(type(self.cache),'content_key',side_effect=AssertionError('不应完整读取原片哈希')):
            root=self.add()
            self.wait(lambda:self.worker.archive_state([root])['ready']==8)
        state=self.worker.archive_state([root]);self.assertGreaterEqual(peak,target)
        self.assertEqual((state['total'],state['thumbnails_ready'],state['pending'],state['failed']),(8,8,0,0))
        # 无一次 items/preview 请求，两个尺寸都由生产队列生成。
        with cache_for(self.catalog.previews).connect() as c:
            self.assertEqual({r[0] for r in c.execute('SELECT DISTINCT edge FROM previews')},{420,2048})
        self.worker.reconcile_archive();self.wait(lambda:not self.worker.archive_state([root])['checking'])
        self.assertEqual(self.cache.state()['pending'],0)

    def test_restart_backfills_historical_index_and_intermediate_size_uses_local_cache(self):
        self.library.warmer=None;self.catalog.on_previews=None
        root=self.add();self.assertEqual(self.cache.state()['pending'],0)
        self.catalog.on_previews=self.worker.enqueue
        self.worker.reconcile_archive();self.wait(lambda:self.worker.archive_state([root])['ready']==8)
        with self.catalog.connect() as c:row=dict(c.execute('SELECT * FROM items LIMIT 1').fetchone())
        Path(row['path']).unlink()
        with patch.object(media,'_companion_preview',side_effect=AssertionError('不应重读 NAS')):
            payload=self.catalog.preview(row['id'],900,read=True)
        import io
        with Image.open(io.BytesIO(payload)) as image:self.assertEqual(max(image.size),900)

    @unittest.skipUnless(WARM_WORKERS>12,'本机硬件预算不足以验证超过 12 路')
    def test_hardware_capacity_really_decodes_more_than_twelve_files_together(self):
        target=min(16,WARM_WORKERS);barrier=threading.Barrier(target,timeout=10)
        for index in range(8,target):Image.new('RGB',(320,200),'green').save(self.photos/f'extra-{index}.jpg')
        original=media._jpeg_source;active=peak=0;lock=threading.Lock()
        def render(*args):
            nonlocal active,peak
            with lock:active+=1;peak=max(peak,active)
            try:
                barrier.wait()
                return original(*args)
            finally:
                with lock:active-=1
        with patch.object(media,'_jpeg_source',side_effect=render):
            root=self.add();self.wait(lambda:self.worker.archive_state([root])['ready']==target)
        state=self.worker.archive_state([root])
        self.assertGreaterEqual(peak,target)
        self.assertEqual((state['workers'],state['ready'],state['failed']),(WARM_WORKERS,target,0))

    def test_incremental_write_schedules_new_file_without_folder_visit(self):
        root=self.add();self.wait(lambda:self.worker.archive_state([root])['ready']==8)
        path=self.photos/'未访问/new.jpg';Image.new('RGB',(64,48),'blue').save(path)
        self.library._index_paths(root,[str(path)])
        self.wait(lambda:self.worker.archive_state([root])['ready']==9)
        path.unlink()
        with self.catalog.connect() as c:c.execute('UPDATE items SET online=0 WHERE path=?',(str(path),))
        self.assertEqual(self.worker.archive_state([root])['total'],8)

    def test_unchanged_rescan_skips_queue_and_changed_file_requeues_only_its_pair(self):
        root=self.add();self.wait(lambda:self.worker.archive_state([root])['ready']==8)
        with patch.object(self.catalog,'queue_previews') as enqueue:
            self.catalog.scan(root)
            enqueue.assert_not_called()
            changed=self.photos/'未访问/1/更深目录/1.jpg'
            Image.new('RGB',(1800,1000),'blue').save(changed)
            self.catalog.scan(root)
            enqueue.assert_called_once_with({self.catalog.item(_id(str(changed).casefold()))['pair_key']})

    def test_restart_restores_pending_jobs_without_rescanning_ready_assets(self):
        root=self.add();self.wait(lambda:self.worker.archive_state([root])['ready']==8)
        self.worker.close();self.worker=PreviewWarmer(self.cache,'unused',catalog=self.catalog,autostart=False)
        self.library.warmer=self.worker;self.catalog.on_previews=self.worker.enqueue
        with patch.object(self.catalog,'queue_all_previews',side_effect=AssertionError('启动不应重扫已完成归档')):
            self.worker.reconcile_archive();self.wait(lambda:not self.worker.archive_state([root])['checking'])
        self.assertEqual((self.worker.archive_state([root])['ready'],self.worker.archive_state([root])['pending']),(8,0))

    def test_multiple_shared_roots_all_index_generate_and_readd_after_removal(self):
        first=self.add();others=[]
        for index in range(3):
            folder=self.base/f'extra-root-{index}';folder.mkdir()
            Image.new('RGB',(320,200),'green').save(folder/f'second-{index}.jpg')
            root=self.library.add_root(folder)['root_id'];others.append((root,folder))
        self.wait(lambda:all(self.library._root(root)['status']=='ready' for root,_ in others))
        roots=[first,*[root for root,_ in others]]
        self.wait(lambda:self.worker.archive_state(roots)['ready']==11)
        self.assertEqual(self.catalog.state()['counts']['total'],11)
        self.assertEqual(len({self.library.catalog(root).db for root in roots}),1)
        removed,folder=others[1];self.library.remove_root(removed)
        self.assertEqual(self.worker.archive_state(roots)['total'],10)
        self.library.add_root(folder)
        self.wait(lambda:self.library._root(removed)['status']=='ready')
        self.wait(lambda:self.worker.archive_state(roots)['ready']==11)
        self.assertEqual(self.catalog.state()['counts']['total'],11)

    def test_failures_stop_after_three_attempts_and_survive_reconciliation(self):
        self.worker.close();self.worker=PreviewWarmer(self.cache,'unused',catalog=self.catalog,autostart=False)
        self.library.warmer=self.worker;self.catalog.on_previews=self.worker.enqueue
        root=self.add()
        with patch.object(self.catalog,'render_preview_batch',side_effect=RuntimeError('损坏文件')) as render:
            while self.worker.process_one():pass
            self.assertEqual(render.call_count,24)
            self.catalog.queue_all_previews()
            self.assertFalse(self.worker.process_one())
        self.assertEqual(self.worker.archive_state([root])['failed'],8)
        self.assertIsNone(self.worker.archive_state([root])['decode_rate'])

    def test_removed_root_cancels_pending_without_decoding_originals(self):
        self.worker.close();self.worker=PreviewWarmer(self.cache,'unused',catalog=self.catalog,autostart=False)
        self.library.warmer=self.worker;self.catalog.on_previews=self.worker.enqueue
        root=self.add();self.library.remove_root(root)
        with patch.object(self.catalog,'render_preview_batch',side_effect=AssertionError('已移除位置不应继续读取')) as render:
            while self.worker.process_one():pass
            self.assertEqual(render.call_count,0)
        self.assertEqual(self.worker.archive_state([root])['total'],0)

    def test_rescan_keeps_unvisited_online_assets_and_restart_backfills_them(self):
        self.worker.close();self.worker=PreviewWarmer(self.cache,'unused',catalog=self.catalog,autostart=False)
        self.library.warmer=self.worker;self.catalog.on_previews=self.worker.enqueue
        root=self.add()
        # NAS 完整扫描尚未走到旧素材；在线状态没有变化，任务不能被取消。
        with self.catalog.connect() as c:
            c.execute("UPDATE roots SET scan_epoch=scan_epoch+1,status='scanning' WHERE id=?",(root,))
        with self.cache.connect() as c:
            c.execute("UPDATE archive_jobs SET state='cancelled'")
            c.execute('DELETE FROM pending')
        self.catalog.queue_all_previews()
        self.assertEqual(self.worker.archive_state([root])['pending'],8)
        while self.worker.process_one():pass
        self.assertEqual(self.worker.archive_state([root])['ready'],8)
        with self.cache.connect() as c:
            c.execute('DELETE FROM archive_jobs');c.execute('DELETE FROM pending')
        self.catalog.queue_all_previews()
        self.assertEqual(self.worker.archive_state([root])['ready'],8)

    def test_progress_reads_while_cache_write_transaction_is_open(self):
        root=self.add();self.wait(lambda:self.worker.archive_state([root])['ready']==8)
        self.worker.close()
        with self.cache.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            with patch.object(self.cache,'connect',side_effect=AssertionError('进度不应走写连接')):
                self.assertEqual(self.worker.archive_state([root])['ready'],8)

    def test_cached_raw_orientation_does_not_wait_for_queue_writer(self):
        root=self.add();self.wait(lambda:self.worker.archive_state([root])['ready']==8);self.worker.close()
        row=self.catalog.items()['rows'][0]
        members=[row,{**row,'id':'raw-fixture','extension':'.cr3','path':str(Path(row['path']).with_suffix('.CR3')),'fingerprint':'raw-fixture'}]
        _,record=self.catalog.preview_record(row,members);record['orientation']=6
        index=cache_for(self.catalog.previews);temp=index.folder/'orientation.tmp'
        Image.new('RGB',(48,32),'blue').save(temp,'JPEG')
        index.publish(temp,index.folder/cache_name(record,420),record,420,False);temp.unlink(missing_ok=True)
        held=threading.Event();release=threading.Event()
        def writer():
            with index.lock,index.connect() as c:c.execute('BEGIN IMMEDIATE');held.set();release.wait(3)
        def read():
            _,value=self.catalog.preview_record(row,members)
            self.assertEqual(value['orientation'],6);value.pop('orientation')
            self.catalog.prepare_preview_record(value,'unused',cached_only=True)
            return value['orientation']
        with ThreadPoolExecutor(2) as pool:
            writing=pool.submit(writer);self.assertTrue(held.wait(1))
            try:self.assertEqual(pool.submit(read).result(timeout=.5),6)
            finally:release.set();writing.result(3)

    def test_excluded_subfolder_is_not_reintroduced_by_scan_or_incremental_write(self):
        root=self.add();self.wait(lambda:self.worker.archive_state([root])['ready']==8)
        folder='未访问/0';paths=list((self.photos/folder).rglob('*.jpg'))
        self.catalog.remove(root,folder)
        records=[{'path':str(path),'size':path.stat().st_size,'modified_ns':path.stat().st_mtime_ns} for path in paths]
        self.library.catalog(root).store_records(root,records)
        self.assertEqual(self.library._index_paths(root,[str(path) for path in paths],records),[])
        self.assertEqual(self.worker.archive_state([root])['total'],5)
        with self.catalog.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM items WHERE online=1').fetchone()[0],5)

    def test_restart_continues_large_stage_without_decoding_small_again(self):
        self.worker.close();self.worker=PreviewWarmer(self.cache,'unused',catalog=self.catalog,autostart=False)
        self.library.warmer=self.worker;self.catalog.on_previews=self.worker.enqueue
        root=self.add()
        original=media._preview
        def interrupt(record,root,cache,tool,edge,**kwargs):
            if edge==2048:raise RuntimeError('模拟大图阶段中断')
            return original(record,root,cache,tool,edge,**kwargs)
        with patch.object(media,'_preview',side_effect=interrupt):
            for _ in range(8):self.worker.process_one()
        self.assertEqual(self.worker.archive_state([root])['thumbnails_ready'],8)
        index=cache_for(self.catalog.previews)
        with index.connect() as c:names=[r[0] for r in c.execute('SELECT name FROM previews WHERE edge=420')]
        versions={name:(index.folder/name).stat().st_mtime_ns for name in names}
        self.worker.close()
        self.worker=PreviewWarmer(self.cache,'unused',catalog=self.catalog,autostart=False)
        self.library.warmer=self.worker;self.catalog.on_previews=self.worker.enqueue
        while self.worker.process_one():pass
        self.assertEqual(versions,{name:(index.folder/name).stat().st_mtime_ns for name in names})
        self.assertEqual(self.worker.archive_state([root])['ready'],8)

    def test_each_photo_finishes_both_sizes_before_claiming_the_next(self):
        self.worker.close();self.worker=PreviewWarmer(self.cache,'unused',catalog=self.catalog,autostart=False)
        self.library.warmer=self.worker;self.catalog.on_previews=self.worker.enqueue
        root=self.add()
        for ready in range(1,9):
            self.assertTrue(self.worker.process_one());state=self.worker.archive_state([root])
            self.assertEqual((state['thumbnails_ready'],state['ready']),(ready,ready))
            if ready==1:self.assertIsNone(state['eta_seconds'],'首次完成缺少稳定速度样本')
            elif ready<8:self.assertGreater(state['eta_seconds'],0)
            else:self.assertEqual(state['eta_seconds'],0)
        with cache_for(self.catalog.previews).connect() as c:
            rows=c.execute('SELECT edge,published_seq FROM previews ORDER BY published_seq').fetchall()
        self.assertEqual([row['edge'] for row in rows],[420,2048]*8)

    def test_frame_change_requeues_videos_and_preserves_cached_old_frame(self):
        from test_video_preview import make_clip,pixel
        from video_preview import extract_frame
        self.worker.close();self.worker=PreviewWarmer(self.cache,'unused',catalog=self.catalog,autostart=False)
        self.library.warmer=self.worker;self.catalog.on_previews=self.worker.enqueue
        make_clip(self.photos/'clip.mkv')
        root=self.add()
        with patch('video_preview.extract_frame',wraps=extract_frame) as extract:
            while self.worker.process_one():pass
        # 小图先完成全局可见性，随后独立补齐清晰封面，避免单个视频占住缩略图队列。
        self.assertEqual(extract.call_count,1)
        self.assertEqual(self.worker.archive_state([root])['ready'],9)
        self.catalog.video.configure(4);self.worker.reconcile_archive(videos_only=True)
        self.wait(lambda:not self.worker.archive_state([root])['checking'])
        self.assertEqual(self.worker.archive_state([root])['pending'],1)
        while self.worker.process_one():pass
        with self.catalog.connect() as c:row=c.execute("SELECT id FROM items WHERE media_type='video'").fetchone()
        self.assertGreater(pixel(self.catalog.preview(row[0],420,read=True))[1],240)
        self.assertGreater(pixel(self.catalog.preview(row[0],420,read=True,frame=3))[0],240)
        self.assertEqual(self.worker.archive_state([root])['ready'],9)


class DecodeTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.worker=PreviewWarmer(cache_for(Path(self.temp.name)/'previews'),'unused',autostart=False)

    def tearDown(self):
        self.worker.close();self.temp.cleanup()

    def estimate(self,pending=8,roots=('root',)):
        with patch('preview_cache.time.monotonic',return_value=100):
            return self.worker._decode_timing({'pending':pending},set(roots))

    def test_wall_clock_rate_includes_parallel_work_without_worker_multiplier(self):
        self.worker.decode_samples.extend([('root',90,92),('root',90,100)])
        state=self.estimate()
        self.assertEqual(state['decode_rate'],.2)
        self.assertEqual(state['eta_seconds'],40)

    def test_cache_hits_restart_and_single_completion_do_not_invent_eta(self):
        self.assertIsNone(self.estimate()['eta_seconds'])
        self.worker.decode_samples.append(('root',90,100))
        self.assertIsNone(self.estimate()['eta_seconds'])
        self.assertEqual(self.estimate(0)['eta_seconds'],0)

    def test_old_samples_and_other_roots_do_not_supply_rate(self):
        self.worker.decode_samples.extend([('root',1,20),('root',1,30),('other',90,95),('other',90,100)])
        self.assertIsNone(self.estimate()['eta_seconds'])
        self.assertEqual(self.estimate(roots=('other',))['eta_seconds'],40)
        self.assertEqual(self.estimate(roots=())['eta_seconds'],None)

    def test_foreground_yield_keeps_real_wall_clock_eta(self):
        self.worker.decode_samples.extend([('root',90,92),('root',90,100)])
        self.worker.foreground_active=1
        self.assertTrue(self.estimate()['paused'])
        self.assertEqual(self.estimate()['eta_seconds'],40)
        self.worker.foreground_active=0;self.worker.foreground_until=101
        self.assertEqual(self.estimate()['eta_seconds'],40)
        self.worker.manual_paused=True
        self.assertIsNone(self.estimate()['eta_seconds'])


if __name__=='__main__':unittest.main()
