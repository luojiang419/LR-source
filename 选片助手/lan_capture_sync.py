"""局域网发现、同步码联机和仅接收的拍摄资料同步。"""
from collections import deque,OrderedDict
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import threading
import time
from urllib.parse import urlsplit,parse_qs
from urllib.request import Request,build_opener,ProxyHandler,HTTPRedirectHandler
import uuid

from catalog_store import stable_json
from file_safety import atomic_write
from lan_sync_data import SyncData,digest,safe_path,mapped_id,folder_label,DIGEST

GROUP='239.255.87.81'
DISCOVERY_PORT=48781
MAX_JSON=100*1024*1024


class SyncServer(ThreadingHTTPServer):
    request_queue_size=128


def local_ip(value):
    try:
        ip=ipaddress.ip_address(value)
        return ip.version==4 and not ip.is_unspecified and not ip.is_multicast and (ip.is_private or ip.is_loopback) and not ip.is_link_local
    except ValueError:return False


def signature(key,message):return hmac.new(bytes.fromhex(key),message.encode(),hashlib.sha256).hexdigest()
def password_key(password,salt):return hashlib.pbkdf2_hmac('sha256',password.encode('utf8'),bytes.fromhex(salt),120000).hex()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('同步地址不允许重定向')


class LanCaptureSync:
    def __init__(self,store,sku,archive,*,host_state=None,before_receive=None,on_update=None,autostart=True,discovery_port=DISCOVERY_PORT):
        self.store=store;self.sku=sku;self.archive=archive;self.data=SyncData(store,sku,archive)
        self.path=store.data/'lan-capture-sync.json';self.lock=threading.RLock();self.transfer_lock=threading.Lock()
        self.closed=threading.Event();self.cancel=threading.Event();self.wake=threading.Event();self.server=None;self.udp=None
        self.discovery_port=discovery_port;self.peers={};self.challenges={};self.tokens={};self.attempts={};self.clients={};self.revoked={}
        self.host_state=host_state or (lambda:{});self.before_receive=before_receive or (lambda:None);self.on_update=on_update or (lambda:None)
        self.manifest_lock=threading.Lock();self.cached=None;self.cached_signature=None;self.blobs={}
        self.snapshots=OrderedDict();self.checked_at=0;self.verified_files={}
        self.status={'phase':'idle','error':'','received_files':0,'total_files':0,'received_bytes':0,'revision':'','host':{},'last_sync':None,'session_id':'','project_id':''}
        self.discovery_error='';self.error='';self.opener=build_opener(ProxyHandler({}),NoRedirect())
        saved={}
        if self.path.is_file():saved=json.loads(self.path.read_text('utf8'))
        self.config={'device':saved.get('device') or uuid.uuid4().hex,'name':saved.get('name') or socket.gethostname(),
                     'enabled':saved.get('enabled',False),'salt':saved.get('salt') or secrets.token_hex(16),'key':saved.get('key',''),
                     'folder':saved.get('folder',''),'remote':saved.get('remote')}
        self._persist()
        self.thread=threading.Thread(target=self._run,daemon=True,name='lan-capture-receiver')
        if autostart:
            if self.config['enabled']:
                try:self._start()
                except OSError as exc:self.error='同步监听失败：'+str(exc)
            self.thread.start()

    def _persist(self):atomic_write(self.path,stable_json(self.config).encode())

    @property
    def receiving(self):return bool(self.config['enabled'] and self.config['remote'])

    def state(self):
        with self.lock:
            remote=self.config['remote']
            stamp=time.time();clients={c['device']:{k:c[k] for k in ('device','name','address')} for token,c in self.clients.items()
                if token in self.tokens and self.tokens[token][1]>stamp and (c['seen']>stamp-30 or c.get('active',0))}
            return {'enabled':self.config['enabled'],'name':self.config['name'],'device':self.config['device'],
                    'password_set':bool(self.config['key']),'folder':self.config['folder'],'receiving':self.receiving,
                    'connected_clients':sorted(clients.values(),key=lambda c:(c['name'],c['device'])),'sending_count':len(clients),
                    'role':'receiver' if self.receiving else 'host','single_writer':True,
                    'peer':{k:remote[k] for k in ('device','name','address','root')} if remote else None,
                    'listening':bool(self.server),'discovery_error':self.discovery_error,**self.status,'error':self.status['error'] or self.error}

    def configure(self,body):
        if body.get('enabled') is False:self.cancel.set()
        try:return self._configure(body)
        except Exception:
            if self.receiving:self.cancel.clear()
            raise

    def _configure(self,body):
        with self.transfer_lock,self.lock:
            config=dict(self.config)
            if 'enabled' in body:
                if type(body['enabled']) is not bool:raise ValueError('同步开关无效')
                config['enabled']=body['enabled']
            if 'name' in body:
                name=body['name']
                if not isinstance(name,str) or not name.strip() or len(name)>80 or any(ord(c)<32 for c in name):raise ValueError('请填写 1–80 字符的本机名称')
                config['name']=name.strip()
            if body.get('password') is not None and body.get('password')!='':
                password=body['password']
                if not isinstance(password,str) or len(password)>256:raise ValueError('同步密码最多 256 字符')
                config.update(salt=secrets.token_hex(16));config['key']=password_key(password,config['salt'])
            if 'folder' in body:
                folder=body['folder']
                if not isinstance(folder,str) or not folder.strip() or not Path(folder).is_absolute():raise ValueError('请选择小档存储文件夹的绝对路径')
                target=Path(folder).absolute();safe_path(target,'同步目录检查')
                # 不得把接收文件写进配置库或源码/运行库。
                for protected in (self.store.data,self.store.root):
                    if target==protected or protected in target.parents:raise ValueError('小档存储文件夹不能位于程序或业务数据库目录内')
                target.mkdir(parents=True,exist_ok=True);config['folder']=str(target)
            if config['enabled'] and not config['key']:raise ValueError('请先设置同步密码')
            if self.receiving and config['folder']!=self.config['folder']:raise ValueError('请先退出同步，再更改小档文件夹')
            prior=self.config;self.config=config
            try:
                if config['enabled'] and self.server is None:self._start()
            except OSError:
                self.config=prior;raise ValueError('局域网同步端口无法使用') from None
            if body.get('password'):self.tokens.clear();self.challenges.clear()
            if not config['enabled']:
                self.config['remote']=None;self._stop();self.status.update(phase='idle',error='')
            self.error='';self.cached_signature=None;self._persist();self.wake.set()
        return self.state()

    def _start(self):
        server=SyncServer(('0.0.0.0',0),SyncHandler);server.daemon_threads=True;server.sync=self
        self.server=server
        threading.Thread(target=server.serve_forever,daemon=True,name='lan-capture-host').start()
        try:
            udp=socket.socket(socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_UDP);udp.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            udp.bind(('',self.discovery_port));udp.setsockopt(socket.IPPROTO_IP,socket.IP_ADD_MEMBERSHIP,socket.inet_aton(GROUP)+socket.inet_aton('0.0.0.0'))
            udp.settimeout(.5);self.udp=udp;self.discovery_error=''
            threading.Thread(target=self._listen,args=(udp,),daemon=True,name='lan-capture-discovery').start()
        except OSError as exc:
            udp.close();self.discovery_error='局域网发现不可用：'+str(exc)

    def _stop(self):
        server,self.server=self.server,None;udp,self.udp=self.udp,None
        if udp:udp.close()
        if server:server.shutdown();server.server_close()
        self.tokens.clear();self.challenges.clear();self.clients.clear();self.revoked.clear()
        with self.manifest_lock:self.snapshots.clear();self.cached=None;self.blobs={};self.checked_at=0

    def _listen(self,udp):
        while not self.closed.is_set() and self.udp is udp:
            try:
                payload,address=udp.recvfrom(4096)
                if not local_ip(address[0]):continue
                data=json.loads(payload)
                if data.get('kind')=='framekeep.capture.probe' and data.get('version')==1 and self.server:
                    reply={'kind':'framekeep.capture.peer','version':1,'nonce':data.get('nonce'),'device':self.config['device'],
                           'name':self.config['name'],'port':self.server.server_port,'receiving':self.receiving}
                    udp.sendto(stable_json(reply).encode(),address)
            except socket.timeout:continue
            except (OSError,ValueError,AttributeError):continue

    def discover(self,timeout=1):
        if not self.config['enabled']:raise ValueError('请先开启同步')
        nonce=secrets.token_hex(12);packet=stable_json({'kind':'framekeep.capture.probe','version':1,'nonce':nonce}).encode()
        found={};deadline=time.monotonic()+timeout
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_UDP) as s:
            s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1);s.setsockopt(socket.IPPROTO_IP,socket.IP_MULTICAST_TTL,1);s.settimeout(.15)
            # 组播加广播；多网卡时逐接口发送，兼容无线/有线并存。
            interfaces={'0.0.0.0'}
            try:interfaces.update(i[4][0] for i in socket.getaddrinfo(socket.gethostname(),None,socket.AF_INET) if local_ip(i[4][0]))
            except OSError:pass
            for interface in interfaces:
                try:s.setsockopt(socket.IPPROTO_IP,socket.IP_MULTICAST_IF,socket.inet_aton(interface));s.sendto(packet,(GROUP,self.discovery_port))
                except OSError:pass
            for ip in ('255.255.255.255','127.0.0.1'):
                try:s.sendto(packet,(ip,self.discovery_port))
                except OSError:pass
            while time.monotonic()<deadline:
                try:
                    packet,address=s.recvfrom(4096);r=json.loads(packet)
                    if r.get('kind')!='framekeep.capture.peer' or r.get('nonce')!=nonce or r.get('version')!=1 or not local_ip(address[0]):continue
                    device=r.get('device','');name=r.get('name','');port=r.get('port')
                    if not re_device(device) or not isinstance(name,str) or len(name)>80 or type(port) is not int or not 1<=port<=65535 or device==self.config['device']:continue
                    found[device]={'device':device,'name':name,'address':f'http://{address[0]}:{port}','receiving':bool(r.get('receiving')),'seen':time.time()}
                except socket.timeout:continue
                except (ValueError,OSError,AttributeError):continue
        with self.lock:self.peers.update(found)
        return {'peers':sorted(found.values(),key=lambda p:p['name']),'error':self.discovery_error}

    def request(self,address,path,body=None,token=None,timeout=10):
        value=urlsplit(address)
        if value.scheme!='http' or not local_ip(value.hostname) or value.username or value.password or value.path not in ('','/') or value.query or value.fragment:raise ValueError('只能连接已发现的局域网客户端')
        headers={'Content-Type':'application/json'}
        if token:headers['Authorization']='Bearer '+token
        req=Request(address+path,data=stable_json(body).encode() if body is not None else None,headers=headers)
        return self.opener.open(req,timeout=timeout)

    def request_json(self,*args,**kwargs):
        with self.request(*args,**kwargs) as response:
            data=response.read(MAX_JSON+1)
            if len(data)>MAX_JSON:raise ValueError('同步清单超出上限')
            return json.loads(data)

    def authenticate(self,peer,*,password=None):
        challenge=self.request_json(peer['address'],'/sync/challenge')
        if challenge['device']!=peer['device']:raise ValueError('客户端身份已改变，请重新发现')
        key=password_key(password,challenge['salt']) if password is not None else peer['key']
        if password is None and peer.get('salt')!=challenge['salt']:raise ValueError('主机同步码已更改，请重新联机')
        proof=signature(key,challenge['nonce']+':'+peer['device'])
        result=self.request_json(peer['address'],'/sync/login',{'nonce':challenge['nonce'],'proof':proof,
            'client_device':self.config['device'],'client_name':self.config['name']})
        if not hmac.compare_digest(result['proof'],signature(key,result['token']+':'+challenge['nonce'])):raise ValueError('主机同步码验证失败')
        return result['token'],key,challenge['salt']

    def connect_peer(self,body):
        from urllib.error import HTTPError
        if not self.config['enabled']:raise ValueError('请先开启同步')
        if self.receiving:raise ValueError('请先退出当前同步')
        if self.state()['sending_count']:raise ValueError('本机正在向客户端发送，请先断开客户端再切换为接收')
        if not self.config['folder']:raise ValueError('请先设置小档存储文件夹')
        peer=self.peers.get(body.get('device'))
        if not peer or time.time()-peer['seen']>120:raise ValueError('客户端已离线，请刷新列表')
        if peer['receiving']:raise ValueError('该客户端正在接收其他机器，请选择拍摄主机')
        password=body.get('password')
        if not isinstance(password,str) or not password or len(password)>256:raise ValueError('请输入同步码')
        with self.transfer_lock:
            try:token,key,salt=self.authenticate(peer,password=password)
            except HTTPError as exc:
                raise ValueError('同步码错误' if exc.code in (401,403) else '主机暂时无法联机') from None
            self.before_receive()
            self.token=None
            prior=self.data.receipt(peer['device'])
            root=prior['root'] if prior and Path(prior['root']).parent==Path(self.config['folder']) else str(safe_path(self.config['folder'],folder_label(peer['name'])+' ['+peer['device'][:8]+']'))
            safe_path(root,'目录检查');Path(root).mkdir(parents=True,exist_ok=True)
            with self.lock:
                self.cancel.clear()
                self.config['remote']={**{k:peer[k] for k in ('device','name','address')},'key':key,'salt':salt,'root':root}
                self.token=token;self.status.update(phase='connecting',error='',revision='',session_id='',project_id='');self._persist();self.wake.set()
        return self.state()

    def disconnect(self,*,notify=True):
        self.cancel.set()
        with self.transfer_lock,self.lock:
            peer=self.config['remote'];token=getattr(self,'token',None);stamp=time.time()
            self.revoked={t:until for t,until in self.revoked.items() if until>stamp}
            self.revoked.update({t:until for t,(_,until) in self.tokens.items() if until>stamp})
            self.tokens.clear();self.clients.clear();self.token=None
            self.config['remote']=None;self._persist();self.status.update(phase='idle',error='');self.wake.set()
        if notify and peer and token:
            try:self.request_json(peer['address'],'/sync/logout',{},token=token,timeout=1)
            except (OSError,ValueError):pass
        return self.state()

    def disconnect_client(self,device):
        if not re_device(device):raise ValueError('客户端身份无效')
        with self.lock:
            stamp=time.time()
            for token,client in list(self.clients.items()):
                if client['device']!=device:continue
                valid=self.tokens.pop(token,None);self.clients.pop(token,None)
                if valid:self.revoked[token]=valid[1]
            self.revoked={t:until for t,until in self.revoked.items() if until>stamp}
        return self.state()

    def manifest(self,*,max_age=0,revision=None):
        if self.receiving:raise ValueError('接收模式不提供转发')
        requested=time.perf_counter()
        with self.manifest_lock:
            if revision is not None:
                entry=self.snapshots.get(revision)
                if not entry:raise ValueError('清单已更新，请重试')
                return entry['payload']
            if self.cached is not None and (self.checked_at>=requested or max_age and requested-self.checked_at<max_age):return self.cached
            def file_signatures():
                signatures=[]
                for p in sorted(self.data.probes):
                    try:s=p.stat();signatures.append((str(p),s.st_size,s.st_mtime_ns))
                    except FileNotFoundError:signatures.append((str(p),None))
                return stable_json(signatures)
            state={'name':self.config['name'],**self.host_state()};before=self.data.revision()
            revision=(before,stable_json(state),file_signatures())
            if self.cached is None or self.cached_signature!=revision:
                payload,blobs=self.data.snapshot(self.config['device'],self.config['name'],state)
                self.cached=payload;self.blobs=blobs
                after=(self.data.revision(),stable_json(state),file_signatures())
                self.cached_signature=revision if revision==after else None
                self.snapshots[payload['revision']]={'payload':payload,'blobs':blobs,'body':stable_json(payload).encode(),'created':requested}
                while len(self.snapshots)>1 and (len(self.snapshots)>4 or sum(len(e['body']) for e in self.snapshots.values())>64*1024*1024 or next(iter(self.snapshots.values()))['created']<requested-120):self.snapshots.popitem(last=False)
                self.blobs={sha:path for entry in self.snapshots.values() for sha,path in entry['blobs'].items()}
            self.checked_at=time.perf_counter()
            return self.cached

    def download(self,peer,info,target):
        sha=info.get('sha256','');size=info.get('size')
        if not DIGEST.fullmatch(sha) or type(size) is not int or not 0<=size<=512*1024*1024:raise ValueError('同步文件清单无效')
        if self.closed.is_set() or self.cancel.is_set():raise ValueError('同步已停止')
        def identity():
            stat=target.stat();return (stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
        try:
            version=identity()
            if version[0]==size and (self.verified_files.get(str(target))==(version,sha) or digest(target)==sha):
                self.verified_files[str(target)]=(version,sha);return False
        except FileNotFoundError:pass
        target.parent.mkdir(parents=True,exist_ok=True)
        temp=target.with_name(target.name+'.'+secrets.token_hex(5)+'.receiving');h=hashlib.sha256();count=0
        try:
            with self.request(peer['address'],'/sync/blob/'+sha,token=self.token) as response,temp.open('xb') as f:
                for chunk in iter(lambda:response.read(1024*1024),b''):
                    if self.closed.is_set() or self.cancel.is_set():raise ValueError('同步已停止')
                    count+=len(chunk)
                    if count>size:raise ValueError('同步文件大小不符')
                    f.write(chunk);h.update(chunk)
                f.flush();os.fsync(f.fileno())
            if count!=size or h.hexdigest()!=sha:raise ValueError('同步文件校验失败，稍后重试')
            safe_path(target.parent,target.name);temp.replace(target)
            self.verified_files[str(target)]=(identity(),sha)
            if len(self.verified_files)>50000:self.verified_files.pop(next(iter(self.verified_files)))
        finally:temp.unlink(missing_ok=True)
        with self.lock:self.status['received_bytes']+=count
        return True

    def sync_once(self,*,fresh=True):
        from urllib.error import HTTPError
        try:return self._sync_once(fresh=fresh)
        except HTTPError as exc:
            if exc.code!=410:raise
            self.disconnect(notify=False)
            with self.lock:self.status.update(phase='idle',error='来源机器已断开同步，文件和拍摄进度已保留')

    def _sync_once(self,*,fresh=True):
        from urllib.error import HTTPError
        with self.transfer_lock:
            if not self.receiving:return
            peer=dict(self.config['remote'])
            pending=self.store.data/('lan-sync-pending-'+peer['device']+'.json')
            if pending.is_file():
                prior=json.loads(pending.read_bytes());self.data.validate(prior,peer['device'])
                ready=[(safe_path(peer['root'],f['relative']),f) for r in prior['tables']['catalog']['captures'] for f in r['sync_files']]
                ready.extend((safe_path(self.sku.root,mapped_id(peer['device'],d['project'])+'/'+d['name']),d) for d in prior['documents'])
                if all(p.is_file() and p.stat().st_size==f['size'] and digest(p)==f['sha256'] for p,f in ready):
                    result=self.data.apply(prior,peer['device'],Path(peer['root']));self.status.update(phase='following',last_sync=time.time(),error='',**result)
                    pending.unlink(missing_ok=True);self.on_update()
            if not getattr(self,'token',None):self.token,_,_=self.authenticate(peer)
            try:
                revision=self.request_json(peer['address'],'/sync/revision'+('?fresh=1' if fresh else ''),token=self.token)['revision']
                if revision==self.status['revision']:
                    self.status.update(phase='following',error='');return
                payload=self.request_json(peer['address'],'/sync/manifest?revision='+revision,token=self.token)
            except HTTPError as exc:
                if exc.code in (401,403):self.token=None
                raise
            self.data.validate(payload,peer['device'])
            if payload['revision']==self.status['revision']:
                self.status.update(phase='following',error='');return
            # 清单先持久保存。文件全部就绪后发布进度，重启可幂等补齐。
            atomic_write(self.store.data/('lan-sync-pending-'+peer['device']+'.json'),stable_json(payload).encode())
            files=[f for r in payload['tables']['catalog']['captures'] for f in r['sync_files']]
            unique={f['relative']:f for f in files}
            self.status.update(phase='receiving',total_files=len(unique),received_files=0,error='')
            for relative,info in unique.items():
                self.download(peer,info,safe_path(peer['root'],relative));self.status['received_files']+=1
            for doc in payload['documents']:
                target=safe_path(self.sku.root,mapped_id(peer['device'],doc['project'])+'/'+doc['name'])
                self.download(peer,doc,target)
            if self.closed.is_set() or self.cancel.is_set():return
            result=self.data.apply(payload,peer['device'],Path(peer['root']))
            self.status.update(phase='following',last_sync=time.time(),error='',**result)
            (self.store.data/('lan-sync-pending-'+peer['device']+'.json')).unlink(missing_ok=True)
            self.on_update()

    def _run(self):
        delay=1
        while not self.closed.is_set():
            try:
                if self.receiving:self.sync_once(fresh=False)
                delay=1
            except Exception as exc:
                from urllib.error import HTTPError
                message=str(exc)
                if isinstance(exc,HTTPError):
                    try:message=json.loads(exc.read(4096)).get('error',message)
                    except (OSError,ValueError):pass
                with self.lock:
                    if self.receiving:self.status.update(phase='reconnecting',error=message[:200])
                delay=min(15,delay*2)
            self.wake.wait(delay);self.wake.clear()

    def close(self):
        self.closed.set();self.cancel.set();self.wake.set()
        if self.thread.is_alive():self.thread.join(timeout=12)
        self._stop()


def re_device(value):return isinstance(value,str) and len(value)==32 and all(c in '0123456789abcdef' for c in value)


class SyncHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def reply(self,value,code=200):
        payload=value if isinstance(value,bytes) else stable_json(value).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(payload)

    def allowed(self):
        sync=self.server.sync
        # 同步监听器不提供浏览器 CORS 或桌面命令，不接收公网请求。
        return sync.config['enabled'] and local_ip(self.client_address[0]) and not self.headers.get('Origin')

    def authorized(self):
        sync=self.server.sync;token=self.headers.get('Authorization','').removeprefix('Bearer ')
        with sync.lock:
            valid=sync.tokens.get(token)
            allowed=valid and valid[0]==self.client_address[0] and valid[1]>time.time()
            if allowed:
                stamp=time.time();sync.tokens[token]=(valid[0],stamp+3600)
                if token in sync.clients:sync.clients[token]['seen']=stamp
            return allowed

    def unauthorized(self):
        sync=self.server.sync;token=self.headers.get('Authorization','').removeprefix('Bearer ')
        with sync.lock:revoked=sync.revoked.get(token,0)>time.time()
        return self.reply({'error':'来源机器已断开同步' if revoked else '请重新联机'},410 if revoked else 401)

    def do_GET(self):
        sync=self.server.sync
        if not self.allowed():return self.reply({'error':'访问被拒绝'},403)
        token=self.headers.get('Authorization','').removeprefix('Bearer ');counted=False
        try:
            path=urlsplit(self.path).path
            if path=='/sync/challenge':
                with sync.lock:
                    sync.challenges={n:v for n,v in sync.challenges.items() if v[1]>time.time()}
                    if len(sync.challenges)>512:return self.reply({'error':'请稍后重试'},429)
                    nonce=secrets.token_hex(32);sync.challenges[nonce]=(self.client_address[0],time.time()+30)
                return self.reply({'device':sync.config['device'],'salt':sync.config['salt'],'nonce':nonce})
            if not self.authorized():return self.unauthorized()
            with sync.lock:
                if token in sync.clients:sync.clients[token]['active']=sync.clients[token].get('active',0)+1;counted=True
            params=parse_qs(urlsplit(self.path).query)
            if path=='/sync/revision':return self.reply({'revision':sync.manifest(max_age=0 if params.get('fresh')==['1'] else .75)['revision']})
            if path=='/sync/manifest':
                payload=sync.manifest(revision=params.get('revision',[None])[0])
                with sync.manifest_lock:body=sync.snapshots.get(payload['revision'],{}).get('body',payload)
                return self.reply(body)
            if path.startswith('/sync/blob/'):
                sha=path.removeprefix('/sync/blob/')
                if not DIGEST.fullmatch(sha):return self.reply({'error':'无效文件'},404)
                with sync.manifest_lock:
                    file=sync.blobs.get(sha)
                    if file is not None and sync.data.file_info(file)['sha256']!=sha:return self.reply({'error':'文件已更新，请重试'},409)
                if file is None:return self.reply({'error':'清单已更新，请重试'},409)
                size=file.stat().st_size
                self.send_response(200);self.send_header('Content-Type','application/octet-stream');self.send_header('Content-Length',str(size));self.end_headers()
                with file.open('rb') as source:
                    for chunk in iter(lambda:source.read(1024*1024),b''):
                        if not self.authorized():return
                        self.wfile.write(chunk)
                return
            return self.reply({'error':'接口不存在'},404)
        except (BrokenPipeError,ConnectionResetError):pass
        except ValueError as exc:return self.reply({'error':str(exc)[:200] or '拍摄资料正在更新，请稍后重试'},409)
        except (OSError,KeyError):return self.reply({'error':'拍摄资料正在更新，请稍后重试'},409)
        finally:
            if counted:
                with sync.lock:
                    if token in sync.clients:sync.clients[token]['active']=max(0,sync.clients[token].get('active',0)-1)

    def do_POST(self):
        sync=self.server.sync
        if not self.allowed():return self.reply({'error':'访问被拒绝'},403)
        if self.path=='/sync/logout':
            if not self.authorized():return self.unauthorized()
            token=self.headers.get('Authorization','').removeprefix('Bearer ')
            with sync.lock:sync.tokens.pop(token,None);sync.clients.pop(token,None)
            return self.reply({'ok':True})
        if self.path!='/sync/login':return self.reply({'error':'接口不存在'},404)
        try:
            length=int(self.headers.get('Content-Length',0))
            if not 0<length<4096:raise ValueError()
            data=json.loads(self.rfile.read(length));ip=self.client_address[0];stamp=time.time()
            with sync.lock:
                attempts=sync.attempts.setdefault(ip,deque());
                while attempts and attempts[0]<stamp-60:attempts.popleft()
                if len(attempts)>=10:return self.reply({'error':'同步码尝试过多，请稍后重试'},429)
                entry=sync.challenges.pop(data.get('nonce',''),None)
                expected=signature(sync.config['key'],data['nonce']+':'+sync.config['device']) if sync.config['key'] else ''
                if not entry or entry[0]!=ip or entry[1]<stamp or not expected or not isinstance(data.get('proof'),str) or not hmac.compare_digest(data['proof'],expected):
                    attempts.append(stamp);return self.reply({'error':'同步码错误'},403)
                token=secrets.token_urlsafe(32)
                sync.tokens={t:v for t,v in sync.tokens.items() if v[1]>stamp};sync.tokens[token]=(ip,stamp+3600)
                sync.clients={t:v for t,v in sync.clients.items() if t in sync.tokens}
                device=data.get('client_device');name=data.get('client_name')
                if not re_device(device):device=hashlib.sha256(token.encode()).hexdigest()[:32]
                if not isinstance(name,str) or not name.strip() or len(name)>80:name=ip
                for old,client in list(sync.clients.items()):
                    if client['device']==device:
                        valid=sync.tokens.pop(old,None);sync.clients.pop(old,None)
                        if valid:sync.revoked[old]=valid[1]
                sync.clients[token]={'device':device,'name':name,'address':ip,'seen':stamp}
            return self.reply({'token':token,'proof':signature(sync.config['key'],token+':'+data['nonce'])})
        except (ValueError,KeyError,TypeError):return self.reply({'error':'联机请求无效'},400)
