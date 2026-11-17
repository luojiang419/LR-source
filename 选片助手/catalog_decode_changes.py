"""与素材索引同事务记录预览变化，跨进程恢复提交后尚未完成的入队。"""


def initialize(connection):
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_preview_changes'").fetchone():return
    connection.executescript('''
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS catalog_preview_changes(pair_key TEXT PRIMARY KEY,version TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS catalog_preview_insert AFTER INSERT ON items WHEN NEW.online=1 BEGIN
          INSERT INTO catalog_preview_changes VALUES(NEW.pair_key,lower(hex(randomblob(16))))
          ON CONFLICT(pair_key) DO UPDATE SET version=excluded.version;
        END;
        CREATE TRIGGER IF NOT EXISTS catalog_preview_update AFTER UPDATE OF fingerprint,pair_key,root,online ON items
        WHEN OLD.fingerprint IS NOT NEW.fingerprint OR OLD.pair_key IS NOT NEW.pair_key
          OR OLD.root IS NOT NEW.root OR OLD.online IS NOT NEW.online BEGIN
          INSERT INTO catalog_preview_changes VALUES(OLD.pair_key,lower(hex(randomblob(16))))
          ON CONFLICT(pair_key) DO UPDATE SET version=excluded.version;
          INSERT INTO catalog_preview_changes VALUES(NEW.pair_key,lower(hex(randomblob(16))))
          ON CONFLICT(pair_key) DO UPDATE SET version=excluded.version;
        END;
        CREATE TRIGGER IF NOT EXISTS catalog_preview_delete AFTER DELETE ON items BEGIN
          INSERT INTO catalog_preview_changes VALUES(OLD.pair_key,lower(hex(randomblob(16))))
          ON CONFLICT(pair_key) DO UPDATE SET version=excluded.version;
        END;
        COMMIT;
    ''')


def acknowledge(catalog, versions):
    if not versions:return
    with catalog.connect() as connection:
        connection.executemany('DELETE FROM catalog_preview_changes WHERE pair_key=? AND version=?',versions)
