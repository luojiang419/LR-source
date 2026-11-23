"""独立的本机原图 HTTP 通道，避免业务接口占满浏览器连接后拖住新图。"""
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import json
import os
import secrets
import threading
from urllib.parse import urlsplit,parse_qs

if os.name=='nt':
    import ctypes
    from ctypes import wintypes
    import msvcrt
    _final_path=ctypes.WinDLL('kernel32',use_last_error=True).GetFinalPathNameByHandleW
    _final_path.argtypes=[wintypes.HANDLE,wintypes.LPWSTR,wintypes.DWORD,wintypes.DWORD]
    _final_path.restype=wintypes.DWORD


def check_original_location(stream,source,root):
    # Windows 通常可直接核对已打开的文件，省去重复按名称打开原图和父目录。
    if os.name=='nt':
        buffer=ctypes.create_unicode_buffer(32768)
        length=_final_path(msvcrt.get_osfhandle(stream.fileno()),buffer,len(buffer),0)
        if 0<length<len(buffer):
            name=buffer.value
            if name.startswith('\\\\?\\UNC\\'):name='\\\\'+name[8:]
            else:name=name.removeprefix('\\\\?\\')
            actual=Path(name)
            if actual!=source:actual.relative_to(root.resolve())
            return
    source.resolve().relative_to(root.resolve())


def send_original_jpeg(handler,path,root):
    # 仅授权路径；不解码、校验或重编码图像，不等待生产者的写句柄关闭。
    root=Path(root).absolute();source=Path(path).absolute()
    if '..' in source.relative_to(root).parts:raise ValueError('图像路径无效')
    try:stream=source.open('rb')
    except FileNotFoundError:return handler.reply({'error':'照片尚未到达或已移走'},404)
    except OSError:return handler.reply({'error':'照片暂时不可读'},423)
    with stream:
        check_original_location(stream,source,root)
        stream.seek(0,2);remaining=stream.tell();stream.seek(0)
        handler.send_response(200);handler.send_header('Content-Type','image/jpeg')
        handler.send_header('Content-Length',str(remaining));handler.end_headers()
        try:
            while remaining:
                chunk=stream.read(min(256*1024,remaining))
                if not chunk:
                    handler.close_connection=True
                    break
                handler.wfile.write(chunk);remaining-=len(chunk)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):handler.close_connection=True


class DirectImageChannel:
    def __init__(self,watch,parent_origin):
        self.watch=watch;self.parent_origin=parent_origin;self.token=secrets.token_urlsafe(32)
        self.lock=threading.Lock();self.sources=OrderedDict();self.by_path={}
        owner=self
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            disable_nagle_algorithm=True
            def log_message(self,*args):pass
            def end_headers(self):
                self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Content-Security-Policy',"default-src 'none'; frame-ancestors 'none'")
                if self.headers.get('Origin')==owner.parent_origin:
                    self.send_header('Access-Control-Allow-Origin',owner.parent_origin);self.send_header('Vary','Origin')
                super().end_headers()
            def trusted(self):
                return self.headers.get('Host')==f'127.0.0.1:{self.server.server_port}' and self.headers.get('Origin',owner.parent_origin)==owner.parent_origin
            def reply(self,data,status=200):
                payload=json.dumps(data,ensure_ascii=False).encode();self.send_response(status)
                self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(payload)));self.end_headers()
                try:self.wfile.write(payload)
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
            def do_OPTIONS(self):
                if not self.trusted():return self.reply({'error':'图像来源无效'},403)
                if urlsplit(self.path).path!='/api/watch/direct':return self.reply({'error':'未知图像接口'},404)
                self.send_response(204);self.send_header('Access-Control-Allow-Methods','GET')
                self.send_header('Access-Control-Allow-Headers','X-Framekeep-Preview, X-Framekeep-After');self.send_header('Access-Control-Max-Age','600');self.send_header('Content-Length','0');self.end_headers()
            def do_GET(self):
                if not self.trusted():return self.reply({'error':'图像来源无效'},403)
                url=urlsplit(self.path);path=url.path
                try:
                    if path=='/api/watch/direct':
                        if not secrets.compare_digest(self.headers.get('X-Framekeep-Preview',''),owner.token):return self.reply({'error':'图像凭证无效'},403)
                        params=parse_qs(url.query,max_num_fields=8)
                        feed=owner.feed()
                        if feed is None:return self.reply({'enabled':False,'event_id':None})
                        after=self.headers.get('X-Framekeep-After',params.get('after',[None])[0])
                        state=feed.wait_state(int(after),max(0,min(15,float(params.get('wait',['15'])[0])))) if after is not None else feed.state()
                        for key in ('receiving','latest'):
                            if state.get(key):state[key]=dict(state[key],url=owner.origin+state[key]['url'])
                        return self.reply(state)
                    if path.startswith('/api/watch/preview/'):
                        parts=path.split('/')
                        feed=owner.feed()
                        source=feed.source_path(parts[4],parts[5]) if feed and len(parts)==6 else None
                        root=feed.source_root(parts[4],parts[5]) if source is not None else None
                    elif path.startswith('/api/direct/image/'):
                        with owner.lock:entry=owner.sources.get(path.rsplit('/',1)[-1])
                        source,root=entry[:2] if entry else (None,None)
                    else:return self.reply({'error':'未知图像接口'},404)
                    if source is None or root is None:return self.reply({'error':'图像地址已失效'},404)
                    return send_original_jpeg(self,source,root)
                except (ValueError,FileNotFoundError):return self.reply({'error':'图像路径无效'},400)
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
                except OSError:return self.reply({'error':'图像暂时不可读'},423)
        self.http=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.http.daemon_threads=True
        self.origin=f'http://127.0.0.1:{self.http.server_port}'
        self.thread=threading.Thread(target=self.http.serve_forever,name='direct-image-http',daemon=True)
        self.thread.start()

    def feed(self):return self.feed_provider() if getattr(self,'feed_provider',None) else self.watch.direct

    def public(self):return {'origin':self.origin,'token':self.token}

    def register(self,root,relative):
        source=Path(root)/relative;identity=(str(root).casefold(),str(relative).casefold())
        with self.lock:
            key=self.by_path.get(identity)
            if key is None:key=secrets.token_urlsafe(24);self.by_path[identity]=key
            self.sources[key]=(source,Path(root),identity);self.sources.move_to_end(key)
            while len(self.sources)>4096:
                old,entry=self.sources.popitem(last=False)
                old_identity=entry[2]
                if self.by_path.get(old_identity)==old:self.by_path.pop(old_identity,None)
            return f'{self.origin}/api/direct/image/{key}?full=1'

    def close(self):
        self.http.shutdown();self.http.server_close();self.thread.join(timeout=2)
