"""独立图搜图设置和数据库边界，不加载模型。"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import sys
import os
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from material_search import MaterialSearch
from material_search_worker import ArchiveIndex


class MaterialSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.service=MaterialSearch(SimpleNamespace(data=self.base),str(self.base/'model'),autostart=False)

    def tearDown(self):self.service.close();self.temp.cleanup()

    def test_constructor_and_disabled_settings_do_not_start_model_or_create_database(self):
        self.assertIsNone(self.service.process)
        self.service.configure({'image_index_enabled':False,'image_index_paused':True})
        self.assertFalse((self.base/'visual-search/assets.db').exists())
        saved=json.loads(self.service.config_path.read_text('utf8'))
        self.assertFalse(saved['image_index_enabled']);self.assertTrue(saved['image_index_paused'])

    def test_invalid_settings_do_not_change_saved_configuration(self):
        for body in ({'image_index_enabled':'true'},{'image_index_paused':1},{'image_index_action':'delete'},{'material_root':''}):
            with self.subTest(body=body),self.assertRaises(ValueError):self.service.configure(body)
        self.assertFalse(self.service.config_path.exists())

    def test_search_reports_disabled_index_instead_of_legacy_appearance(self):
        with self.assertRaisesRegex(ValueError,'自动建立图搜图库'):self.service.search({'image':'fixture'})

    def test_model_discovery_uses_explicit_material_root(self):
        hub=Path(self.service.config['material_root'])/'huggingface/hub/models--OFA-Sys--chinese-clip-vit-large-patch14-336px/snapshots/revision'
        hub.mkdir(parents=True);(hub/'config.json').write_text('{}')
        self.assertEqual(self.service.model_directory(),hub)

    def test_worker_refuses_metadata_database_before_loading_model(self):
        directory=self.base/'independent';directory.mkdir()
        model=self.base/'snapshot';model.mkdir()
        for name in ('config.json','preprocessor_config.json'):(model/name).write_text('{}')
        database=directory/'assets.db'
        with closing(sqlite3.connect(database)) as c:
            with c:c.execute('CREATE TABLE items(id TEXT)');c.execute("INSERT INTO items VALUES('preserved')")
        with self.assertRaisesRegex(ValueError,'元数据库'):
            ArchiveIndex({'directory':str(directory),'model':str(model),'media_types':{'.jpg':'image'}})
        with closing(sqlite3.connect(database)) as c:
            self.assertEqual(c.execute('SELECT * FROM items').fetchall(),[('preserved',)])
            self.assertEqual(c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),[('items',)])

    def test_worker_refuses_model_change_without_replacing_existing_library(self):
        directory=self.base/'independent';directory.mkdir();model=self.base/'snapshot';model.mkdir()
        for name in ('config.json','preprocessor_config.json'):(model/name).write_text('{}')
        (directory/'model-manifest.json').write_text('{"model":"old-revision"}')
        with self.assertRaisesRegex(ValueError,'模型版本不一致'):
            ArchiveIndex({'directory':str(directory),'model':str(model),'media_types':{'.jpg':'image'}})
        self.assertFalse((directory/'assets.db').exists())

    def test_reference_binds_original_path_and_file_version_even_when_integer_id_is_reused(self):
        original=ArchiveIndex.reference_id('material',7,'A/photo.jpg',[100,200])
        self.assertNotEqual(original,ArchiveIndex.reference_id('material',7,'B/other.jpg',[100,200]))
        self.assertNotEqual(original,ArchiveIndex.reference_id('material',7,'A/photo.jpg',[100,201]))

    def test_discovery_skips_system_folders_and_retains_accessible_files_on_partial_error(self):
        root=self.base/'photos';root.mkdir();(root/'normal.jpg').write_bytes(b'fixture')
        protected=root/'System Volume Information';protected.mkdir();(protected/'ignored.jpg').write_bytes(b'fixture')
        context=SimpleNamespace(valid=lambda epoch:True,directory=self.base/'data/visual-search',
                                media_types={'.jpg':'image'},inside=ArchiveIndex.inside)
        paths,errors=ArchiveIndex._enumerate(context,{'path':str(root),'excluded':[]},0)
        self.assertEqual(paths,[str(root/'normal.jpg')]);self.assertEqual(errors,[])
        import os
        walk=os.walk
        def partial(path,*,followlinks,onerror):
            onerror(PermissionError('fixture: one folder denied'))
            yield from walk(path,followlinks=followlinks,onerror=onerror)
        with patch('material_search_worker.os.walk',side_effect=partial):
            paths,errors=ArchiveIndex._enumerate(context,{'path':str(root),'excluded':[]},0)
        self.assertEqual(paths,[str(root/'normal.jpg')]);self.assertEqual(len(errors),1)

    def test_image_search_defaults_to_original_85_percent_without_tightening_text_search(self):
        for body in ({},{'image':'reference'},{'path':'photo.jpg'},{'item_id':'id'},{'image':'reference','text':'old input'}):
            with self.subTest(body=body):self.assertEqual(ArchiveIndex.similarity_threshold(body),.85)
        self.assertEqual(ArchiveIndex.similarity_threshold({'text':'红色短袖'}),.36)
        self.assertEqual(ArchiveIndex.similarity_threshold({'image':'reference'},image_default=.9),.9)

    def test_explicit_threshold_is_respected_and_invalid_values_are_rejected(self):
        for value in (0,.5,.85,1):
            self.assertEqual(ArchiveIndex.similarity_threshold({'image':'reference','threshold':value}),value)
        for value in (-.1,1.1,None,True,float('nan'),float('inf'),'bad'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                ArchiveIndex.similarity_threshold({'image':'reference','threshold':value})

    def test_picture_matching_defaults_to_frame_and_explicit_content_is_preserved(self):
        self.assertEqual(ArchiveIndex.match_mode({'image':'reference'}),'frame')
        self.assertEqual(ArchiveIndex.match_mode({'image':'reference','match_mode':'semantic'}),'semantic')
        self.assertEqual(ArchiveIndex.match_mode({'text':'正面短裤'}),'semantic')
        for mode in (None,True,'unknown'):
            with self.subTest(mode=mode),self.assertRaises(ValueError):ArchiveIndex.match_mode({'image':'reference','match_mode':mode})

    def test_frozen_worker_uses_isolated_runtime_instead_of_partial_bundle_packages(self):
        (self.base/'simplejson').mkdir();(self.base/'material_search_worker.py').write_text('# old root layout')
        with patch.object(sys,'frozen',True,create=True),patch.object(sys,'_MEIPASS',str(self.base),create=True):
            with self.assertRaisesRegex(ValueError,'运行文件不完整'):self.service.worker_directory()
            isolated=self.base/'material-search-runtime';isolated.mkdir()
            (isolated/'material_search_worker.py').write_text('# complete isolated layout')
            self.assertEqual(self.service.worker_directory(),isolated)

    def test_real_worker_command_does_not_write_bytecode_into_runtime(self):
        runtime=self.base/'runtime';runtime.mkdir()
        (runtime/'dependency.py').write_text('value=7\n')
        (runtime/'material_search_worker.py').write_text('import dependency\nprint(dependency.value)\n')
        env=os.environ.copy();env.pop('PYTHONDONTWRITEBYTECODE',None)
        result=subprocess.run(self.service.worker_command(sys.executable,runtime,self.base/'settings.json'),
                              capture_output=True,text=True,encoding='utf8',env=env,check=True)
        self.assertEqual(result.stdout.strip(),'7');self.assertFalse((runtime/'__pycache__').exists())

    def test_runtime_stage_excludes_caches_and_unrelated_files_without_deleting_source(self):
        import build_release
        source=self.base/'选片助手';source.mkdir()
        for name in ('material_search_worker.py','material_search_preview.py','material_search_detail.py','design_preview.py','preview_errors.py'):(source/name).write_text('# fixture')
        vendor=source/'vendor/materialsearch_core';vendor.mkdir(parents=True)
        for name in ('__init__.py','models.py','LICENSE','SOURCE.json','credentials.env'):(vendor/name).write_text('fixture')
        cache=vendor/'__pycache__';cache.mkdir();(cache/'models.pyc').write_bytes(b'fixture')
        with patch.object(build_release,'ROOT',self.base):stage=build_release.stage_material_runtime()
        actual={path.relative_to(stage).as_posix() for path in stage.rglob('*') if path.is_file()}
        self.assertIn('vendor/materialsearch_core/models.py',actual)
        self.assertFalse(any('__pycache__' in path or path.endswith('.env') for path in actual))
        self.assertTrue((cache/'models.pyc').exists())


if __name__=='__main__':unittest.main()
