from datetime import datetime,timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('release_version',Path(__file__).resolve().parents[1]/'scripts/release_version.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class ReleaseVersionTests(unittest.TestCase):
    def fixture(self,root):
        (root/'更新系统').mkdir();(root/'桌面程序').mkdir()
        (root/'更新系统/update-config.json').write_text(json.dumps({'version':'1.0.1','releaseId':'20261116030917'}),'utf8')
        (root/'更新系统/SharedTheme.cs').write_text('[assembly: System.Reflection.AssemblyFileVersion("1.0.1.0")]','utf8')
        (root/'桌面程序/Cargo.toml').write_bytes(b'[package]\r\nname = "framekeep-desktop"\r\nversion = "1.0.1"\r\n')
        (root/'桌面程序/Cargo.lock').write_bytes(b'[[package]]\r\nname = "dependency"\r\nversion = "1.0.1"\r\n\r\n[[package]]\r\nname = "framekeep-desktop"\r\nversion = "1.0.1"\r\n')
        (root/'桌面程序/tauri.conf.json').write_text('{"version":"1.0.1"}','utf8')

    def test_advances_above_remote_and_preserves_dependencies_and_line_endings(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.fixture(root)
            result=module.prepare(root,{'version':'1.0.9','releaseId':'20261117000000'},now=datetime(2026,11,16,tzinfo=timezone.utc))
            self.assertEqual((result['version'],result['releaseId']),('1.0.10','20261117000001'))
            lock=(root/'桌面程序/Cargo.lock').read_bytes()
            self.assertIn(b'name = "dependency"\r\nversion = "1.0.1"',lock)
            self.assertIn(b'name = "framekeep-desktop"\r\nversion = "1.0.10"',lock)
            self.assertEqual(json.loads((root/'桌面程序/tauri.conf.json').read_text())['version'],'1.0.10')
            self.assertIn(b'version = "1.0.10"\r\n',(root/'桌面程序/Cargo.toml').read_bytes())
            self.assertIn('AssemblyFileVersion("1.0.10.0")',(root/'更新系统/SharedTheme.cs').read_text('utf8'))

    def test_invalid_project_metadata_changes_no_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.fixture(root);(root/'桌面程序/Cargo.lock').write_text('invalid','utf8')
            before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
            with self.assertRaises(ValueError):module.prepare(root,{'version':'1.0.1','releaseId':'20261116030917'})
            self.assertEqual(before,{p:p.read_bytes() for p in before})


if __name__=='__main__':unittest.main()
