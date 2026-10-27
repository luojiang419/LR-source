"""局域网只读工作台：与桌面共享数据库，独立监听和会话边界。"""
from collections import OrderedDict
import hashlib
import base64
from http.cookies import SimpleCookie,CookieError
import ipaddress
import json
from pathlib import Path
import secrets
import socket
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit

from file_safety import atomic_write
from preview_codec import image_mime
from retrieval_schema import DIMENSIONS
from reference_api import SharedServer
from http.server import BaseHTTPRequestHandler

NETWORKS = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','127.0.0.0/8'))
ASSETS = {'reference-search.js','reference-search.css','lan-workspace.html','lan-workspace.js','lan-workspace.css','theme.js','style.css',
          'theme.css','motion.css','workbench.css','classic-themes.css','view-menu.css','toolbar-layout.css'}
COOKIE = 'framekeep_lan_session'


def local_address(value):
    try:
        address = ipaddress.ip_address(value)
        return address.version == 4 and any(address in network for network in NETWORKS)
    except ValueError:return False


def bounded(value, length=300):
    if not isinstance(value,str) or len(value)>length:raise ValueError('查询条件过长')
    return value.strip()


class LanWorkspace:
    def __init__(self, catalog, sku, intelligence, web, *, autostart=True, require_access_code=True):
        self.catalog=catalog;self.sku=sku;self.intelligence=intelligence;self.web=Path(web)
        self.path=catalog.data/'lan-workspace.json';self.lock=threading.RLock()
        self.server=None;self.thread=None;self.sessions={};self.failures=OrderedDict();self.image_slot=threading.BoundedSemaphore(1)
        self.require_access_code=require_access_code
        self.error='';self.closed=False
        try:
            saved=json.loads(self.path.read_text(encoding='utf8')) if self.path.is_file() else {}
            if not isinstance(saved,dict) or type(saved.get('enabled',False)) is not bool or type(saved.get('port',8780)) is not int or not 1024<=saved.get('port',8780)<=65535:
                raise ValueError('共享配置无效')
            if 'access_code' in saved and (not isinstance(saved['access_code'],str) or not 16<=len(saved['access_code'])<=128):
                raise ValueError('访问码配置无效')
        except (OSError,ValueError):
            saved={};self.error='共享配置不可读，已保持关闭，请重新保存共享设置'
        self.config={'enabled':False,'port':8780,'access_code':secrets.token_urlsafe(18),**saved}
        self.cookie_name=COOKIE+'_'+hashlib.sha256(str(self.path.resolve()).encode()).hexdigest()[:12]
        with sku.connect() as c:
            c.execute('CREATE INDEX IF NOT EXISTS lan_progress_page ON sku_rows(project,sheet,excel_row,id)')
            c.execute('CREATE INDEX IF NOT EXISTS lan_progress_sku ON sku_rows(project,sheet,normalized,status)')
        if autostart and self.config['enabled']:
            try:self._start(self.config['port'])
            except ValueError as exc:self.error=str(exc)

    def _persist(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        atomic_write(self.path,json.dumps(self.config,ensure_ascii=False,indent=2).encode('utf8'))

    def _start(self,port):
        try:server=SharedServer(('0.0.0.0',port),LanHandler)
        except OSError as exc:raise ValueError(f'局域网端口 {port} 无法使用，请选择其他端口') from exc
        server.workspace=self;self.server=server
        self.thread=threading.Thread(target=server.serve_forever,daemon=True,name='lan-workspace');self.thread.start()

    def configure(self,body):
        with self.lock:
            enabled=body.get('enabled',self.config['enabled']);port=body.get('port',self.config['port'])
            if type(enabled) is not bool or type(port) is not int or not 1024<=port<=65535:raise ValueError('请设置有效共享开关及 1024–65535 端口')
            if self.closed:raise ValueError('局域网服务已关闭')
            old=self.server
            if enabled and (not old or old.server_port!=port):
                self._start(port)
            elif not enabled:self.server=None
            if old and old is not self.server:old.shutdown();old.server_close()
            self.config.update(enabled=enabled,port=port)
            if body.get('rotate') is True:
                self.config['access_code']=secrets.token_urlsafe(18);self.sessions.clear()
            if not enabled:self.sessions.clear()
            self.error='';self._persist()
        return self.state()

    def state(self):
        hosts={'127.0.0.1'}
        try:hosts.update(info[4][0] for info in socket.getaddrinfo(socket.gethostname(),None,socket.AF_INET) if local_address(info[4][0]))
        except OSError:pass
        with self.lock:
            return {'enabled':bool(self.server),'configured_enabled':self.config['enabled'],'port':self.config['port'],
                    'access_code':self.config['access_code'] if self.require_access_code else '',
                    'access_code_required':self.require_access_code,'addresses':[f'http://{host}:{self.config["port"]}/' for host in sorted(hosts)],
                    'error':self.error,'mode':'readonly','refresh_seconds':3}

    def login(self,code,address):
        if not self.require_access_code:return None
        now=time.time()
        with self.lock:
            failures=[stamp for stamp in self.failures.get(address,[]) if now-stamp<60]
            if len(failures)>=10:raise PermissionError('尝试过于频繁，请稍后重试')
            if not isinstance(code,str) or len(code)>128 or not secrets.compare_digest(code,self.config['access_code']):
                self.failures[address]=failures+[now]
                while len(self.failures)>512:self.failures.popitem(last=False)
                raise PermissionError('访问码不正确')
            self.failures.pop(address,None)
            self.sessions={key:expiry for key,expiry in self.sessions.items() if expiry>now}
            if len(self.sessions)>=256:raise ValueError('当前访问会话已满，请稍后重试')
            token=secrets.token_urlsafe(32);self.sessions[token]=now+12*3600
            return token

    def authorize(self,header):
        if not self.require_access_code:return None
        try:cookie=SimpleCookie();cookie.load(header or '');token=cookie[self.cookie_name].value
        except (KeyError,ValueError,CookieError):raise PermissionError('请先输入访问码') from None
        with self.lock:
            if self.sessions.get(token,0)<time.time():raise PermissionError('访问已过期，请重新输入访问码')
        return token

    def revision(self):
        parts=[]
        for db in (self.catalog.db,self.sku.db):
            for path in (db,Path(str(db)+'-wal')):
                try:stat=path.stat();parts.append((stat.st_size,stat.st_mtime_ns))
                except OSError:parts.append(None)
        return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:24]

    def summary(self):
        with self.sku.connect() as c:
            projects=[dict(r) for r in c.execute('SELECT id,name,scope,created FROM projects ORDER BY created DESC LIMIT 200')]
            active=c.execute("SELECT value FROM settings WHERE key='active'").fetchone()
        with self.catalog.connect() as c:
            roots=[{'id':r['id'],'name':Path(r['path']).name or r['path'],'status':r['status']} for r in c.execute('SELECT id,path,status FROM roots WHERE removed=0 ORDER BY path')]
        config=self.intelligence.config
        return {'projects':projects,'active_project':active[0] if active else '', 'roots':roots,
                'dimensions':DIMENSIONS,'custom_dimensions':config['custom_dimensions'],
                'visual_backend':config['visual_backend'],'login_required':self.require_access_code,'revision':self.revision(),'refresh_seconds':3}

    def progress(self,query):
        if query.get('view'):return self.progress_view(query)
        project=bounded(query.get('project',''),80);sheet=bounded(query.get('sheet',''),120)
        text=bounded(query.get('q',''));status=query.get('status','');offset=max(0,min(10000000,int(query.get('offset',0))))
        if status not in ('','shot','pending'):raise ValueError('进度筛选无效')
        with self.sku.connect() as c:
            if not project:
                active=c.execute("SELECT value FROM settings WHERE key='active'").fetchone();project=active[0] if active else ''
            known=c.execute('SELECT id,name,scope FROM projects WHERE id=?',(project,)).fetchone()
            if not known:return {'project':None,'sheets':[],'counts':{'total':0,'shot':0,'pending':0},'rows':[],'has_more':False,'revision':self.revision()}
            sheets=[]
            for row in c.execute('''SELECT sheet,count(*) AS total,sum(shot) AS shot FROM
                (SELECT sheet,normalized,max(status='已拍摄') AS shot FROM sku_rows WHERE project=? GROUP BY sheet,normalized)
                GROUP BY sheet ORDER BY sheet''',(project,)):
                row=dict(row);row['pending']=row['total']-row['shot'];sheets.append(row)
            clauses=['project=?'];args=[project]
            if sheet:clauses.append('sheet=?');args.append(sheet)
            if text:clauses.append('(instr(lower(sku),lower(?))>0)');args.append(text)
            grouped='''SELECT min(id) AS id,sheet,min(sku) AS sku,normalized,min(excel_row) AS number,
                max(status='已拍摄') AS shot,max(completed_at) AS completed_at FROM sku_rows WHERE '''+' AND '.join(clauses)+' GROUP BY sheet,normalized'
            filtered='SELECT * FROM ('+grouped+')'+(' WHERE shot=?' if status else '')
            if status:args.append(int(status=='shot'))
            deadline=time.monotonic()+2;c.set_progress_handler(lambda:int(time.monotonic()>deadline),5000)
            rows=[dict(r) for r in c.execute(filtered+' ORDER BY sheet,number,id LIMIT 61 OFFSET ?',args+[offset])]
        totals=[r for r in sheets if not sheet or r['sheet']==sheet]
        counts={key:sum(r[key] for r in totals) for key in ('total','shot','pending')}
        generation=self.revision()
        for row in rows:row['preview']='/api/progress/image?row='+row['id']+'&v='+generation
        return {'project':dict(known),'sheets':sheets,'counts':counts,'rows':rows[:60],'has_more':len(rows)>60,'offset':offset,'revision':self.revision()}

    def progress_view(self,query):
        """软件清单的只读视图；筛选与分页不改变当前项目及 OCR 工作表。"""
        project=bounded(query.get('project',''),80);sheet=bounded(query.get('sheet',''),120)
        view=query.get('view','sheet');status=query.get('status','');text=bounded(query.get('q',''))
        offset=max(0,min(10000000,int(query.get('offset',0))))
        limit=max(1,min(5000,int(query.get('limit',60))))
        if view not in ('sheet','all','common') or status not in ('','shot','pending'):raise ValueError('进度筛选无效')
        p=self.sku.project(project or None)
        empty={'project':None,'sheets':[],'counts':{'total':0,'shot':0,'pending':0},'rows':[],
               'has_more':False,'offset':offset,'view':view,'columns':[],'headers':{},'all_total':0,'common_total':0}
        if not p:return empty
        plans=[s for s in p['metadata']['sheets'] if not s.get('skipped')]
        names=[s['name'] for s in plans]
        if view=='sheet' and sheet not in names:
            if sheet:raise ValueError('工作表不存在')
            sheet=p['scope'] if p['scope'] in names else next(iter(names),'')
        from sku_presentation import preferences
        headers=preferences(self.sku,p['id'])['headers']
        with self.sku.connect() as c:
            deadline=time.monotonic()+2;c.set_progress_handler(lambda:int(time.monotonic()>deadline),5000)
            base='SELECT *,status=\'已拍摄\' AS shot FROM sku_rows WHERE project=?'
            args=[p['id']]
            base+=' AND sheet IN ('+','.join('?' for _ in names)+')';args.extend(names)
            cte='WITH base AS ('+base+'''), sheets AS (
                SELECT sheet,normalized,min(id) AS id,min(sku) AS sku,min(excel_row) AS number,
                max(shot) AS shot,max(completed_at) AS completed_at FROM base GROUP BY sheet,normalized), groups AS (
                SELECT normalized,min(id) AS id,min(sku) AS sku,min(number) AS number,
                min(shot) AS shot,max(completed_at) AS completed_at,count(*) AS sheet_count FROM sheets GROUP BY normalized) '''
            statistics={r['sheet']:dict(r) for r in c.execute(cte+'''SELECT sheet,count(*) AS total,
                sum(shot) AS shot FROM sheets GROUP BY sheet''',args)}
            sheets=[{'sheet':name,'total':statistics.get(name,{}).get('total',0),
                     'shot':statistics.get(name,{}).get('shot',0)} for name in names]
            for item in sheets:item['pending']=item['total']-item['shot']
            all_total,common_total=c.execute(cte+'SELECT count(*),coalesce(sum(sheet_count>1),0) FROM groups',args).fetchone()
            source='sheets WHERE sheet=?' if view=='sheet' else 'groups'+(' WHERE sheet_count>1' if view=='common' else '')
            count_args=args+([sheet] if view=='sheet' else [])
            count=c.execute(cte+'SELECT count(*) AS total,coalesce(sum(shot),0) AS shot FROM '+source,count_args).fetchone()
            counts=dict(count);counts['pending']=counts['total']-counts['shot']
            select='SELECT *,excel_row AS number FROM base WHERE sheet=?' if view=='sheet' else 'SELECT * FROM '+source
            where=' AND ' if view=='sheet' or view=='common' else ' WHERE '
            if text:select+=where+'instr(lower(sku),lower(?))>0';count_args.append(text);where=' AND '
            if status:select+=where+'shot=?';count_args.append(int(status=='shot'))
            rows=[dict(r) for r in c.execute(cte+select+' ORDER BY shot DESC,completed_at DESC,number,id LIMIT ? OFFSET ?',count_args+[limit+1,offset])]
            has_more=len(rows)>limit;rows=rows[:limit]
            memberships={}
            if rows:
                codes=list({r['normalized'] for r in rows})
                for r in c.execute(base+' AND normalized IN ('+','.join('?' for _ in codes)+') ORDER BY excel_row,id',args+codes):
                    memberships.setdefault(r['normalized'],[]).append(dict(r))
        generation=self.revision()
        for row in rows:
            sources=memberships.get(row['normalized'],[])
            row['sources']=[{'sheet':r['sheet'],'number':r['excel_row'],'shot':bool(r['shot'])} for r in sources]
            # 汇总封面使用首个有拍摄证据的来源行，照片选择继续走软件的规则。
            evidence=next((r for r in sources if r.get('completion_job')),None)
            if view!='sheet' and evidence:row['id']=evidence['id']
            row['preview']='/api/progress/image?row='+row['id']+'&v='+generation
            row['values']=json.loads(row.pop('source_values','') or '{}') if view=='sheet' else {}
            source_plan=next((s for s in plans if s['name']==row.get('sheet')),{})
            images=[im for im in source_plan.get('images',[]) if int(''.join(filter(str.isdigit,im['cell'])))==row['number']]
            row['images']=[{'letter':''.join(filter(str.isalpha,im['cell'])),
                            'url':f"/api/progress/image?row={row['id']}&kind=source:{index}&v={generation}"} for index,im in enumerate(images)] if view=='sheet' else []
            row['completion']=bool(row.pop('completion_job','')) if view=='sheet' else bool(evidence)
            row['shot']=bool(row['shot'])
            for key in ('project','baseline','baseline_completed_at','status'):row.pop(key,None)
        plan=next((s for s in plans if s['name']==sheet),{})
        columns=[{'letter':col['letter'],'title':headers.get('sku' if col['index']==plan.get('sku_col') else f"column:{sheet}:{col['letter']}",col['title']),
                  'sku':col['index']==plan.get('sku_col'),'note':col['title'].strip()=='备注'} for col in plan.get('columns',[])]
        return {'project':{'id':p['id'],'name':p['name'],'scope':p['scope']},'sheets':sheets,'counts':counts,
                'rows':rows,'has_more':has_more,'offset':offset,'view':view,'sheet':sheet,'columns':columns,
                'headers':headers,'all_total':all_total,'common_total':common_total,'revision':generation}

    def progress_image(self,row_id,kind='cover'):
        with self.sku.connect() as c:row=c.execute('SELECT * FROM sku_rows WHERE id=?',(bounded(row_id,80),)).fetchone()
        if not row:raise ValueError('款号不存在')
        if kind.startswith('source:'):return self.sku.image(row['id'],int(kind.removeprefix('source:')))
        # 沿用拍摄清单的图片选择与区间规则，仅为当前一款读取已有资料。
        from sku_reconciliation import decorate_images
        value=dict(row)
        with self.sku.connect() as c:
            evidence=c.execute('SELECT session_id,capture_id,captured_at,capture_time_source FROM jobs WHERE id=?',(row['completion_job'],)).fetchone()
        value['completion']=dict(evidence) if evidence else None
        if kind=='completion':
            image=dict(evidence) if evidence else None
        elif kind=='cover':
            decorate_images(self.sku,[value]);image=value.get('sku_image')
        else:raise ValueError('图片类型无效')
        if image:
            if image.get('imported'):return self.sku.image(image.get('row_id',row['id']),image.get('index',0))
            records=self.sku.store.records(image['session_id'],ids=[image['capture_id']])
            if records:
                import media_engine as media
                session=self.sku.store.session(image['session_id'])
                return media.preview_bytes(records[0],session['root'],self.sku.store.data/'previews',lambda:media.find_exiftool(self.sku.store.root),420,False)
        if kind=='cover':
            with self.catalog.connect() as c:
                item=c.execute('''SELECT i.id FROM capture_items i JOIN roots r ON r.id=i.root
                    WHERE i.normalized=? AND r.removed=0 ORDER BY i.modified DESC LIMIT 1''',(row['normalized'],)).fetchone()
            if item:return self.catalog.preview(item['id'],420,read=True)
        raise ValueError('暂无款号图片')

    def _item(self,item_id):
        item=self.catalog.item(bounded(item_id,80));self.catalog._root(item['root'])
        if not item['online']:raise ValueError('素材已离线')
        return item

    @staticmethod
    def public_item(row):
        keys=('id','root','folder','name','sku','media_type','media_format','modified','size','summary','reasons','score','frame','width','height','duration_ms')
        version=hashlib.sha256(str(row.get('fingerprint','')).encode()).hexdigest()[:16]
        return {key:row[key] for key in keys if key in row}|{'preview':'/api/preview?item='+row['id']+'&v='+version}

    def search(self,query):
        result=self.intelligence.search(text=bounded(query.get('q','')),facets=json.loads(query.get('facets','{}')),
            root=bounded(query.get('root',''),80),media=query.get('media',''),cursor=bounded(query.get('cursor',''),2000),limit=60)
        result['rows']=[self.public_item(row) for row in result['rows']]
        return result

    def archive(self,query):
        root=bounded(query.get('root',''),80);folder=bounded(query.get('folder',''),1000)
        if root:self.catalog._root(root)
        elif folder:raise ValueError('请先选择归档位置')
        # 只对明确选择的位置展开一级目录；不在每次轮询聚合整库目录树。
        folders=[];folders_truncated=False
        if root:
            prefix='' if not folder or folder=='/' else folder.rstrip('/')+'/'
            with self.catalog.connect() as c:
                folder_rows=c.execute('SELECT DISTINCT folder FROM items WHERE root=? AND online=1 AND folder>=? AND folder<? ORDER BY folder LIMIT 2001',(root,prefix,prefix+'\uffff')).fetchall()
                folders_truncated=len(folder_rows)>2000
                for row in folder_rows[:2000]:
                    value=row[0]
                    if value.startswith(prefix):
                        name=value[len(prefix):].strip('/').split('/')[0]
                        if name and name not in folders:folders.append(name)
        text=bounded(query.get('q',''));media=query.get('media','');cursor=bounded(query.get('cursor',''),2000)
        if media not in ('','image','video','design'):raise ValueError('素材类型无效')
        signature=hashlib.sha256(json.dumps([root,folder,text,media],ensure_ascii=False).encode()).hexdigest()[:24]
        clauses=['online=1','root IN (SELECT id FROM roots WHERE removed=0)'];args=[]
        if root:clauses.append('root=?');args.append(root)
        if folder:
            pattern=folder.rstrip('/').replace('\\','\\\\').replace('%',r'\%').replace('_',r'\_')+'/%'
            clauses.append("(folder=? OR folder LIKE ? ESCAPE '\\')");args.extend([folder,pattern])
        if media:clauses.append('media_type=?');args.append(media)
        if cursor:
            try:
                marker=json.loads(base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4)))
                if marker['q']!=signature:raise ValueError()
                modified=float(marker['modified']);item=str(marker['id'])
            except (ValueError,KeyError,TypeError):raise ValueError('目录或条件已变化，请重新浏览') from None
            clauses.append('(modified<? OR (modified=? AND id>?))');args.extend([modified,modified,item])
        with self.catalog.connect() as c:
            deadline=time.monotonic()+2;c.set_progress_handler(lambda:int(time.monotonic()>deadline),5000)
            if text:
                condition,values=self.catalog.search_condition(c,text);clauses.append(condition);args.extend(values)
            rows=[dict(row) for row in c.execute('SELECT * FROM capture_items WHERE '+' AND '.join(clauses)+' ORDER BY modified DESC,id LIMIT 61',args)]
        result={'rows':rows[:60],'has_more':len(rows)>60,'next_cursor':''}
        if result['has_more']:
            last=rows[59];result['next_cursor']=base64.urlsafe_b64encode(json.dumps({'q':signature,'modified':last['modified'],'id':last['id']}).encode()).decode().rstrip('=')
        result['rows']=[self.public_item(row) for row in result['rows']]
        result['folders']=[{'name':name,'folder':('/'.join([folder.strip('/'),name])).lstrip('/')} for name in folders[:100]]
        result['folders_truncated']=folders_truncated or len(folders)>100
        return result

    def details(self,item_id):
        item=self._item(item_id);cached=self.catalog.metadata.cached(item_id)['data']
        parsed=self.intelligence.details(item_id)
        return {'item':self.public_item(item),'capture':cached.get('capture',{}),'description':cached.get('description',{}),
                'parsed':parsed['parsed'],'stale':parsed.get('stale',False),'frames':parsed['frames']}

    def image(self,body):
        if not self.image_slot.acquire(blocking=False):raise ValueError('图片搜索正在处理，请稍后重试')
        try:
            if body.get('item_id'):self._item(body['item_id'])
            result=self.intelligence.image_search({key:body[key] for key in ('item_id','image','text','root','threshold') if key in body}|{'limit':max(1,min(100,int(body.get('limit',60))))})
            result['rows']=[self.public_item(row) for row in result['rows']]
            return result
        finally:self.image_slot.release()

    def close(self):
        with self.lock:
            self.closed=True;self.sessions.clear();server=self.server;self.server=None
        if server:server.shutdown();server.server_close()


class LanHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def reply(self,value,status=200,kind=None,cookie=None):
        raw=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode('utf8')
        self.send_response(status);self.send_header('Content-Type',kind or ('application/json; charset=utf-8'))
        self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','private, max-age=30' if kind and kind.startswith('image/') else 'no-store')
        self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:self.send_header('Set-Cookie',cookie)
        self.end_headers()
        try:self.wfile.write(raw)
        except OSError:pass

    def boundary(self):
        host=self.headers.get('Host','');parsed=urlsplit('http://'+host)
        try:valid=local_address(parsed.hostname or '') and parsed.port==self.server.server_port and not parsed.username and not parsed.path and not parsed.query and not parsed.fragment
        except ValueError:valid=False
        origin=self.headers.get('Origin');site=self.headers.get('Sec-Fetch-Site','')
        if not valid or not local_address(self.client_address[0]) or (origin and origin!='http://'+host) or site=='cross-site':
            raise PermissionError('仅允许同源局域网访问')

    def do_GET(self):
        try:
            self.boundary();url=urlsplit(self.path);workspace=self.server.workspace
            route=url.path;query={key:values[0] for key,values in parse_qs(url.query,max_num_fields=16).items()}
            filename='lan-workspace.html' if route=='/' else route.removeprefix('/assets/') if route.startswith('/assets/') else ''
            if filename in ASSETS:
                import mimetypes
                payload=(workspace.web/filename).read_bytes()
                return self.reply(payload,kind=mimetypes.guess_type(filename)[0] or 'application/octet-stream')
            workspace.authorize(self.headers.get('Cookie'))
            if route=='/api/state':return self.reply(workspace.summary())
            if route=='/api/changes':return self.reply({'revision':workspace.revision(),'time':time.time()})
            if route=='/api/progress':return self.reply(workspace.progress(query))
            if route=='/api/progress/image':return self.binary(workspace.progress_image(query.get('row',''),query.get('kind','cover')))
            if route=='/api/search':return self.reply(workspace.search(query))
            if route=='/api/archive':return self.reply(workspace.archive(query))
            if route=='/api/facets':return self.reply(workspace.intelligence.facets(query.get('key','')))
            if route=='/api/details':return self.reply(workspace.details(query.get('item','')))
            if route=='/api/preview':
                item=workspace._item(query.get('item',''));return self.binary(workspace.catalog.preview(item['id'],min(2048,max(160,int(query.get('size',420)))),read=True))
            return self.reply({'error':'页面或接口不存在'},404)
        except PermissionError as exc:return self.reply({'error':str(exc)},401)
        except (ValueError,KeyError,TypeError,OSError,RuntimeError,sqlite3.Error):return self.reply({'error':'读取失败，请刷新条件或检查资源是否在线'},400)

    def binary(self,payload):
        # 表格位图沿用工作簿校验后的类型，预览缓存仍为 JPEG/WebP。
        if isinstance(payload,tuple):return self.reply(payload[0],kind=payload[1])
        return self.reply(payload,kind=image_mime(payload))

    def do_POST(self):
        try:
            self.boundary();route=urlsplit(self.path).path;workspace=self.server.workspace
            if route not in ('/api/login','/api/logout','/api/image','/api/reference'):return self.reply({'error':'同事工作台仅支持查阅'},405)
            length=int(self.headers.get('Content-Length','0'))
            if route!='/api/login':workspace.authorize(self.headers.get('Cookie'))
            if route=='/api/reference':
                from reference_upload import receive
                return self.reply(receive(self.rfile,length,self.headers.get('X-Reference-Name',''),workspace.sku.store.root))
            if length<=0 or route not in ('/api/image',) and length>4096:raise ValueError('请求大小无效')
            body=json.loads(self.rfile.read(length))
            if not isinstance(body,dict):raise ValueError('请求格式无效')
            if route=='/api/login':
                token=workspace.login(body.get('code'),self.client_address[0])
                return self.reply({'ok':True},cookie=f'{workspace.cookie_name}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200' if token else None)
            token=workspace.authorize(self.headers.get('Cookie'))
            if route=='/api/logout':
                with workspace.lock:workspace.sessions.pop(token,None)
                return self.reply({'ok':True},cookie=f'{workspace.cookie_name}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
            return self.reply(workspace.image(body))
        except PermissionError as exc:return self.reply({'error':str(exc)},401)
        except (ValueError,KeyError,TypeError,OSError,RuntimeError,sqlite3.Error) as exc:return self.reply({'error':str(exc)[:300]},400)
