"""保持既有脚本顺序的单请求工作台，源码更新时自动重建内存载荷。"""
import ast
import hashlib
import re
import threading


class WorkbenchAssets:
    def __init__(self,web):
        self.web=web;self.lock=threading.Lock();self.version=None;self.payload=None

    def read(self):
        with self.lock:
            boot=(self.web/'boot.js').read_text(encoding='utf8')
            modules=ast.literal_eval(re.search(r'const modules=(\[[^;]+\]);',boot).group(1))
            modules+=re.findall(r"modules.push\('([^']+)'\)",boot)
            if any('/' in name or '\\' in name or not name.endswith('.js') for name in modules):raise ValueError('工作台模块名无效')
            files=[self.web/name for name in modules]
            version=tuple((p.name,p.stat().st_size,p.stat().st_mtime_ns) for p in files)
            if version!=self.version:
                self.payload='\n;\n'.join(p.read_text(encoding='utf8') for p in files).encode()
                self.version=version
            return self.payload,hashlib.sha256(self.payload).hexdigest()
