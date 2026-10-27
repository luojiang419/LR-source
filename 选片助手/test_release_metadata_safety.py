"""发行前拒绝旧元数据引擎，避免源码与客户机运行版本脱节。"""
from pathlib import Path
import unittest
from unittest.mock import Mock,patch

import build_release


class ReleaseMetadataSafetyTests(unittest.TestCase):
    def archive(self):
        modules={name:compile((build_release.ROOT/'选片助手'/(name+'.py')).read_bytes(),'old/build/'+name+'.py','exec')
                 for name in ('file_safety','metadata_writer','asset_archive')}
        archive=Mock();archive.extract.side_effect=lambda name:modules[name]
        return archive,modules

    def test_current_modules_pass_with_different_build_paths(self):
        archive,_=self.archive()
        with patch('PyInstaller.archive.readers.CArchiveReader') as reader:
            reader.return_value.open_embedded_archive.return_value=archive
            build_release.validate_metadata_engine(Path('engine.exe'))
        self.assertEqual(archive.extract.call_count,3)

    def test_old_backup_or_work_directory_module_is_rejected(self):
        for name in ('file_safety','metadata_writer','asset_archive'):
            with self.subTest(module=name):
                archive,modules=self.archive()
                modules[name]=compile("backup='.framekeep-recovery-old/original.bin'",name+'.py','exec')
                with patch('PyInstaller.archive.readers.CArchiveReader') as reader:
                    reader.return_value.open_embedded_archive.return_value=archive
                    with self.assertRaisesRegex(RuntimeError,name):
                        build_release.validate_metadata_engine(Path('old-engine.exe'))

    def test_missing_module_is_rejected(self):
        archive,modules=self.archive();del modules['file_safety']
        with patch('PyInstaller.archive.readers.CArchiveReader') as reader:
            reader.return_value.open_embedded_archive.return_value=archive
            with self.assertRaisesRegex(RuntimeError,'无法校验'):
                build_release.validate_metadata_engine(Path('incomplete-engine.exe'))


if __name__=='__main__':unittest.main()
