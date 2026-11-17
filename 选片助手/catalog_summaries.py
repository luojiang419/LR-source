"""独立于业务写锁的目录/总数快照；只复用相同数据库和结构版本。"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

from file_safety import atomic_write


class CatalogSummaries:
    SCHEMA='framekeep.catalog-summary-v1'
    FILES={'counts':'counts.json','tree:':'tree-all.json','tree:image':'tree-image.json',
           'tree:video':'tree-video.json','tree:design':'tree-design.json'}

    def __init__(self,catalog):
        self.catalog=catalog;self.folder=catalog.data/'catalog-summaries'
        self.database=hashlib.sha256((str(catalog.db.resolve()).casefold()+':'+str(catalog.db.stat().st_ino)).encode()).hexdigest()
        self.lock=threading.Lock()

    @staticmethod
    def _limit(key):return 512*1024 if key=='counts' else 4*1024*1024

    def _load(self,key):
        path=self.folder/self.FILES[key]
        try:
            with path.open('rb') as f:raw=f.read(self._limit(key)+1)
            if len(raw)>self._limit(key):return None
            value=json.loads(raw)
            return value if isinstance(value,dict) and value.get('schema')==self.SCHEMA and value.get('kind')==key else None
        except (OSError,ValueError):return None

    def read(self,key,connection):
        revision=connection.execute('SELECT value FROM catalog_page_revision WHERE id=1').fetchone()[0]
        with self.lock:value=self._load(key)
        payload=value.get('payload') if value and value.get('database')==self.database and value.get('revision')==revision else None
        return revision,payload if isinstance(payload,dict) else None

    def write(self,key,revision,payload):
        content=json.dumps({'schema':self.SCHEMA,'kind':key,'database':self.database,
                            'revision':revision,'payload':payload},ensure_ascii=False,separators=(',',':')).encode('utf8')
        if len(content)>self._limit(key):return
        # 只读确认版本；主库持有写事务时也能发布文件，不在前台排队等待写锁。
        try:
            with self.lock:
                with closing(sqlite3.connect(self.catalog.db.as_uri()+'?mode=ro',uri=True,timeout=0)) as c:
                    if c.execute('SELECT value FROM catalog_page_revision WHERE id=1').fetchone()[0]!=revision:return
                self.folder.mkdir(exist_ok=True)
                if self.folder.is_symlink() or self.folder.is_junction():return
                path=self.folder/self.FILES[key]
                if path.is_symlink() or path.is_junction():return
                if path.exists() and self._load(key) is None:return  # 不覆盖未知文件。
                atomic_write(path,content)
        except (OSError,sqlite3.Error):pass
