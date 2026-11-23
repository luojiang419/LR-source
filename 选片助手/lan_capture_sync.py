"""局域网发现、同步码联机和仅接收的拍摄资料同步。"""
from collections import deque,OrderedDict
from datetime import datetime,timedelta,timezone
from concurrent.futures import ThreadPoolExecutor,as_completed
import copy
from direct_jpeg import DirectJpegFeed
import worker_activity as activity
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import threading
import time
from urllib.parse import urlsplit,parse_qs
from urllib.error import HTTPError
from urllib.request import Request,build_opener,ProxyHandler,HTTPRedirectHandler
import uuid

from catalog_store import stable_json
from file_safety import atomic_write
from lan_sync_share import TemporaryCaptureShare,SharedFileReader
from lan_sync_data import SyncData,preview_slice,digest,safe_path,mapped_id,folder_label,DIGEST

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
    def __init__(self,store,sku,archive,*,host_state=None,before_receive=None,on_update=None,autostart=True,discovery_port=DISCOVERY_PORT,share_enabled=True):
        self.store=store;self.sku=sku;self.archive=archive;self.data=SyncData(store,sku,archive)
        self.path=store.data/'lan-capture-sync.json';self.lock=threading.RLock();self.transfer_lock=threading.Lock()
        self.closed=threading.Event();self.cancel=threading.Event();self.wake=threading.Event();self.server=None;self.udp=None
        self.discovery_port=discovery_port;self.peers={};self.challenges={};self.tokens={};self.attempts={};self.clients={};self.revoked={}
        self.host_state=host_state or (lambda:{});self.before_receive=before_receive or (lambda:None);self.on_update=on_update or (lambda:None)
        self.projection_lock=threading.RLock();self.live_blobs={};self.live_supported=False;self.live_revision='';self.preview_thread=None;self.receive_generation=0;self.preview_generation=-1;self.direct=DirectJpegFeed(store,autostart=False);self.direct_seed_thread=None
        self.manifest_lock=threading.Lock();self.cached=None;self.cached_signature=None;self.blobs={}
        self.pk_cache={}
        self.snapshots=OrderedDict();self.checked_at=0;self.verified_files={};self.active_downloads={}
        self.status={'phase':'idle','error':'','received_files':0,'total_files':0,'received_bytes':0,'revision':'','host':{},'last_sync':None,'session_id':'','project_id':'','set':None,'seq':0,'pending_files':0,'current_file':'','rate':0,'skipped':[]}
        self.discovery_error='';self.error='';self.opener=build_opener(ProxyHandler({}),NoRedirect())
        saved={}
        if self.path.is_file():saved=json.loads(self.path.read_text('utf8'))
        self.config={'device':saved.get('device') or uuid.uuid4().hex,'name':saved.get('name') or socket.gethostname(),
                     'enabled':saved.get('enabled',False),'salt':saved.get('salt') or secrets.token_hex(16),'key':saved.get('key',''),
                     'folder':saved.get('folder',''),'remote':saved.get('remote'),'port':saved.get('port',0),'share_roots':saved.get('share_roots') or {}}
        self.retry_files=False
        if self.config['remote']:
            peer=self.config['remote'];receipt=self.data.receipt(peer['device'])
            with self.store.connect() as c:prior=c.execute('SELECT state FROM lan_sync_receive_state WHERE device=?',(peer['device'],)).fetchone()
            if prior and receipt and receipt['root']==peer['root']:
                self.status.update(json.loads(prior[0]),phase='connecting')
                self.retry_files=self.status.pop('retry_files',False)
        self.confirmed={k:self.status.get(k) for k in ('revision','seq','set')}
        self.share=TemporaryCaptureShare(self.config['device'],share_enabled);self.shared_reader=SharedFileReader()
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

    def version(self):
        for path in (self.store.root/'update-config.json',Path(__file__).resolve().parent/'更新系统/update-config.json',Path(__file__).resolve().parents[1]/'更新系统/update-config.json'):
            try:return json.loads(path.read_text('utf-8-sig'))['version']
            except (OSError,ValueError,KeyError):continue
        return 'dev'

    def state(self):
        if self.config['enabled'] and not self.receiving:
            try:self.manifest(max_age=.5)
            except (OSError,ValueError,sqlite3.Error) as exc:self.error='当前套暂时无法读取：'+str(exc)[:120]
        with self.lock:
            remote=self.config['remote'];stamp=time.time();clients={};latest=self.cached or {};seq=latest.get('seq',0)
            for token,c in self.clients.items():
                if token not in self.tokens:continue
                item=dict(c);age=max(0,stamp-c['seen']);lag=max(0,seq-c.get('seq',0))
                if age>=30:phase='disconnected';text=f'连接中断（{int(age)} 秒前）'
                elif c.get('protocol',1)<2:phase='outdated';text='对方版本过旧，请更新'
                elif c.get('error'):phase='error';text='错误：'+c['error']
                elif c.get('active_transfers',0):phase='transferring';text=f"正在传输 {c.get('received_files',0)} / {c.get('total_files',0)} · {c.get('rate',0):.2f} MB/s"
                elif c.get('pending_files',0):phase='behind';text=f"待下载 {c['pending_files']} 个文件"
                elif c.get('acked') and not lag:phase='synced';text=f"同步完成 · 延迟 {c.get('delay',0):.1f} 秒"+(f" · 跳过 {len(c['skipped'])} 张" if c.get('skipped') else '')
                else:phase='behind';text=f'落后 {lag} 项' if lag else '正在联机'
                item.update(phase=phase,status=text,lag=lag,heartbeat_age=round(age,1));clients[c['device']]=item
            clients=sorted(clients.values(),key=lambda c:(c['name'],c['device']))
            connected=sum(c['phase']!='disconnected' for c in clients);complete=sum(c['phase']=='synced' for c in clients)
            return {'enabled':self.config['enabled'],'name':self.config['name'],'device':self.config['device'],
                    'password_set':bool(self.config['key']),'folder':self.config['folder'],'receiving':self.receiving,
                    'connected_clients':clients,'sending_count':sum(c['phase']=='transferring' for c in clients),
                    'connected_count':connected,'synced_count':complete,'current_set':latest.get('set'),'latest_seq':seq,
                    'role':'receiver' if self.receiving else 'host','single_writer':True,'protocol':2,
                    'peer':{k:remote[k] for k in ('device','name','address','root')} if remote else None,
                    'shared_folders':dict(self.config['share_roots']),'listening':bool(self.server),'discovery_error':self.discovery_error,**self.status,'error':self.status['error'] or self.error}

    def configure(self,body):
        if body.get('enabled') is False:
            self.cancel.set()
            if self.receiving and getattr(self,'token',None):
                try:self.request_json(self.config['remote']['address'],'/sync/logout',{},token=self.token,timeout=1)
                except (OSError,ValueError):pass
            activity.notify()
        try:return self._configure(body)
        except Exception:
            if self.receiving:self.cancel.clear()
            raise

    def _configure(self,body):
        with self.projection_lock,self.lock:
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
                self.direct.configure({'enabled':False});self.config['remote']=None;self._stop();self.status.update(phase='idle',error='')
            self.error='';self.cached_signature=None;self._persist();self.wake.set()
        return self.state()

    def _start(self):
        try:server=SyncServer(('0.0.0.0',self.config.get('port',0)),SyncHandler)
        except OSError:server=SyncServer(('0.0.0.0',0),SyncHandler)
        self.config['port']=server.server_port;self._persist();server.daemon_threads=True;server.sync=self
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
        if challenge['device']==self.config['device']:raise ValueError('不能连接本机')
        if challenge.get('protocol',1)<2:raise ValueError('主机版本过旧，请先更新主机与客户端')
        self.live_supported=bool(challenge.get('live_preview'))
        key=password_key(password,challenge['salt']) if password is not None else peer['key']
        if password is None and peer.get('salt')!=challenge['salt']:raise ValueError('主机同步码已更改，请重新联机')
        proof=signature(key,challenge['nonce']+':'+peer['device'])
        result=self.request_json(peer['address'],'/sync/login',{'nonce':challenge['nonce'],'proof':proof,
            'client_device':self.config['device'],'client_name':self.config['name'],'protocol':2,
            'client_data':hashlib.sha256(str(self.store.data.resolve()).casefold().encode()).hexdigest(),'client_machine':socket.gethostname(),'client_version':self.version()})
        if not hmac.compare_digest(result['proof'],signature(key,result['token']+':'+challenge['nonce'])):raise ValueError('主机同步码验证失败')
        return result['token'],key,challenge['salt']

    def connect_peer(self,body):
        from urllib.error import HTTPError
        if not self.config['enabled']:raise ValueError('请先开启同步')
        if self.receiving:raise ValueError('请先退出当前同步')
        if any(c['phase']!='disconnected' for c in self.state()['connected_clients']):raise ValueError('本机正在向客户端发送，请先断开客户端再切换为接收')
        if not self.config['folder']:raise ValueError('请先设置小档存储文件夹')
        peer=self.peers.get(body.get('device'))
        if not peer or time.time()-peer['seen']>120:raise ValueError('客户端已离线，请刷新列表')
        if peer['receiving']:raise ValueError('该客户端正在接收其他机器，请选择拍摄主机')
        password=body.get('password')
        if not isinstance(password,str) or not password or len(password)>256:raise ValueError('请输入同步码')
        shared=None;share_root=body.get('share_root',self.config['share_roots'].get(peer['device'],''))
        if not isinstance(share_root,str):raise ValueError('共享目录无效')
        if share_root:
            from pathlib import PureWindowsPath
            root=PureWindowsPath(share_root);parts=root.parts
            if not root.is_absolute() or not root.drive.startswith(chr(92)*2) or root.drive.split(chr(92))[2]!=urlsplit(peer['address']).hostname or any(p in ('.','..') for p in parts):raise ValueError('共享目录必须属于当前联机主机')
            name=root.drive.split(chr(92))[-1]
            if not __import__('re').fullmatch(r'[\w .-]{1,80}',name):raise ValueError('共享名称无效')
            shared={'name':name,'prefix':'/'.join(parts[1:])}
        with self.projection_lock:
            try:token,key,salt=self.authenticate(peer,password=password)
            except HTTPError as exc:
                raise ValueError('同步码错误' if exc.code in (401,403) else '主机暂时无法联机') from None
            self.before_receive()
            self.token=None
            prior=self.data.receipt(peer['device'])
            root=prior['root'] if prior and Path(prior['root']).parent==Path(self.config['folder']) else str(safe_path(self.config['folder'],folder_label(peer['name'])+' ['+peer['device'][:8]+']'))
            safe_path(root,'目录检查');Path(root).mkdir(parents=True,exist_ok=True)
            with self.lock:
                self.receive_generation+=1;self.cancel=threading.Event()
                if share_root:self.config['share_roots'][peer['device']]=share_root
                else:self.config['share_roots'].pop(peer['device'],None)
                self.config['remote']={**{k:peer[k] for k in ('device','name','address')},'key':key,'salt':salt,'root':root,**({'share':shared} if shared else {})}
                self.confirmed={'revision':'','seq':0,'set':None};self.live_revision=''
                self.token=token;self.status.update(phase='connecting',error='',revision='',session_id='',project_id='',set=None,seq=0,latest_photo='',latest_capture_id='');self._persist();self.wake.set()
        self.ensure_preview_thread()
        return self.state()

    def disconnect(self,*,notify=True):
        self.cancel.set()
        peer=self.config['remote'];token=getattr(self,'token',None)
        # 注销远端后只串行收尾正在提交的业务事务，不等待阻塞的网络请求。
        if notify and peer and token:
            try:self.request_json(peer['address'],'/sync/logout',{},token=token,timeout=1)
            except (OSError,ValueError):pass
        activity.notify()
        with self.projection_lock,self.lock:
            self.receive_generation+=1;stamp=time.time()
            self.revoked={t:until for t,until in self.revoked.items() if until>stamp}
            self.revoked.update({t:until for t,(_,until) in self.tokens.items() if until>stamp})
            self.tokens.clear();self.clients.clear();self.token=None
            self.direct.configure({'enabled':False})
            self.config['remote']=None;self._persist();self.status.update(phase='idle',error='');self.wake.set()
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

    @staticmethod
    def seal(payload):
        payload.pop('revision',None)
        payload['revision']=hashlib.sha256(stable_json(payload).encode()).hexdigest()
        return payload

    def row_key(self,key,table,row):
        if (key,table) not in self.pk_cache:
            with self.data.services[key].connect() as c:self.pk_cache[key,table]=[r['name'] for r in sorted(c.execute('PRAGMA table_info("'+table+'")'),key=lambda r:r['pk']) if r['pk']]
        return stable_json([row[p] for p in self.pk_cache[key,table]])

    def manifest(self,*,max_age=0,revision=None):
        if self.receiving:raise ValueError('接收模式不提供转发')
        requested=time.perf_counter()
        with self.manifest_lock:
            if revision is not None:
                entry=self.snapshots.get(revision)
                if not entry:raise ValueError('清单已更新，请重试')
                return entry['payload']
            if self.cached is not None and (self.checked_at>=requested or max_age and requested-self.checked_at<max_age):return self.cached
            signatures=[]
            for p in sorted(self.data.probes):
                try:st=p.stat();signatures.append((str(p),st.st_size,st.st_mtime_ns))
                except FileNotFoundError:signatures.append((str(p),None))
            raw=self.host_state();watch=raw.get('watch') or {}
            state={'watch':{k:watch[k] for k in ('enabled','session_id','folder','generation','source') if k in watch},'shooting_context':raw.get('shooting_context',{})}
            shared=self.share.describe(watch.get('folder') or (self.store.session(watch.get('session_id')) or {}).get('root','')) if watch.get('enabled') else None
            signature=(self.data.revision(),stable_json(shared),stable_json(state),stable_json(signatures),self.config['name'],self.data.clock().date().isoformat())
            if self.cached is None or self.cached_signature!=signature:
                payload,blobs=self.data.snapshot_current(self.config['device'],self.config['name'],state)
                if shared:payload['share']=shared;self.seal(payload)
                with self.store.connect() as c:
                    prior=c.execute('SELECT payload FROM lan_sync_snapshot WHERE id=1').fetchone()
                    prior=json.loads(prior[0]) if prior else None
                    content=lambda p:{k:v for k,v in p.items() if k not in ('revision','seq')}
                    if prior and content(prior)==content(payload):payload=prior
                    else:
                        sid=(payload.get('set') or {}).get('id','');old_sid=(prior.get('set') or {}).get('id','') if prior else None
                        delta=copy.deepcopy(payload);delta['deleted']={};switched=prior is None or sid!=old_sid
                        if not switched:
                            for key,tables in payload['tables'].items():
                                delta['deleted'][key]={}
                                for table,rows in tables.items():
                                    old={self.row_key(key,table,r):r for r in prior['tables'][key].get(table,[])}
                                    new={self.row_key(key,table,r):r for r in rows}
                                    delta['tables'][key][table]=[r for pk,r in new.items() if old.get(pk)!=r]
                                    delta['deleted'][key][table]=[r for pk,r in old.items() if pk not in new]
                            delta['documents']=[d for d in payload['documents'] if d not in prior['documents']]
                        # 切套和行/文件变化先落日志再发布 seq；重启后继续自增。
                        cur=c.execute('INSERT INTO lan_sync_changes(set_id,kind,ref,created) VALUES (?,?,?,?)',(sid,'set_changed' if switched else 'update','',time.time()))
                        payload['seq']=cur.lastrowid;self.seal(payload)
                        delta.update(seq=payload['seq'],target_revision=payload['revision'],set_changed=switched);self.seal(delta)
                        c.execute('UPDATE lan_sync_changes SET ref=? WHERE seq=?',(stable_json(delta),payload['seq']))
                        c.execute('INSERT OR REPLACE INTO lan_sync_snapshot VALUES (1,?)',(stable_json(payload),))
                        keep=[sid]+([old_sid] if old_sid and sid!=old_sid else [])
                        if len(keep)==1:
                            older=c.execute('SELECT set_id FROM lan_sync_changes WHERE set_id<>? ORDER BY seq DESC LIMIT 1',(sid,)).fetchone()
                            if older:keep.append(older[0])
                        c.execute('DELETE FROM lan_sync_changes WHERE set_id NOT IN (SELECT value FROM json_each(?))',(stable_json(keep),))
                self.cached=payload;self.cached_signature=signature
                self.snapshots[payload['revision']]={'payload':payload,'blobs':blobs,'body':stable_json(payload).encode(),'created':requested}
                while len(self.snapshots)>4:self.snapshots.popitem(last=False)
                self.blobs={sha:path for entry in self.snapshots.values() for sha,path in entry['blobs'].items()}
            self.checked_at=time.perf_counter()
            return self.cached

    def changes(self,set_id,after,timeout=25,token=None):
        if not 0<=after:raise ValueError('同步序号无效')
        deadline=time.monotonic()+min(25,max(0,timeout))
        while not self.closed.is_set():
            observed=activity.revision();current=self.manifest(max_age=0 if timeout==0 else .15);sid=(current.get('set') or {}).get('id','')
            if sid!=set_id or after>current['seq']:return {'set_changed':True,'seq':current['seq']}
            with self.store.connect() as c:
                first=c.execute('SELECT min(seq) FROM lan_sync_changes').fetchone()[0]
                rows=c.execute('SELECT ref FROM lan_sync_changes WHERE seq>? ORDER BY seq LIMIT 128',(after,)).fetchall()
            if after<current['seq'] and (first is None or after<first-1):return {'set_changed':True,'seq':current['seq']}
            if rows:
                changes=[json.loads(r[0]) for r in rows]
                if any(change['seq']!=after+index+1 for index,change in enumerate(changes)):return {'set_changed':True,'seq':current['seq']}
                return {'changes':changes,'seq':current['seq']}
            if time.monotonic()>=deadline or token is not None and token not in self.tokens:break
            activity.wait(self.closed,observed,timeout=min(.25,max(0,deadline-time.monotonic())))
        return {'changes':[],'seq':(self.cached or {}).get('seq',after)}

    def ack(self,token,body):
        for key in ('seq','pending_files','received_files','total_files','received_bytes','active_transfers'):
            if type(body.get(key,0)) is not int or not 0<=body.get(key,0)<=2**53:raise ValueError('同步进度无效')
        with self.store.connect() as c:applied=c.execute('SELECT created FROM lan_sync_changes WHERE seq=?',(body.get('seq',0),)).fetchone()
        with self.lock:
            client=self.clients.get(token)
            if not client:raise ValueError('请重新联机')
            stamp=time.time();previous_seq=client.get('seq');previous=client.get('received_bytes',0);elapsed=max(.01,stamp-client.get('ack_at',stamp))
            for key in ('seq','pending_files','received_files','total_files','received_bytes','active_transfers'):client[key]=body.get(key,0)
            skipped=body.get('skipped',[])
            client['skipped']=[{'name':str(r.get('name',''))[:120],'reason':str(r.get('reason',''))[:160]} for r in skipped[:10] if isinstance(r,dict)] if isinstance(skipped,list) else []
            client.update(acked=True,ack_at=stamp,current_file=str(body.get('current_file',''))[:240],error=str(body.get('error',''))[:200],
                          rate=max(0,(client['received_bytes']-previous)/elapsed/1000000),delay=max(0,stamp-applied[0]) if applied and previous_seq!=client['seq'] else client.get('delay',0))
        return {'ok':True}

    def report(self,error=''):
        if not getattr(self,'token',None) or not self.receiving:return
        body={k:self.status.get(k,0) for k in ('seq','pending_files','received_files','total_files','received_bytes')}
        body['active_transfers']=len(self.active_downloads)
        body.update(current_file=self.status.get('current_file',''),error=error,last_sync=self.status.get('last_sync'),skipped=self.status.get('skipped',[])[:10])
        try:self.request_json(self.config['remote']['address'],'/sync/ack',body,token=self.token,timeout=2)
        except (OSError,ValueError):pass

    def download(self,peer,info,target):
        sha=info.get('sha256','');size=info.get('size')
        if not DIGEST.fullmatch(sha) or type(size) is not int or not 0<=size<=512*1024*1024:raise ValueError('同步文件清单无效')
        if self.closed.is_set() or self.cancel.is_set() or peer.get('_generation',self.receive_generation)!=self.receive_generation:raise ValueError('同步已停止')
        def identity():
            stat=target.stat();return (stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
        try:
            version=identity()
            if version[0]==size and (self.verified_files.get(str(target))==(version,sha) or digest(target)==sha):
                self.verified_files[str(target)]=(version,sha);return False
        except FileNotFoundError:pass
        target.parent.mkdir(parents=True,exist_ok=True)
        temp=target.with_name(target.name+'.'+secrets.token_hex(5)+'.receiving');h=hashlib.sha256();count=0
        with self.lock:
            self.active_downloads[str(temp)]=target.name;self.status['current_file']='、'.join(self.active_downloads.values())
        try:
            shared=info.get('share')
            if shared and self.shared_reader.copy(urlsplit(peer['address']).hostname,shared.get('name'),info.get('share_relative',''),temp,info,self.cancel,self.received_chunk):
                safe_path(target.parent,target.name);temp.replace(target)
                self.verified_files[str(target)]=(identity(),sha);self.status['transport']='SMB';return True
            self.status['transport']='HTTP'
            with self.request(peer['address'],'/sync/blob/'+sha,token=self.token) as response,temp.open('xb') as f:
                for chunk in iter(lambda:response.read(1024*1024),b''):
                    if self.closed.is_set() or self.cancel.is_set() or peer.get('_generation',self.receive_generation)!=self.receive_generation:raise ValueError('同步已停止')
                    count+=len(chunk)
                    if count>size:raise ValueError('同步文件大小不符')
                    f.write(chunk);h.update(chunk)
                    with self.lock:self.status['received_bytes']+=len(chunk)
                f.flush();os.fsync(f.fileno())
            if count!=size or h.hexdigest()!=sha:raise ValueError('同步文件校验失败，稍后重试')
            safe_path(target.parent,target.name);temp.replace(target)
            self.verified_files[str(target)]=(identity(),sha)
            if len(self.verified_files)>50000:self.verified_files.pop(next(iter(self.verified_files)))
        finally:
            temp.unlink(missing_ok=True)
            with self.lock:
                self.active_downloads.pop(str(temp),None);self.status['current_file']='、'.join(self.active_downloads.values())
        return True

    def sync_once(self,*,fresh=True):
        from urllib.error import HTTPError
        try:return self._sync_once(fresh=fresh)
        except HTTPError as exc:
            if exc.code!=410:raise
            if not self.receiving:return
            self.disconnect(notify=False)
            with self.lock:self.status.update(phase='idle',error='来源机器已断开同步，文件和拍摄进度已保留')

    def _sync_once(self,*,fresh=True):
        from urllib.error import HTTPError
        with self.transfer_lock:
            if not self.receiving:return
            peer={**self.config['remote'],'_generation':self.receive_generation};pending=self.store.data/('lan-sync-pending-'+peer['device']+'.json')
            if pending.is_file():
                prior=json.loads(pending.read_bytes());self.data.validate(prior,peer['device'])
                ready=self.batch_files(peer,prior)
                if all(p.is_file() and p.stat().st_size==f['size'] and digest(p)==f['sha256'] for p,f in ready):
                    result=self.project(prior,peer);self.status.update(phase='following' if prior.get('set') else 'waiting_host',last_sync=time.time(),error='',**result)
                    pending.unlink(missing_ok=True);self.on_update()
            if not getattr(self,'token',None):self.token,_,_=self.authenticate(peer)
            try:
                if not self.confirmed['revision'] or getattr(self,'retry_files',False):
                    self.status.update(phase='fetching',error='')
                    batches=[self.request_json(peer['address'],'/sync/current',token=self.token,timeout=30)]
                else:
                    sid=(self.confirmed.get('set') or {}).get('id','')
                    response=self.request_json(peer['address'],f"/sync/changes?set={sid}&after={self.confirmed['seq']}&wait={0 if fresh else 25}",token=self.token,timeout=32)
                    batches=[self.request_json(peer['address'],'/sync/current',token=self.token,timeout=30)] if response.get('set_changed') else response.get('changes',[])
            except HTTPError as exc:
                if peer.get('_generation',self.receive_generation)!=self.receive_generation:return
                if exc.code in (401,403):self.token=None
                raise
            if self.closed.is_set() or self.cancel.is_set() or peer.get('_generation',self.receive_generation)!=self.receive_generation:return
            for payload in batches:
                if self.closed.is_set() or self.cancel.is_set():return
                self.receive_batch(peer,payload,pending)
            if not batches:self.status.update(phase='following' if self.status.get('set') else 'waiting_host',error='')
            self.ensure_preview_thread()
            self.report()

    def record_latest(self,payload,peer):
        if peer.get('_generation',self.receive_generation)!=self.receive_generation:return
        captures=[r for r in payload['tables']['catalog']['captures'] if r['sync_files']]
        if not captures:return
        def stamp(r):
            data=json.loads(r['data']);return data.get('ingested_at') or data.get('time') or ''
        def epoch(value):
            try:
                parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
                if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=timezone(timedelta(hours=8)))
                return parsed.timestamp()
            except (ValueError,TypeError):return 0
        latest=max(captures,key=lambda r:(epoch(stamp(r)),r['position']))
        if epoch(stamp(latest))>=epoch(self.status.get('latest_photo','')):
            self.status['latest_photo']=stamp(latest)
            scope=(payload.get('set') or {}).get('id','')
            self.status['latest_capture_id']=mapped_id(peer['device'],latest['id']+(':'+scope if scope else ''))

    def received_chunk(self,count):
        with self.lock:self.status['received_bytes']+=count

    def project(self,payload,peer):
        with self.projection_lock:
            if self.closed.is_set() or self.cancel.is_set() or not self.receiving or peer.get('_generation',self.receive_generation)!=self.receive_generation:raise ValueError('同步已停止')
            result=self.data.apply(payload,peer['device'],Path(peer['root']))
            self.configure_direct(payload,peer,result)
            if not payload.get('partial'):self.confirmed={k:result[k] for k in ('revision','seq','set')}
            return result

    def resolve_share(self,peer,payload):
        shared=peer.get('share') or payload.get('share');current=payload.get('set')
        if not shared or not current:return None
        from pathlib import PureWindowsPath
        source=PureWindowsPath(current['root']);prefix=PureWindowsPath(shared.get('prefix',''))
        count=len(prefix.parts)
        if not shared.get('source_base') and count and tuple(p.casefold() for p in source.parts[-count:])==tuple(p.casefold() for p in prefix.parts):
            with self.lock:
                shared['source_base']=str(source.parents[count-1])
                if self.config.get('remote') and peer.get('device')==self.config['remote']['device']:self._persist()
        if shared.get('source_base'):
            try:return {**shared,'prefix':source.relative_to(PureWindowsPath(shared['source_base'])).as_posix()}
            except ValueError:return None
        return shared

    def configure_direct(self,payload,peer,result):
        shared=self.resolve_share(peer,payload);current=payload.get('set')
        if not shared or not current or not self.receiving or self.cancel.is_set():
            self.direct.configure({'enabled':False});return
        host=urlsplit(peer['address']).hostname
        root=Path(chr(92)*2+host+chr(92)+shared['name'])
        if shared.get('prefix'):root=root.joinpath(*shared['prefix'].split('/'))
        prior=self.direct.state();changed=prior['generation']!=current['id'] or prior['folder']!=str(root)
        self.direct.configure({'enabled':True,'folder':str(root),'recursive':True,'session_id':result['session_id'],'source':'lan','generation':current['id']})
        aliases={}
        for row in payload['tables']['catalog']['captures']:
            jpg=next((f for f in row['sync_files'] if f['kind']=='jpg'),None)
            if not jpg:continue
            relative='/'.join(jpg['relative'].split('/')[2:])
            key=hashlib.sha256(relative.casefold().encode()).hexdigest()[:24]
            aliases[key]=mapped_id(peer['device'],row['id']+':'+current['id'])
        self.direct.acknowledge(list(aliases.values()),aliases)
        if self.thread.is_alive():self.direct.start()
        if changed:
            def seed():
                try:
                    # 已存在文件的元数据回写不能冒充新快门；后续新建/写入通知直达预览。
                    known={hashlib.sha256(p.relative_to(root).as_posix().casefold().encode()).hexdigest()[:24] for p in root.rglob('*') if p.suffix.lower() in ('.jpg','.jpeg')}
                    with self.direct.condition:
                        active=(self.direct.info.get('receiving') or {}).get('id')
                        if self.direct.info['generation']==current['id']:self.direct.admitted.update(known-{active})
                except OSError:pass
            self.direct_seed_thread=threading.Thread(target=seed,daemon=True,name='lan-shared-preview-seed');self.direct_seed_thread.start()

    def live_manifest(self):
        if self.receiving:raise ValueError('接收模式不提供转发')
        raw=self.host_state();watch=raw.get('watch') or {}
        state={'watch':{k:watch[k] for k in ('enabled','session_id','folder','generation','source') if k in watch}}
        payload,blobs=self.data.snapshot_current(self.config['device'],self.config['name'],state,limit=3)
        shared=self.share.describe((payload.get('set') or {}).get('root',''))
        if shared:payload['share']=shared
        payload.update(partial=True,seq=0);self.seal(payload)
        with self.lock:self.live_blobs=blobs
        return payload

    def ensure_preview_thread(self):
        if self.receiving and self.thread.is_alive() and (self.preview_thread is None or not self.preview_thread.is_alive() or self.preview_generation!=self.receive_generation) and not self.closed.is_set():
            self.preview_generation=self.receive_generation
            self.preview_thread=threading.Thread(target=self._preview_run,args=(self.receive_generation,),daemon=True,name='lan-live-preview');self.preview_thread.start()

    def preview_once(self):
        if not self.receiving or not getattr(self,'token',None) or self.cancel.is_set():return
        if not self.live_supported and not self.config['remote'].get('share'):return
        peer={**self.config['remote'],'_generation':self.receive_generation}
        payload=self.request_json(peer['address'],'/sync/live' if self.live_supported else '/sync/current',token=self.token,timeout=10)
        if not self.live_supported:payload=preview_slice(payload);self.seal(payload)
        if not payload.get('set') or payload['revision']==self.live_revision:return
        self.data.validate(payload,peer['device'])
        for path,info in self.batch_files(peer,payload):self.download(peer,info,path)
        with self.projection_lock:
            if self.cancel.is_set() or not self.receiving or self.config['remote']['device']!=peer['device']:return
            result=self.project(payload,peer)
            for key in ('seq','skipped','retry_files'):result.pop(key,None)
            self.status.update(**result,last_sync=time.time())
            self.record_latest(payload,peer)
            self.live_revision=payload['revision'];self.on_update()

    def _preview_run(self,generation):
        while not self.closed.is_set() and self.receiving and generation==self.receive_generation:
            try:self.preview_once();self.status.pop('preview_error',None)
            except (OSError,ValueError,KeyError,sqlite3.Error) as exc:self.status['preview_error']=self.chinese_error(exc)  # 文件仍在写入时继续主通道，下次快速轮询补齐。
            self.closed.wait(.25 if self.live_supported else 1)

    def batch_files(self,peer,payload):
        shared=self.resolve_share(peer,payload)
        files=[(safe_path(peer['root'],f['relative']),{**f,**({'share':shared,'share_relative':'/'.join(v for v in (shared.get('prefix',''),'/'.join(f['relative'].split('/')[2:])) if v)} if shared else {})}) for r in reversed(payload['tables']['catalog']['captures']) for f in r['sync_files']]
        files.extend((safe_path(self.sku.root,mapped_id(peer['device'],d['project']+(':'+payload['set']['id'] if payload.get('set') else ''))+'/'+d['name']),d) for d in payload['documents'])
        return list({str(p):(p,f) for p,f in files}.values())

    def receive_batch(self,peer,payload,pending):
        self.data.validate(payload,peer['device'])
        if payload.get('seq',0)<=self.confirmed.get('seq',0) and payload.get('target_revision',payload['revision'])==self.confirmed['revision'] and not getattr(self,'retry_files',False):return
        previous=(self.status.get('set') or {}).get('id');files=self.batch_files(peer,payload)
        atomic_write(pending,stable_json(payload).encode())
        self.status.update(phase='receiving' if files else 'fetching',total_files=len(files),received_files=0,pending_files=len(files),error='')
        self.report();failures={};done=threading.Event();started=time.monotonic();bytes_before=self.status['received_bytes']
        def heartbeat():
            while not done.wait(2):self.report()
        pulse=threading.Thread(target=heartbeat,daemon=True);pulse.start()
        try:
            with ThreadPoolExecutor(max_workers=2,thread_name_prefix='lan-download') as pool:
                tasks={pool.submit(self.download,peer,info,path):(path,info) for path,info in files}
                for task in as_completed(tasks):
                    path,info=tasks[task]
                    try:task.result()
                    except HTTPError as exc:
                        if exc.code in (401,403,410):raise
                        failures[str(path)]=self.chinese_error(exc)
                    except (OSError,ValueError) as exc:
                        if self.closed.is_set() or self.cancel.is_set():return
                        failures[str(path)]=self.chinese_error(exc)
                    self.status['received_files']+=1;self.status['pending_files']-=1;self.report()
        finally:done.set();pulse.join(timeout=3)
        if self.closed.is_set() or self.cancel.is_set():return
        skipped=list(payload.get('skipped',[]));self.retry_files=bool(failures)
        if failures:
            # 单张失败不发布不完整照片，保留其它照片和已确认游标；后续轮询重新请求当前套补齐。
            payload=copy.deepcopy(payload)
            for r in payload['tables']['catalog']['captures']:
                failed=[f for f in r['sync_files'] if str(safe_path(peer['root'],f['relative'])) in failures]
                if failed:
                    skipped.append({'capture':r['id'],'name':json.loads(r['data']).get('name',''),'reason':failures[str(safe_path(peer['root'],failed[0]['relative']))]})
                    r['sync_files']=[]
            payload['documents']=[d for d in payload['documents'] if not any(str(p) in failures and f==d for p,f in files)]
            payload['retry_files']=True
            payload['skipped']=skipped;payload.setdefault('target_revision',payload['revision']);self.seal(payload)
            atomic_write(pending,stable_json(payload).encode())
        result=self.project(payload,peer)
        new_set=payload.get('set');changed=previous is not None and new_set and previous!=new_set['id']
        self.status.update(phase='following' if new_set else 'waiting_host',last_sync=time.time(),error='',pending_files=0,current_file='',rate=round((self.status['received_bytes']-bytes_before)/max(.001,time.monotonic()-started)/1000000,2),
                           detail=('已切换到新的一套（'+new_set['date']+'）') if changed else '',**result)
        self.record_latest(payload,peer)
        pending.unlink(missing_ok=True);self.report();self.on_update()

    @staticmethod
    def chinese_error(exc):
        from urllib.error import HTTPError
        message=str(exc)
        if isinstance(exc,HTTPError):
            try:message=json.loads(exc.read(4096)).get('error',message)
            except (OSError,ValueError):pass
        lower=message.lower()
        if 'timed out' in lower or 'timeout' in lower:return '连接超时，请检查主机与局域网'
        if any(word in lower for word in ('refused','10061','10060','unreachable','reset','10054','remote end closed')):return '无法连接主机，请检查网络或主机是否开启同步'
        return message[:200]

    def _run(self):
        delay=1
        while not self.closed.is_set():
            try:
                if self.receiving:
                    self.ensure_preview_thread();self.sync_once(fresh=False)
                delay=1
            except Exception as exc:
                from urllib.error import HTTPError
                message=self.chinese_error(exc)
                self.report(message)
                with self.lock:
                    if self.receiving:self.status.update(phase='reconnecting',error=message[:200])
                if self.receiving and isinstance(exc,OSError):
                    try:
                        peers=self.discover(timeout=.5)['peers'];peer=next((p for p in peers if p['device']==self.config['remote']['device']),None)
                        if peer and peer['address']!=self.config['remote']['address']:
                            self.config['remote']['address']=peer['address'];self.token=None;self._persist()
                    except (OSError,ValueError,TypeError):pass
                delay=min(15,delay*2)
            self.wake.wait(.05 if self.receiving and not self.retry_files and self.status['phase'] in ('following','waiting_host') else delay);self.wake.clear()

    def close(self):
        self.closed.set();self.cancel.set();self.wake.set()
        if self.receiving and getattr(self,'token',None):
            try:self.request_json(self.config['remote']['address'],'/sync/logout',{},token=self.token,timeout=1)
            except (OSError,ValueError):pass
        activity.notify()
        if self.thread.is_alive():self.thread.join(timeout=4)
        if self.preview_thread and self.preview_thread.is_alive():self.preview_thread.join(timeout=4)
        self.direct.close()
        self.share.close()
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
                return self.reply({'device':sync.config['device'],'salt':sync.config['salt'],'nonce':nonce,'protocol':2,'live_preview':True})
            if not self.authorized():return self.unauthorized()
            with sync.lock:
                if token in sync.clients:sync.clients[token]['active']=sync.clients[token].get('active',0)+1;counted=True
            params=parse_qs(urlsplit(self.path).query)
            if path=='/sync/live':return self.reply(sync.live_manifest())
            if path=='/sync/current':return self.reply(sync.manifest())
            if path=='/sync/changes':return self.reply(sync.changes(params.get('set',[''])[0],int(params.get('after',['0'])[0]),float(params.get('wait',['25'])[0]),token))
            if path=='/sync/revision':return self.reply({'revision':sync.manifest(max_age=0 if params.get('fresh')==['1'] else .75)['revision']})
            if path=='/sync/manifest':
                payload=sync.manifest(revision=params.get('revision',[None])[0])
                with sync.manifest_lock:body=sync.snapshots.get(payload['revision'],{}).get('body',payload)
                return self.reply(body)
            if path.startswith('/sync/blob/'):
                sha=path.removeprefix('/sync/blob/')
                if not DIGEST.fullmatch(sha):return self.reply({'error':'无效文件'},404)
                with sync.lock:file=sync.live_blobs.get(sha)
                if file is None:
                    with sync.manifest_lock:file=sync.blobs.get(sha)
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
        except (OSError,KeyError,sqlite3.Error):return self.reply({'error':'拍摄资料正在更新，请稍后重试'},409)
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
            activity.notify()
            return self.reply({'ok':True})
        if self.path=='/sync/ack':
            if not self.authorized():return self.unauthorized()
            try:
                length=int(self.headers.get('Content-Length',0))
                if not 0<length<4096:raise ValueError('同步进度无效')
                return self.reply(sync.ack(self.headers.get('Authorization','').removeprefix('Bearer '),json.loads(self.rfile.read(length))))
            except (ValueError,TypeError):return self.reply({'error':'同步进度无效'},400)
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
                same_machine=data.get('client_machine')==socket.gethostname()
                same_data=data.get('client_data')==hashlib.sha256(str(sync.store.data.resolve()).casefold().encode()).hexdigest()
                if data.get('client_device')==sync.config['device'] or same_machine and same_data:return self.reply({'error':'不能连接本机'},409)
                if sync.receiving:return self.reply({'error':'该机器正在接收其他主机'},409)
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
                sync.clients[token]={'device':device,'name':name,'address':ip,'seen':stamp,'protocol':2 if data.get('protocol')==2 else 1,'version':str(data.get('client_version','未知'))[:40],'local_instance':same_machine and not same_data}
            return self.reply({'token':token,'proof':signature(sync.config['key'],token+':'+data['nonce']),'protocol':2})
        except (ValueError,KeyError,TypeError):return self.reply({'error':'联机请求无效'},400)
