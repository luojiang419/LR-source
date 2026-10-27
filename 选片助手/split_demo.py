"""隔离源码演示启动器：真实选片服务 + 独立受鉴权的参考库设备。"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'build/split-capture-demo'
os.environ['FRAMEKEEP_APP_ROOT'] = str(ROOT)
os.environ['FRAMEKEEP_DATA'] = str(BASE / 'capture-data')
os.environ['FRAMEKEEP_CONFIG'] = str(BASE / 'empty-model-config.md')
# 不继承正式安装的模型配置或桌面监护进程。
for name in ('FRAMEKEEP_DESKTOP_PID', 'FRAMEKEEP_DESKTOP_TOKEN'):
    os.environ.pop(name, None)

from model_providers import Providers
Providers(BASE/'capture-data').set_builtin(False)
import server
from split_resources import ResourceLibrary, SUFFIX
from everything_search import EverythingSearch
from types import SimpleNamespace


def query_dict(path):
    return {k: v[0] for k,v in parse_qs(urlsplit(path).query).items()}


def resource_response(library, route, query, allowed=None):
    if route=='roots':
        return library.roots(allowed)
    root, folder = query.get('root', ''), query.get('folder', '')
    if route=='folders':
        return library.folders(root, folder, allowed)
    if route=='items':
        return library.items(root, folder, query.get('q', ''), query.get('offset', 0), allowed)
    if route=='preview':
        return library.preview(root, query.get('file', ''), query.get('size', 420), allowed)
    raise ValueError('接口不存在')


class PeerHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        try:
            if not urlsplit(self.path).path.startswith(SUFFIX+'/'):
                raise ValueError('接口不存在')
            token = self.headers.get('Authorization', '').removeprefix('Bearer ')
            allowed = PEER.authorize(token)
            result = resource_response(PEER, urlsplit(self.path).path.rsplit('/',1)[-1], query_dict(self.path), allowed)
            self.send(result)
        except PermissionError as error:
            self.send({'error': str(error)}, 401)
        except (ValueError, OSError) as error:
            self.send({'error': str(error)}, 400)

    def send(self, result, status=200):
        raw = result if isinstance(result, bytes) else json.dumps(result, ensure_ascii=False).encode('utf8')
        self.send_response(status)
        self.send_header('Content-Type', 'image/jpeg' if isinstance(result, bytes) else 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(raw)


class DemoHandler(server.Handler):
    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if not self.trusted():
            return self.reply({'error': '本机 Host 不匹配'}, 403)
        try:
            if path in ('/split-capture.js','/split-capture.css'):
                payload=(ROOT/'演示方案/split-demo-legacy'/path.lstrip('/')).read_bytes()
                return self.reply(payload,kind='text/javascript; charset=utf-8' if path.endswith('.js') else 'text/css; charset=utf-8')
            if path.startswith(SUFFIX+'/'):
                allowed = LOCAL.authorize(self.headers.get('Authorization','').removeprefix('Bearer '))
                result = resource_response(LOCAL, path.rsplit('/',1)[-1], query_dict(self.path), allowed)
                return self.reply(result, kind='image/jpeg' if isinstance(result,bytes) else 'application/json; charset=utf-8')
            if path in ('/', '/index.html'):
                html = (server.WEB / 'index.html').read_text(encoding='utf8')
                # 当前启动器由 boot.js 加载分屏模块，避免静态脚本提前执行或重复初始化。
                return self.reply(html.encode('utf8'), kind='text/html; charset=utf-8')
            if path=='/api/split/config':
                return self.reply({**LOCAL.public_config(), 'endpoint': f'http://127.0.0.1:{self.server.server_port}{SUFFIX}', 'demo': True})
            if path=='/api/split/sources':
                devices = [{'id': 'local', 'name': '本机参考库', **LOCAL.roots()}]
                for peer in LOCAL.config['peers']:
                    try:
                        devices.append({**LOCAL.remote(peer, 'roots'), 'id': peer['id'], 'name': peer['name'], 'remote': True})
                    except (HTTPError, URLError, ValueError, OSError):
                        devices.append({'id': peer['id'], 'name': peer['name'], 'remote': True, 'roots': [], 'error': '设备离线、连接超时或密钥已停用'})
                return self.reply({'devices': devices})
            if path.startswith('/api/split/library/'):
                route, query = path.rsplit('/',1)[-1], query_dict(self.path)
                device = query.pop('device', 'local')
                if device=='local':
                    result = resource_response(LOCAL, route, query)
                else:
                    peer = next(p for p in LOCAL.config['peers'] if p['id']==device)
                    result = LOCAL.remote(peer, route, query)
                return self.reply(result, kind='image/jpeg' if isinstance(result, bytes) else 'application/json; charset=utf-8')
        except PermissionError as error:
            return self.reply({'error': str(error)}, 401)
        except (ValueError, OSError, StopIteration) as error:
            message = '设备鉴权失败，请检查 API Key' if isinstance(error, HTTPError) and error.code==401 else str(error) or '设备已移除'
            return self.reply({'error': message}, 400)
        return super().do_GET()

    def do_POST(self):
        path = urlsplit(self.path).path
        if not path.startswith('/api/split/'):
            return super().do_POST()
        origin = f'http://127.0.0.1:{self.server.server_port}'
        if not self.trusted() or self.headers.get('X-Framekeep-Token')!=server.TOKEN or self.headers.get('Origin', origin)!=origin:
            return self.reply({'error': '请从本机演示工作台发起操作'}, 403)
        try:
            length = int(self.headers.get('Content-Length','0'))
            if not 0 < length < 16000:
                raise ValueError('请求大小无效')
            body = json.loads(self.rfile.read(length))
            action = path.rsplit('/',1)[-1]
            if action=='add-root':
                result = LOCAL.add_root(body.get('path', ''), body.get('name', ''))
            elif action=='create-key':
                result = LOCAL.create_key(body.get('name', ''), body.get('roots', []))
            elif action=='reveal-key':
                result = {'api_key': LOCAL.reveal(body['id'])}
            elif action=='disable-key':
                with LOCAL.lock:
                    next(k for k in LOCAL.config['keys'] if k['id']==body['id'])['disabled'] = True
                    LOCAL.save()
                result = {'ok': True}
            elif action=='add-peer':
                result = LOCAL.add_peer(body.get('name'), body.get('address'), body.get('api_key'))
            elif action=='remove-peer':
                with LOCAL.lock:
                    LOCAL.config['peers'] = [p for p in LOCAL.config['peers'] if p['id']!=body['id']]
                    LOCAL.save()
                result = {'ok': True}
            else:
                raise ValueError('操作不存在')
            return self.reply(result)
        except (ValueError, OSError, StopIteration, KeyError, TypeError) as error:
            message = '连接失败：对方拒绝了 API Key' if isinstance(error, HTTPError) and error.code==401 else str(error) or '数据不存在或密钥已停用'
            return self.reply({'error': message}, 400)


def prepare(source):
    samples = source / 'build/local-model-validation'
    names = ['XSY_5194.jpg','XSY_5234.jpg','XSY_5294.jpg','XSY_5378.jpg','XSY_5458.jpg','XSY_5174.jpg']
    provenance = []
    for group in ('current/2026-10-23', 'local/秋冬棚拍/造型参考', 'peer/摄影参考库/秋冬女装/棚拍', 'peer/品牌视觉库/秋冬企划'):
        target = BASE / 'samples' / group
        target.mkdir(parents=True, exist_ok=True)
        selected = names if group.startswith('current') else names[:4] if '摄影' in group or 'local' in group else names[3:]
        for name in selected:
            original = samples / name
            if not original.is_file():
                original = None
            destination = target / name
            if not destination.exists():
                if original:
                    shutil.copy2(original, destination)
                else:
                    from PIL import Image,ImageDraw
                    number=names.index(name)+1
                    image=Image.new('RGB',(640,480),('#8b273c','#3e5d85','#7c734a')[(number-1)%3])
                    draw=ImageDraw.Draw(image);draw.rectangle((200,80,440,400),outline='white',width=8)
                    draw.text((24,24),f'GENERATED DEMO SAMPLE {number}',fill='white');image.save(destination)
            provenance.append({'original': str(original) if original else None, 'sample': str(destination),
                               'source': 'existing-validation-photo' if original else 'generated-demo-pattern'})
    runtime = ROOT / '.runtime/everything'
    runtime.mkdir(parents=True, exist_ok=True)
    for name in ('Everything64.dll','Everything32.dll'):
        original = source / '.runtime/everything' / name
        if original.is_file() and not (runtime/name).exists():
            shutil.copy2(original, runtime/name)
    BASE.mkdir(parents=True, exist_ok=True)
    (BASE/'sample-provenance.json').write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding='utf8')


def main():
    global LOCAL, PEER, PEER_PORT
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default=str(ROOT))
    parser.add_argument('--port', type=int, default=8792)
    parser.add_argument('--peer-port', type=int, default=8793)
    parser.add_argument('--browser', action='store_true')
    args = parser.parse_args()
    if args.browser:
        try:
            with urlopen(f'http://127.0.0.1:{args.port}/api/split/config', timeout=1) as response:
                existing = json.load(response)
            if existing.get('demo') is True:
                import webbrowser
                webbrowser.open(f'http://127.0.0.1:{args.port}/')
                return
        except (OSError, ValueError):
            pass
    prepare(Path(args.source))
    os.environ['FRAMEKEEP_EXIFTOOL'] = str(server.media.find_exiftool(Path(args.source)))
    search = EverythingSearch(SimpleNamespace(store=SimpleNamespace(root=ROOT)))
    LOCAL = ResourceLibrary(BASE/'local-library', '拍摄工作站', search)
    PEER = ResourceLibrary(BASE/'peer-library', '参考资料工作站', search)
    LOCAL.add_root(BASE/'samples/local', '本机参考盘')
    for name in ('摄影参考库', '品牌视觉库'):
        PEER.add_root(BASE/'samples/peer'/name, name)
    PEER_PORT = args.peer_port
    # 两个独立数据库与真正的 Bearer HTTP 请求；仅监听回环，演示不公开本机资料。
    peer_server = ThreadingHTTPServer(('127.0.0.1', PEER_PORT), PeerHandler)
    peer_server.daemon_threads = True
    threading.Thread(target=peer_server.serve_forever, daemon=True).start()
    if not LOCAL.config['peers']:
        token = PEER.create_key('拍摄工作站 · 演示连接', [r['id'] for r in PEER.config['roots']])['api_key']
        LOCAL.add_peer('参考资料工作站', f'127.0.0.1:{PEER_PORT}', token)
    server.Handler = DemoHandler
    sys.argv = [sys.argv[0], '--port', str(args.port), '--folder', str(BASE/'samples/current/2026-10-23'), '--browser' if args.browser else '--no-browser']
    print(f'分屏拍摄隔离演示：http://127.0.0.1:{args.port}/', flush=True)
    try:
        server.main()
    finally:
        peer_server.shutdown()
        peer_server.server_close()


if __name__=='__main__':
    main()
