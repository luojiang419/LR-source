"""分屏演示的只读图片数据库；独立持久化、DPAPI 密钥和有界目录访问。"""
from __future__ import annotations

import base64
import ctypes
from contextlib import contextmanager
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from PIL import Image, ImageOps

SUFFIX = '/api/library/v1'
EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff', '.bmp'}


def normalize_address(value):
    text = str(value).strip()
    parsed = urlsplit(text if '://' in text else 'http://' + text)
    try:
        address = ipaddress.IPv4Address(parsed.hostname or '')
        private = address.is_loopback or any(address in ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
        port = parsed.port if parsed.port is not None else 8793
    except ValueError:
        raise ValueError('请输入局域网 IPv4 地址及端口，例如 192.168.1.23:8793') from None
    if not private or parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path.rstrip('/') not in ('', SUFFIX):
        raise ValueError('仅支持局域网 IP，接口后缀由程序补齐')
    if not 1 <= port <= 65535:
        raise ValueError('端口须在 1–65535 之间')
    return f'{parsed.scheme}://{address}:{port}{SUFFIX}'


class Blob(ctypes.Structure):
    _fields_ = [('size', ctypes.c_uint32), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def protect(value, decrypt=False):
    """沿用 MiniMax 的本机可查看模式；磁盘上只存 Windows 用户加密密文。"""
    raw = base64.b64decode(value) if decrypt else value.encode('utf8')
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError('Windows 用户密钥加密失败')
    try:
        result = ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)
    return result.decode('utf8') if decrypt else base64.b64encode(result).decode('ascii')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('设备接口不能重定向，请检查 IP 和端口')


class ResourceLibrary:
    def __init__(self, data, name, everything=None):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        self.name, self.everything = name, everything
        self.lock = threading.RLock()
        self.config_path = self.data / 'resource-config.json'
        self.config = json.loads(self.config_path.read_text(encoding='utf8')) if self.config_path.exists() else {'roots': [], 'keys': [], 'peers': []}
        self.db = self.data / 'resource-index.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS files(root TEXT, relative TEXT, folder TEXT, name TEXT, PRIMARY KEY(root,relative))')
            db.execute('CREATE INDEX IF NOT EXISTS files_folder ON files(root,folder,name)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self):
        temporary = self.config_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding='utf8')
        temporary.replace(self.config_path)

    def create_key(self, name, roots):
        name = str(name).strip()
        if not name or len(name) > 48:
            raise ValueError('请填写 1–48 字符的调用方名称')
        known = {r['id'] for r in self.config['roots']}
        if not isinstance(roots, list) or not roots or not set(roots) <= known:
            raise ValueError('至少分配一个已添加的共享目录')
        token = 'fk_' + secrets.token_urlsafe(30)
        row = {'id': secrets.token_hex(8), 'name': name, 'roots': roots, 'hash': hashlib.sha256(token.encode()).hexdigest(), 'secret': protect(token), 'prefix': token[:10], 'disabled': False, 'created': time.strftime('%Y-%m-%d %H:%M')}
        with self.lock:
            self.config['keys'].append(row)
            self.save()
        return {'id': row['id'], 'api_key': token}

    def reveal(self, key_id):
        row = next(k for k in self.config['keys'] if k['id'] == key_id and not k['disabled'])
        return protect(row['secret'], True)

    def authorize(self, token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = next((k for k in self.config['keys'] if not k['disabled'] and secrets.compare_digest(digest, k['hash'])), None)
        if row is None:
            raise PermissionError('API Key 无效或已停用')
        return set(row['roots'])

    def resolve(self, root_id, relative='', allowed=None):
        if allowed is not None and root_id not in allowed:
            raise PermissionError('此 API Key 未分配该共享目录')
        row = next((r for r in self.config['roots'] if r['id'] == root_id), None)
        if row is None:
            raise ValueError('共享目录不存在')
        root = Path(row['path']).resolve()
        relative = str(relative).replace('\\', '/')
        if relative.startswith('/') or ':' in relative or '..' in relative.split('/'):
            raise ValueError('目录路径无效')
        candidate = root / relative
        # 不通过目录链接越过授权边界，即使链接最终指回共享目录也不跟随。
        for part in [candidate, *candidate.parents]:
            if part == root:
                break
            if part.is_symlink() or part.is_junction():
                raise ValueError('不访问目录链接')
        target = candidate.resolve()
        if not target.is_relative_to(root):
            raise ValueError('超出共享目录范围')
        if not target.exists():
            raise ValueError('设备目录离线或文件已移动')
        return root, target

    def add_root(self, path, alias):
        root = Path(str(path)).resolve(strict=True)
        if not root.is_dir() or root.is_symlink() or root.is_junction():
            raise ValueError('请选择真实文件夹或分区')
        alias = str(alias).strip()[:48] or root.name or '共享资料'
        with self.lock:
            row = next((r for r in self.config['roots'] if r['path'] == str(root)), None)
            if row is None:
                row = {'id': secrets.token_hex(8), 'path': str(root), 'name': alias}
                self.config['roots'].append(row)
            self.save()
        self.scan(row)
        return {'root_id': row['id']}

    def scan(self, row):
        root, _ = self.resolve(row['id'])
        seen = set()
        paths = []
        if self.everything:
            paths.extend(self.everything.candidates(root, limit=2048))
        # 演示有界索引。真实百万库沿用生产 AssetCatalog 的后台分页索引。
        for parent, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [n for n in dirs if not n.startswith('.') and not (Path(parent)/n).is_symlink() and not (Path(parent)/n).is_junction()]
            paths.extend(Path(parent)/name for name in files if Path(name).suffix.lower() in EXTENSIONS)
            if len(paths) >= 20000:
                break
        with self.connect() as db:
            db.execute('DELETE FROM files WHERE root=?', (row['id'],))
            for path in paths[:20000]:
                try:
                    relative = path.relative_to(root).as_posix()
                    self.resolve(row['id'], relative)
                    if not path.is_file() or path.suffix.lower() not in EXTENSIONS or relative in seen:
                        continue
                    seen.add(relative)
                    db.execute('INSERT INTO files VALUES(?,?,?,?)', (row['id'], relative, str(Path(relative).parent).replace('\\', '/').removeprefix('./'), path.name))
                except (OSError, ValueError):
                    continue
        with self.lock:
            row['count'] = len(seen)
            row['limited'] = len(paths) >= 20000
            self.save()

    def roots(self, allowed=None):
        return {'device': self.name, 'roots': [{k: r.get(k) for k in ('id', 'name', 'count', 'limited')} for r in self.config['roots'] if allowed is None or r['id'] in allowed]}

    def folders(self, root_id, folder='', allowed=None):
        root, target = self.resolve(root_id, folder, allowed)
        with os.scandir(target) as entries:
            rows = []
            for item in entries:
                if not item.name.startswith('.') and item.is_dir(follow_symlinks=False) and not Path(item.path).is_junction():
                    rows.append({'name': item.name, 'folder': Path(item.path).relative_to(root).as_posix()})
                if len(rows) >= 400:
                    break
        return {'folders': sorted(rows, key=lambda r: r['name'].casefold())}

    def items(self, root_id, folder='', text='', offset=0, allowed=None):
        started = time.perf_counter()
        root, _ = self.resolve(root_id, folder, allowed)
        text = str(text).strip()[:180]
        offset = max(0, min(20000, int(offset)))
        engine, note = '目录数据库', '已索引图片'
        if text and self.everything:
            result = self.everything._request({'root': str(root), 'text': text, 'limit': 2048, 'offset': 0})
            if result.get('available'):
                engine, note = 'Everything + 目录数据库', 'Everything 候选与目录索引合并'
                # 索引遗漏由持久目录数据库补齐，禁止将索引结果当作访问权限。
                with self.connect() as db:
                    for name in result.get('paths', []):
                        try:
                            path = Path(name)
                            relative = path.relative_to(root).as_posix()
                            self.resolve(root_id, relative, allowed)
                            if path.is_file() and path.suffix.lower() in EXTENSIONS:
                                db.execute('INSERT OR IGNORE INTO files VALUES(?,?,?,?)', (root_id, relative, path.parent.relative_to(root).as_posix(), path.name))
                        except (OSError, ValueError):
                            pass
            else:
                note = result.get('message', 'Everything 不可用，使用目录索引')
        where, args = 'root=?', [root_id]
        if text:
            where += " AND instr(lower(relative),lower(?))>0"
            args.append(text)
            if folder:
                where += " AND (folder=? OR substr(folder,1,?)=?)"
                args.extend([folder, len(folder)+1, folder+'/'])
        else:
            where += ' AND folder=?'
            args.append(folder or '.')
        with self.connect() as db:
            total = db.execute('SELECT COUNT(*) FROM files WHERE '+where, args).fetchone()[0]
            rows = [dict(r) for r in db.execute('SELECT relative,name,folder FROM files WHERE '+where+' ORDER BY name LIMIT 60 OFFSET ?', [*args, offset])]
        return {'rows': rows, 'total': total, 'offset': offset, 'has_more': offset+len(rows)<total, 'engine': engine, 'note': note, 'elapsed_ms': round((time.perf_counter()-started)*1000, 1)}

    def preview(self, root_id, relative, size=420, allowed=None):
        _, path = self.resolve(root_id, relative, allowed)
        if path.suffix.lower() not in EXTENSIONS:
            raise ValueError('此演示支持 JPG、PNG、WebP、TIFF 与 BMP 参考图')
        size = max(80, min(1800, int(size)))
        with Image.open(path) as source:
            source.draft('RGB', (size, size))
            image = ImageOps.exif_transpose(source)
            image.thumbnail((size, size))
            output = io.BytesIO()
            image.convert('RGB').save(output, format='JPEG', quality=86)
        return output.getvalue()

    def public_config(self):
        return {'roots': self.config['roots'], 'keys': [{k: v for k,v in r.items() if k not in ('secret','hash')} for r in self.config['keys']], 'peers': [{k:v for k,v in r.items() if k!='secret'} for r in self.config['peers']], 'device': self.name, 'everything': self.everything.status() if self.everything else {'available': False, 'message': '使用设备目录数据库'}}

    def remote(self, peer, route, query=None):
        url = normalize_address(peer['address']) + '/' + route + ('?'+urlencode(query) if query else '')
        request = Request(url, headers={'Authorization': 'Bearer '+protect(peer['secret'], True)})
        with build_opener(NoRedirect()).open(request, timeout=6) as response:
            raw = response.read(12*1024*1024+1)
            if len(raw)>12*1024*1024:
                raise ValueError('设备返回内容过大')
            return raw if route=='preview' else json.loads(raw)

    def add_peer(self, name, address, token):
        address = normalize_address(address)
        if not str(token).startswith('fk_') or len(str(token)) > 256:
            raise ValueError('请填写对方创建并分配的 API Key')
        row = {'id': secrets.token_hex(8), 'name': str(name).strip()[:48] or '局域网设备', 'address': address, 'secret': protect(token)}
        result = self.remote(row, 'roots')
        if not isinstance(result.get('roots'), list):
            raise ValueError('对方不是兼容的末帧文件数据库')
        with self.lock:
            self.config['peers'].append(row)
            self.save()
        return {'id': row['id'], **result}
