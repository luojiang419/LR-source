"""重叠目录扫描和旧相对路径修复，保护业务元数据及原件。"""
import json
from pathlib import Path
import tempfile
import unittest
from PIL import Image

from reference_library import ReferenceCatalog,ReferenceLibrary
from catalog_path_consistency import repair_relative_paths
from metadata_writer import sha


class CatalogPathConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.parent=self.base/'共享';self.child=self.parent/'现场小档'/'2026-11-04';self.child.mkdir(parents=True)
        self.file=self.child/'XSY_2466.JPG';Image.new('RGB',(40,50),'red').save(self.file);self.original=sha(self.file)
        self.catalog=ReferenceCatalog(self.base/'data',self.base/'protected',lambda:False);self.addCleanup(self.catalog.close)
        with self.catalog.connect() as c:
            for key,path in (('parent',self.parent),('child',self.child)):
                c.execute('INSERT INTO roots(id,path,created) VALUES (?,?,0)',(key,str(path)))
        self.record={'path':str(self.file),'size':self.file.stat().st_size,'modified_ns':self.file.stat().st_mtime_ns}

    def assert_consistent(self):
        with self.catalog.connect() as c:
            rows=c.execute('SELECT i.*,r.path AS base FROM items i JOIN roots r ON r.id=i.root').fetchall()
        for row in rows:
            self.assertEqual(Path(row['base'])/row['relative'],Path(row['path']))
            expected=Path(row['relative']).parent.as_posix()
            self.assertEqual(row['folder'],'/' if expected=='.' else expected)
        self.assertEqual(sha(self.file),self.original)

    def test_store_records_updates_root_relative_and_folder_together(self):
        self.catalog.store_records('child',[self.record])
        row=self.catalog.items()['rows'][0];self.catalog.assign(row['id'],'LC7877847')
        for key in ('parent','child','parent'):
            self.catalog.store_records(key,[self.record]);self.assert_consistent()
        with self.catalog.connect() as c:row=c.execute('SELECT * FROM items').fetchone()
        self.assertEqual(row['sku'],'LC7877847');self.assertEqual(row['source'],'manual')

    def test_search_index_updates_root_relative_and_folder_together(self):
        library=ReferenceLibrary(self.catalog.data,catalog=self.catalog,autostart=False);self.addCleanup(library.close)
        for key in ('child','parent','child'):
            library._index_paths(key,[str(self.file)]);self.assert_consistent()

    def test_repair_preserves_business_data_and_is_idempotent(self):
        self.catalog.store_records('child',[self.record])
        with self.catalog.connect() as c:
            c.execute("UPDATE items SET relative=?,folder=?,sku='LC7877847',source='manual'",('现场小档/2026-11-04/'+self.file.name,'现场小档/2026-11-04'))
            before=dict(c.execute('SELECT * FROM items').fetchone())
        journal=self.base/'repair.json'
        result=repair_relative_paths(self.catalog,journal=journal)
        self.assertEqual(result['repaired'],1);self.assert_consistent()
        with self.catalog.connect() as c:after=dict(c.execute('SELECT * FROM items').fetchone())
        for key in before:
            if key not in ('relative','folder'):self.assertEqual(before[key],after[key],key)
        self.assertEqual(json.loads(journal.read_text(encoding='utf8'))['changes'][0]['before_folder'],'现场小档/2026-11-04')
        self.assertEqual(repair_relative_paths(self.catalog)['repaired'],0)

    def test_repair_does_not_reassign_outside_root(self):
        self.catalog.store_records('child',[self.record])
        with self.catalog.connect() as c:c.execute('UPDATE items SET path=?',(str(self.parent/'其他.jpg'),))
        result=repair_relative_paths(self.catalog)
        self.assertEqual(result['repaired'],0);self.assertEqual(len(result['outside_root']),1)


if __name__=='__main__':unittest.main()
