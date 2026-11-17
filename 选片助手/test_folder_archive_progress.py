"""扫描进度在元数据前可见，NAS不再逐文件resolve，摘要阶段可取消。"""
from pathlib import Path
import tempfile,time,unittest
from unittest.mock import patch
from sku_folder_archive import files_under,manifest_hashes
from metadata_writer import sha

class ArchiveProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        for folder in (self.root,self.root/'sub'):
            folder.mkdir(exist_ok=True)
            for index in range(4):(folder/f'p{index}.JPG').write_bytes(bytes([index]))

    def test_scan_reports_before_entries_and_completes_with_actual_counts(self):
        events=[];calls=[];original=Path.resolve
        def resolve(path,*args,**kwargs):calls.append(path);return original(path,*args,**kwargs)
        with patch.object(Path,'resolve',resolve):result=files_under(self.root,progress=lambda **v:events.append(v),stage='scanning_sources')
        self.assertEqual(len(result),8);self.assertEqual(calls,[self.root])
        self.assertEqual(events[0]['done'],0);self.assertEqual(events[-1]['done'],8)
        self.assertEqual(events[-1]['directories'],2)
        self.assertTrue(all(e['indeterminate'] and e['total']==0 for e in events))
        self.assertTrue(all(path.is_file() for path in result))

    def test_cancel_at_scan_start_prevents_directory_io(self):
        def cancel(**values):raise RuntimeError('已取消')
        with patch('sku_folder_archive.os.scandir',side_effect=AssertionError('取消后不能继续扫描')):
            with self.assertRaisesRegex(RuntimeError,'已取消'):files_under(self.root,progress=cancel)

    def test_scandir_failure_is_not_silently_skipped(self):
        with patch('sku_folder_archive.os.scandir',side_effect=PermissionError('不可读')):
            with self.assertRaises(PermissionError):files_under(self.root,progress=lambda **_:None)

    def test_hash_progress_and_content_remain_real(self):
        files=files_under(self.root);events=[]
        result=manifest_hashes(files,lambda **v:events.append(v),'verifying_source')
        self.assertEqual(result,{str(p):sha(p) for p in files})
        self.assertEqual(events[0]['done'],0);self.assertEqual(events[-1]['done'],8)
        self.assertTrue(all(e['total']==8 and not e['indeterminate'] for e in events))
        self.assertEqual(events[0]['path'],str(files[0]))

    def test_hash_can_be_cancelled_before_reading(self):
        def cancel(**values):raise RuntimeError('已取消')
        with patch('sku_folder_archive.sha',side_effect=AssertionError('不应继续读文件')):
            with self.assertRaisesRegex(RuntimeError,'已取消'):manifest_hashes(files_under(self.root),cancel)

if __name__=='__main__':unittest.main()
