from pathlib import Path
import json,sqlite3,tempfile,unittest
from folder_archive_catalog_sync import synchronize_catalog_paths


class CatalogPathSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.before=str(self.base/'photos'/'one.JPG');self.after=str(self.base/'photos'/'LC78'/'LC7877847-P1'/'one.JPG')
        self.c=sqlite3.connect(':memory:');self.addCleanup(self.c.close);self.c.row_factory=sqlite3.Row
        self.c.executescript('''PRAGMA foreign_keys=ON;
          CREATE TABLE roots(id TEXT PRIMARY KEY,path TEXT,scan_epoch INTEGER,removed INTEGER);
          CREATE TABLE items(id TEXT PRIMARY KEY,root TEXT,path TEXT UNIQUE,relative TEXT,folder TEXT,name TEXT,
            pair_key TEXT,online INTEGER,sku TEXT,source TEXT,scan_epoch INTEGER,fingerprint TEXT DEFAULT 'version');
          CREATE TABLE corrections(id TEXT PRIMARY KEY,item TEXT REFERENCES items(id),note TEXT);
          CREATE TABLE analyses(item TEXT REFERENCES items(id),payload TEXT);
          CREATE TABLE shooting_catalog_items(item TEXT PRIMARY KEY,path TEXT);''')
        self.c.executemany('INSERT INTO roots VALUES(?,?,?,0)',[('specific',str(self.base/'photos'),4),('broad',str(self.base),9)])
        self.c.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('old','specific',self.before,'one.JPG','','one.JPG','before',1,'LC7877847-P1','shooting',3,'old-fingerprint'))
        self.c.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('new','broad',self.after,'photos/LC78/LC7877847-P1/one.JPG','photos/LC78/LC7877847-P1','one.JPG','after',1,'LC7877847','embedded',9,'new-fingerprint'))
        self.c.executemany('INSERT INTO analyses VALUES(?,?)',[('old','confirmed result'),('new','new scan result')])
        self.c.execute('INSERT INTO shooting_catalog_items VALUES(?,?)',('old',self.before));self.c.commit()
        self.moves=[{'before':self.before,'after':self.after}]

    def sync(self):
        with self.c:synchronize_catalog_paths(self.c,self.moves)

    def test_collision_preserves_confirmed_identity_and_both_records(self):
        self.sync()
        old=dict(self.c.execute("SELECT * FROM items WHERE id='old'").fetchone())
        self.assertEqual((old['path'],old['sku'],old['source'],old['online']),(self.after,'LC7877847-P1','shooting',1))
        new=dict(self.c.execute("SELECT * FROM items WHERE id='new'").fetchone())
        self.assertEqual((new['path'],new['online']),(self.before,0))
        self.assertEqual(self.c.execute('SELECT count(*) FROM items').fetchone()[0],2)
        self.assertEqual(self.c.execute('SELECT count(*) FROM analyses').fetchone()[0],2)
        self.assertEqual(self.c.execute("SELECT path FROM shooting_catalog_items WHERE item='old'").fetchone()[0],self.after)
        audit=self.c.execute('SELECT retained_snapshot FROM folder_archive_path_collisions').fetchone()[0]
        self.assertEqual(json.loads(audit)['path'],self.after)

    def test_replay_is_idempotent(self):
        self.sync();first=[tuple(r) for r in self.c.execute('SELECT * FROM items ORDER BY id')]
        self.sync();self.assertEqual(first,[tuple(r) for r in self.c.execute('SELECT * FROM items ORDER BY id')])
        self.assertEqual(self.c.execute('SELECT count(*) FROM folder_archive_path_collisions').fetchone()[0],1)

    def test_manual_conflict_rolls_back_without_overwriting(self):
        self.c.execute("UPDATE items SET source='manual',sku='LC7999999' WHERE id='new'");self.c.commit()
        with self.assertRaisesRegex(ValueError,'已确认SKU冲突'):self.sync()
        self.assertEqual(self.c.execute("SELECT path FROM items WHERE id='old'").fetchone()[0],self.before)
        self.assertEqual(self.c.execute("SELECT path FROM items WHERE id='new'").fetchone()[0],self.after)

    def test_correction_history_remains_attached_to_live_item(self):
        self.c.execute("INSERT INTO corrections VALUES('history','new','original manual change')");self.c.commit();self.sync()
        self.assertEqual(tuple(self.c.execute('SELECT item,note FROM corrections').fetchone()),('old','original manual change'))

    def test_target_outside_old_nested_root_uses_registered_parent(self):
        self.c.execute('UPDATE roots SET path=? WHERE id=?',(str(self.base/'photos'/'nested'),'specific'))
        self.before=str(self.base/'photos'/'nested'/'one.JPG')
        self.c.execute("UPDATE items SET path=? WHERE id='old'",(self.before,));self.c.commit()
        self.moves=[{'before':self.before,'after':self.after}];self.sync()
        row=self.c.execute("SELECT root,relative FROM items WHERE id='old'").fetchone()
        self.assertEqual(tuple(row),('broad','photos/LC78/LC7877847-P1/one.JPG'))


if __name__=='__main__':unittest.main()
