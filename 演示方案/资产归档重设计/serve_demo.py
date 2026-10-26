"""演示覆盖层：原引擎、鉴权、工作台和预览链路均来自本次源码快照。"""
import json
import os
from pathlib import Path
import secrets
import server
from archive_engine import ArchiveDemo

OriginalHandler = server.Handler
OriginalCatalog = server.AssetCatalog


class Handler(OriginalHandler):
    def do_GET(self):
        if self.path.startswith('/api/archive-v2/'):
            from urllib.parse import urlsplit, parse_qs
            url = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                return self.reply(server.CATALOG.get(url.path.rsplit('/', 1)[-1], query))
            except (ValueError, KeyError) as error:
                return self.reply({'error': str(error)}, 400)
        return super().do_GET()

    def do_POST(self):
        if not self.path.startswith('/api/archive-v2/'):
            return super().do_POST()
        origin = f'http://127.0.0.1:{self.server.server_port}'
        if not secrets.compare_digest(self.headers.get('X-Framekeep-Token', ''), server.TOKEN) or self.headers.get('Origin', origin) != origin:
            return self.reply({'error': '请从本机演示发起操作'}, 403)
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 65536:
                raise ValueError('请求长度无效')
            body = json.loads(self.rfile.read(size))
            return self.reply(server.CATALOG.post(self.path.rsplit('/', 1)[-1], body))
        except (ValueError, KeyError, OSError) as error:
            return self.reply({'error': str(error)[:500]}, 400)


def make_catalog(data):
    return ArchiveDemo(data, server.ROOT, Path(os.environ['ARCHIVE_V2_BASE']))


server.Handler = Handler
server.AssetCatalog = make_catalog
if __name__ == '__main__':
    server.main()
