"""参考库的本机管理路由及独立 LAN 只读鉴权服务。"""
import ipaddress
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from urllib.parse import parse_qs, urlsplit

from reference_library import SUFFIX
from preview_codec import compatible_image,image_mime


def resource(library, route, query, allowed=None):
    if route=='roots':
        return library.roots(allowed)
    root, folder = query.get('root', ''), query.get('folder', '')
    if route=='folders':
        return library.folders(root, folder, allowed)
    if route=='items':
        return library.items(root, folder, query.get('q', ''), query.get('offset', 0), query.get('cursor', ''), allowed)
    if route=='preview':
        return library.preview(root, query.get('file', ''), query.get('size', 420), allowed)
    raise ValueError('参考库接口不存在')


def local_get(library, route, query):
    if library is None:
        raise ValueError('参考资源服务未启动')
    if route=='config':
        return library.public_config()
    if route=='sources':
        return library.sources()
    if route=='preferences':
        return library.preferences()
    if route=='search':
        return library.search_all(query.get('q',''),query.get('cursor',''),query.get('cache_generation'))
    if route.startswith('library/'):
        query = dict(query)
        device = query.pop('device', 'local')
        action = route.removeprefix('library/')
        return library.client_resource(device, action, query)
    raise ValueError('参考库接口不存在')


def local_post(library, action, body):
    if library is None:
        raise ValueError('参考资源服务未启动')
    if not isinstance(body, dict):
        raise ValueError('请求格式不正确')
    if action=='add-root':
        return library.add_root(body.get('path', ''), body.get('name', ''))
    if action=='everything':
        return library.set_everything(body.get('enabled'))
    if action=='index-storage':
        return library.storage.start(body.get('path'))
    if action=='add-roots':
        return library.add_roots(body.get('entries'))
    if action=='root-visibility':
        return library.set_root_visibility(body.get('id', ''), body.get('visible'))
    if action=='remove-root':
        return library.remove_root(body.get('id', ''))
    if action=='rescan':
        return library.rescan(body.get('id', ''))
    if action=='create-key':
        return library.create_key(body.get('name'), body.get('roots'))
    if action=='assign-key':
        return library.assign_key(body.get('id', ''), body.get('roots'))
    if action=='reveal-key':
        return {'api_key': library.reveal(body.get('id', ''))}
    if action=='disable-key':
        return library.disable_key(body.get('id', ''))
    if action=='add-peer':
        return library.add_peer(body.get('name'), body.get('address'), body.get('api_key'))
    if action=='remove-peer':
        return library.remove_peer(body.get('id', ''))
    if action=='sharing':
        return library.configure_sharing(body.get('enabled'), body.get('port'))
    if action=='preferences':
        return library.save_preferences(body)
    if action=='reveal':
        return library.reveal_reference(body.get('device', 'local'), body.get('root', ''), body.get('file', ''))
    if action=='clear-reference':
        return library.clear_reference(body, body.get('cache_generation'))
    raise ValueError('参考库操作不存在')


class SharedServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, address, handler):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(10)
        return request, address

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class SharedHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value, status=200):
        binary = isinstance(value, bytes)
        if binary:value=compatible_image(value,self.headers.get('Accept',''))
        raw = value if binary else json.dumps(value, ensure_ascii=False).encode('utf8')
        self.send_response(status)
        self.send_header('Content-Type', image_mime(raw) if binary else 'application/json; charset=utf-8')
        if binary:self.send_header('Vary','Accept')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (OSError, TimeoutError):
            pass

    def do_POST(self):
        self.reply({'error': '共享服务只提供读取接口'}, 405)

    def do_GET(self):
        try:
            address = ipaddress.ip_address(self.client_address[0])
            if not address.is_private and not address.is_loopback or self.headers.get('Origin'):
                return self.reply({'error': '共享服务仅支持局域网应用访问'}, 403)
            parsed = urlsplit(self.path)
            route = parsed.path.removeprefix(SUFFIX+'/')
            if not parsed.path.startswith(SUFFIX+'/') or route not in ('roots', 'folders', 'items', 'preview'):
                return self.reply({'error': '接口不存在'}, 404)
            authorization = self.headers.get('Authorization', '')
            if not authorization.startswith('Bearer ') or len(authorization)>256:
                raise PermissionError('API Key 无效或已停用')
            allowed = self.server.library.authorize(authorization[7:])
            query = {key: values[0] for key, values in parse_qs(parsed.query, max_num_fields=16).items()}
            result = resource(self.server.library, route, query, allowed)
            return self.reply(result)
        except PermissionError as error:
            return self.reply({'error': str(error)}, 401)
        except (ValueError, OSError, RuntimeError, sqlite3.Error):
            return self.reply({'error': '目录或图片不可用，请检查资源位置及访问权限'}, 400)


def create_shared_server(library, port):
    try:
        server = SharedServer(('0.0.0.0', port), SharedHandler)
    except OSError as error:
        raise ValueError(f'共享服务启动失败，请检查端口 {port} 是否已被其他程序占用') from error
    server.library = library
    threading.Thread(target=server.serve_forever, name='reference-sharing', daemon=True).start()
    return server
