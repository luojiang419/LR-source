"""安装目录数据迁移：WAL、完整性、外部路径与失败保护。"""
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import closing

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import installation as i
import updater as u


class DataStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'程序';self.root.mkdir()
        self.source=self.base/'旧资料';self.source.mkdir()

    def test_new_install_and_reinstall_use_local_data_and_uninstall_preserves_it(self):
        target=i.data_directory(self.root);self.assertEqual(target,self.root/'data')
        self.assertEqual(i.prepare_data_directory(self.root,target),target)
        (target/'ratings.txt').write_text('人工星级')
        self.assertEqual(i.data_directory(self.root),target)
        (self.root/'末帧选片.exe').write_bytes(b'app')
        manifest={'version':'1.0.0','releaseId':'20261023000001','files':[{'path':'末帧选片.exe','sha256':i.digest(self.root/'末帧选片.exe'),'size':3}]}
        i.complete_registration(self.root,i.prepare_marker(self.root,target,manifest,False))
        i.uninstall(self.root,is_running=lambda _:False)
        self.assertEqual((target/'ratings.txt').read_text(),'人工星级')
        self.assertEqual(i.data_directory(self.root),target)

    def test_migration_includes_committed_wal_and_preserves_source_and_external_paths(self):
        db=self.source/'framekeep.sqlite3';con=sqlite3.connect(db)
        self.addCleanup(con.close);con.execute('PRAGMA journal_mode=WAL')
        con.execute('CREATE TABLE ratings(id PRIMARY KEY,rating)');con.execute('INSERT INTO ratings VALUES (1,5)');con.commit()
        before=db.read_bytes();config={'path':str(self.source/'previews'),'external':'E:/独立索引','nested':{'path':str(self.source)}}
        i.write_json(self.source/'location.json',config);(self.source/'token').write_text('keep')
        target=i.prepare_data_directory(self.root,self.source)
        with closing(sqlite3.connect(target/db.name)) as new:
            self.assertEqual(new.execute('SELECT rating FROM ratings').fetchone()[0],5)
            self.assertEqual(new.execute('PRAGMA quick_check').fetchone()[0],'ok')
        self.assertEqual(db.read_bytes(),before);self.assertEqual((target/'token').read_text(),'keep')
        self.assertEqual(i.read_json(target/'location.json'),{**config,'path':str(target/'previews'),'nested':{'path':str(target)}})

    def test_nonempty_target_and_overlap_cannot_overwrite_data(self):
        target=self.root/'data';target.mkdir();(target/'customer').write_text('keep')
        with self.assertRaisesRegex(ValueError,'已有资料'):i.prepare_data_directory(self.root,self.source)
        self.assertEqual((target/'customer').read_text(),'keep')
        with self.assertRaisesRegex(ValueError,'重叠'):i.prepare_data_directory(self.root,self.root)

    def test_bad_database_fails_without_publishing_or_changing_original(self):
        (self.source/'framekeep.sqlite3').write_bytes(b'invalid sqlite')
        with self.assertRaises(sqlite3.DatabaseError):i.prepare_data_directory(self.root,self.source)
        self.assertFalse((self.root/'data').exists());self.assertFalse(list(self.root.glob('.data-migration-*')))
        self.assertEqual((self.source/'framekeep.sqlite3').read_bytes(),b'invalid sqlite')

    def test_missing_old_data_fails_instead_of_creating_empty_database(self):
        with self.assertRaisesRegex(ValueError,'不存在'):i.prepare_data_directory(self.root,self.base/'missing')
        self.assertFalse((self.root/'data').exists())

    def test_low_space_does_not_publish_partial_migration(self):
        (self.source/'token').write_bytes(b'keep')
        with patch.object(i.shutil,'disk_usage',return_value=i.shutil._ntuple_diskusage(100,99,1)),self.assertRaises(OSError):
            i.prepare_data_directory(self.root,self.source)
        self.assertFalse((self.root/'data').exists())

    def test_retry_after_application_rollback_reuses_verified_copy_but_not_changed_source(self):
        (self.source/'settings.json').write_text('{}')
        target=i.prepare_data_directory(self.root,self.source);(target/'new-user-edit').write_text('keep')
        self.assertEqual(i.prepare_data_directory(self.root,self.source),target)
        self.assertEqual((target/'new-user-edit').read_text(),'keep')
        (self.source/'settings.json').write_text('{"changed":true}')
        with self.assertRaisesRegex(ValueError,'已有资料'):i.prepare_data_directory(self.root,self.source)

    def test_legacy_full_photo_backup_becomes_field_record_only_if_hash_matches(self):
        identity='a'*32;other='b'*32
        backups=self.source/'metadata-backups';backups.mkdir()
        (backups/(identity+'.bin')).write_bytes(b'original-photo')
        (backups/(other+'.bin')).write_bytes(b'unverified-original')
        with closing(sqlite3.connect(self.source/'asset_archive.sqlite3')) as db,db:
            db.execute('CREATE TABLE metadata_jobs(id,path,status,journal)')
            journal={'before':{'SKU':'old'},'after':{'SKU':'new'},'before_hash':i.digest(backups/(identity+'.bin')),'after_hash':'c'*64}
            db.execute('INSERT INTO metadata_jobs VALUES (?,?,?,?)',(identity,'E:/photo.CR3','applied',json.dumps(journal)))
            db.execute('INSERT INTO metadata_jobs VALUES (?,?,?,?)',(other,'E:/other.CR3','applied',json.dumps(journal)))
        target=i.prepare_data_directory(self.root,self.source)
        self.assertFalse((target/'metadata-backups'/(identity+'.bin')).exists())
        self.assertEqual((target/'metadata-backups'/(other+'.bin')).read_bytes(),b'unverified-original')
        record=i.read_json(target/'metadata-history'/(identity+'.json'))
        self.assertEqual(record['journal']['before'],{'SKU':'old'})
        self.assertTrue((backups/(identity+'.bin')).exists())

    def test_retry_detects_source_changes_still_only_in_wal(self):
        with closing(sqlite3.connect(self.source/'framekeep.sqlite3')) as db:
            db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE ratings(rating)');db.commit()
            db.execute('INSERT INTO ratings VALUES (1)');db.commit()
            target=i.prepare_data_directory(self.root,self.source)
            db.execute('INSERT INTO ratings VALUES (5)');db.commit()
            with self.assertRaisesRegex(ValueError,'已有资料'):i.prepare_data_directory(self.root,self.source)
            with closing(sqlite3.connect(target/'framekeep.sqlite3')) as new:
                self.assertEqual(new.execute('SELECT count(*) FROM ratings').fetchone()[0],1)

    def test_completed_update_payload_released_but_incomplete_preserved(self):
        session=self.base/'session';session.mkdir()
        for name in ('stage','backup'):(session/name).mkdir();(session/name/'engine').write_bytes(b'payload')
        (session/'package.zip').write_bytes(b'zip');(session/'updater.log').write_text('log')
        i.write_json(session/'journal.json',{'status':'installing'});i.write_json(session/'status.json',{'status':'error'})
        self.assertFalse(u.release_completed_payload(session));self.assertTrue((session/'package.zip').exists())
        i.write_json(session/'journal.json',{'status':'complete'});i.write_json(session/'status.json',{'status':'complete'})
        self.assertTrue(u.release_completed_payload(session));self.assertFalse((session/'package.zip').exists())
        self.assertFalse((session/'backup').exists());self.assertTrue((session/'updater.log').exists())


if __name__=='__main__':unittest.main()
