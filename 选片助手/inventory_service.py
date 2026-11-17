"""手机双图出入库独立账本：离线补传、幂等回执与可选网络入口。"""
from __future__ import annotations
import base64
from contextlib import closing
from datetime import datetime,date,timedelta,time as day_time
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import io
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from urllib.parse import urlsplit,parse_qs
from urllib.request import Request,build_opener,ProxyHandler,HTTPRedirectHandler
import uuid
from PIL import Image,ImageOps

MAX_BODY=20_000_000
XLSX_MIME='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

def atomic(path,content):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:temporary.write_bytes(content);temporary.replace(path)
    finally:temporary.unlink(missing_ok=True)

class InventoryService:
    def __init__(self,data,rules=None,*,autostart=True):
        self.data=Path(data)/'inventory';self.data.mkdir(parents=True,exist_ok=True)
        self.images=self.data/'images';self.images.mkdir(exist_ok=True);self.thumbnail_lock=threading.Lock()
        self.db=self.data/'inventory.sqlite3';self.path=self.data/'connection.json'
        self.lock=threading.RLock();self.server=None;self.error='';self.rules=rules or (lambda:{'rules':[{'kind':'prefix','value':'LC'}]})
        self.config={'enabled':False,'port':8791,'token':secrets.token_urlsafe(32),'theme':'dark','workspace':str(uuid.uuid4())}
        if self.path.exists():
            try:
                saved=json.loads(self.path.read_text(encoding='utf8'))
                if type(saved.get('enabled')) is not bool or type(saved.get('port')) is not int or not 1024<=saved['port']<=65535 or not isinstance(saved.get('token'),str) or not 20<=len(saved['token'])<=128:raise ValueError()
                self.config.update({k:v for k,v in saved.items() if k in self.config})
                if 'workspace' not in saved:atomic(self.path,json.dumps(self.config).encode())
            except (ValueError,OSError,TypeError):self.error='连接配置不可读，网络服务保持关闭；请重新保存'
        else:atomic(self.path,json.dumps(self.config).encode())
        with closing(self.connect()) as c:
            c.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY,device TEXT NOT NULL,mode TEXT NOT NULL,sku TEXT NOT NULL,at TEXT NOT NULL,raw TEXT NOT NULL,source TEXT NOT NULL,product_sha TEXT NOT NULL,label_sha TEXT NOT NULL,received TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS records_at ON records(at DESC,id);
            CREATE INDEX IF NOT EXISTS records_sku ON records(sku);
            CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,name TEXT NOT NULL,cursor INTEGER NOT NULL DEFAULT 0,last_seen TEXT NOT NULL DEFAULT '');''')
            if 'revision' not in {r['name'] for r in c.execute('PRAGMA table_info(records)')}:
                c.execute('ALTER TABLE records ADD COLUMN revision INTEGER NOT NULL DEFAULT 0');c.commit()
        from inventory_usb import InventoryUsb
        self.usb_link=InventoryUsb(self)
        if autostart and self.config['enabled']:
            try:self._start(self.config['port'])
            except ValueError as exc:self.error=str(exc)

    def connect(self):
        c=sqlite3.connect(self.db,timeout=20);c.row_factory=sqlite3.Row;return c

    def _start(self,port):
        try:server=ThreadingHTTPServer(('0.0.0.0',port),InventoryHandler)
        except OSError as exc:raise ValueError(f'手机通信端口 {port} 无法使用，请更换端口') from exc
        server.daemon_threads=True;server.inventory=self
        self.server=server;threading.Thread(target=server.serve_forever,daemon=True,name='inventory-mobile').start()

    def configure(self,body):
        with self.lock:
            enabled=body.get('enabled',self.config['enabled']);port=body.get('port',self.config['port']);theme=body.get('theme',self.config['theme'])
            if type(enabled) is not bool or type(port) is not int or not 1024<=port<=65535:raise ValueError('请设置有效开关及 1024–65535 端口')
            if theme not in ('dark','light','pure-white','hardcore'):raise ValueError('主题无效')
            old=self.server
            if enabled and (not old or old.server_port!=port):self._start(port)
            elif not enabled:self.server=None
            if old and old is not self.server:old.shutdown();old.server_close()
            self.config.update(enabled=enabled,port=port,theme=theme)
            atomic(self.path,json.dumps(self.config).encode());self.error=''
        return self.state()

    def state(self):
        import socket
        hosts={'127.0.0.1'}
        try:hosts.update(i[4][0] for i in socket.getaddrinfo(socket.gethostname(),None,socket.AF_INET) if not i[4][0].startswith('169.254.'))
        except OSError:pass
        with closing(self.connect()) as c:
            counts={r['mode']:r['n'] for r in c.execute('SELECT mode,count(*) n FROM records GROUP BY mode')}
            devices=c.execute('SELECT count(DISTINCT device) FROM records').fetchone()[0]
        return {'enabled':bool(self.server),'port':self.config['port'],'token':self.config['token'],'theme':self.config['theme'],'addresses':[f'http://{h}:{self.config["port"]}' for h in sorted(hosts)],'error':self.error,'summary':{'in':counts.get('in',0),'out':counts.get('out',0),'devices':devices,'total':sum(counts.values()),'net':counts.get('in',0)-counts.get('out',0)},'connection':self.usb_link.state()}

    @staticmethod
    def identity(value):
        if not isinstance(value,str):raise ValueError('记录标识无效')
        try:return str(uuid.UUID(value))
        except ValueError as exc:raise ValueError('记录标识无效') from exc

    def receive(self,body):
        if not isinstance(body,dict):raise ValueError('记录格式无效')
        item={k:body.get(k,'') for k in ('id','device','mode','sku','at','raw','source','product_sha','label_sha')}
        item['id']=self.identity(item['id']);item['device']=self.identity(item['device'])
        item['revision']=self.record_revision(body)
        if item['mode'] not in ('in','out'):raise ValueError('出入库模式无效')
        if not isinstance(item['sku'],str) or not re.fullmatch(r'[A-Z0-9][A-Z0-9._/-]{0,79}',item['sku']):raise ValueError('款号格式无效')
        if not isinstance(item['at'],str) or len(item['at'])>50:raise ValueError('时间格式无效')
        try:
            at=datetime.fromisoformat(item['at'].replace('Z','+00:00'))
            if at.tzinfo is None:raise ValueError()
        except ValueError as exc:raise ValueError('记录时间必须包含时区') from exc
        if not isinstance(item['raw'],str) or len(item['raw'])>20000 or not isinstance(item['source'],str) or len(item['source'])>80:raise ValueError('识别资料过长')
        images={}
        for kind in ('product','label'):
            sha=item[kind+'_sha'];data=body.get(kind)
            if not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha) or not isinstance(data,str) or not 8<=len(data)<=11_000_000:raise ValueError('双图及校验信息不完整')
            try:content=base64.b64decode(data,validate=True)
            except ValueError as exc:raise ValueError('图片编码无效') from exc
            if hashlib.sha256(content).hexdigest()!=sha:raise ValueError('图片校验不一致，请重新同步')
            try:
                with Image.open(io.BytesIO(content)) as image:
                    if image.format!='JPEG' or image.width*image.height>40_000_000:raise ValueError('图片必须是有效 JPEG 且不超过 4000 万像素')
                    image.verify()
            except (OSError,Image.DecompressionBombError) as exc:raise ValueError('图片无法读取') from exc
            images[kind]=content
        receipt={k:item[k] for k in ('id','product_sha','label_sha')}
        receipt['revision']=item['revision']
        with self.lock,closing(self.connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM records WHERE id=?',(item['id'],)).fetchone()
            if old:
                newer=self.record_update(old,item)
                for kind,content in images.items():
                    path=self.images/f'{item["id"]}-{kind}.jpg'
                    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=item[kind+'_sha']:atomic(path,content)
                if newer:
                    c.execute('UPDATE records SET sku=?,source=?,revision=? WHERE id=?',(item['sku'],item['source'],item['revision'],item['id']));c.commit()
                return {**receipt,'revision':max(old['revision'],item['revision']),'duplicate':True,'updated':newer}
            written=[]
            try:
                for kind,content in images.items():
                    path=self.images/f'{item["id"]}-{kind}.jpg';atomic(path,content);written.append(path)
                item['received']=datetime.now().astimezone().isoformat()
                with c:c.execute('INSERT INTO records('+','.join(item)+') VALUES('+','.join('?' for _ in item)+')',list(item.values()))
            except Exception:
                for path in written:path.unlink(missing_ok=True)
                raise
        return {**receipt,'duplicate':False}

    def records(self,query):
        mode=query.get('mode','');text=query.get('query','')
        if not isinstance(text,str) or len(text)>100:raise ValueError('搜索文字过长')
        offset=max(0,int(query.get('offset',0)));limit=min(200,max(1,int(query.get('limit',60))))
        where='1=1';args=[]
        if mode in ('in','out'):where+=' AND mode=?';args.append(mode)
        if text:where+=' AND instr(sku,?)>0';args.append(text.upper())
        start=query.get('start','');end=query.get('end','')
        if bool(start)!=bool(end):raise ValueError('请同时选择开始和结束日期')
        if start:
            try:first=date.fromisoformat(start);last=date.fromisoformat(end)
            except ValueError as exc:raise ValueError('日期格式无效') from exc
            if first>last:raise ValueError('结束日期不能早于开始日期')
            lower=datetime.combine(first,day_time.min).astimezone().isoformat();upper=datetime.combine(last+timedelta(days=1),day_time.min).astimezone().isoformat()
            where+=' AND julianday(at)>=julianday(?)-0.00002 AND julianday(at)<julianday(?)+0.00002';args.extend([lower,upper])
        with closing(self.connect()) as c:
            aliases={r['id']:r['name'] for r in c.execute('SELECT id,name FROM devices')}
            if start:
                matched=[dict(r) for r in c.execute('SELECT * FROM records WHERE '+where+' ORDER BY at DESC,id',args) if first<=datetime.fromisoformat(r['at'].replace('Z','+00:00')).astimezone().date()<=last]
                total=len(matched);rows=matched[offset:offset+limit]
            else:
                total=c.execute('SELECT count(*) FROM records WHERE '+where,args).fetchone()[0]
                rows=[dict(r) for r in c.execute('SELECT * FROM records WHERE '+where+' ORDER BY at DESC,id LIMIT ? OFFSET ?',[*args,limit,offset])]
        for r in rows:
            r['device_name']=aliases.get(r['device'],'手机 '+r['device'][:8])
            for k in ('product','label'):r[k+'_url']=f'/api/inventory/image?id={r["id"]}&kind={k}'
        return {'rows':rows,'total':total,'offset':offset,'has_more':offset+len(rows)<total}

    def image(self,id,kind,edge=None):
        id=self.identity(id)
        if kind not in ('product','label'):raise ValueError('图片类型无效')
        source=self.images/f'{id}-{kind}.jpg'
        if edge is None:return source.read_bytes()
        edge=int(edge)
        if not 96<=edge<=600:raise ValueError('缩略图尺寸须为96–600')
        folder=self.data/'thumbnails';path=folder/f'preview-{id}-{kind}-{edge}-v1.json'
        linked=folder.is_symlink() or folder.is_junction() or path.is_symlink() or path.is_junction()
        try:
            if not linked and path.stat().st_size<=512*1024:
                value=json.loads(path.read_bytes());payload=base64.b64decode(value['data'],validate=True)
                if hashlib.sha256(payload).hexdigest()==value['digest']:return payload
        except (OSError,ValueError,TypeError,KeyError):pass
        with Image.open(source) as image:
            image=ImageOps.exif_transpose(image);image.thumbnail((edge,edge));output=io.BytesIO()
            image.convert('RGB').save(output,format='JPEG',quality=82);payload=output.getvalue()
        if not linked and self.thumbnail_lock.acquire(False):
            try:
                atomic(path,json.dumps({'digest':hashlib.sha256(payload).hexdigest(),'data':base64.b64encode(payload).decode()}).encode())
                files=sorted(folder.glob('preview-*.json'),key=lambda p:p.stat().st_mtime_ns,reverse=True)
                for old in files[128:]:old.unlink(missing_ok=True)
            except OSError:pass
            finally:self.thumbnail_lock.release()
        return payload

    def pull(self,body):
        url=body.get('url','').strip().rstrip('/');token=body.get('token','').strip();parsed=urlsplit(url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.query or parsed.fragment or not 20<=len(token)<=128:raise ValueError('请填写手机完整 http(s) 地址及手机连接令牌')
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        opener=build_opener(ProxyHandler({}),NoRedirect())
        def request(path,data=None):
            req=Request(url+path,data=json.dumps(data).encode() if data is not None else None,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
            with opener.open(req,timeout=35) as response:
                content=response.read(MAX_BODY+1)
                if len(content)>MAX_BODY:raise ValueError('手机响应过大')
                return json.loads(content)
        # 握手检查防止把非手机服务误当库存来源。
        health=request('/v1/health')
        if health.get('protocol')!=1:raise ValueError('手机协议版本不匹配')
        count=duplicates=offset=0
        while True:
            data=request('/v1/records?offset='+str(offset));records=data.get('records')
            if not isinstance(records,list) or len(records)>1:raise ValueError('手机分页响应无效')
            if not records:break
            for record in records:
                receipt=self.receive(record);request('/v1/ack',{k:receipt[k] for k in ('id','product_sha','label_sha','revision')})
                count+=not receipt['duplicate'];duplicates+=receipt['duplicate']
            next_offset=data.get('next')
            if type(next_offset) is not int or next_offset<=offset:raise ValueError('手机分页游标无效')
            offset=next_offset
        return {'imported':count,'duplicates':duplicates,'summary':self.state()['summary']}

    def export(self,query):
        from inventory_workbook import export
        return export(self,query)

    def remember_device(self,id,name):
        id=self.identity(id)
        with closing(self.connect()) as db:
            with db:db.execute("INSERT INTO devices(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",(id,name[:100]))

    @staticmethod
    def record_revision(metadata):
        revision=metadata.get('revision',0)
        if type(revision) is not int or not 0<=revision<=2147483647:raise ValueError('记录修订无效')
        return revision

    def record_update(self,old,metadata):
        revision=self.record_revision(metadata)
        immutable=('device','mode','at','raw','product_sha','label_sha')
        changed=any(old[k]!=metadata.get(k) for k in ('sku','source'))
        if any(old[k]!=metadata.get(k) for k in immutable) or (changed and (revision==old['revision'] or (revision>old['revision'] and metadata.get('source')!='manual'))):
            raise ValueError('同一记录标识内容冲突，已保留电脑原记录')
        return revision>old['revision']

    def has_complete(self,metadata):
        id=self.identity(metadata.get('id'))
        with closing(self.connect()) as db:old=db.execute('SELECT * FROM records WHERE id=?',(id,)).fetchone()
        if not old:return False
        if self.record_update(old,metadata):return False
        return all((self.images/f'{id}-{kind}.jpg').is_file() and hashlib.sha256((self.images/f'{id}-{kind}.jpg').read_bytes()).hexdigest()==old[kind+'_sha'] for kind in ('product','label'))

    def usb(self,body=None):return self.usb_link.activate(body)

    def close(self):
        self.usb_link.close()
        if self.server:self.server.shutdown();self.server.server_close();self.server=None

class InventoryHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,data,status=200):
        content=json.dumps(data,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(content)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers()
        try:self.wfile.write(content)
        except (BrokenPipeError,ConnectionResetError):pass
    def allowed(self):
        return not self.headers.get('Origin') and secrets.compare_digest(self.headers.get('Authorization',''),'Bearer '+self.server.inventory.config['token'])
    def do_GET(self):
        if not self.allowed():return self.reply({'error':'连接令牌无效'},401)
        if urlsplit(self.path).path!='/v1/health':return self.reply({'error':'接口不存在'},404)
        s=self.server.inventory
        self.reply({'protocol':1,'application':'framekeep-inventory','workspace':s.config['workspace'],'theme':s.config['theme'],'rules':s.rules().get('rules',[])})
    def do_POST(self):
        if not self.allowed():return self.reply({'error':'连接令牌无效'},401)
        if urlsplit(self.path).path!='/v1/records':return self.reply({'error':'接口不存在'},404)
        try:
            self.connection.settimeout(30);length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=MAX_BODY:raise ValueError('请求为空或超过双图大小限制')
            content=self.rfile.read(length)
            if len(content)!=length:raise ValueError('图片上传未完成')
            self.reply(self.server.inventory.receive(json.loads(content)))
        except (ValueError,TypeError,KeyError) as exc:self.reply({'error':str(exc)},400)
        except Exception:self.reply({'error':'电脑保存失败，手机记录可稍后重试'},500)
