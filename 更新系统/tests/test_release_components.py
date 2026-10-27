"""发布程序更新时，历史运行时须可验证复用，不能依赖已清理的本地包。"""
from pathlib import Path
import hashlib
import importlib.util
import json
import tempfile
import unittest
from unittest.mock import Mock,patch

SOURCE=Path(__file__).resolve().parents[1]/'scripts/release.py'
spec=importlib.util.spec_from_file_location('framekeep_release_test',SOURCE)
release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)


class ReleaseComponentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'选片助手').mkdir()
        self.digest=hashlib.sha256(b'runtime').hexdigest();self.relative='components/'+self.digest+'/runtime.zip'
        self.url=release.file_url(self.relative)
        self.item={'name':'runtime.zip','path':'runtime/runtime.zip','url':self.url,'sha256':self.digest,'size':7}
        (self.root/'选片助手/component-manifest.json').write_text(json.dumps({'archives':[self.item],'assets':[]}),encoding='utf-8')
        self.addCleanup(patch.stopall);patch.object(release,'ROOT',self.root).start()

    def test_existing_remote_component_can_be_verified_without_local_archive(self):
        api=Mock()
        with patch.object(release,'remote_digest',return_value=self.digest) as remote:
            result=release.publish_components(api,{self.relative})
        api.upload_file.assert_not_called();remote.assert_called_once_with(self.url)
        self.assertEqual(result[0]['sha256'],self.digest)

    def test_missing_remote_component_requires_a_verified_local_archive(self):
        api=Mock()
        with patch.object(release,'remote_digest') as remote,self.assertRaisesRegex(ValueError,'远端组件缺失'):
            release.publish_components(api,set())
        api.upload_file.assert_not_called();remote.assert_not_called()

    def test_existing_remote_hash_mismatch_aborts_before_activation(self):
        api=Mock()
        with patch.object(release,'remote_digest',return_value='0'*64),self.assertRaisesRegex(ValueError,'匿名校验失败'):
            release.publish_components(api,{self.relative})
        api.upload_file.assert_not_called()

    def test_missing_remote_uploads_local_verified_content_then_checks_anonymous_bytes(self):
        local=self.root/'.runtime/component-archives/runtime.zip';local.parent.mkdir(parents=True);local.write_bytes(b'runtime')
        api=Mock()
        with patch.object(release,'remote_digest',return_value=self.digest) as remote:
            release.publish_components(api,set())
        api.upload_file.assert_called_once();remote.assert_called_once_with(self.url)


    def test_package_build_rejects_stale_engine_before_creating_output(self):
        output=self.root/'package'
        with patch('build_release.validate_release',side_effect=RuntimeError('旧元数据引擎')) as validate:
            with self.assertRaisesRegex(RuntimeError,'旧元数据引擎'):
                release.build(output=output)
        validate.assert_called_once_with(self.root/'build/tauri-release/末帧选片')
        self.assertFalse(output.exists())


if __name__=='__main__':unittest.main()
