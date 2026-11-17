"""带资料版本校验的持久浏览页，重启复用本地列表与已缓存缩略图地址。"""
from contextlib import closing
import json
import sqlite3
import time


class CatalogPages:
    def __init__(self,catalog,connection):
        self.catalog=catalog
        if 'cutoff' in {row[1] for row in connection.execute('PRAGMA table_info(catalog_pages)')}:return
        connection.executescript('''BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS catalog_page_revision(id INTEGER PRIMARY KEY,value INTEGER NOT NULL);
            INSERT OR IGNORE INTO catalog_page_revision VALUES(1,0);
            CREATE TABLE IF NOT EXISTS catalog_pages(cache_key TEXT PRIMARY KEY,revision INTEGER NOT NULL,payload TEXT NOT NULL,touched INTEGER NOT NULL);
            CREATE TABLE catalog_page_members(cache_key TEXT NOT NULL,item TEXT NOT NULL,pair_key TEXT NOT NULL,PRIMARY KEY(cache_key,item)) WITHOUT ROWID;
            CREATE INDEX catalog_page_member_item ON catalog_page_members(item,cache_key);
            CREATE INDEX catalog_page_member_pair ON catalog_page_members(pair_key,cache_key);
            DELETE FROM catalog_pages;
            CREATE TRIGGER catalog_page_prune AFTER DELETE ON catalog_pages BEGIN
                DELETE FROM catalog_page_members WHERE cache_key=OLD.cache_key; END;
            CREATE TRIGGER IF NOT EXISTS catalog_page_insert AFTER INSERT ON items BEGIN
                UPDATE catalog_page_revision SET value=value+1 WHERE id=1; END;
            CREATE TRIGGER IF NOT EXISTS catalog_page_delete AFTER DELETE ON items BEGIN
                UPDATE catalog_page_revision SET value=value+1 WHERE id=1; END;''')
        connection.execute('ALTER TABLE catalog_pages ADD COLUMN cutoff REAL')
        connection.execute('ALTER TABLE catalog_pages ADD COLUMN known_total INTEGER NOT NULL DEFAULT 1')
        if 'serial' not in {row[1] for row in connection.execute('PRAGMA table_info(catalog_page_revision)')}:
            connection.execute('ALTER TABLE catalog_page_revision ADD COLUMN serial INTEGER NOT NULL DEFAULT 0')
        # 修改时间低于页面末项的无关素材不能改变这页的排序或成员。带精确
        # 总数的页面仍保守失效；配对成员即使更旧也必须刷新has_raw/代表图。
        def affected(*refs):
            timestamps=','.join(ref+'.modified' for ref in refs)
            stamp='max('+timestamps+')' if len(refs)>1 else timestamps
            pairs=','.join(ref+'.pair_key' for ref in refs)
            return f'''DELETE FROM catalog_pages WHERE cutoff IS NULL OR known_total=1 OR cutoff<={stamp}
                OR cache_key IN (SELECT cache_key FROM catalog_page_members WHERE pair_key IN ({pairs}));'''
        for event,refs in [('INSERT',('NEW',)),('DELETE',('OLD',))]:
            name='catalog_page_'+event.lower();connection.execute('DROP TRIGGER '+name)
            connection.execute(f'CREATE TRIGGER {name} AFTER {event} ON items BEGIN UPDATE catalog_page_revision SET value=value+1 WHERE id=1;'+affected(*refs)+' END')
        # 扫描的到访时间/epoch不改变显示内容，不能让每次开机核对废弃全部浏览页。
        fields=('root','path','relative','folder','extension','modified','pair_key','online','sku','normalized','source','media_type')
        changed=' OR '.join(f'old.{name} IS NOT new.{name}' for name in fields)
        connection.execute('DROP TRIGGER IF EXISTS catalog_page_update')
        connection.execute('CREATE TRIGGER catalog_page_update AFTER UPDATE OF '+','.join(fields)+' ON items WHEN '+changed+''' BEGIN
            UPDATE catalog_page_revision SET value=value+1 WHERE id=1;'''+affected('OLD','NEW')+' END')
        details=('name','size','fingerprint','confidence','media_format','codec','duration_ms','width','height','inspected_fingerprint')
        changed=' OR '.join(f'old.{name} IS NOT new.{name}' for name in details)
        connection.execute('CREATE TRIGGER catalog_page_details AFTER UPDATE OF '+','.join(details)+' ON items WHEN '+changed+''' BEGIN
            UPDATE catalog_page_revision SET serial=serial+1 WHERE id=1;
            DELETE FROM catalog_pages WHERE cache_key IN (SELECT cache_key FROM catalog_page_members WHERE item=NEW.id OR pair_key=NEW.pair_key); END''')
        for table in ('roots','exclusions'):
            for event in ('INSERT','DELETE','UPDATE'):
                when=" WHEN old.path IS NOT new.path OR old.removed IS NOT new.removed" if table=='roots' and event=='UPDATE' else ''
                name=f'catalog_page_{table}_{event.lower()}';connection.execute('DROP TRIGGER IF EXISTS '+name)
                connection.execute(f'CREATE TRIGGER {name} AFTER {event} ON {table}'+when+''' BEGIN
                    UPDATE catalog_page_revision SET value=value+1 WHERE id=1; DELETE FROM catalog_pages; END''')

    def read(self,key):
        with closing(sqlite3.connect(self.catalog.db.as_uri()+'?mode=ro',uri=True,timeout=.1)) as c:
            row=c.execute('''SELECT r.value,p.payload,r.serial FROM catalog_page_revision r LEFT JOIN catalog_pages p
                ON p.cache_key=? WHERE r.id=1''',(key,)).fetchone()
        value=json.loads(row[1]) if row and row[1] else None
        return ((row[0],row[2]) if row else None),value

    def write(self,key,revision,value):
        payload=json.dumps(value,ensure_ascii=False,separators=(',',':'))
        if len(payload.encode('utf8'))>512*1024:return
        # 页面返回不能等待另一写事务；失败留给下一次访问补记。
        try:
            with closing(sqlite3.connect(self.catalog.db,timeout=0)) as c:
                c.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE,True)
                c.execute('PRAGMA wal_autocheckpoint=0')
                with c:
                    cutoff=min((row['modified'] for row in value['rows']),default=None)
                    inserted=c.execute('''INSERT OR REPLACE INTO catalog_pages(cache_key,revision,payload,touched,cutoff,known_total)
                        SELECT ?,value,?,?,?,? FROM catalog_page_revision
                        WHERE id=1 AND value=? AND serial=?''',(key,payload,time.time_ns(),cutoff,int(value.get('total') is not None),*revision))
                    if inserted.rowcount!=1:return
                    c.execute('DELETE FROM catalog_page_members WHERE cache_key=?',(key,))
                    c.executemany('''INSERT OR IGNORE INTO catalog_page_members SELECT ?,?,? WHERE EXISTS
                        (SELECT 1 FROM catalog_pages WHERE cache_key=? AND revision=?)''',
                        [(key,row['id'],row['pair_key'],key,revision[0]) for row in value['rows']])
                    c.execute('DELETE FROM catalog_pages WHERE cache_key NOT IN (SELECT cache_key FROM catalog_pages ORDER BY touched DESC LIMIT 8)')
        except sqlite3.Error:pass
