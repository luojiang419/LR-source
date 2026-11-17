from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from metadata_writer import sha
import sku_archive_preprocess as preprocess


class PreprocessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.small=self.root/'小档';self.final=self.root/'大档'
        self.small.mkdir();self.final.mkdir()
        self.jpg=self.small/'原小档.jpg';self.raw=self.final/'原大档.CR3'
        self.jpg.write_bytes(b'jpeg-fixture');self.raw.write_bytes(b'raw-fixture')
        self.side=self.raw.with_suffix('.xmp');self.side.write_bytes(b'xmp-fixture')
        self.db=self.small/'现场.sqlite'
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('CREATE TABLE captures(id TEXT,source TEXT,sku TEXT,fingerprint TEXT)')
            c.execute('INSERT INTO captures VALUES (?,?,?,?)',('1',str(self.jpg),'LC7877847','capture-test'))
        self.ref={'id':'1','source':str(self.jpg),'sku':'LC7877847','fingerprint':'capture-test','input_sha256':sha(self.jpg)}
        self.plan={'preprocess':True,'reference_root':str(self.small),'source':str(self.final),'reference_records':[self.ref],
                   'items':[{'source':str(self.raw),'reference':str(self.jpg),'sku':'LC7877847','input_sha256':sha(self.raw)}],
                   'skipped_reference_records':[],'manifest':{str(self.raw):sha(self.raw)}}

    def test_on_moves_both_roots_with_sidecar_and_updates_database(self):
        result=preprocess.arrange_sources(self.plan,self.db)
        moved_raw=self.final/'LC78/LC7877847/原大档.CR3'
        moved_jpg=self.small/'LC78/LC7877847/原小档.jpg'
        self.assertEqual(moved_raw.read_bytes(),b'raw-fixture')
        self.assertEqual(moved_raw.with_suffix('.xmp').read_bytes(),b'xmp-fixture')
        self.assertEqual(moved_jpg.read_bytes(),b'jpeg-fixture')
        self.assertFalse(self.raw.exists());self.assertFalse(self.jpg.exists())
        with closing(sqlite3.connect(self.db)) as c, c:self.assertEqual(c.execute('SELECT source FROM captures').fetchone()[0],str(moved_jpg))
        self.assertEqual(result['items'][0]['original_source'],str(self.raw))
        self.assertEqual(result['items'][0]['source'],str(moved_raw))

    def test_off_preserves_paths_and_database_bytes(self):
        before=self.db.read_bytes()
        result=preprocess.arrange_sources({**self.plan,'preprocess':False},self.db)
        self.assertFalse(result['preprocess']);self.assertEqual(self.db.read_bytes(),before)
        self.assertTrue(self.jpg.exists());self.assertTrue(self.raw.exists());self.assertTrue(self.side.exists())

    def test_any_destination_collision_blocks_every_move(self):
        conflict=self.final/'LC78/LC7877847/原大档.CR3'
        conflict.parent.mkdir(parents=True);conflict.write_bytes(b'user-content')
        with self.assertRaisesRegex(ValueError,'同名文件'):preprocess.arrange_sources(self.plan,self.db)
        self.assertTrue(self.jpg.exists());self.assertTrue(self.raw.exists())
        self.assertEqual(conflict.read_bytes(),b'user-content')

    def test_move_failure_rolls_back_prior_moves_and_database(self):
        real_move=preprocess.move_no_replace
        count=0
        def fail_once(source,target):
            nonlocal count
            count+=1
            if count==2:raise OSError('模拟第二次移动失败')
            return real_move(source,target)
        with patch.object(preprocess,'move_no_replace',side_effect=fail_once):
            with self.assertRaises(OSError):preprocess.arrange_sources(self.plan,self.db)
        self.assertTrue(self.jpg.exists());self.assertTrue(self.raw.exists());self.assertTrue(self.side.exists())
        with closing(sqlite3.connect(self.db)) as c, c:self.assertEqual(c.execute('SELECT source FROM captures').fetchone()[0],str(self.jpg))
        journal=next(self.small.glob('.归档前处理-*.json'))
        self.assertEqual(json.loads(journal.read_text(encoding='utf8'))['state'],'rolled_back')

    def test_uncommitted_interrupted_move_is_recovered(self):
        after=self.small/'LC78/LC7877847/原小档.jpg';after.parent.mkdir(parents=True)
        self.jpg.rename(after)
        path=self.small/'.归档前处理-中断.json'
        preprocess.save_journal(path,{'state':'prepared','small_root':str(self.small),'final_root':str(self.final),
            'reference_updates':[{'id':'1','before':str(self.jpg),'after':str(after)}],
            'moves':[{'before':str(self.jpg),'after':str(after),'sha256':sha(after)}]})
        preprocess.recover_source_moves(self.db)
        self.assertTrue(self.jpg.exists());self.assertFalse(after.exists())
        self.assertEqual(json.loads(path.read_text(encoding='utf8'))['state'],'rolled_back')

    def test_committed_database_keeps_moved_files_when_final_log_write_was_interrupted(self):
        after=self.small/'LC78/LC7877847/原小档.jpg';after.parent.mkdir(parents=True)
        self.jpg.rename(after)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('UPDATE captures SET source=?',(str(after),))
        path=self.small/'.归档前处理-已提交.json'
        preprocess.save_journal(path,{'state':'moved_pending_database','small_root':str(self.small),'final_root':str(self.final),
            'reference_updates':[{'id':'1','before':str(self.jpg),'after':str(after)}],
            'moves':[{'before':str(self.jpg),'after':str(after),'sha256':sha(after)}]})
        preprocess.recover_source_moves(self.db)
        self.assertTrue(after.exists());self.assertFalse(self.jpg.exists())
        self.assertEqual(json.loads(path.read_text(encoding='utf8'))['state'],'completed')


if __name__=='__main__':unittest.main()
