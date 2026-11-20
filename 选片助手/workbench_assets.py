"""保持既有脚本顺序的单请求工作台，源码更新时自动重建内存载荷。"""
import ast
import gzip
import hashlib
import re
import sys
import threading


class WorkbenchAssets:
    def __init__(self,web):
        self.web=web;self.lock=threading.Lock();self.version=None;self.payload=None
        self.boot_version=None;self.modules=[];self.etag=None
        self.compressed=None

    def read(self,*,compressed=False):
        with self.lock:
            if self.payload is not None and getattr(sys,'frozen',False):
                return self._response(compressed)
            boot_path=self.web/'boot.js';stat=boot_path.stat();boot_version=(stat.st_size,stat.st_mtime_ns)
            if boot_version!=self.boot_version:
                boot=boot_path.read_text(encoding='utf8')
                modules=ast.literal_eval(re.search(r'const modules=(\[[^;]+\]);',boot).group(1))
                modules+=re.findall(r"modules.push\('([^']+)'\)",boot)
                if any('/' in name or '\\' in name or not name.endswith('.js') for name in modules):raise ValueError('工作台模块名无效')
                self.modules=modules;self.boot_version=boot_version
            files=[self.web/name for name in self.modules]
            version=tuple((p.name,p.stat().st_size,p.stat().st_mtime_ns) for p in files)
            if version!=self.version:
                self.payload='\n;\n'.join(p.read_text(encoding='utf8') for p in files).encode()
                self.etag=hashlib.sha256(self.payload).hexdigest()
                self.compressed=None
                self.version=version
            return self._response(compressed)

    def _response(self,compressed):
        if compressed:
            if self.compressed is None:self.compressed=gzip.compress(self.payload,compresslevel=6,mtime=0)
            return self.compressed,self.etag+'-gzip'
        return self.payload,self.etag


def accepts_gzip(value):
    for entry in value.lower().split(','):
        name,*parameters=entry.strip().split(';')
        if name.strip()!='gzip':continue
        try:return next((float(p.split('=',1)[1]) for p in parameters if p.strip().startswith('q=')),1)>0
        except ValueError:return False
    return False
