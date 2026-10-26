"""拍摄任务内的参考结果与图片临时副本；只清理登记且校验一致的缓存文件。"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
from preview_codec import image_mime

from file_safety import delete_verified, publish_new


class ReferenceCache:
    def __init__(self, folder):
        self.folder = Path(folder).absolute()
        self.folder.mkdir(parents=True, exist_ok=True)
        self._check_folder()
        self.lock = threading.RLock()
        self.stripes = [threading.Lock() for _ in range(32)]
        with self.connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS entries(
                    name TEXT PRIMARY KEY,device TEXT,root TEXT,relative TEXT,kind TEXT,digest TEXT);
                CREATE INDEX IF NOT EXISTS reference_owner ON entries(device,root,relative);
                CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY,generation INTEGER);
                INSERT OR IGNORE INTO state VALUES(1,0);
            ''')
            self.generation = connection.execute('SELECT generation FROM state WHERE id=1').fetchone()[0]
        self.revision = 0

    def _check_folder(self):
        if any(path.is_symlink() or path.is_junction() for path in (self.folder, *self.folder.parents)):
            raise ValueError('临时参考缓存目录不能经过链接')

    @contextmanager
    def connect(self):
        self._check_folder()
        database = self.folder/'index.sqlite3'
        if database.is_symlink() or database.is_junction():
            raise ValueError('临时参考缓存索引不能是链接')
        connection = sqlite3.connect(database, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _path(self, name):
        self._check_folder()
        if not re.fullmatch(r'[0-9a-f]{64}\.(?:jpg|webp|json)', name):
            raise ValueError('临时参考缓存文件名无效')
        path = self.folder/name
        if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(self.folder.resolve()):
            raise ValueError('临时参考缓存文件不能是链接')
        return path

    def get_or_load(self, device, root, relative, kind, params, loader, generation=None):
        with self.lock:
            if generation is not None and str(generation)!=str(self.generation):
                raise ValueError('拍摄任务已切换，旧请求不能写入参考缓存')
            task_generation = self.generation
        encoded = json.dumps([task_generation, device, root, relative, kind, params], ensure_ascii=False, sort_keys=True).encode()
        stem=hashlib.sha256(encoded).hexdigest()
        name = stem+('.webp' if kind=='preview' else '.json')
        with self.stripes[int(name[:8], 16)%len(self.stripes)]:
            with self.lock, self.connect() as connection:
                if task_generation!=self.generation:
                    raise ValueError('拍摄任务已切换，请重新搜索参考图片')
                revision = self.revision
                candidates=(stem+'.webp',stem+'.jpg') if kind=='preview' else (name,name)
                row = connection.execute('SELECT name,digest FROM entries WHERE name IN (?,?) ORDER BY name DESC LIMIT 1', candidates).fetchone()
                if row:name=row['name']
                path = self._path(name)
                if row and path.is_file():
                    raw = path.read_bytes()
                    if hashlib.sha256(raw).hexdigest()==row['digest']:
                        return raw if kind=='preview' else json.loads(raw)
            value = loader()
            raw = value if kind=='preview' else json.dumps(value, ensure_ascii=False).encode('utf8')
            if kind=='preview':name=stem+('.webp' if image_mime(raw)=='image/webp' else '.jpg')
            with self.lock:
                # 清理期间完成的旧请求可以结束，但不得重新写回已清理的缓存。
                if revision!=self.revision:
                    return value
                path = self._path(name)
                if path.exists():
                    return value
                with tempfile.NamedTemporaryFile(dir=self.folder, prefix='.reference-', delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(raw)
                try:
                    try:
                        publish_new(temporary, path)
                    except FileExistsError:
                        return value
                    with self.connect() as connection:
                        connection.execute('INSERT OR REPLACE INTO entries VALUES(?,?,?,?,?,?)',
                                           (name, device, root, relative, kind, hashlib.sha256(raw).hexdigest()))
                finally:
                    temporary.unlink(missing_ok=True)
            return value

    def clear(self, reference=None, device=None):
        with self.lock, self.connect() as connection:
            self.revision += 1
            if reference is not None:
                condition, args = 'device=? AND root=? AND (relative=? OR kind=?)', (
                    reference['device'], reference['root'], reference['relative'], 'items')
            elif device is not None:
                condition, args = 'device=?', (device,)
            else:
                condition, args = '1=1', ()
                self.generation += 1
                connection.execute('UPDATE state SET generation=? WHERE id=1', (self.generation,))
            removed = 0
            for row in connection.execute('SELECT * FROM entries WHERE '+condition, args).fetchall():
                try:
                    path = self._path(row['name'])
                except ValueError:
                    continue
                try:
                    if path.exists() and not delete_verified(path, row['digest']):
                        continue
                except OSError:
                    continue
                connection.execute('DELETE FROM entries WHERE name=?', (row['name'],))
                removed += 1
            return {'removed_files': removed, 'cache_generation': self.generation}
