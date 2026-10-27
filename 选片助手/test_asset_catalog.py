import tempfile
import unittest
from pathlib import Path
import os
from unittest.mock import patch

from PIL import Image,ImageOps

from asset_catalog import AssetCatalog,archive_path


class AssetCatalogTests(unittest.TestCase):
    def test_media_migration_runs_once_and_only_changes_legacy_classification(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            data=Path(folder)/'data';catalog=AssetCatalog(data)
            with catalog.connect() as c:
                c.execute("INSERT INTO roots(id,path,created) VALUES ('root','photos',0)")
                for name,extension,kind,format in (
                    ('video','.mov','image',''),('design','.psd','image','PSD'),
                    ('blank','.jpg','image',''),('ready','.jpg','image','JPEG'),
                ):
                    c.execute('''INSERT INTO items(id,root,path,relative,folder,name,extension,size,modified,fingerprint,last_seen,media_type,media_format)
                        VALUES (?,'root',?,?,'',?,?,1,0,'fingerprint',0,?,?)''',
                        (name,name,name,name,extension,kind,format))
                c.execute('CREATE TABLE migration_updates(id TEXT)')
                c.execute('CREATE TRIGGER track_media AFTER UPDATE OF media_type,media_format ON items BEGIN INSERT INTO migration_updates VALUES(new.id); END')
                c.execute('PRAGMA user_version=1')
            catalog.close()
            migrated=AssetCatalog(data)
            with migrated.connect() as c:
                rows={r['id']:(r['media_type'],r['media_format']) for r in c.execute('SELECT * FROM items')}
                self.assertEqual(rows,{'video':('video','MOV'),'design':('design','PSD'),'blank':('image','JPG'),'ready':('image','JPEG')})
                self.assertEqual({r[0] for r in c.execute('SELECT id FROM migration_updates')},{'video','design','blank'})
                self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],2)
            migrated.close()
            statements=[];connect=sqlite3.connect
            def traced(*args,**kwargs):
                connection=connect(*args,**kwargs);connection.set_trace_callback(statements.append);return connection
            with patch('asset_catalog.sqlite3.connect',side_effect=traced):
                reopened=AssetCatalog(data)
                reopened.close()
            self.assertFalse(any(sql.lstrip().upper().startswith('UPDATE ITEMS SET MEDIA_') for sql in statements),'重启不能重新全表分类')

    def test_raw_direction_is_read_once_per_file_version_for_both_preview_sizes(self):
        import io
        import media_engine as media
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder); photos=base/'photos'; photos.mkdir()
            raw=photos/'portrait.CR3'; raw.write_bytes(b'raw-one')
            catalog=AssetCatalog(base/'data'); root=catalog.add_root(str(photos))['root_id']
            item=catalog.items()['rows'][0]; old_url=item['preview']
            payload=io.BytesIO(); Image.new('RGB',(300,200),'teal').save(payload,format='JPEG')
            def embedded(path,tool,tags,size,orientation,**kwargs):
                return media._read_preview(io.BytesIO(payload.getvalue()),size,orientation)
            with patch('media_engine.find_exiftool',return_value=Path('exiftool')),patch('media_engine.raw_embedded_metadata',return_value=(6,payload.getvalue())) as metadata:
                for size in (420,900):
                    with Image.open(catalog.preview(item['id'],size)) as image:self.assertEqual(ImageOps.exif_transpose(image).size,(200,300))
                self.assertEqual(metadata.call_count,1)
                raw.write_bytes(b'raw-version-two'); catalog.scan(root)
                self.assertNotEqual(catalog.items()['rows'][0]['preview'],old_url)
                catalog.preview(item['id'],420); self.assertEqual(metadata.call_count,2)

    def test_pairs_share_counts_pagination_corrections_and_small_jpeg_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder); photos = base / 'photos' / 'SKU-AB1234'; photos.mkdir(parents=True)
            Image.new('RGB', (2400, 1600), 'teal').save(photos / 'look.JPG')
            (photos / 'look.CR3').write_bytes(b'not-a-decoded-raw')
            Image.new('RGB', (40, 30), 'red').save(photos / 'other.jpg')
            catalog = AssetCatalog(base / 'data'); root = catalog.add_root(str(base / 'photos'))['root_id']
            self.assertEqual(catalog.state()['counts']['total'], 2)
            self.assertEqual(catalog.tree()['roots'][0]['folders'][0]['count'], 2)
            self.assertEqual(catalog.sku_groups()['rows'][0]['count'], 2)
            rows = catalog.items()['rows']; paired = next(r for r in rows if r['has_raw'])
            self.assertTrue(paired['has_jpg']); self.assertEqual(paired['extension'], '.jpg')
            first = catalog.items(limit=1); second = catalog.items(limit=1, cursor=first['next_cursor'])
            self.assertNotEqual(first['rows'][0]['id'], second['rows'][0]['id']); self.assertFalse(second['has_more'])
            with patch('media_engine.find_exiftool', side_effect=RuntimeError('ExifTool 不可用时回退 JPG')):
                with Image.open(catalog.preview(paired['id'], size=900)) as image:
                    self.assertEqual(max(image.size), 900)
            catalog.assign(paired['id'], 'FINAL-9001')
            with catalog.connect() as c:
                members = c.execute('SELECT sku FROM items WHERE pair_key=?', (paired['pair_key'],)).fetchall()
                self.assertEqual([r['sku'] for r in members], ['FINAL-9001', 'FINAL-9001'])
            catalog.scan(root); self.assertEqual(catalog.items(sku='FINAL9001')['total'], 1)
            (photos / 'look.JPG').unlink(); catalog.scan(root)
            row = catalog.items(sku='FINAL9001')['rows'][0]
            self.assertTrue(row['has_raw']); self.assertFalse(row['has_jpg'])
            self.assertEqual(row['source'], 'manual')

    def test_nested_folder_names_keep_dots_and_literal_wildcards_and_migrate(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos'
            for name in ('v1.2/2026_10/detail','v1.2/2026x10/detail','v1.2/100%/detail'):
                path=photos/name;path.mkdir(parents=True);Image.new('RGB',(40,30),'teal').save(path/'one.jpg')
            catalog=AssetCatalog(base/'data')
            with patch.object(Path,'resolve',side_effect=AssertionError('扫描不能把映射盘转换为 UNC')):
                root=catalog.add_root(str(photos))['root_id']
            self.assertEqual(catalog.items(root=root,folder='v1.2/2026_10')['total'],1)
            self.assertEqual(catalog.items(root=root,folder='v1.2/100%')['total'],1)
            row=catalog.items(root=root,folder='v1.2/2026_10')['rows'][0];catalog.assign(row['id'],'MANUAL-1234')
            with catalog.connect() as c:
                c.execute("UPDATE items SET folder='v12\\2026_10\\detail' WHERE id=?",(row['id'],));c.execute('PRAGMA user_version=0')
            reopened=AssetCatalog(base/'data');fixed=reopened.item(row['id'])
            self.assertEqual(fixed['folder'],'v1.2/2026_10/detail');self.assertEqual(fixed['sku'],'MANUAL-1234');self.assertEqual(fixed['source'],'manual')
            with self.assertRaisesRegex(ValueError,'不可访问'):catalog.add_root(str(base/'missing'))

    @unittest.skipUnless(os.name=='nt','Windows 路径语义')
    def test_network_paths_keep_drive_letters_and_normalize_ip_and_extended_unc(self):
        self.assertEqual(str(archive_path(r'X:\照片\..\归档')),r'X:\归档')
        self.assertEqual(str(archive_path(r'\\192.168.0.181\共享\照片')),r'\\192.168.0.181\共享\照片')
        self.assertEqual(str(archive_path(r'192.168.0.181\共享\照片')),r'\\192.168.0.181\共享\照片')
        self.assertEqual(str(archive_path(r'\\?\UNC\192.168.0.181\共享\照片')),r'\\192.168.0.181\共享\照片')
        for value in ('',None,'smb://192.168.0.181/share'):
            with self.assertRaises(ValueError):archive_path(value)

    def test_scan_groups_and_manual_sku_fingerprint_without_copying_source_files(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / 'photos' / '2026-10-21' / 'SKU-AB1234'
            source.mkdir(parents=True)
            tagged = source / 'look-01.jpg'
            unknown = source / 'detail.jpg'
            for path, color in ((tagged, (20, 80, 130)), (unknown, (120, 60, 50))):
                Image.new('RGB', (160, 100), color).save(path)
            catalog = AssetCatalog(base / 'data')
            result = catalog.add_root(str(base / 'photos'))
            self.assertEqual(result['indexed'], 2)
            rows = catalog.items(limit=10)['rows']
            self.assertEqual({row['sku'] for row in rows}, {'SKU-AB1234'})
            self.assertTrue(all(Path(row['path']).is_file() for row in rows))
            item = next(row for row in rows if row['name'] == 'detail.jpg')
            fixed = catalog.assign(item['id'], 'LC-9001')
            self.assertEqual(fixed['sku'], 'LC-9001')
            self.assertEqual(fixed['source'], 'manual')
            self.assertEqual(catalog.sku_groups()['rows'][0]['count'], 1)
            self.assertEqual(catalog.sku_groups('LC9001')['rows'][0]['count'], 1)
            preview = catalog.preview(item['id'])
            self.assertTrue(preview.is_file())
            self.assertEqual(tagged.read_bytes()[:2], b'\xff\xd8')
            catalog.scan(result['root_id'])
            self.assertEqual(catalog.item(item['id'])['sku'], 'LC-9001')

    def test_cursor_pagination_uses_stable_keyset_order(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            photos = base / 'photos' / 'SKU-X12345'
            photos.mkdir(parents=True)
            for index in range(3):
                Image.new('RGB', (40, 30), (index * 30, 80, 120)).save(photos / f'{index}.jpg')
            catalog = AssetCatalog(base / 'data')
            catalog.add_root(str(base / 'photos'))
            first = catalog.items(limit=1)
            second = catalog.items(limit=1, cursor=first['next_cursor'])
            third = catalog.items(limit=1, cursor=second['next_cursor'])
            self.assertTrue(first['has_more'])
            self.assertTrue(second['has_more'])
            self.assertFalse(third['has_more'])
            self.assertEqual(len({first['rows'][0]['id'], second['rows'][0]['id'], third['rows'][0]['id']}), 3)


if __name__ == '__main__':
    unittest.main()
