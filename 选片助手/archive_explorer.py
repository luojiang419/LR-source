"""当前层文件浏览与显式文件操作；不依赖媒体索引或递归建库。"""
import os
from pathlib import Path
import re
import shutil
import threading
import uuid
import tempfile
import sqlite3
from contextlib import contextmanager


class ArchiveExplorer:
    def __init__(self, catalog):
        self.catalog = catalog
        self.lock = threading.RLock()
        self.job_lock = threading.Lock()
        self.jobs = {}
        self.db=catalog.data/'archive-browser.sqlite3'
        with self.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS explorer_counts(root TEXT,folder TEXT,files INTEGER,folders INTEGER,PRIMARY KEY(root,folder))')
            c.execute('CREATE TABLE IF NOT EXISTS visited_directories(root TEXT,folder TEXT,parent TEXT,PRIMARY KEY(root,folder))')

    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.db,timeout=2)
        try:
            c.execute('PRAGMA journal_mode=WAL')
            with c:yield c
        finally:c.close()

    def tree_rows(self):
        with self.connect() as c:
            return list(c.execute('SELECT d.root,d.folder,e.files FROM visited_directories d LEFT JOIN explorer_counts e ON e.root=d.root AND e.folder=d.folder')),set(c.execute('SELECT root,folder FROM explorer_counts'))

    def path(self, root, folder=''):
        path = self.catalog.folder_path(root, folder)
        base = Path(self.catalog._root(root)['path'])
        # resolve 会隐藏链接本身，因此还要逐段拒绝重解析点。
        current = base
        for part in path.relative_to(base).parts:
            current = current / part
            if current.is_symlink() or current.is_junction():
                raise ValueError('不能操作链接或重解析目录')
        return path

    def browse(self, root, folder=''):
        from asset_catalog import ARCHIVE_MEDIA_EXTENSIONS, media_type
        path = self.path(root, folder)
        rows = []
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name.startswith('.') or entry.name in ('#recycle', '$RECYCLE.BIN', 'System Volume Information'):
                    continue
                if entry.is_symlink() or entry.is_junction():
                    continue
                try:
                    directory = entry.is_dir(follow_symlinks=False)
                    extension = Path(entry.name).suffix.lower()
                    # 浏览只列出受支持的图片、视频与 Adobe 素材，伴随文件仍留在磁盘上。
                    if not directory and extension not in ARCHIVE_MEDIA_EXTENSIONS:
                        continue
                    stat = entry.stat(follow_symlinks=False)
                    rows.append({'name': entry.name, 'folder': '/'.join(filter(None, [folder.strip('/'), entry.name])),
                                 'directory': directory, 'size': None if directory else stat.st_size,
                                 'media_type': media_type(extension) if not directory else '',
                                 'modified': stat.st_mtime})
                except FileNotFoundError:
                    continue
        rows.sort(key=lambda r: (not r['directory'], r['name'].casefold()))
        directories = [r for r in rows if r['directory']]
        files = len(rows) - len(directories)
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO explorer_counts VALUES(?,?,?,?)', (root, folder, files, len(directories)))
            # 枚举完成即发布目录，不等待元数据或缩略图。
            old={r[0] for r in c.execute('SELECT folder FROM visited_directories WHERE root=? AND parent=?',(root,folder))}
            for removed in old-{r['folder'] for r in directories}:
                pattern=removed.replace('\\','\\\\').replace('%',r'\%').replace('_',r'\_')+'/%'
                c.execute("DELETE FROM visited_directories WHERE root=? AND (folder=? OR folder LIKE ? ESCAPE '\\')",(root,removed,pattern))
                c.execute("DELETE FROM explorer_counts WHERE root=? AND (folder=? OR folder LIKE ? ESCAPE '\\')",(root,removed,pattern))
            c.execute('DELETE FROM visited_directories WHERE root=? AND parent=?', (root, folder))
            c.executemany('INSERT OR REPLACE INTO visited_directories VALUES(?,?,?)', [(root, r['folder'], folder) for r in directories])
        return {'root': root, 'folder': folder, 'path': str(path), 'rows': rows,
                'files': files, 'folders': len(directories), 'total': len(rows)}

    @staticmethod
    def name(value):
        value = str(value)
        if (not value or value in ('.', '..') or value != value.strip() or value.endswith('.')
                or re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
                or re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', value, re.I)):
            raise ValueError('文件名无效，请勿使用路径、保留名称或特殊字符')
        return value

    def submit(self, body):
        job = uuid.uuid4().hex
        with self.job_lock:
            if any(r['status'] in ('queued','running') for r in self.jobs.values()):
                raise ValueError('已有文件操作正在进行，请等待完成')
            self.jobs = dict(list(self.jobs.items())[-15:])
            self.jobs[job] = {'status':'queued','error':''}
        def run():
            with self.job_lock:self.jobs[job]['status']='running'
            try:
                result=self.operate(body)
                with self.job_lock:self.jobs[job].update(status='complete',result=result)
            except Exception as exc:
                with self.job_lock:self.jobs[job].update(status='failed',error=str(exc))
        self.catalog.visits.pool.submit(run)
        return {'job':job}

    def job(self, job):
        with self.job_lock:
            if job not in self.jobs:raise ValueError('文件操作记录不存在，请刷新目录核对结果')
            return dict(self.jobs[job])

    @staticmethod
    def copy(source, target):
        # 只在完整复制后公开目标名称，失败仅清理本次创建的临时目录。
        with tempfile.TemporaryDirectory(prefix='.shiying-copy-',dir=target.parent) as staging:
            staged=Path(staging)/target.name
            if source.is_dir():shutil.copytree(source,staged)
            else:shutil.copy2(source,staged)
            if target.exists():raise ValueError('目标已存在，复制未覆盖已有文件')
            staged.rename(target)

    def operate(self, body):
        action = body.get('action')
        root, folder = body['root'], body.get('folder', '')
        with self.lock:
            if action in ('rename','move'):
                check=sqlite3.connect(self.catalog.db,timeout=.1)
                try:check.execute('BEGIN IMMEDIATE');check.rollback()
                except sqlite3.OperationalError as exc:raise ValueError('归档索引正在写入，请稍后再重命名或移动；文件尚未改动') from exc
                finally:check.close()
            parent = self.path(root, folder)
            if action == 'mkdir':
                target = parent / self.name(body.get('name', ''))
                target.mkdir()
            elif action in ('rename', 'copy', 'move'):
                source = self.path(body.get('source_root', root), body['source'])
                source_base = Path(self.catalog._root(body.get('source_root', root))['path'])
                if source == source_base:
                    raise ValueError('不能重命名或移动归档根目录')
                if not source.exists():
                    raise ValueError('源文件不存在或已离线')
                target = parent / self.name(body.get('name', source.name))
                if target.exists():
                    raise ValueError('目标已存在，请更换名称；不会覆盖已有文件')
                from asset_catalog import MEDIA_EXTENSIONS
                sidecar = source.with_suffix('.xmp') if source.is_file() and source.suffix.lower() in MEDIA_EXTENSIONS else None
                side_target = target.with_suffix('.xmp')
                if sidecar is not None and sidecar.is_file():
                    if sidecar.is_symlink() or side_target.exists() or side_target == target:
                        raise ValueError('配套 XMP 目标已存在或无效，请更换名称')
                else:
                    sidecar = None
                if source.is_dir() and target.resolve().is_relative_to(source.resolve()):
                    raise ValueError('不能复制或移动到自身的子目录')
                if action == 'copy':
                    if source.is_dir():
                        # 预检链接，避免复制逃出根目录的内容。
                        for base, dirs, files in os.walk(source, followlinks=False):
                            for name in dirs + files:
                                child = Path(base) / name
                                if child.is_symlink() or child.is_junction():
                                    raise ValueError('目录包含链接，无法复制')
                    self.copy(source,target)
                else:
                    # Windows rename 不覆盖；跨盘 move 先拒绝同名。
                    shutil.move(str(source), str(target))
                if sidecar is not None:
                    try:
                        if action == 'copy':
                            self.copy(sidecar,side_target)
                        else:
                            shutil.move(str(sidecar), str(side_target))
                    except Exception:
                        # 主文件刚创建/移动；配套资料失败时恢复主文件。
                        if action == 'copy':
                            target.unlink()
                        else:
                            shutil.move(str(target), str(source))
                        raise
                if action != 'copy':
                    with self.catalog.connect() as c:
                        prefix = str(source).rstrip('\\/') + os.sep
                        pattern = prefix.replace('\\', '\\\\').replace('%', r'\%').replace('_', r'\_') + '%'
                        c.execute("UPDATE items SET online=0 WHERE root=? AND (path=? OR path LIKE ? ESCAPE '\\')", (body.get('source_root', root), str(source), pattern))
                        c.execute('DELETE FROM visited_directories WHERE root=? AND (folder=? OR folder LIKE ? ESCAPE \'\\\')', (body.get('source_root', root), body['source'], body['source'].replace('\\','\\\\').replace('%',r'\%').replace('_',r'\_')+'/%'))
                    with self.connect() as c:c.execute('DELETE FROM explorer_counts WHERE root=?', (body.get('source_root', root),))
            else:
                raise ValueError('不支持的文件操作')
            with self.connect() as c:
                c.execute('DELETE FROM explorer_counts WHERE root=? AND folder=?', (root, folder))
            visits=self.catalog.visits
            with visits.lock:
                if visits.key == (root,folder):visits.touch(root, folder, refresh=True)
            return {'ok': True, 'path': str(target)}

    def open(self, root, folder, reveal=False):
        path = self.path(root, folder)
        if not path.exists():
            raise ValueError('文件不存在或位置已离线')
        if reveal:
            import subprocess
            subprocess.Popen(['explorer.exe', '/select,', str(path)])
        else:
            os.startfile(str(path))
        return {'ok': True}
