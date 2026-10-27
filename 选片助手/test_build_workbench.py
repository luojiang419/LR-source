"""发行构建必须拒绝混入旧版的工作台资源。"""
from pathlib import Path
import shutil
import tempfile
import unittest

from build_release import ROOT, STARTUP_ASSETS, WORKBENCH_FILES, validate_workbench_sources


class WorkbenchBuildTests(unittest.TestCase):
    def test_current_assets_pass_and_stale_layout_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            web=Path(folder)
            for name in (*WORKBENCH_FILES,*STARTUP_ASSETS):
                shutil.copy2(ROOT/'演示方案'/name,web/name)
            validate_workbench_sources(web)
            for name in ('asset-retrieval.js','asset-retrieval.css','workbench-ui.js'):
                with self.subTest(name=name):
                    path=web/name;source=path.read_bytes();path.write_bytes(source+b'\n/* stale */')
                    with self.assertRaisesRegex(RuntimeError,name.replace('.',r'\.')):
                        validate_workbench_sources(web)
                    path.write_bytes(source)
            (web/'reference-search.js').unlink()
            with self.assertRaisesRegex(RuntimeError,'reference-search'):
                validate_workbench_sources(web)


if __name__=='__main__':unittest.main()
