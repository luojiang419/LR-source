"""正式本机选片服务、照片预览与 Lightroom 桥接队列。"""
from __future__ import annotations
import argparse
from datetime import datetime
from contextlib import nullcontext
import base64
import json
import mimetypes
import os
import secrets
import sys
from pathlib import Path
if len(sys.argv)>2 and sys.argv[1]=='--everything-worker':
    from everything_search import worker_main
    worker_main(sys.argv[2]);raise SystemExit(0)
if len(sys.argv)>2 and sys.argv[1]=='--prepare-components':
    from component_setup import ComponentManager, DEFAULT_MODEL_IDS
    from mouse_native.driver_setup import DriverSetup
    import time
    component_data=Path(sys.argv[2])
    progress_marker=[None,0]
    def component_progress(state):
        marker=(state.get('status'),state.get('message'))
        if marker==progress_marker[0] and time.monotonic()-progress_marker[1]<.2:return
        progress_marker[:]=[marker,time.monotonic()]
        if len(sys.argv)>4:
            from component_setup import write_json
            write_json(Path(sys.argv[4]),state)
        if sys.stdout is not None:print(json.dumps(state,ensure_ascii=True),flush=True)
    manager=ComponentManager(component_data,seeds=[sys.argv[3]] if len(sys.argv)>3 and sys.argv[3] else [],on_progress=component_progress)
    try:
        from windows_dependencies import ensure_webview
        webview=ensure_webview(manager)
        manager.ensure([*DEFAULT_MODEL_IDS,'driver'])
        from model_providers import Providers
        providers=Providers(component_data)
        if not providers.public()['providers']:providers.set_builtin(True)
        from mouse_native.activation import activate_standalone
        driver=DriverSetup(manager,component_data,activator=activate_standalone)
        status=driver.ensure() if os.environ.get('FRAMEKEEP_PREPARE_DRIVER')=='1' else driver.state()
        component_progress({'status':'ready','message':'全部识别模型和依赖已就绪','driver':status,'webview':webview})
    except Exception as exc:
        component_progress({'status':'error','message':str(exc)});raise SystemExit(1)
    raise SystemExit(0)
import threading
import uuid
import fast_http_bind  # noqa: F401  启动绑定端口不等待反向 DNS
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer as _ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit,parse_qs,unquote,quote
import assistant_config
import media_engine as media
from eos_monitor import EosMonitor
from folder_watch import FolderWatch
from direct_channel import DirectImageChannel,send_original_jpeg
from catalog_store import Store,now,stable_json
from catalog_search_requests import CatalogSearchRequests,SearchCancelled,SearchBusy
from direct_previews import preview_bytes as file_preview_bytes,state as direct_preview_state
import re
import session_browser
from preview_codec import image_mime
from startup_lazy import install_services
install_services(globals())
XLSX_MIME='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


class ThreadingHTTPServer(_ThreadingHTTPServer):
    # 预览/状态轮询突发时先接住连接，避免默认 5 项队列造成 TCP 重传等待。
    request_queue_size=128

ROOT=Path(os.environ['FRAMEKEEP_APP_ROOT']) if os.environ.get('FRAMEKEEP_APP_ROOT') else (Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1])
WEB=Path(getattr(sys,'_MEIPASS',ROOT))/'演示方案'
DATA=Path(os.environ.get('FRAMEKEEP_DATA',str(ROOT/'data')))
if not os.environ.get('FRAMEKEEP_DATA'):
    if not getattr(sys,'frozen',False):sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'更新系统'))
    from installation import data_directory
    DATA=data_directory(ROOT)
TOKEN=secrets.token_urlsafe(32)
DATA.mkdir(parents=True,exist_ok=True)
_bridge_key_file=DATA/'bridge-token'
if _bridge_key_file.exists():BRIDGE_TOKEN=_bridge_key_file.read_text(encoding='ascii').strip()
else:
    BRIDGE_TOKEN=secrets.token_urlsafe(32);_bridge_key_file.write_text(BRIDGE_TOKEN,encoding='ascii')
STORE=None;BRIDGE_LAST_SEEN=None
CATALOG_SEARCH=CatalogSearchRequests()
BRIDGE_DIAGNOSTIC=None
MOUSE=None
WATCH=None
DIRECT=None
MODELS=None
SKU=None
ARCHIVE=None
ANALYSIS=None
INTELLIGENCE=None
LANWORKSPACE=None
CAPTURESYNC=None
INVENTORY=None
RAWLINK=None
FILESEARCH=None
UPDATES=None
CATALOG=None
COMPONENTS=None
DRIVER=None
PREVIEWS=None
STORAGE=None
CANON=None
REFERENCES=None
BOOT_TASKS=None
WORKFLOW=None
BOOT_GATE=None
BACKUP=None


def preparation_groups(path, body):
    if MODELS is None:return []
    if path=='/api/providers/test':providers=[MODELS.by_id(body.get('id'))]
    else:
        categories=([('ocr' if body.get('kind')=='ocr' else 'multimodal')] if path.startswith('/api/workflow/') else
                    ['multimodal'] if path.startswith('/api/analysis/') or body.get('channel')=='multimodal' else ['ocr'] if path=='/api/parsing/ocr' else ['ocr','multimodal'])
        providers=[MODELS.purpose('asset_analysis') if c=='multimodal' and path.startswith('/api/analysis/') else MODELS.selected(c) for c in categories]
    for provider in providers:
        if provider and provider.get('protocol')=='local':MODELS.check_inference(provider)
    return list(dict.fromkeys(p['model'] for p in providers if p and p.get('protocol')=='local'))

def ui_photos(sid=None,records=None):
    session=STORE.session(sid)
    if not session:return []
    result=[]
    records=STORE.records(session['id']) if records is None else records
    if PREVIEWS:PREVIEWS.request(session['root'],records)
    for record in records:
        r=dict(record);r['preview']=f"/api/preview/{session['id']}/{r['id']}?size=480&thumbnail_source=file";r['previewLarge']=f"/api/preview/{session['id']}/{r['id']}?size=2048&preview_source=file"
        if r.get('raw'):r['previewRaw']=r['previewLarge']
        elif Path(r.get('jpg') or '').suffix.lower() in ('.jpg','.jpeg'):
            r['previewFull']=DIRECT.register(session['root'],r['jpg']) if DIRECT else f"/api/preview/{session['id']}/{r['id']}?full=1"
        for key in ('preview','previewLarge','previewRaw','previewFull'):
            if key in r:r[key]+='&v='+quote(r['fingerprint'])
        result.append(r)
    return result


def browse_state(sid=None,query=None):
    session=STORE.session(sid)
    if not session:return None,[]
    records=STORE.records(session['id'])
    if ARCHIVE and (not BOOT_GATE or BOOT_GATE.ready.is_set()):session=ARCHIVE.ratings.session(session,records)
    session['browser']={'continuous':True,'total':len(records)}
    return session,ui_photos(session['id'],records=records)


_WORKBENCH_VIEWS=None
_WORKBENCH_ASSETS=None

def workbench_views():
    global _WORKBENCH_VIEWS
    if not STORE:return None
    from workbench_views import WorkbenchViews
    if _WORKBENCH_VIEWS is None or _WORKBENCH_VIEWS.db!=STORE.db:
        _WORKBENCH_VIEWS=WorkbenchViews(STORE.data,STORE.db)
    return _WORKBENCH_VIEWS


def archive_thumbnail_settings():
    return {**CATALOG.thumbnail_settings.state(),'visited_mode':bool(getattr(CATALOG,'visits',None))}


def bootstrap_state(*,include_views=True):
    session,rows=browse_state()
    data={'id':session['id'],'name':session['name'],'root':session['root'],'snapshot':session['snapshot'],
          'revision':session['revision'],'saved':session['state'],'sharedRatings':session.get('sharedRatings',{}),
          'browser':session.get('browser'),'demo':False,'requestToken':TOKEN} if session else {
          'id':None,'name':'未打开文件夹','root':'','snapshot':'','revision':0,'saved':{},'demo':False,'requestToken':TOKEN}
    if DIRECT:data['directPreview']=DIRECT.public()
    if include_views and CATALOG and getattr(CATALOG,'startup',None):
        options=getattr(CATALOG,'thumbnail_settings',None)
        data['archiveBootstrap']=CATALOG.startup.boot(include_thumbnails=False) if options is not None and not options.cache_enabled else CATALOG.startup.boot()
    if CATALOG and (not BOOT_GATE or BOOT_GATE.ready.is_set()) and getattr(CATALOG,'thumbnail_settings',None):data['archiveThumbnailSettings']=archive_thumbnail_settings()
    views=workbench_views() if include_views else None
    if views:data['viewBootstrap']=views.bootstrap(data['id'])
    return {'session':data,'photos':rows,'request_token':TOKEN,'workbench_bundle':True}


def capture_folder_state():
    if not BOOT_GATE or BOOT_GATE.ready.is_set():return STORE.folder_state()
    session=STORE.session()
    rows=[{key:session[key] for key in ('id','name','root','updated')}] if session else []
    if rows:rows[0]['capture_count']=session_browser.count(STORE,session['id'])
    return {'sessions':rows,'scans':[],'archive_sessions':[]}

def shooting_export_name(name):
    if __import__('re').fullmatch(r'\d{4}-\d{2}-\d{2}\.xlsx',str(name)):return Path(name).name
    return Path(name).stem+'-拍摄记录-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.xlsx'


def rated_session(sid=None):
    session=STORE.session(sid)
    return ARCHIVE.ratings.session(session,STORE.records(session['id'])) if ARCHIVE and session else session


def save_session_state(body,partial=False):
    if CAPTURESYNC and CAPTURESYNC.receiving:
        current=STORE.session(body['session_id'])
        return {'revision':current['revision'],'readonly':True}
    # 会话版本更新与评级/SKU 回调保持同序，防止另一窗口的新评级被迟到回调覆盖。
    with STORE.browser_lock:
        sid=body['session_id']
        if partial or isinstance(body.get('changes'),dict) and isinstance(body.get('state_baseline'),dict):
            current_ratings=None
            if ARCHIVE and isinstance(body.get('state_baseline'),dict) and body['changes'].get('overrides'):
                current=STORE.session(sid)
                if current and current['revision']!=body['revision']:
                    records=STORE.records(sid,ids=list(body['changes']['overrides']))
                    current_ratings=ARCHIVE.ratings.session(current,records)['sharedRatings']
            before,state,revision=session_browser.patch_state(STORE,sid,body['revision'],body['changes'],body.get('state_baseline'),current_ratings)
        else:
            before=STORE.session(sid) if isinstance(body.get('rating_baseline'),dict) else rated_session(sid)
            state=body['state'];revision=STORE.save(sid,body['revision'],state)
        baseline={**before['state'].get('overrides',{}),**before.get('sharedRatings',{}),**(body.get('rating_baseline') or {})}
        result={'revision':revision}
        if ARCHIVE:ARCHIVE.ratings.saved(sid,before,state,baseline)
        if SKU:
            try:SKU.on_save(sid,{**before['state'],'overrides':baseline},state)
            except Exception as exc:
                from operation_errors import record_error
                identity=record_error(DATA,'/api/session/sku-enqueue',exc)
                reason=str(exc)[:300] if isinstance(exc,(ValueError,OSError)) else '后台任务登记异常'
                result.update(sku_error='星级已保存，但 SKU 识别未能排队：'+reason+'（错误编号 '+identity+'）',sku_error_id=identity)
        if RAWLINK:
            changed=[cid for cid in baseline.keys()|state.get('overrides',{}).keys() if baseline.get(cid)!=state.get('overrides',{}).get(cid)]
            if changed:RAWLINK.request(sid,capture_ids=changed)
        return result


def rated_manifest(selected_only=True,sid=None):
    result=STORE.manifest(False,sid)
    session=rated_session(sid)
    for capture in result['captures']:
        value=session.get('sharedRatings',{}).get(capture['id']) if session else None
        if value is not None:capture.update(rating=max(0,value),excluded=value<0)
    if selected_only:result['captures']=[r for r in result['captures'] if r['rating']>0]
    result['applyPolicy']['mode']='selected-only' if selected_only else 'include-unselected'
    return result


def parsing_state():
    result={'analysis':ANALYSIS.state() if ANALYSIS else {'schemas':[],'jobs':[],'counts':{}},'ocr':SKU.task_state() if SKU else {'mode':'auto','paused':False,'jobs':[],'counts':{},'model':'未启动'},'metadata':RAWLINK.task_state() if RAWLINK else {'batches':[]}}
    if WORKFLOW:
        result['workflow']=WORKFLOW.state()
        for name,kind in (('analysis','vision'),('ocr','ocr')):
            jobs,counts=WORKFLOW.tasks(kind)
            result[name]['jobs']=sorted([*result[name]['jobs'],*jobs],key=lambda j:j['created'],reverse=True)[:100]
            for status,count in counts.items():result[name]['counts'][status]=result[name]['counts'].get(status,0)+count
        result['analysis']['paused']=result['analysis']['paused'] or WORKFLOW.config['vision_paused']
        result['ocr']['paused']=result['ocr']['paused'] or WORKFLOW.config['ocr_paused']
    return result

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(WEB),**kwargs)
    def log_message(self,*args):pass
    def end_headers(self):
        connect="'self' ipc: http://ipc.localhost" if os.environ.get('FRAMEKEEP_DESKTOP_TOKEN') else "'self'"
        direct=' '+DIRECT.origin if DIRECT else ''
        self.send_header('X-Content-Type-Options','nosniff');self.send_header('Cache-Control',getattr(self,'preview_cache','no-store'));self.send_header('Content-Security-Policy',f"default-src 'self'; img-src 'self' data: blob:{direct}; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src {connect}{direct}; frame-ancestors 'none'");super().end_headers()
    def trusted(self):return self.headers.get('Host')==f'127.0.0.1:{self.server.server_port}'
    def reply(self,data,status=200,kind='application/json; charset=utf-8',filename=None):
        if status==200:
            if self.command=='POST':
                from worker_activity import notify
                notify()
            from workbench_views import PATHS
            target=urlsplit(self.path).path
            relevant=self.command=='GET' and target in PATHS or self.command=='POST' and target.startswith(('/api/sku/','/api/providers/','/api/inventory/','/api/session','/api/folders','/api/video-preview','/api/preview-cache','/api/parsing/','/api/catalog/','/api/archive/','/api/intelligence/config'))
            views=workbench_views() if relevant else None
            if views:
                if self.command=='GET':
                    if isinstance(data,dict):views.remember(self.path,data,self.headers.get('X-Framekeep-View-Session',views.context),getattr(self,'view_epoch',None))
                elif self.command=='POST' and urlsplit(self.path).path.startswith(('/api/sku/','/api/providers/','/api/inventory/','/api/session','/api/folders','/api/video-preview','/api/preview-cache','/api/parsing/','/api/catalog/','/api/archive/','/api/intelligence/config')):
                    if target not in ('/api/catalog/visit','/api/catalog/thumbnail-settings'):views.invalidate(target)
        content=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(content)))
        if filename:self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+quote(filename))
        self.end_headers()
        try:self.wfile.write(content)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
    def original_jpeg(self,path,root):
        return send_original_jpeg(self,path,root)
    def do_GET(self):
        global BRIDGE_LAST_SEEN
        if not self.trusted():return self.reply({'error':'本机 Host 不匹配'},403)
        url=urlsplit(self.path);path=unquote(url.path)
        if BOOT_GATE:
            if path=='/api/startup':
                photo=BOOT_GATE.latest_photo()
                return self.reply({**BOOT_GATE.state(),'photo':photo})
            if path=='/api/startup/photo':
                try:return self.reply(BOOT_GATE.preview(parse_qs(url.query).get('id',[''])[0]),kind='image/jpeg')
                except (OSError,ValueError,RuntimeError):return self.reply({'error':'照片正在传输，请稍后重试'},409)
            if path=='/api/bootstrap' and parse_qs(url.query).get('shell')==['1']:
                return self.reply({'session':{'id':None,'name':'正在恢复照片','root':'','snapshot':'','revision':0,'saved':{},'demo':False,'requestToken':TOKEN,'restoring':True},
                                   'photos':[],'request_token':TOKEN,'workbench_bundle':True,'progressive':True})
            capture_path=path in ('/api/bootstrap','/api/state','/api/folders','/api/watch','/api/watch/direct') or path.startswith(('/api/preview/','/api/watch/preview/'))
            available=BOOT_GATE.wait_capture if capture_path else BOOT_GATE.wait
            if path.startswith(('/api/','/bridge/')) and path!='/api/ready' and not available():
                return self.reply({'error':BOOT_GATE.state()['error'] or '服务仍在准备，请稍后重试'},503)
        from workbench_views import PATHS
        if path in PATHS:
            views=workbench_views();self.view_epoch=views.epoch if views else None
        try:
            if path=='/workbench.js':
                global _WORKBENCH_ASSETS
                from workbench_assets import WorkbenchAssets,accepts_gzip
                if _WORKBENCH_ASSETS is None or _WORKBENCH_ASSETS.web!=WEB:_WORKBENCH_ASSETS=WorkbenchAssets(WEB)
                compressed=accepts_gzip(self.headers.get('Accept-Encoding',''))
                payload,etag=_WORKBENCH_ASSETS.read(compressed=compressed);self.preview_cache='private, max-age=0, must-revalidate'
                if self.headers.get('If-None-Match')=='"'+etag+'"':
                    self.send_response(304);self.send_header('ETag','"'+etag+'"');self.send_header('Vary','Accept-Encoding');self.end_headers();return
                self.send_response(200);self.send_header('Content-Type','text/javascript; charset=utf-8');self.send_header('Content-Length',str(len(payload)));self.send_header('ETag','"'+etag+'"');self.send_header('Vary','Accept-Encoding')
                if compressed:self.send_header('Content-Encoding','gzip')
                self.end_headers()
                try:self.wfile.write(payload)
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
                return
            # 桌面启动握手只证明本进程已提供服务，不读取数据库或等待业务锁。
            if path.startswith('/api/folder-archive/'):
                return folder_archive_api.get(self,path,url.query)
            if path.startswith('/api/archive-capture/'):
                return archive_capture_api.get(self,path,url.query)
            if path=='/api/view-bootstrap':
                views=workbench_views();scope='' if parse_qs(url.query).get('scope',[''])[0]=='quick' else (STORE.active_id() or '')
                return self.reply({**(views.bootstrap(scope) if views else {}),'request_token':TOKEN})
            if path=='/api/ready':return self.reply({'application':'framekeep','process_id':os.getpid(),'ready':True,'services_ready':not BOOT_GATE or BOOT_GATE.ready.is_set() and not BOOT_GATE.error})
            if path=='/api/automatic-backup':
                if BACKUP is None:return self.reply({'error':'自动备份服务尚未启动'},503)
                return self.reply(BACKUP.state())
            if path=='/api/lan-sync':return self.reply(CAPTURESYNC.state() if CAPTURESYNC else {'enabled':False,'receiving':False})
            if path.startswith('/api/inventory/'):
                if INVENTORY is None:return self.reply({'error':'出入库服务尚未启动'},503)
                query={key:values[0] for key,values in parse_qs(url.query,max_num_fields=12).items()}
                if path=='/api/inventory/state':return self.reply(INVENTORY.state())
                if path=='/api/inventory/records':return self.reply(INVENTORY.records(query))
                if path=='/api/inventory/image':
                    payload=INVENTORY.image(query.get('id',''),query.get('kind',''),query['size']) if query.get('size') else INVENTORY.image(query.get('id',''),query.get('kind',''))
                    self.preview_cache='private, max-age=31536000, immutable'
                    return self.reply(payload,kind='image/jpeg')
                if path=='/api/inventory/export':return self.reply(INVENTORY.export(query),kind=XLSX_MIME,filename='SHIYING 库存管理.xlsx')
                return self.reply({'error':'出入库接口不存在'},404)
            if path=='/api/bootstrap':
                if BOOT_GATE and any((DATA/'canon-link/inbox').glob('*.json')):
                    return self.reply({'pending':True},202)
                with PREVIEWS.foreground() if PREVIEWS else nullcontext():
                    return self.reply(bootstrap_state(include_views=False) if BOOT_GATE else bootstrap_state())
            if path.startswith('/api/reference/'):
                query={key:values[0] for key,values in parse_qs(url.query,max_num_fields=16).items()}
                result=reference_api.local_get(REFERENCES,path.removeprefix('/api/reference/'),query)
                if path=='/api/reference/preferences':result={**result,'request_token':TOKEN}
                if isinstance(result,bytes):self.preview_cache='private, max-age=30'
                return self.reply(result,kind=image_mime(result) if isinstance(result,bytes) else 'application/json; charset=utf-8')
            if path=='/api/update':return self.reply(UPDATES.state() if UPDATES else {'status':'idle','available':False})
            if path=='/api/update/health':
                busy=any(j['status'] in ('queued','running','cancelling') for j in STORE.folder_state()['scans'])
                with STORE.connect() as c:busy=busy or bool(c.execute("SELECT 1 FROM bridge_jobs WHERE status='pending' LIMIT 1").fetchone())
                for service,tables in ((SKU,('jobs','parse_jobs')),(ANALYSIS,('jobs','auto_inputs')),(ARCHIVE,('metadata_jobs','raw_session_work','archive_sync_targets'))):
                    if service:
                        with service.connect() as c:
                            for table in tables:
                                busy=busy or bool(c.execute(f"SELECT 1 FROM {table} WHERE status IN ('queued','running') LIMIT 1").fetchone())
                blocking=bool(STORAGE and STORAGE.job.get('status')=='running') or bool(REFERENCES and REFERENCES.storage.job.get('status')=='running')
                return self.reply({'busy':busy or blocking,'blocking':blocking})
            if path=='/assets/demo-data.js':
                boot=bootstrap_state()
                js='window.FRAMEKEEP_SESSION='+stable_json(boot['session'])+';window.DEMO_PHOTOS='+stable_json(boot['photos'])+';'
                return self.reply(js.encode(),kind='text/javascript; charset=utf-8')
            if path=='/api/state':
                params={key:values[0] for key,values in parse_qs(url.query,keep_blank_values=True).items()};sid=params.get('session_id')
                if WATCH and sid and params.get('live_after','').isdigit():
                    ids=WATCH.delta(sid,params.get('live_generation',''),int(params['live_after']))
                    if ids is not None:
                        session=STORE.session(sid);records=STORE.records(sid,ids=ids) if ids else []
                        if ARCHIVE and (not BOOT_GATE or BOOT_GATE.ready.is_set()):session=ARCHIVE.ratings.session(session,records)
                        session['browser']={'continuous':True,'total':session_browser.count(STORE,sid)}
                        return self.reply({'session':session,'catalog':ui_photos(sid,records=records),'catalog_delta':True})
                session,rows=browse_state(sid,params)
                if sid and not session:return self.reply({'error':'文件夹会话不存在'},404)
                catalog=[dict(r,captured_at_local=r.get('time')) for r in rows];browser=session.get('browser',{}) if session else {}
                return self.reply({'demo':False,'database':'SQLite','session':session,**capture_folder_state(),'catalog':catalog,'indexed_captures':browser.get('total',len(rows)),'indexed_files':browser.get('files',sum(r.get('fileCount',0) for r in rows)),'snapshot_hash':session['snapshot'] if session else '', 'request_token':TOKEN,'bridge':{'last_seen':BRIDGE_LAST_SEEN,'latest_job':self.bridge_job()}})
            if path=='/api/config':return self.reply(assistant_config.public_config())
            if path=='/api/selection/tree':
                from selection_folders import tree
                return self.reply(tree(STORE))
            if path=='/api/selection/group-tail':
                params={k:v[0] for k,v in parse_qs(url.query).items()}
                session,records=session_browser.read(STORE,params['session_id'],{**params,'tail_capture':params['capture_id'],'filter':'all','q':'','folder':'','limit':2,'offset':0},ARCHIVE)
                return self.reply({'photos':ui_photos(session['id'],records)})
            if path=='/api/selection/files':
                from selection_folders import files
                q=parse_qs(url.query)
                return self.reply(files(STORE,q.get('session_id',[''])[0],q.get('folder',[''])[0],int(q.get('offset',['0'])[0])))
            if path=='/api/ratings/sync':return self.reply(ARCHIVE.ratings.sync.state() if ARCHIVE else {'pending':0,'failed':0,'applied':0,'issues':[]})
            if path=='/api/providers':return self.reply(MODELS.public() if MODELS else {'providers':[],'selected':{}})
            if path=='/api/providers/runtime':return self.reply(local_inference.snapshot())
            if path=='/api/sku/full-auto':return self.reply(SKU.full_auto_state(parse_qs(url.query).get('session_id',[None])[0]) if SKU else {'enabled':False,'can_enable':False,'reason':'OCR 服务尚未启动'})
            if path=='/api/workflow':return self.reply(WORKFLOW.state() if WORKFLOW else {'config':{},'runs':{},'available':False})
            if path=='/api/components':return self.reply(COMPONENTS.snapshot() if COMPONENTS else {'status':'ready','ready':[]})
            if path=='/api/mouse/driver':return self.reply(DRIVER.state() if DRIVER else {'status':'ready','installed':True})
            if path=='/api/search/status':return self.reply(FILESEARCH.status() if FILESEARCH else {'available':False,'message':'文件检索未启动'})
            if path=='/api/search/files':
                q=parse_qs(url.query);service=FILESEARCH or EverythingSearch(ARCHIVE)
                return self.reply(service.search(q.get('q',[''])[0],int(q.get('offset',['0'])[0])))
            if path=='/api/assets/search':
                q=parse_qs(url.query)
                return self.reply(ARCHIVE.search(**{k:q.get(k,['prefix' if k=='mode' else ''])[0] for k in ('sku','scope','project','mode','cursor')}))
            if path=='/api/video-preview':return self.reply(CATALOG.video.state() if CATALOG else {'frame_number':3,'ready':False,'message':'归档服务未启动'})
            if path=='/api/gpu-media':
                import gpu_media,gpu_video
                return self.reply({'images':gpu_media.state(),'video':gpu_video.state()})
            if path.startswith('/api/catalog/video/stream/'):
                query=parse_qs(url.query)
                return CATALOG.playback.stream(self,path.rsplit('/',1)[-1],query.get('session',[''])[0],query.get('start',[0])[0],query.get('end',[None])[0],query.get('v',[None])[0])
            if path=='/api/catalog/video/state':
                return self.reply(CATALOG.playback.state(parse_qs(url.query).get('session',[''])[0]))
            if path=='/api/catalog/state':
                if getattr(CATALOG,'visits',None):
                    visit=CATALOG.visits.state()
                    return self.reply({'initialized':True,'counts':{'total':visit['total']},'metadata':{},'previews':visit,'visits':visit})
                return self.reply(CATALOG.progress.snapshot(PREVIEWS))
            if path=='/api/catalog/thumbnail-settings':return self.reply(archive_thumbnail_settings())
            if path=='/api/catalog/revision':
                return self.reply({'revision':CATALOG.startup.revision()})
            if path=='/api/catalog/bootstrap':
                q=parse_qs(url.query)
                if getattr(CATALOG,'visits',None):return self.reply({'tree':CATALOG.visits.tree(),'page':None,'view':[q.get('root',[''])[0],q.get('folder',[''])[0],q.get('media',[''])[0]]})
                media_filter=q.get('media',[''])[0]
                if media_filter not in ('','image','video','design'):raise ValueError('素材类型筛选无效')
                return self.reply(CATALOG.startup.read(q.get('root',[''])[0],q.get('folder',[''])[0],media_filter,q.get('cursor',[''])[0],max(1,min(240,int(q.get('limit',[120])[0])))))
            if path=='/api/catalog/thumbnails/repair':
                return self.reply(PREVIEWS.design_repair_state() if PREVIEWS else {'status':'idle'})
            if path=='/api/catalog/tree':
                q=parse_qs(url.query)
                if getattr(CATALOG,'visits',None):return self.reply(CATALOG.visits.tree())
                with CATALOG_SEARCH.begin(q.get('client',[''])[0],q.get('sequence',['0'])[0],self.connection) as cancel:
                    return self.reply(CATALOG.tree(q.get('q',[''])[0],q.get('media',[''])[0],cancel=cancel))
            if path=='/api/catalog/browse':
                q=parse_qs(url.query)
                return self.reply(CATALOG.visits.explorer.browse(q['root'][0],q.get('folder',[''])[0]))
            if path=='/api/catalog/file-job':
                return self.reply(CATALOG.visits.explorer.job(parse_qs(url.query)['id'][0]))
            if path=='/api/catalog/items':
                q=parse_qs(url.query)
                visits=getattr(CATALOG,'visits',None)
                if visits and not q.get('root',[''])[0]:
                    return self.reply({'rows':[],'total':0,'has_more':False,'next_cursor':'','engine':'按访问加载'})
                with PREVIEWS.foreground() if PREVIEWS and q.get('background',['0'])[0]!='1' else nullcontext(), CATALOG_SEARCH.begin(q.get('client',[''])[0],q.get('sequence',['0'])[0],self.connection) as cancel:
                    data=CATALOG.items(q.get('root',[''])[0],q.get('folder',[''])[0],q.get('sku',[''])[0],q.get('offset',[0])[0],q.get('limit',[120])[0],q.get('cursor',[''])[0],q.get('q',[''])[0],q.get('media',[''])[0],include_tree=q.get('tree',['0'])[0]=='1' and not visits,cancel=cancel,quick=q.get('quick',['0'])[0]=='1',sort=q.get('sort',['original'])[0],visual_only=q.get('visual_only',['0'])[0]=='1')
                    # 首屏浏览优先返回索引页。共享评级查询跨两个资料库，慢盘或
                    # 后台写入期间不能因此让整个网格保持空白；前端随后低优先级回填。
                    if ARCHIVE and q.get('quick',['0'])[0]!='1':ARCHIVE.ratings.decorate(data['rows'])
                return self.reply(data)
            if path=='/api/catalog/ratings':
                q=parse_qs(url.query);item_ids=q.get('id',[])
                if len(item_ids)>240:raise ValueError('单次最多读取 240 项素材评级')
                rows=CATALOG.rating_rows(item_ids)
                if ARCHIVE:ARCHIVE.ratings.decorate(rows)
                return self.reply({'ratings':{row['id']:row.get('rating',0) for row in rows},'items':{row['id']:{'rating':row.get('rating',0),'sku':row.get('sku',''),'source':row.get('source',''),'metadata_status':row.get('metadata_status','ready')} for row in rows}})
            if path=='/api/catalog/skus':
                from sku_library import browse
                return self.reply({'rows':browse(SKU,parse_qs(url.query).get('q',[''])[0])['groups']})
            if path.startswith('/api/catalog/preview/'):
                params=parse_qs(url.query)
                payload=CATALOG.preview(path.rsplit('/',1)[-1],params.get('size',[420])[0],read=True,frame=params.get('frame',[None])[0])
                # 列表地址带源文件指纹v（视频含帧号、设计含修复版本），内容变化即换地址；
                # 允许WebView复用，避免重绘、预取和返回页面反复读取NAS原片。无指纹地址保持不缓存。
                self.preview_cache='private, max-age=86400, immutable' if params.get('v',[''])[0] else 'no-store'
                return self.reply(payload,kind=image_mime(payload))
            if path.startswith('/api/catalog/details/'):
                item_id=path.rsplit('/',1)[-1]
                if parse_qs(url.query).get('refresh',['0'])[0]=='1':CATALOG.metadata.request(item_id,force=True)
                data=CATALOG.details(item_id,ROOT)
                if ARCHIVE:ARCHIVE.ratings.decorate([data['item']])
                return self.reply(data)
            if path.startswith('/api/catalog/metadata/'):
                if ARCHIVE is None:raise ValueError('元数据服务未启动')
                item_id=path.rsplit('/',1)[-1]
                CATALOG._root(CATALOG.item(item_id)['root'])
                CATALOG.metadata.request(item_id,force=parse_qs(url.query).get('refresh',['0'])[0]=='1')
                cached=CATALOG.metadata.cached(item_id)
                return self.reply({**cached['data'].get('description',{}),'metadata_status':cached['status'],'metadata_error':cached['error']})
            if path=='/api/assets':
                q=parse_qs(url.query)
                return self.reply(ARCHIVE.query(q.get('sku',[''])[0],q.get('scope',[''])[0],q.get('project',[''])[0],int(q.get('offset',['0'])[0])))
            if path.startswith('/api/assets/'):
                return self.reply(ARCHIVE.asset(path.rsplit('/',1)[-1]))
            if path=='/api/archive/streams':return self.reply(ARCHIVE.stream_state())
            if path=='/api/lan-workspace':return self.reply(LANWORKSPACE.state() if LANWORKSPACE else {'enabled':False,'port':8780,'addresses':[],'error':'局域网工作台未启动'})
            if path.startswith('/api/intelligence/'):
                if INTELLIGENCE is None:return self.reply({'error':'多维检索服务未启动'},503)
                q=parse_qs(url.query)
                try:
                    if path=='/api/intelligence/config':return self.reply({**INTELLIGENCE.configuration_state(),'request_token':TOKEN})
                    if path=='/api/intelligence/state':return self.reply({**INTELLIGENCE.state(),'request_token':TOKEN})
                    if path=='/api/intelligence/search-state':return self.reply({**INTELLIGENCE.search_state(),'request_token':TOKEN})
                    if path=='/api/intelligence/frames':return self.reply(INTELLIGENCE.video_frames.page(q.get('item',[''])[0],q.get('cursor',[-1])[0],q.get('limit',[60])[0]))
                    if path=='/api/intelligence/frame-info':return self.reply(INTELLIGENCE.video_frames.frame_info(q.get('item',[''])[0],q.get('frame',[0])[0]))
                    if path.startswith('/api/intelligence/frame/'):
                        parts=path.split('/')
                        if len(parts)!=6:raise ValueError('视频帧地址无效')
                        payload=INTELLIGENCE.video_frames.preview(parts[4],parts[5],q.get('time',[None])[0],q.get('v',[None])[0])
                        self.preview_cache='private, max-age=31536000, immutable';return self.reply(payload,kind='image/jpeg')
                    if path=='/api/intelligence/pipeline':return self.reply(INTELLIGENCE.pipeline_state())
                    if path=='/api/intelligence/item':
                        item=CATALOG.item(q.get('item',[''])[0]);CATALOG._root(item['root']);item['preview']='/api/catalog/preview/'+item['id'];return self.reply(item)
                    if path=='/api/intelligence/facets':return self.reply(INTELLIGENCE.facets(q.get('key',[''])[0]))
                    if path=='/api/intelligence/details':return self.reply(INTELLIGENCE.details(q.get('item',[''])[0],q.get('frame',[None])[0]))
                    if path=='/api/intelligence/search':
                        with CATALOG_SEARCH.begin(q.get('client',[''])[0],q.get('sequence',['0'])[0],self.connection) as cancel:
                            data=INTELLIGENCE.search(text=q.get('q',[''])[0],smart=q.get('smart',['auto'])[0]=='1',facets=json.loads(q.get('facets',['{}'])[0]),root=q.get('root',[''])[0],media=q.get('media',[''])[0],cursor=q.get('cursor',[''])[0],limit=q.get('limit',[60])[0],cancel=cancel,quick=q.get('quick',['0'])[0]=='1')
                        return self.reply(data)
                except (ValueError,TypeError) as exc:return self.reply({'error':str(exc)},400)
            if path=='/api/analysis':return self.reply(ANALYSIS.state())
            if path=='/api/parsing':return self.reply(parsing_state())
            if path=='/api/sku/library/labels':
                from sku_library import labels
                params=parse_qs(url.query,keep_blank_values=True)
                return self.reply(labels(SKU,params.get('session_id',[''])[0],[v for v in params['ids'][0].split(',') if v] if 'ids' in params else None))
            if path=='/api/sku/rules':return self.reply(SKU.sku_rules())
            if path=='/api/sku/ocr/details':
                q=parse_qs(url.query);return self.reply(SKU.ocr_details(q.get('session_id',[''])[0],q.get('capture_id',[''])[0]))
            if path=='/api/sku/library':
                from sku_library_index import for_sku
                q=parse_qs(url.query)
                data=for_sku(SKU).browse(q.get('q',[''])[0],q.get('sort',['name'])[0],q.get('root',[''])[0],q.get('folder',[''])[0],q.get('sku',[''])[0],q.get('offset',[0])[0],q.get('limit',[120])[0],bin_id=q.get('bin_id',[''])[0],fresh=q.get('fresh',['0'])[0]=='1')
                return self.reply(data)
            if path=='/api/parsing/capture':
                from capture_parsing import capture_status
                q=parse_qs(url.query)
                return self.reply(capture_status(STORE,SKU,ARCHIVE,ANALYSIS,q.get('session_id',[''])[0],q.get('capture_id',[''])[0]))
            if path=='/api/raw-sync':
                q=parse_qs(url.query)
                return self.reply(RAWLINK.state(q.get('session_id',[None])[0],q.get('status',[''])[0],int(q.get('offset',['0'])[0])) if RAWLINK else {'version':0,'pairs':[],'work':[]})
            if path=='/api/analysis/results':
                aid=parse_qs(url.query).get('asset_id',[''])[0]
                with ANALYSIS.connect() as c:rows=[dict(r) for r in c.execute('SELECT * FROM results WHERE asset=? ORDER BY created DESC LIMIT 50',(aid,))]
                return self.reply({'results':rows})
            if path=='/api/metadata/operations':
                with ARCHIVE.connect() as c:rows=[dict(r) for r in c.execute('SELECT * FROM operations ORDER BY created DESC LIMIT 50')]
                return self.reply({'operations':rows})
            if path=='/api/metadata/queue':
                return self.reply({**ARCHIVE.metadata_queue_diagnostics(),'data_path':str(DATA),'releaseId':UPDATES.state().get('currentReleaseId') if UPDATES else ''})
            if path.startswith('/api/metadata/operations/'):return self.reply(ARCHIVE.operation(path.rsplit('/',1)[-1]))
            if path=='/api/sku/feedback':
                from shooting_feedback import feedback
                return self.reply(feedback(SKU) if SKU else {'labels':[],'total':0,'confirmed':0,'pending':0,'review':0,'failed':0})
            if path=='/api/sku':return self.reply(SKU.state() if SKU else {'project':None,'projects':[],'rows':[],'jobs':[]})
            if path=='/api/sku/documents':return self.reply({'documents':SKU.document_inventory(),'today':SKU.document_day()})
            if path=='/api/sku/documents/overview':return self.reply(SKU.document_overview(parse_qs(url.query).get('scope',['today'])[0]))
            if path=='/api/sku/gallery':
                if SKU is None:raise ValueError('SKU 服务未启动')
                from sku_presentation import gallery
                q=parse_qs(url.query)
                if not q.get('sku',[''])[0].strip():raise ValueError('请选择需要查看的 SKU 款号')
                return self.reply(gallery(SKU,q.get('project_id',[''])[0],q.get('sku',[''])[0],q.get('offset',[0])[0],q.get('limit',[60])[0]))
            if path=='/api/sku/image':
                if SKU is None:raise ValueError('SKU 服务未启动')
                params=parse_qs(url.query)
                data,kind=SKU.image(params.get('row_id',[''])[0],int(params.get('index',['0'])[0]))
                return self.reply(data,kind=kind)
            if path=='/api/sku/download':
                if SKU is None:raise ValueError('SKU 服务未启动')
                query=parse_qs(url.query)
                if query.get('job_id'):
                    output,job=SKU.exports.output(query['job_id'][0])
                    if query.get('project_id',[job['project_id']])[0]!=job['project_id']:raise ValueError('导出文档不匹配')
                    return self.reply(output.read_bytes(),kind='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',filename=job['name'])
                pid=parse_qs(url.query).get('project_id',[None])[0];p=SKU.project(pid)
                if not p:raise ValueError('对账任务不存在')
                output=SKU.export(p['id'])  # 兼容直接请求导出的旧调用；新界面始终按 job_id 下载已有结果。
                return self.reply(output.read_bytes(),kind='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',filename=shooting_export_name(p['name']))
            if path=='/api/sku/export':
                return self.reply(SKU.exports.state(parse_qs(url.query).get('job_id',[''])[0]))
            if path=='/api/mouse':return self.reply(MOUSE.state(refresh=True) if MOUSE else {'active':False,'devices':[],'message':'鼠标输入服务未启动'})
            if path=='/api/mouse/options':return self.reply(MOUSE.state() if MOUSE else {'active':False,'options':{},'message':'鼠标输入服务未启动'})
            if path=='/api/folders':return self.reply(capture_folder_state())
            if path=='/api/watch/direct':
                if not WATCH:return self.reply({'enabled':False,'event_id':0,'receiving':None,'last_photo_id':None})
                params=parse_qs(url.query)
                if 'after' in params:
                    return self.reply(WATCH.direct.wait_state(int(params['after'][0]),max(0,min(15,float(params.get('wait',['15'])[0])))))
                return self.reply(WATCH.direct.state())
            if path=='/api/watch':
                if not WATCH:return self.reply({'enabled':False,'message':'监控服务未启动'})
                params=parse_qs(url.query)
                if 'after' in params:
                    return self.reply(WATCH.wait_state(int(params['after'][0]),max(0,min(15,float(params.get('wait',['15'])[0])))))
                return self.reply(WATCH.state())
            if path.startswith('/api/watch/preview/'):
                parts=path.split('/')
                source=WATCH.direct.source_path(parts[4],parts[5]) if WATCH and len(parts)==6 else None
                root=WATCH.direct.source_root(parts[4],parts[5]) if source is not None else None
                if source is None or root is None:return self.reply({'error':'接收中的照片已失效'},404)
                return self.original_jpeg(source,root)
            if path=='/api/health':
                with STORE.connect() as c:pending=c.execute("SELECT COUNT(*) FROM bridge_jobs WHERE status='pending'").fetchone()[0]
                return self.reply({'application':'framekeep','process_id':os.getpid(),'busy':any(j['status'] in ('queued','running','cancelling') for j in STORE.folder_state()['scans']) or bool(pending) or bool(STORAGE and STORAGE.job.get('status')=='running') or bool(REFERENCES and REFERENCES.storage.job.get('status')=='running')})
            if path=='/api/task-status':return self.reply(task_status.snapshot(STORE,SKU,ARCHIVE,CATALOG,ANALYSIS,RAWLINK,COMPONENTS,UPDATES,STORAGE))
            if path=='/api/preview-cache':return self.reply(direct_preview_state())
            if path=='/api/sku/metadata/sync':
                return self.reply({'sync':getattr(SKU,'metadata_sync',None).state() if getattr(SKU,'metadata_sync',None) else None})
            if path=='/api/canon':return self.reply({'error':'联机拍摄已移除，请使用现场监控'},410)
            if path.startswith('/api/scan/'):
                job=STORE.jobs.get(path.rsplit('/',1)[-1]);return self.reply(job or {'error':'索引任务不存在'},200 if job else 404)
            if path.startswith('/api/runs/'):
                return self.reply({'error':'AI 选片已移除。'},410)
            if path.startswith('/api/preview/') or path.startswith('/api/sku/library/preview/'):
                sid,cid=path.split('/')[-2:]
                session=STORE.session(sid)
                with STORE.connect() as c:row=c.execute('SELECT data FROM captures WHERE session=? AND id=?',(sid,cid)).fetchone()
                if not session or not row:return self.reply({'error':'照片不存在'},404)
                try:
                    params=parse_qs(url.query);size=int(params.get('size',['480'])[0]);decode=params.get('decode',['0'])[0]=='1'
                    record=json.loads(row[0])
                    if params.get('full',['0'])[0]=='1' and not record.get('raw') and Path(record.get('jpg') or '').suffix.lower() in ('.jpg','.jpeg'):
                        return self.original_jpeg(Path(session['root'])/record['jpg'],session['root'])
                    if path.startswith('/api/sku/library/preview/'):
                        from sku_library_previews import library_preview_bytes
                        payload=library_preview_bytes(record,session['root'],lambda:media.find_exiftool(ROOT),size,decode)
                    else:payload=file_preview_bytes(record,session['root'],lambda:media.find_exiftool(ROOT),size,decode)
                    self.preview_cache='no-store'
                    return self.reply(payload,kind=image_mime(payload))
                except Exception:
                    self.preview_cache='no-store';return self.reply(media.placeholder(),422,kind='image/jpeg')
            if path=='/bridge/hello':
                BRIDGE_LAST_SEEN=now();return self.reply({'schema':'framekeep.bridge','version':1,'token':BRIDGE_TOKEN})
            if path=='/bridge/next':
                if self.headers.get('X-Framekeep-Bridge')!=BRIDGE_TOKEN:return self.reply({'error':'桥接令牌无效'},403)
                BRIDGE_LAST_SEEN=now()
                with STORE.connect() as c:job=c.execute("SELECT * FROM bridge_jobs WHERE status='pending' ORDER BY created LIMIT 1").fetchone()
                return self.reply({'job':{'id':job['id'],'manifest':json.loads(job['manifest'])} if job else None})
            if path=='/api/bridge/status':
                from datetime import datetime,timezone
                age=(datetime.now(timezone.utc)-datetime.fromisoformat(BRIDGE_LAST_SEEN)).total_seconds() if BRIDGE_LAST_SEEN else None
                return self.reply({'last_seen':BRIDGE_LAST_SEEN,'heartbeat_age_seconds':age,'connected':age is not None and age<20,'latest_job':self.bridge_job(),'diagnostic':BRIDGE_DIAGNOSTIC})
            if path.startswith('/api/') or path.startswith('/bridge/'):return self.reply({'error':'未知接口'},404)
            resolved=(WEB/path.lstrip('/')).resolve();resolved.relative_to(WEB.resolve())
            if resolved.is_dir() and resolved!=WEB.resolve():return self.reply({'error':'禁止目录列表'},403)
            if resolved!=WEB.resolve() and resolved.suffix.lower() not in ('.js','.css','.html','.jpg','.png','.svg'):return self.reply({'error':'非公开文件'},403)
            return super().do_GET()
        except SearchCancelled:return self.reply({'cancelled':True,'error':'搜索请求已取消'},409)
        except SearchBusy:return self.reply({'error':'搜索请求繁忙，请稍后再试'},429)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):return
        except (ValueError,FileNotFoundError):return self.reply({'error':'请求或路径非法'},400)
        except Exception:return self.reply({'error':'本机服务处理失败'},500)
    def bridge_job(self):
        with STORE.connect() as c:row=c.execute('SELECT id,status,result,created FROM bridge_jobs ORDER BY created DESC LIMIT 1').fetchone()
        if not row:return None
        data=dict(row);data['result']=json.loads(data['result']) if data['result'] else None;return data
    def do_POST(self):
        global BRIDGE_LAST_SEEN,BRIDGE_DIAGNOSTIC
        if not self.trusted():return self.reply({'error':'无效本机请求'},403)
        path=urlsplit(self.path).path;bridge=path in ('/bridge/result','/bridge/diagnostics')
        if path=='/api/desktop/shutdown':
            secret=os.environ.get('FRAMEKEEP_DESKTOP_TOKEN')
            if not secret or not secrets.compare_digest(self.headers.get('X-Framekeep-Desktop',''),secret):return self.reply({'error':'桌面实例不匹配'},403)
            if BOOT_GATE:BOOT_GATE.closed.set()
            for job in STORE.folder_state()['scans'] if STORE else []:
                if job['status'] in ('queued','running'):STORE.cancel_scan(job['id'])
            self.reply({'ok':True});threading.Thread(target=self.server.shutdown,daemon=True).start();return
        if path=='/api/desktop/canon':
            secret=os.environ.get('FRAMEKEEP_DESKTOP_TOKEN')
            if not secret or not secrets.compare_digest(self.headers.get('X-Framekeep-Desktop',''),secret):return self.reply({'error':'桌面实例不匹配'},403)
            if BOOT_GATE and not BOOT_GATE.capture_ready.is_set():return self.reply({'pending':True},503)
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=65536:raise ValueError('无效的照片通知')
                body=json.loads(self.rfile.read(length))
                if CANON is None:raise ValueError('EOS现场监控接入尚未启动')
                if CAPTURESYNC and CAPTURESYNC.receiving:raise ValueError('同步接收中，本机拍摄已停用')
                return self.reply(CANON.receive(body.get('paths')))
            except (ValueError,OSError) as exc:return self.reply({'error':str(exc)},400)
        if path in ('/api/canon','/api/canon/destination'):
            return self.reply({'error':'联机拍摄已移除，请使用现场监控'},410)
        if bridge:
            if self.headers.get('X-Framekeep-Bridge')!=BRIDGE_TOKEN:return self.reply({'error':'桥接令牌无效'},403)
        elif self.headers.get('X-Framekeep-Token')!=TOKEN or self.headers.get('Origin',f'http://127.0.0.1:{self.server.server_port}')!=f'http://127.0.0.1:{self.server.server_port}':return self.reply({'error':'请从本机工作台发起操作'},403)
        if BOOT_GATE:
            available=BOOT_GATE.wait_capture if path in ('/api/activate','/api/watch/start','/api/watch/stop') else BOOT_GATE.wait
            if not available():return self.reply({'error':BOOT_GATE.state()['error'] or '服务仍在准备，请稍后重试'},503)
            if BOOT_GATE.capture_readonly and not BOOT_GATE.ready.is_set() and path.startswith('/api/watch/'):
                return self.reply({'error':'正在单向接收，请退出同步后修改本机资料'},423)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if path=='/api/intelligence/reference':
                from reference_upload import receive
                return self.reply(receive(self.rfile,length,self.headers.get('X-Reference-Name',''),ROOT))
            maximum=None if path=='/api/intelligence/image' else 280_000_000 if path=='/api/sku/metadata/import' else 30_000_000 if path=='/api/sku/import' else 16_000_000
            if length<=0 or maximum is not None and length>maximum:return self.reply({'error':'请求过大或为空'},400)
            body=json.loads(self.rfile.read(length))
            if path.startswith('/api/automatic-backup'):
                if BACKUP is None:raise ValueError('自动备份服务尚未启动')
                if path=='/api/automatic-backup/configure':return self.reply(BACKUP.configure(body))
                if path=='/api/automatic-backup/retry':return self.reply(BACKUP.retry())
                return self.reply({'error':'备份接口不存在'},404)
            if path.startswith('/api/lan-sync/'):
                if CAPTURESYNC is None:raise ValueError('局域网同步服务尚未启动')
                if path=='/api/lan-sync/configure':return self.reply(CAPTURESYNC.configure(body))
                if path=='/api/lan-sync/discover':return self.reply(CAPTURESYNC.discover())
                if path=='/api/lan-sync/connect':return self.reply(CAPTURESYNC.connect_peer(body))
                if path=='/api/lan-sync/disconnect':return self.reply(CAPTURESYNC.disconnect())
                if path=='/api/lan-sync/disconnect-client':return self.reply(CAPTURESYNC.disconnect_client(body.get('device')))
                return self.reply({'error':'同步接口不存在'},404)
            if CAPTURESYNC and CAPTURESYNC.receiving and path not in ('/api/activate','/api/session','/api/session/patch','/api/sku/activate','/api/boot/complete','/api/archive/selection','/api/reveal-file'):
                return self.reply({'error':'正在单向接收，请退出同步后修改本机资料'},423)
            if path.startswith('/api/folder-archive/'):
                return folder_archive_api.post(self,path,body)
            if path.startswith('/api/archive-capture/'):
                return archive_capture_api.post(self,path,body)
            if path.startswith('/api/inventory/'):
                if INVENTORY is None:raise ValueError('出入库服务尚未启动')
                if path=='/api/inventory/configure':return self.reply(INVENTORY.configure(body))
                if path=='/api/inventory/pull':return self.reply(INVENTORY.pull(body))
                if path=='/api/inventory/usb':return self.reply(INVENTORY.usb(body))
                return self.reply({'error':'出入库接口不存在'},404)
            if path=='/api/workflow/sku-writeback/retry':
                if not ARCHIVE or not ARCHIVE.catalog_writeback:raise ValueError('SKU 元数据写入服务尚未启动')
                ARCHIVE.catalog_writeback.retry()
                return self.reply(WORKFLOW.state())
            if path in ('/api/workflow/config','/api/workflow/execute','/api/workflow/stop'):
                if WORKFLOW is None:raise ValueError('多维度解析服务尚未启动')
                if path.endswith('config'):return self.reply(WORKFLOW.configure(body))
                if path.endswith('stop'):return self.reply(WORKFLOW.stop(body['kind']))
                return self.reply(WORKFLOW.execute(body['kind'],body.get('session_id')))
            if path=='/api/boot/complete':
                if BOOT_TASKS:BOOT_TASKS.release()
                return self.reply({'ok':True})
            if path=='/api/lan-workspace':
                if LANWORKSPACE is None:raise ValueError('局域网工作台未启动')
                return self.reply(LANWORKSPACE.configure(body))
            if path.startswith('/api/intelligence/'):
                if INTELLIGENCE is None:raise ValueError('多维检索服务未启动')
                if path=='/api/intelligence/config':
                    from asset_intelligence import QUICK_APPEARANCE
                    return self.reply(INTELLIGENCE.configure(body,include_state=not (bool(body) and body.keys()<=QUICK_APPEARANCE)))
                if path=='/api/intelligence/backup':return self.reply(INTELLIGENCE.backup())
                if path=='/api/intelligence/enqueue':return self.reply(INTELLIGENCE.enqueue(body.get('item_ids'),body.get('root',''),body.get('force',False)))
                if path=='/api/intelligence/control':return self.reply(INTELLIGENCE.control(body['action'],body.get('job_id','')))
                if path=='/api/intelligence/image':return self.reply(INTELLIGENCE.image_search(body))
                if path=='/api/intelligence/group':return self.reply(INTELLIGENCE.confirm_group(body['id'],body['signature'],reject=body.get('reject',False),sku=body.get('sku','')))
            if path=='/api/reference/task':
                if REFERENCES is None:raise ValueError('参考资源服务未启动')
                project=SKU.project() if SKU else None
                return self.reply(REFERENCES.sync_task(STORE.active_id(),project.get('id') if project else None))
            if path.startswith('/api/reference/'):
                return self.reply(reference_api.local_post(REFERENCES,path.removeprefix('/api/reference/'),body))
            if path=='/api/video-preview':
                if CATALOG is None:raise ValueError('归档服务未启动')
                result=CATALOG.video.configure(body.get('frame_number'))
                if PREVIEWS:PREVIEWS.reconcile_archive(videos_only=True,full=True)
                return self.reply(result)
            if path=='/api/preview-cache':return self.reply({'error':'当前版本仅支持文件直读，预览缓存已停用'},410)
            if path=='/api/catalog/thumbnail-settings':
                CATALOG.thumbnail_settings.configure(body.get('cache_enabled'))
                return self.reply(archive_thumbnail_settings())
            if path=='/api/preview-cache/manage':return self.reply({'error':'当前版本仅支持文件直读，预览缓存已停用'},410)
            if path=='/api/components/prepare':
                groups=body['groups'] if 'groups' in body else preparation_groups(body.get('path',''),body.get('body',{}))
                return self.reply({**(COMPONENTS.start(groups) if COMPONENTS and groups else {'status':'ready','ready':[]}), 'groups':groups if COMPONENTS else []})
            if path=='/api/mouse/driver/prepare':return self.reply(DRIVER.start(activate=body.get('activate') is True) if DRIVER else {'status':'ready','installed':True})
            if path=='/api/sku/full-auto':
                if SKU is None:raise ValueError('OCR 服务尚未启动')
                return self.reply(SKU.set_full_auto(body['enabled'],body.get('session_id'),body.get('project_id'),body.get('scope')))
            if path=='/api/update/check':return self.reply(UPDATES.check())
            if path=='/api/update/open':return self.reply(UPDATES.open())
            if path=='/api/update/cancel':return self.reply(UPDATES.cancel())
            if path=='/api/folders/open':return self.reply(open_session_folder(STORE,body['session_id']))
            if path=='/api/catalog/visit':
                visits=getattr(CATALOG,'visits',None)
                return self.reply(visits.touch(body.get('root',''),body.get('folder',''),refresh=body.get('refresh',False)) if visits else {'mode':'legacy'})
            if path=='/api/catalog/file-operation':
                return self.reply(CATALOG.visits.explorer.submit(body))
            if path=='/api/catalog/file-open':
                return self.reply(CATALOG.visits.explorer.open(body['root'],body.get('folder',''),body.get('reveal',False)))
            if path=='/api/catalog/add':return self.reply(REFERENCES.add_root(body['folder']) if REFERENCES else CATALOG.add_root(body['folder']))
            if path=='/api/catalog/video/stop':
                CATALOG.playback.stop(body.get('session',''));return self.reply({'ok':True})
            if path=='/api/catalog/scan':return self.reply(REFERENCES.rescan(body['root_id']) if REFERENCES else CATALOG.scan(body['root_id']))
            if path in ('/api/catalog/thumbnails/repair','/api/catalog/decode'):return self.reply({'error':'当前版本仅支持文件直读，不再构建预览缓存'},410)
            if path=='/api/catalog/metadata':
                if ARCHIVE is None:raise ValueError('元数据服务未启动')
                from photo_metadata import save
                return self.reply(save(ARCHIVE,CATALOG,body['item_id'],body.get('metadata'),body.get('token')))
            if path=='/api/catalog/assign':
                capture=getattr(ARCHIVE,'capture_service',None)
                if capture:
                    return self.reply(capture.confirm(body['item_id'],body['sku'],whole_group=False))
                operation=None
                if ARCHIVE and CATALOG.item(body['item_id'])['media_type']=='image':
                    from photo_metadata import assign_sku
                    operation=assign_sku(ARCHIVE,CATALOG,body['item_id'],body['sku'])
                item=CATALOG.assign(body['item_id'],body['sku'])
                if operation:item['metadata_operation']=operation['operation']['id']
                return self.reply(item)
            if path=='/api/catalog/rating':
                capture=getattr(ARCHIVE,'capture_service',None)
                if capture:
                    return self.reply(capture.rate(body['item_id'],body.get('rating')))
                item=CATALOG.item(body['item_id']);rating=body.get('rating')
                if type(rating) is not int or rating not in range(6):raise ValueError('星级必须为 0–5 的整数')
                ARCHIVE.ratings.set([item['path']],rating)
                if RAWLINK:
                    with ARCHIVE.connect() as c:
                        sessions={r[0] for r in c.execute('SELECT DISTINCT k.session FROM locations l JOIN files f ON f.id=l.file JOIN catalog_links k ON k.asset=f.asset WHERE l.path=? COLLATE NOCASE',(item['path'],))}
                    for sid in sessions:RAWLINK.request(sid)
                return self.reply({'item_id':item['id'],'rating':rating})
            if path=='/api/catalog/open':return self.reply(CATALOG.open_folder(body['root_id'],body.get('folder','')))
            if path=='/api/catalog/remove':
                if REFERENCES and body.get('folder','') in ('','/'):
                    return self.reply(REFERENCES.remove_root(body['root_id']))
                return self.reply(CATALOG.remove(body['root_id'],body.get('folder','')))
            if path=='/api/folders/remove':
                sid=body['session_id']
                if not STORE.session(sid):raise ValueError('文件夹不存在')
                if WATCH and WATCH.state().get('session_id')==sid:WATCH.stop()
                active=STORE.remove_session(sid)
                return self.reply({'ok':True,'active_session_id':active,'watch':WATCH.state() if WATCH else None,**STORE.folder_state()})
            if path=='/api/reveal-file':
                if body.get('item_id'):
                    from file_reveal import reveal_catalog
                    return self.reply(reveal_catalog(CATALOG,body['item_id']))
                return self.reply(reveal_capture(STORE,body['session_id'],body['capture_id'],body.get('component')))
            if path.startswith('/api/raw-sync/'):
                if RAWLINK is None:raise ValueError('RAW 补写服务未启动')
                if path=='/api/raw-sync/targets':return self.reply(RAWLINK.add_target(body.get('folder')))
                if path=='/api/raw-sync/sync-target':return self.reply(RAWLINK.sync_target(body.get('target_id')))
                if path=='/api/raw-sync/retry-session':RAWLINK.request(body['session_id'],discover=True);return self.reply(RAWLINK.state(body['session_id']))
                if path=='/api/raw-sync/retry':return self.reply(RAWLINK.retry(body['pair_id']))
                if path=='/api/raw-sync/review':return self.reply(RAWLINK.review(body['pair_id'],body['source_session'],body['source_capture'],body.get('overwrite',False)))
            if path.startswith('/api/parsing/'):
                channel=body.get('channel')
                if path=='/api/parsing/clear-completed':
                    for service in (SKU, ANALYSIS, RAWLINK,WORKFLOW):
                        if service:service.clear_completed_tasks()
                elif path=='/api/parsing/clear-list':
                    if channel!='ocr':raise ValueError('解析任务类型无效')
                    if SKU is None:raise ValueError('OCR 服务尚未启动')
                    SKU.clear_task_list()
                elif path=='/api/parsing/ocr':
                    if SKU is None:raise ValueError('OCR 服务尚未启动')
                    SKU.enqueue_parses(body['session_id'],body['capture_ids'])
                elif path=='/api/parsing/configure':
                    if channel=='ocr' and SKU:SKU.configure_tasks(body.get('mode'),body.get('paused'))
                    elif channel=='multimodal' and ANALYSIS:
                        if 'mode' in body:
                            if body['mode'] not in ('auto','manual'):raise ValueError('解析模式无效')
                            ANALYSIS.configure_auto(body['mode']=='auto',body.get('session_id'),body.get('schema_id'),body.get('version'))
                        if 'paused' in body or 'concurrency' in body:ANALYSIS.control(paused=body.get('paused'),concurrency=body.get('concurrency'))
                    else:raise ValueError('解析服务或类型无效')
                    if WORKFLOW and 'paused' in body and channel in ('ocr','multimodal'):
                        kind='ocr' if channel=='ocr' else 'vision'
                        run=WORKFLOW.state()['runs'].get(kind,{})
                        if body['paused'] is False and run.get('status')=='stopped':WORKFLOW.execute(kind)
                        else:WORKFLOW.configure({kind+'_paused':body['paused']})
                elif path=='/api/parsing/control':
                    if body.get('action') not in ('cancel','retry'):raise ValueError('任务操作无效')
                    if str(body.get('job_id','')).startswith('workflow:') and WORKFLOW:WORKFLOW.control_job(body['action'],body['job_id'])
                    elif channel=='ocr' and SKU:SKU.configure_tasks(action=body['action'],job_id=body['job_id'])
                    elif channel=='multimodal' and ANALYSIS:ANALYSIS.control(action=body['action'],job_id=body['job_id'])
                    else:raise ValueError('解析服务或类型无效')
                else:raise ValueError('解析任务操作无效')
                return self.reply(parsing_state())
            if path.startswith('/api/analysis/'):
                if ANALYSIS is None:raise ValueError('视觉服务尚未启动')
                if path=='/api/analysis/schemas':return self.reply(ANALYSIS.save_schema(body['name'],body['prompt'],body['schema'],body.get('schema_id')))
                if path=='/api/analysis/batches':return self.reply(ANALYSIS.create_batch(body['asset_ids'],body['schema_id'],body['version'],body.get('provider_id')))
                if path=='/api/analysis/control':return self.reply(ANALYSIS.control(body.get('paused'),body.get('concurrency'),body.get('action'),body.get('job_id')))
            if path=='/api/archive/selection':
                ids=body.get('capture_ids',[])
                if not isinstance(ids,list) or not 1<=len(ids)<=2000:raise ValueError('请选择照片')
                if CAPTURESYNC and CAPTURESYNC.receiving:
                    with ARCHIVE.connect() as c:assets=[r[0] for cid in dict.fromkeys(ids) for r in c.execute('SELECT asset FROM catalog_links WHERE session=? AND capture=?',(body['session_id'],cid))]
                    return self.reply({'assets':[ARCHIVE.asset(aid) for aid in assets]})
                assets=[ARCHIVE.asset(ARCHIVE.ensure(body['session_id'],cid)) for cid in dict.fromkeys(ids)]
                capture=getattr(ARCHIVE,'capture_service',None)
                if capture:assets=capture.view_selection(body['session_id'],assets)
                return self.reply({'assets':assets})
            if path=='/api/archive/start':
                p=SKU.project()
                if not p or not p['scope'] or p['session_id']!=body.get('session_id'):raise ValueError('请先选择并绑定对账工作表')
                return self.reply(ARCHIVE.start_stream(p['session_id'],p['id'],p['scope'],mode='sidecar',start_capture=body.get('start_capture')))
            if path=='/api/archive/configure':return self.reply(ARCHIVE.configure_stream(body['stream_id'],True,'sidecar'))
            if path=='/api/archive/close':return self.reply(ARCHIVE.close_stream(body['stream_id'],body['sku']))
            if path=='/api/archive/segment-apply':return self.reply(ARCHIVE.apply_segment(body['segment_id']))
            if path=='/api/assets/relocate':return self.reply(ARCHIVE.relocate(body['folder']))
            if path=='/api/metadata/group/preview':
                from metadata_group import preview_group
                return self.reply(preview_group(ARCHIVE,SKU,body['session_id'],body['capture_id'],body.get('capture_ids'),body['sku'],body['scope'],body.get('project','')))
            if path=='/api/metadata/group/apply':
                from metadata_group import apply_group
                return self.reply(apply_group(ARCHIVE,SKU,body['preview_id'],body['sku']))
            if path=='/api/metadata/group/confirm':
                from metadata_group import confirm_group
                return self.reply(confirm_group(ARCHIVE,SKU,body['session_id'],body['capture_id'],body.get('capture_ids'),body['sku'],body.get('scope',''),body.get('project','')))
            if path=='/api/metadata/preview':return self.reply(ARCHIVE.preview(body['session_id'],body['capture_ids'],body['sku'],body['scope'],body.get('project',''),'sidecar',body.get('overwrite',False)))
            if path=='/api/metadata/apply':return self.reply(ARCHIVE.apply(body['preview_id']))
            if path=='/api/metadata/retry':return self.reply(ARCHIVE.retry(body['operation_id']))
            if path=='/api/metadata/revert':return self.reply(ARCHIVE.revert(body['operation_id']))
            if path.startswith('/api/providers/'):
                if MODELS is None:raise ValueError('模型配置服务未启动')
                if path=='/api/providers/builtin':
                    state=MODELS.set_builtin(body['enabled'])
                    state['runtime']=local_inference.preload(MODELS.selected('ocr') if state['builtin_enabled'] else None)
                    return self.reply(state)
                if path=='/api/providers/save':return self.reply(MODELS.save(body))
                if path=='/api/providers/models':return self.reply(MODELS.models(body))
                if path=='/api/providers/select':return self.reply(MODELS.select(body['id']))
                if path=='/api/providers/delete':return self.reply(MODELS.delete(body['id']))
                if path=='/api/providers/bind':return self.reply(MODELS.bind(body['purpose'],body['id']))
                if path=='/api/providers/test':return self.reply(ANALYSIS.test_provider(body['id']))
                if path=='/api/providers/import-legacy':
                    config_path=Path(body['path']).resolve();c=assistant_config.load_config(config_path)
                    return self.reply(MODELS.save({'id':'legacy-config','name':'原 AI 助手','category':'multimodal','protocol':'responses','url':c['url'],'model':c['model'],'key':c['key']}))
            if path.startswith('/api/sku/'):
                if SKU is None:raise ValueError('SKU 对账服务未启动')
                if path=='/api/sku/header':
                    from sku_presentation import rename_header
                    return self.reply(rename_header(SKU,body.get('project_id'),body.get('key'),body.get('title')))
                if path=='/api/sku/cover':
                    from sku_presentation import select_images
                    if not str(body.get('sku','')).strip():raise ValueError('请选择需要设置图片的 SKU 款号')
                    return self.reply(select_images(SKU,body.get('project_id'),body.get('choices'),body['sku']))
                if path=='/api/sku/metadata/export':
                    from sku_metadata_backup import export_metadata
                    return self.reply(export_metadata(SKU))
                if path=='/api/sku/metadata/import':
                    from sku_metadata_backup import import_metadata
                    from sku_metadata_sync import attach
                    try:content=base64.b64decode(body['content_base64'],validate=True)
                    except Exception:raise ValueError('元数据文件内容无效') from None
                    imported=import_metadata(SKU,content,sync=attach(SKU,CATALOG,RAWLINK),mode=body.get('mode','merge'))
                    return self.reply(imported)
                if path=='/api/sku/library/manage':
                    from sku_library_management import manage
                    return self.reply(manage(SKU,ARCHIVE,body))
                if path=='/api/sku/parse':return self.reply(SKU.parse_capture(body['session_id'],body['capture_id']))
                if path=='/api/sku/rules':return self.reply(SKU.set_sku_rules(body.get('rules'),body.get('length_filter')))
                if path=='/api/sku/ocr/choose':return self.reply(SKU.choose_ocr_sku(body['session_id'],body['capture_id'],body['sku'],body['reading_key'],body['image_key'],body.get('scope','')))
                if path=='/api/sku/import':
                    try:content=base64.b64decode(body['content_base64'],validate=True)
                    except Exception:raise ValueError('Excel 文件内容无效') from None
                    return self.reply(SKU.import_file(body['name'],content,body.get('session_id'),body.get('preferred_scope')))
                if path=='/api/sku/configure':return self.reply(SKU.configure(body['project_id'],body.get('session_id'),body['scope'],body.get('enabled',True)))
                if path=='/api/sku/type':return self.reply(SKU.create_shooting_type(body['project_id'],body['name'],body.get('session_id')))
                if path=='/api/sku/document/manage':return self.reply(SKU.manage_document(body['project_id'],body['action']))
                if path=='/api/sku/row/rename':return self.reply(SKU.rename_shooting_row(body['project_id'],body['row_id'],body['old_sku'],body['sku']))
                if path=='/api/sku/sync-progress':
                    watch=WATCH.state() if WATCH else {};generation=body.get('watch_generation')
                    if (watch.get('enabled') or generation) and (not watch.get('enabled') or body.get('session_id')!=watch.get('session_id') or generation!=watch.get('generation')):
                        raise ValueError('监控目录已变化，请重新点击同步拍摄进度')
                    return self.reply(SKU.sync_progress(body['project_id'],body.get('session_id')))
                if path=='/api/sku/activate':return self.reply(SKU.activate(body['project_id']))
                if path=='/api/sku/recognize':return self.reply(SKU.enqueue(body['session_id'],body['capture_id']))
                if path=='/api/sku/review':return self.reply(SKU.review(body['job_id'],body['action'],body.get('row_id')))
                if path=='/api/sku/export':
                    p=SKU.project(body.get('project_id'))
                    if not p:raise ValueError('对账任务不存在')
                    if body.get('background'):return self.reply(SKU.exports.start(p['id'],shooting_export_name(p['name'])))
                    output=SKU.export(p['id'])
                    return self.reply({'path':str(output),'source_name':output.name,'project_id':p['id'],'name':shooting_export_name(p['name'])})
            if path=='/api/watch/start':
                if WATCH is None:raise ValueError('监控服务未启动')
                if CANON:CANON.pause()
                result=WATCH.start(body['folder'],body.get('recursive',True));STORE.activate(result['session_id']);return self.reply(result)
            if path=='/api/watch/stop':
                if WATCH is None:raise ValueError('监控服务未启动')
                if CANON:CANON.pause()
                return self.reply(WATCH.stop())
            if path in ('/api/open','/api/folders') and WATCH and WATCH.state()['enabled']:
                watched=WATCH.state()['folder'].casefold()
                folders=body.get('folders',[body.get('folder','')])
                if any(str(Path(p).resolve()).casefold()==watched for p in folders):raise ValueError('此目录正在现场监控，请停止监控后再重新索引。')
            if path.startswith('/api/mouse/'):
                if MOUSE is None:raise ValueError('鼠标输入服务未启动')
                if path=='/api/mouse/start':return self.reply(MOUSE.start(body['client'],body['device']))
                if path=='/api/mouse/auto-start':return self.reply(MOUSE.auto_start(body['client']))
                if path=='/api/mouse/options':return self.reply(MOUSE.configure(body['options'],body.get('client')))
                if path=='/api/mouse/stop':return self.reply(MOUSE.stop())
                if path=='/api/mouse/poll':return self.reply(MOUSE.poll(body['client'],body['generation'],body['after']))
            if path=='/bridge/diagnostics':
                BRIDGE_DIAGNOSTIC=body
                (DATA/'bridge-diagnostic.json').write_text(json.dumps(body,ensure_ascii=False,indent=2),encoding='utf8')
                BRIDGE_LAST_SEEN=now();return self.reply({'ok':True})
            if path=='/api/config':
                config_path=Path(body['path']).resolve()
                assistant_config.load_config(config_path)
                (DATA/'config-location').write_text(str(config_path),encoding='utf8')
                os.environ['FRAMEKEEP_CONFIG']=str(config_path)
                return self.reply(assistant_config.public_config())
            if path=='/api/choose-folder':
                import tkinter as tk
                from tkinter import filedialog
                root=tk.Tk();root.withdraw();root.attributes('-topmost',True)
                try:folder=filedialog.askdirectory(title='选择备份文件夹' if body.get('purpose')=='backup' else '选择照片文件夹',parent=root)
                finally:root.destroy()
                return self.reply({'folder':folder or None})
            if path=='/api/bridge/install':
                from install_bridge import install
                return self.reply({'path':str(install(ROOT))})
            if path=='/api/ratings/retry':return self.reply(ARCHIVE.ratings.sync.retry())
            if path=='/api/open':return self.reply({'scan_id':STORE.scan(body['folder'],body.get('recursive',True))},202)
            if path=='/api/folders':return self.reply({'scan_ids':STORE.scan_many(body['folders'],body.get('recursive',True))},202)
            if path=='/api/scan/cancel':return self.reply(STORE.cancel_scan(body['scan_id']))
            if path=='/api/activate':STORE.activate(body['session_id']);return self.reply({'ok':True})
            if path=='/api/session':
                return self.reply(save_session_state(body))
            if path=='/api/session/patch':
                return self.reply(save_session_state(body,partial=True))
            if path in ('/api/analyze','/api/reviews'):return self.reply({'error':'AI 选片已移除。'},410)
            if path=='/api/export':return self.reply(rated_manifest(body.get('selected_only',True),body.get('session_id')))
            if path=='/api/bridge':
                manifest=rated_manifest(True,body.get('session_id'))
                if not manifest['captures']:raise ValueError('没有已选照片。')
                job=uuid.uuid4().hex
                with STORE.connect() as c:
                    c.execute("UPDATE bridge_jobs SET status='superseded' WHERE status='pending'")
                    c.execute('INSERT INTO bridge_jobs VALUES (?,?,?,?,?)',(job,now(),'pending',stable_json(manifest),None))
                return self.reply({'id':job,'status':'pending'})
            if bridge:
                status=body.get('status')
                if status not in ('applied','cancelled','failed'):raise ValueError('无效桥接状态。')
                with STORE.connect() as c:
                    count=c.execute("UPDATE bridge_jobs SET status=?,result=? WHERE id=? AND status='pending'",(status,stable_json(body.get('result',{})),body.get('id'))).rowcount
                BRIDGE_LAST_SEEN=now();return self.reply({'ok':bool(count)})
            return self.reply({'error':'未知接口'},404)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):return
        except (ValueError,KeyError) as e:return self.reply({'error':str(e)[:500]},409 if '其他窗口' in str(e) else 400)
        except Exception as exc:
            from operation_errors import record_error
            identity=record_error(DATA,path,exc)
            message='SKU 应用未完成，详细错误已记录，请重试' if path=='/api/sku/ocr/choose' else '本机操作未完成，请重试'
            return self.reply({'error':message+'（错误编号 '+identity+'）','error_id':identity},500)

def watch_desktop_parent(http):
    """桌面主程序异常结束时也退出引擎，避免留下无窗口后台。"""
    parent=os.environ.get('FRAMEKEEP_DESKTOP_PID')
    if os.name!='nt' or not parent:return
    def watch():
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x00100000,False,int(parent))
        if handle:
            kernel.WaitForSingleObject(handle,0xFFFFFFFF);kernel.CloseHandle(handle)
        http.shutdown()
    threading.Thread(target=watch,daemon=True).start()

def main():
    from startup_status import StartupProgress
    with StartupProgress(DATA) as startup:_run_server(startup)


def _run_server(startup):
    global BOOT_GATE
    global STORE,MOUSE,WATCH,DIRECT,MODELS,SKU,ARCHIVE,ANALYSIS,INTELLIGENCE,LANWORKSPACE,CAPTURESYNC,INVENTORY,RAWLINK,FILESEARCH,UPDATES,CATALOG,COMPONENTS,DRIVER,PREVIEWS,CANON,STORAGE,REFERENCES,BOOT_TASKS,WORKFLOW
    startup_checkpoint=startup.checkpoint
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8769);parser.add_argument('--folder');parser.add_argument('--browser',action='store_true');parser.add_argument('--no-browser',action='store_true');parser.add_argument('--install-bridge',action='store_true');args=parser.parse_args()
    if args.install_bridge:
        from install_bridge import install
        print(install(ROOT));return
    assistant_config.ROOT=ROOT
    if (DATA/'config-location').is_file() and not os.environ.get('FRAMEKEEP_CONFIG'):os.environ['FRAMEKEEP_CONFIG']=(DATA/'config-location').read_text(encoding='utf8').strip()
    try:server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    except OSError:
        if not args.no_browser:
            import webbrowser
            webbrowser.open(f'http://127.0.0.1:{args.port}/')
        return
    server.daemon_threads=True
    from startup_gate import StartupGate
    BOOT_GATE=StartupGate(DATA,ROOT)
    server._startup_http_thread=threading.Thread(target=server.serve_forever,name='startup-http',daemon=True)
    server._startup_http_thread.start()
    startup_checkpoint('shell ready')
    watch_desktop_parent(server)
    try:_initialize_server(server,args,startup)
    except BaseException as error:
        BOOT_GATE.error=type(error).__name__
        raise
    finally:
        BOOT_GATE.closed.set();BOOT_GATE.ready.set();BOOT_GATE.capture_ready.set()
        if BOOT_GATE.error:
            if WATCH:WATCH.close()
            if DIRECT:DIRECT.close()
        server.shutdown();server.server_close();server._startup_http_thread.join(2)
        BOOT_GATE=None


def _initialize_server(server,args,startup):
    global BACKUP
    global STORE,MOUSE,WATCH,DIRECT,MODELS,SKU,ARCHIVE,ANALYSIS,INTELLIGENCE,LANWORKSPACE,CAPTURESYNC,INVENTORY,RAWLINK,FILESEARCH,UPDATES,CATALOG,COMPONENTS,DRIVER,PREVIEWS,CANON,STORAGE,REFERENCES,BOOT_TASKS,WORKFLOW
    startup_checkpoint=startup.checkpoint
    STORE=Store(DATA,ROOT)
    # 拍摄接入只依赖轻量会话库；大归档库及模型仍在后续初始化。
    sync_path=DATA/'lan-capture-sync.json'
    sync_config=json.loads(sync_path.read_text('utf8')) if sync_path.is_file() else {}
    receiving=bool(sync_config.get('enabled') and sync_config.get('remote'))
    BOOT_GATE.capture_readonly=receiving
    pending_eos=any((DATA/'canon-link/inbox').glob('*.json'))
    WATCH=FolderWatch(STORE,autostart=False,restore=not pending_eos and not receiving)
    DIRECT=DirectImageChannel(WATCH,f'http://127.0.0.1:{server.server_port}')
    CANON=EosMonitor(STORE,WATCH)
    if receiving:CANON.pause()
    from catalog_notifications import CatalogNotifications
    live_notifications=CatalogNotifications(lambda sid,records:catalog_changed(sid,records),autostart=False)
    STORE.on_catalog=live_notifications.request;STORE.on_live_catalog=live_notifications.request
    WATCH.on_arrivals=BOOT_GATE.capture_arrivals
    WATCH.thread.start()
    BOOT_GATE.capture_ready.set();startup_checkpoint('capture ready')
    import gpu_media,gpu_video
    from video_preview import find_tool
    CATALOG=AssetCatalog(DATA,keep_wal_open=True)
    CATALOG.metadata.maintenance_blocked=lambda:bool(WATCH and WATCH.state().get('enabled') or getattr(CATALOG,'visits',None) and CATALOG.visits.state()['running'])
    from archive_visits import ArchiveVisits
    CATALOG.visits=ArchiveVisits(CATALOG)
    startup_checkpoint('catalog ready')
    STORAGE=None
    PREVIEWS=None
    startup_checkpoint('preview queue ready')
    STORE.on_previews=None
    CATALOG.on_previews=None
    MOUSE=MouseRemote(settings_path=DATA/'mouse-remote.json')
    MODELS=Providers(DATA,legacy=assistant_config.load_config)
    COMPONENTS=ComponentManager(DATA,seeds=[ROOT/'runtime/components',ROOT/'.runtime/component-seed',ROOT/'.runtime',ROOT.parent.parent/'.runtime'])
    DRIVER=DriverSetup(COMPONENTS,DATA,activator=MOUSE.activate_driver)
    local_inference.configure(ROOT,DATA,COMPONENTS)
    scheduler=VisionScheduler()
    ARCHIVE=AssetArchive(STORE,autostart=False)
    # 正在监控拍摄时，文件写入队列只服务当前拍摄会话，旧照片补写暂停。
    ARCHIVE.live_session=lambda:(lambda s:s.get('session_id') if s.get('enabled') else None)(WATCH.state()) if WATCH else None
    from automatic_backup import AutomaticBackup
    BACKUP=AutomaticBackup(DATA,ARCHIVE.writer,roots=lambda:[WATCH.state()['folder']] if WATCH and WATCH.state().get('folder') else [])
    # 后台归档可能长时间持锁读取照片，须先完成共享评级初始化。
    ratings=ARCHIVE.ratings
    # 大会话评级合并由 historical-ratings 完成；首屏读取仍可按需获取评级。
    startup_checkpoint('ratings ready')
    FILESEARCH=EverythingSearch(ARCHIVE)
    STORE.discovery=FILESEARCH
    CATALOG.discovery=FILESEARCH
    REFERENCES=ReferenceLibrary(DATA,FILESEARCH,PREVIEWS,catalog=CATALOG,autostart=False)
    CATALOG.preview_warmer=PREVIEWS
    startup_checkpoint('reference library ready')
    SKU=SkuService(STORE,MODELS,archive=ARCHIVE,scheduler=scheduler,require_sku_choice=True,autostart=False)
    ARCHIVE.sku=SKU
    from archive_capture_service import ArchiveCaptureService
    capture_service=ArchiveCaptureService(STORE,ARCHIVE,CATALOG,SKU,watch=WATCH,autostart=False,readonly=lambda:bool(CAPTURESYNC and CAPTURESYNC.receiving))
    ANALYSIS=AssetAnalysis(ARCHIVE,MODELS,scheduler,autostart=False)
    startup_checkpoint('analysis ready')
    INTELLIGENCE=AssetIntelligence(CATALOG,MODELS,SKU,autostart=False)
    from catalog_sku_writeback import CatalogSkuWriteback
    CatalogSkuWriteback(ARCHIVE,CATALOG,SKU)
    startup_checkpoint('intelligence ready')
    WORKFLOW=AnalysisWorkflow(ANALYSIS,INTELLIGENCE,SKU,lambda:WATCH.state())
    SKU.workflow=WORKFLOW;SKU.analysis=ANALYSIS;SKU.intelligence=INTELLIGENCE
    CATALOG.on_media_changes=WORKFLOW.indexed
    LANWORKSPACE=LanWorkspace(CATALOG,SKU,INTELLIGENCE,WEB)
    INVENTORY=InventoryService(DATA,SKU.sku_rules)
    RAWLINK=RawReconcile(ARCHIVE,autostart=False)
    UPDATES=UpdateClient(ROOT,DATA,args.port)
    def catalog_changed(sid,records=None):
        session=STORE.session(sid)
        if session and (session['state'].get('sync_origin') or CAPTURESYNC and CAPTURESYNC.data.origin(sid)):return
        if records is None:ARCHIVE.ratings.seed_session(sid)
        else:
            session=STORE.session(sid)
            ARCHIVE.ratings.seed_records(session,records)
            if PREVIEWS:PREVIEWS.request(session['root'],records)
        if records is None:RAWLINK.request(sid,discover=True)
        else:RAWLINK.request(sid,capture_ids=[record['id'] for record in records])
        ANALYSIS.request_auto(sid);PIPELINE.changed(sid)
        WORKFLOW.changed(sid,records)
    PIPELINE=ArchivePipeline(CATALOG,ARCHIVE,SKU,RAWLINK,autostart=False)
    from sku_metadata_sync import attach
    metadata_sync=attach(SKU,CATALOG,RAWLINK,autostart=False)
    STORE.on_catalog=catalog_changed
    live_notifications.thread.start()
    STORE.on_external_metadata=ARCHIVE.ratings.observe_sidecars
    def before_sync_receive():
        CANON.pause()
        WATCH.stop()
    def capture_sync_updated():
        views=workbench_views()
        if views:views.invalidate()
    CAPTURESYNC=LanCaptureSync(STORE,SKU,ARCHIVE,before_receive=before_sync_receive,on_update=capture_sync_updated,
                               host_state=lambda:{'watch':WATCH.state(),'shooting_context':SKU.shooting_context()})
    if CAPTURESYNC.receiving:before_sync_receive()
    BOOT_GATE.bind_arrivals(STORE,SKU.live_arrivals)
    if args.folder:STORE.scan(args.folder)
    recovery_cleanup=LegacyRecoveryCleanup(DATA,lambda:configured_roots(STORE,CATALOG,WATCH,REFERENCES))
    from startup_tasks import StartupTasks
    def warm_video():
        tool=find_tool('ffmpeg')
        if tool:gpu_video.warmup(tool)
    def warm_models():
        if MODELS.public().get('builtin_enabled'):local_inference.preload(MODELS.selected('ocr'))
    def seed_history():
        for session in STORE.sessions():
            if BOOT_TASKS.closed.is_set():return
            ARCHIVE.ratings.seed_session(session['id'])
    tasks=[('catalog-wal-maintenance',CATALOG.wal_maintenance.start),
           ('catalog-wal-recovery',CATALOG.wal_maintenance.wait_for_startup_recovery),
           ('historical-ratings',seed_history),
           ('archive',ARCHIVE.thread.start),('ratings',ratings.sync.thread.start),
           ('pipeline',PIPELINE.thread.start),('sku',SKU.thread.start),('rawlink',RAWLINK.thread.start),('archive-capture',capture_service.thread.start),
           *[(f'analysis-{i}',thread.start) for i,thread in enumerate((*ANALYSIS.threads,ANALYSIS.auto_thread))],
           *([('quick-file-index',INTELLIGENCE.quick_index.start)] if INTELLIGENCE.quick_index else []),
           ('literal-search-index',CATALOG.literal_index.start),('metadata',CATALOG.metadata.start),('natural-search-index',INTELLIGENCE.search_index.start),
           ('reference',REFERENCES.start),('analysis-workflow',WORKFLOW.start),
           # 线程启动先于同步预热：GPU/视频/模型预热可能耗时数秒，不能拖后检索服务就绪。
           *[(f'intelligence-{i}',thread.start) for i,thread in enumerate((*INTELLIGENCE.threads,INTELLIGENCE.discovery))],
           ('cleanup',recovery_cleanup.start),
           ('gpu-images',gpu_media.warmup),('gpu-video',warm_video),('models',warm_models)]
    from sku_library_index import for_sku
    tasks=[(name,run) for name,run in tasks if name not in ('literal-search-index','natural-search-index','quick-file-index','preview-reconcile','cache-maintenance')]
    tasks.insert(0,('sku-library-view',lambda:for_sku(SKU).start()))
    if metadata_sync:tasks.insert(7,('metadata-sync',metadata_sync.thread.start))
    BOOT_TASKS=StartupTasks(tasks);BOOT_TASKS.start()
    # 文件持久队列独立恢复，不等待首屏释放、历史扫描或 WAL 回收任务。
    ARCHIVE.start_metadata()
    startup_checkpoint('http ready')
    BOOT_GATE.ready.set()
    url=f'http://127.0.0.1:{args.port}/';print('SHIYING工作台：'+url,flush=True)
    if not args.no_browser and (args.browser or getattr(sys,'frozen',False)):
        import webbrowser
        webbrowser.open(url)
    try:server._startup_http_thread.join()
    except KeyboardInterrupt:pass
    finally:
        if _WORKBENCH_VIEWS:_WORKBENCH_VIEWS.flush()
        if BACKUP:BACKUP.close()
        CAPTURESYNC.close()
        folder_archive_api.close(server)
        capture_service.close()
        BOOT_TASKS.closed.set()
        # 停止生产者期间仍须回收其最后提交的 WAL；等所有后台服务退出后
        # 由 CATALOG.close 停止维护，避免关窗时先停回收、后继续写库。
        BOOT_TASKS.close()
        WATCH.close()
        DIRECT.close()
        live_notifications.close()
        LANWORKSPACE.close()
        INVENTORY.close()
        recovery_cleanup.close()
        REFERENCES.close()
        if getattr(SKU,'metadata_sync',None):SKU.metadata_sync.close()
        PIPELINE.close()
        RAWLINK.close()
        INTELLIGENCE.close()
        WORKFLOW.close()
        ANALYSIS.close()
        SKU.close()
        local_inference.close()
        COMPONENTS.close()
        ARCHIVE.close()
        WATCH.close()
        if STORAGE:STORAGE.close()
        if PREVIEWS:PREVIEWS.close()
        CATALOG.close()
        MOUSE.close()
        server.server_close()
        for job in STORE.folder_state()['scans']:
            if job['status'] in ('queued','running'):STORE.cancel_scan(job['id'])
        # 给正在读取元数据的子进程时间接收取消，避免桌面退出后残留。
        from time import monotonic,sleep
        until=monotonic()+1.5
        while STORE.scan_worker_running and monotonic()<until:sleep(.05)

if __name__=='__main__':main()
