from pathlib import Path
from types import SimpleNamespace
from contextlib import closing
import json,sqlite3,tempfile,threading,unittest
from unittest.mock import patch
from folder_archive_metadata import capture_metadata_batches,signature,metadata_sidecars
from sku_folder_archive import manifest_hashes

class MetadataBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.files=[self.root/f'{i}.JPG' for i in range(4)]
        for file in self.files:file.write_bytes(b'image')
        self.calls=[]
        def request(args):
            paths=[p for p in args if isinstance(p,Path)];self.calls.append(paths)
            return json.dumps([{'SourceFile':str(p),'IFD0:Model':'Camera','XMP-fk:SKU':'LC7877847'} for p in paths]).encode()
        self.writer=SimpleNamespace(data=self.root,_read_request=request)

    def read(self,**options):return list(capture_metadata_batches(self.writer,self.files,**options))

    def test_cache_reuses_unchanged_files_and_reloads_only_changed_version(self):
        first=self.read();calls=len(self.calls);second=self.read()
        self.assertEqual(first,second);self.assertEqual(len(self.calls),calls)
        self.files[1].write_bytes(b'changed image')
        self.read();self.assertEqual(self.calls[-1],[self.files[1]])

    def test_requests_overlap_but_yield_stable_file_order(self):
        original=self.writer._read_request;lock=threading.Lock();both=threading.Event();active=0;peak=0
        def request(args):
            nonlocal active,peak
            with lock:
                active+=1;peak=max(peak,active)
                if active==2:both.set()
            both.wait(2)
            try:return original(args)
            finally:
                with lock:active-=1
        self.writer._read_request=request
        rows=self.read(workers=2,batch_size=1)
        self.assertEqual([p for p,_ in rows],self.files);self.assertEqual(peak,2)

    def test_source_changes_during_read_are_not_cached_as_valid(self):
        original=self.writer._read_request
        def request(args):
            result=original(args);self.files[0].write_bytes(b'changed during reading');return result
        self.writer._read_request=request
        with self.assertRaisesRegex(ValueError,'读取期间来源文件改变'):self.read()

    def test_wrong_source_response_is_rejected(self):
        self.writer._read_request=lambda args:json.dumps([{'SourceFile':'wrong.JPG'} for _ in self.files]).encode()
        with self.assertRaisesRegex(ValueError,'路径不一致'):self.read()

    def test_corrupt_cache_is_refetched(self):
        self.read();before=len(self.calls)
        with closing(sqlite3.connect(self.root/'folder-archive-metadata.sqlite3')) as c,c:c.execute("UPDATE metadata SET payload='invalid json'")
        self.read();self.assertGreater(len(self.calls),before)

    def test_cancel_before_read_starts(self):
        def cancel(**values):raise RuntimeError('已取消')
        with self.assertRaisesRegex(RuntimeError,'已取消'):self.read(progress=cancel)
        self.assertFalse(self.calls)

    def test_hash_refuses_file_changed_after_metadata(self):
        expected={str(p):signature(p) for p in self.files};self.files[0].write_bytes(b'changed')
        with patch('sku_folder_archive.sha',side_effect=AssertionError('变化后不得读取摘要')):
            with self.assertRaisesRegex(ValueError,'元数据读取后文件已改变'):manifest_hashes(self.files,expected_versions=expected)

    def test_sidecar_lookup_enumerates_each_directory_once(self):
        first=self.files[0].with_suffix('.xmp');second=Path(str(self.files[0])+'.xmp')
        first.write_text('xmp');second.write_text('extra')
        import os
        with patch('folder_archive_metadata.os.scandir',wraps=os.scandir) as scan:
            result=metadata_sidecars(self.files)
        scan.assert_called_once_with(self.root)
        self.assertEqual(result[str(self.files[0])],[first,second])
        self.assertTrue(all(result[str(p)]==[] for p in self.files[1:]))

if __name__=='__main__':unittest.main()
