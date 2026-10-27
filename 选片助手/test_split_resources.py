"""只读共享协议的权限、路径边界、真实图片及 Everything 回退验证。"""
from pathlib import Path
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from PIL import Image
from split_resources import ResourceLibrary, normalize_address


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.images = self.base/'images'
        (self.images/'秋冬'/'棚拍').mkdir(parents=True)
        for name in ('a.jpg','b.jpg'):
            Image.new('RGB',(120,180),'tan').save(self.images/'秋冬'/'棚拍'/name)
        Image.new('RGB',(80,60),'navy').save(self.images/'root.jpg')
        self.library = ResourceLibrary(self.base/'data','test')
        self.root = self.library.add_root(self.images,'样片库')['root_id']

    def tearDown(self):
        self.temporary.cleanup()

    def test_address_completion_and_private_only(self):
        self.assertEqual(normalize_address('192.168.1.20:8792'),'http://192.168.1.20:8792/api/library/v1')
        self.assertEqual(normalize_address('http://10.0.0.3:8899/api/library/v1/'),'http://10.0.0.3:8899/api/library/v1')
        self.assertEqual(normalize_address('127.0.0.1'),'http://127.0.0.1:8793/api/library/v1')
        for value in ('https://example.com','8.8.8.8:80','http://u:p@192.168.1.2','192.168.1.2/path','192.168.1.2:0','192.168.1.2?a=b'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_address(value)

    def test_named_keys_encrypted_and_scoped(self):
        result = self.library.create_key('工作站 B',[self.root])
        self.assertEqual(self.library.authorize(result['api_key']),{self.root})
        self.assertEqual(self.library.reveal(result['id']),result['api_key'])
        self.assertNotIn(result['api_key'],self.library.config_path.read_text(encoding='utf8'))
        public = self.library.public_config()['keys'][0]
        self.assertNotIn('secret',public)
        self.assertNotIn('hash',public)
        with self.assertRaises(PermissionError):
            self.library.preview(self.root,'root.jpg',allowed={'another-root'})
        with self.assertRaises(PermissionError):
            self.library.authorize('wrong-key')

    def test_revoked_key_rejected_and_restart_preserves_key(self):
        result=self.library.create_key('只读调用',[self.root])
        reopened=ResourceLibrary(self.base/'data','test')
        self.assertEqual(reopened.authorize(result['api_key']),{self.root})
        reopened.config['keys'][0]['disabled']=True
        reopened.save()
        with self.assertRaises(PermissionError):
            reopened.authorize(result['api_key'])
        with self.assertRaises(StopIteration):
            reopened.reveal(result['id'])

    def test_public_roots_hide_physical_paths(self):
        result=self.library.roots({self.root})
        self.assertEqual(result['roots'][0]['name'],'样片库')
        self.assertNotIn('path',result['roots'][0])
        self.assertNotIn(str(self.images),json.dumps(result))
        self.assertEqual(self.library.roots(set())['roots'],[])

    def test_folder_tree_and_real_images(self):
        self.assertEqual(self.library.folders(self.root)['folders'],[{'name':'秋冬','folder':'秋冬'}])
        result=self.library.items(self.root,'秋冬/棚拍')
        self.assertEqual(result['total'],2)
        self.assertEqual(result['rows'][0]['name'],'a.jpg')
        raw=self.library.preview(self.root,'秋冬/棚拍/a.jpg',80)
        self.assertTrue(raw.startswith(b'\xff\xd8'))
        self.assertEqual(self.library.items(self.root)['total'],1)

    def test_traversal_and_non_image_rejected(self):
        for path in ('../outside.jpg','秋冬/../../outside.jpg','C:/private/file.jpg','/root.jpg','..\\outside.jpg'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.library.preview(self.root,path)
        (self.images/'secret.txt').write_text('not a picture')
        with self.assertRaises(ValueError):
            self.library.preview(self.root,'secret.txt')

    def test_search_literal_and_folder_scope(self):
        self.assertEqual(self.library.items(self.root,text='a.jpg')['total'],1)
        self.assertEqual(self.library.items(self.root,'秋冬',text='.jpg')['total'],2)
        self.assertEqual(self.library.items(self.root,text="%' OR 1=1 --")['total'],0)
        self.assertEqual(self.library.items(self.root,text='不存在')['total'],0)

    def test_everything_scoped_search_merges_and_checks_boundary(self):
        outside=self.base/'outside.jpg'
        Image.new('RGB',(10,10)).save(outside)
        search=Mock()
        search._request.return_value={'available':True,'paths':[str(outside),str(self.images/'秋冬/棚拍/a.jpg')]}
        self.library.everything=search
        result=self.library.items(self.root,text='a.jpg')
        self.assertEqual(result['total'],1)
        self.assertEqual(result['engine'],'Everything + 目录数据库')
        self.assertEqual(search._request.call_args.args[0]['text'],'a.jpg')
        self.assertEqual(search._request.call_args.args[0]['root'],str(self.images))

    def test_everything_failure_uses_persistent_index(self):
        search=Mock()
        search._request.return_value={'available':False,'message':'超时回退'}
        self.library.everything=search
        result=self.library.items(self.root,text='.jpg')
        self.assertEqual(result['total'],3)
        self.assertEqual(result['engine'],'目录数据库')
        self.assertEqual(result['note'],'超时回退')

    def test_unallocated_or_empty_scope_rejected(self):
        for roots in ([],['unknown']):
            with self.assertRaises(ValueError):
                self.library.create_key('name',roots)


if __name__=='__main__':
    unittest.main()
