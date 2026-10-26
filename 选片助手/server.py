"""正式本机选片服务、照片预览与 Lightroom 桥接队列。"""
from __future__ import annotations
import argparse
from datetime import datetime
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
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit,parse_qs,unquote,quote
import assistant_config
import task_status
import media_engine as media
from mouse_remote import MouseRemote
from folder_watch import FolderWatch
from model_providers import Providers
import local_inference
from component_setup import ComponentManager
from mouse_native.driver_setup import DriverSetup
from sku_service import SkuService
from catalog_store import Store,now,stable_json
from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from asset_analysis import AssetAnalysis
from vision_scheduler import VisionScheduler
from raw_reconcile import RawReconcile
from archive_pipeline import ArchivePipeline
from everything_search import EverythingSearch
from file_reveal import reveal_capture,open_session_folder
from update_client import UpdateClient
from preview_cache import cache_for,PreviewWarmer
from cache_storage import CacheStorage
from canon_link import CanonLink
from reference_library import ReferenceLibrary
import reference_api
import session_browser
from preview_codec import image_mime

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
BRIDGE_DIAGNOSTIC=None
MOUSE=None
WATCH=None
MODELS=None
SKU=None
ARCHIVE=None
ANALYSIS=None
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


def preparation_groups(path, body):
    if MODELS is None:return []
    if path=='/api/providers/test':providers=[MODELS.by_id(body.get('id'))]
    else:
        categories=['multimodal'] if path.startswith('/api/analysis/') or body.get('channel')=='multimodal' else ['ocr'] if path=='/api/parsing/ocr' else ['ocr','multimodal']
        providers=[MODELS.purpose('asset_analysis') if c=='multimodal' and path.startswith('/api/analysis/') else MODELS.selected(c) for c in categories]
    for provider in providers:
        if provider and provider.get('protocol')=='local':MODELS.check_inference(provider)
    return list(dict.fromkeys(p['model'] for p in providers if p and p.get('protocol')=='local'))

def ui_photos(sid=None,records=None):
    session=STORE.session(sid)
    if not session:return []
    result=[]
    bounded=records is not None
    records=STORE.records(session['id']) if records is None else records
    if PREVIEWS:
        if bounded:PREVIEWS.enqueue(session['root'],records)
        else:PREVIEWS.ensure_session(session,records)
    for record in records:
        r=dict(record);r['preview']=f"/api/preview/{session['id']}/{r['id']}?size=480";r['previewLarge']=f"/api/preview/{session['id']}/{r['id']}?size=2048"
        if r.get('raw'):r['previewRaw']=r['previewLarge']
        for key in ('preview','previewLarge','previewRaw'):
            if key in r:r[key]+='&v='+quote(r['fingerprint'])
        result.append(r)
    return result


def browse_state(sid=None,query=None):
    session=STORE.session(sid)
    if not session:return None,[]
    if session_browser.count(STORE,session['id'])>session_browser.LARGE_SESSION:
        session,records=session_browser.read(STORE,session['id'],query,ARCHIVE)
        return session,ui_photos(session['id'],records=records)
    return rated_session(session['id']),ui_photos(session['id'])

def shooting_export_name(name):
    if __import__('re').fullmatch(r'\d{4}-\d{2}-\d{2}\.xlsx',str(name)):return Path(name).name
    return Path(name).stem+'-拍摄记录-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.xlsx'


def rated_session(sid=None):
    session=STORE.session(sid)
    return ARCHIVE.ratings.session(session,STORE.records(session['id'])) if ARCHIVE and session else session


def save_session_state(body,partial=False):
    # 会话版本更新与评级/SKU 回调保持同序，防止另一窗口的新评级被迟到回调覆盖。
    with STORE.browser_lock:
        sid=body['session_id']
        if partial:
            before,state,revision=session_browser.patch_state(STORE,sid,body['revision'],body['changes'])
        else:
            before=STORE.session(sid) if isinstance(body.get('rating_baseline'),dict) else rated_session(sid)
            state=body['state'];revision=STORE.save(sid,body['revision'],state)
        baseline={**before['state'].get('overrides',{}),**before.get('sharedRatings',{}),**(body.get('rating_baseline') or {})}
        result={'revision':revision}
        if ARCHIVE:ARCHIVE.ratings.saved(sid,before,state,baseline)
        if SKU:
            try:SKU.on_save(sid,{**before['state'],'overrides':baseline},state)
            except Exception:result['sku_error']='星级已保存，但 SKU 识别未能排队，请在拍摄对账面板重试'
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
    return {'analysis':ANALYSIS.state() if ANALYSIS else {'schemas':[],'jobs':[],'counts':{}},'ocr':SKU.task_state() if SKU else {'mode':'auto','paused':False,'jobs':[],'counts':{},'model':'未启动'},'metadata':RAWLINK.task_state() if RAWLINK else {'batches':[]}}

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(WEB),**kwargs)
    def log_message(self,*args):pass
    def end_headers(self):
        connect="'self' ipc: http://ipc.localhost" if os.environ.get('FRAMEKEEP_DESKTOP_TOKEN') else "'self'"
        self.send_header('X-Content-Type-Options','nosniff');self.send_header('Cache-Control',getattr(self,'preview_cache','no-store'));self.send_header('Content-Security-Policy',f"default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src {connect}; frame-ancestors 'none'");super().end_headers()
    def trusted(self):return self.headers.get('Host')==f'127.0.0.1:{self.server.server_port}'
    def reply(self,data,status=200,kind='application/json; charset=utf-8',filename=None):
        content=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(content)))
        if filename:self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+quote(filename))
        self.end_headers()
        try:self.wfile.write(content)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
    def do_GET(self):
        global BRIDGE_LAST_SEEN
        if not self.trusted():return self.reply({'error':'本机 Host 不匹配'},403)
        url=urlsplit(self.path);path=unquote(url.path)
        try:
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
                return self.reply({'busy':busy or bool(STORAGE and STORAGE.job.get('status')=='running') or bool(REFERENCES and REFERENCES.storage.job.get('status')=='running')})
            if path=='/assets/demo-data.js':
                session,rows=browse_state();data={'id':session['id'],'name':session['name'],'root':session['root'],'snapshot':session['snapshot'],'revision':session['revision'],'saved':session['state'],'sharedRatings':session.get('sharedRatings',{}),'browser':session.get('browser'),'demo':False} if session else {'id':None,'name':'未打开文件夹','root':'','snapshot':'','revision':0,'saved':{},'demo':False}
                js='window.FRAMEKEEP_SESSION='+stable_json(data)+';window.DEMO_PHOTOS='+stable_json(rows)+';'
                return self.reply(js.encode(),kind='text/javascript; charset=utf-8')
            if path=='/api/state':
                params={key:values[0] for key,values in parse_qs(url.query,keep_blank_values=True).items()};sid=params.get('session_id');session,rows=browse_state(sid,params)
                if sid and not session:return self.reply({'error':'文件夹会话不存在'},404)
                catalog=[dict(r,captured_at_local=r.get('time')) for r in rows];browser=session.get('browser',{}) if session else {}
                return self.reply({'demo':False,'database':'SQLite','session':session,**STORE.folder_state(),'catalog':catalog,'indexed_captures':browser.get('total',len(rows)),'indexed_files':browser.get('files',sum(r.get('fileCount',0) for r in rows)),'snapshot_hash':session['snapshot'] if session else '', 'request_token':TOKEN,'bridge':{'last_seen':BRIDGE_LAST_SEEN,'latest_job':self.bridge_job()}})
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
                return CATALOG.playback.stream(self,path.rsplit('/',1)[-1],query.get('session',[''])[0],query.get('start',[0])[0])
            if path=='/api/catalog/video/state':
                return self.reply(CATALOG.playback.state(parse_qs(url.query).get('session',[''])[0]))
            if path=='/api/catalog/state':
                state=CATALOG.state()
                if PREVIEWS:state['previews']=PREVIEWS.archive_state(root['id'] for root in state['roots'])
                return self.reply(state)
            if path=='/api/catalog/tree':
                q=parse_qs(url.query)
                return self.reply(CATALOG.tree(q.get('q',[''])[0],q.get('media',[''])[0]))
            if path=='/api/catalog/items':
                q=parse_qs(url.query)
                data=CATALOG.items(q.get('root',[''])[0],q.get('folder',[''])[0],q.get('sku',[''])[0],q.get('offset',[0])[0],q.get('limit',[120])[0],q.get('cursor',[''])[0],q.get('q',[''])[0],q.get('media',[''])[0])
                if ARCHIVE:ARCHIVE.ratings.decorate(data['rows'])
                return self.reply(data)
            if path=='/api/catalog/skus':
                from sku_library import browse
                return self.reply({'rows':browse(SKU,parse_qs(url.query).get('q',[''])[0])['groups']})
            if path.startswith('/api/catalog/preview/'):
                self.preview_cache='private, max-age=31536000, immutable'
                params=parse_qs(url.query)
                payload=CATALOG.preview(path.rsplit('/',1)[-1],params.get('size',[420])[0],read=True,frame=params.get('frame',[None])[0])
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
                from sku_library import browse
                q=parse_qs(url.query)
                data=browse(SKU,q.get('q',[''])[0],q.get('sort',['name'])[0],q.get('root',[''])[0],q.get('folder',[''])[0],q.get('sku',[''])[0],q.get('offset',[0])[0],q.get('limit',[120])[0],bin_id=q.get('bin_id',[''])[0])
                if ARCHIVE:ARCHIVE.ratings.decorate(data['rows'])
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
            if path.startswith('/api/metadata/operations/'):return self.reply(ARCHIVE.operation(path.rsplit('/',1)[-1]))
            if path=='/api/sku':return self.reply(SKU.state() if SKU else {'project':None,'projects':[],'rows':[],'jobs':[]})
            if path=='/api/sku/gallery':
                if SKU is None:raise ValueError('SKU 服务未启动')
                from sku_presentation import gallery
                q=parse_qs(url.query)
                return self.reply(gallery(SKU,q.get('project_id',[''])[0],q.get('sku',[''])[0],q.get('offset',[0])[0],q.get('limit',[60])[0]))
            if path=='/api/sku/image':
                if SKU is None:raise ValueError('SKU 服务未启动')
                params=parse_qs(url.query)
                data,kind=SKU.image(params.get('row_id',[''])[0],int(params.get('index',['0'])[0]))
                return self.reply(data,kind=kind)
            if path=='/api/sku/download':
                if SKU is None:raise ValueError('SKU 服务未启动')
                pid=parse_qs(url.query).get('project_id',[None])[0];p=SKU.project(pid)
                if not p:raise ValueError('对账任务不存在')
                output=SKU.export(p['id'])
                return self.reply(output.read_bytes(),kind='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',filename=shooting_export_name(p['name']))
            if path=='/api/mouse':return self.reply(MOUSE.state(refresh=True) if MOUSE else {'active':False,'devices':[],'message':'鼠标输入服务未启动'})
            if path=='/api/mouse/options':return self.reply(MOUSE.state() if MOUSE else {'active':False,'options':{},'message':'鼠标输入服务未启动'})
            if path=='/api/folders':return self.reply(STORE.folder_state())
            if path=='/api/watch':
                if not WATCH:return self.reply({'enabled':False,'message':'监控服务未启动'})
                params=parse_qs(url.query)
                if 'after' in params:
                    return self.reply(WATCH.wait_state(int(params['after'][0]),max(0,min(15,float(params.get('wait',['15'])[0])))))
                return self.reply(WATCH.state())
            if path=='/api/health':
                with STORE.connect() as c:pending=c.execute("SELECT COUNT(*) FROM bridge_jobs WHERE status='pending'").fetchone()[0]
                return self.reply({'application':'framekeep','process_id':os.getpid(),'busy':any(j['status'] in ('queued','running','cancelling') for j in STORE.folder_state()['scans']) or bool(pending) or bool(STORAGE and STORAGE.job.get('status')=='running') or bool(REFERENCES and REFERENCES.storage.job.get('status')=='running')})
            if path=='/api/task-status':return self.reply(task_status.snapshot(STORE,SKU,ARCHIVE,CATALOG,ANALYSIS,RAWLINK,COMPONENTS,UPDATES,STORAGE))
            if path=='/api/preview-cache':return self.reply(STORAGE.state() if STORAGE else cache_for(DATA/'previews').state())
            if path=='/api/sku/metadata/sync':
                return self.reply({'sync':getattr(SKU,'metadata_sync',None).state() if getattr(SKU,'metadata_sync',None) else None})
            if path=='/api/canon':return self.reply(CANON.state() if CANON else {'enabled':False,'message':'Canon 接入服务未启动'})
            if path.startswith('/api/scan/'):
                job=STORE.jobs.get(path.rsplit('/',1)[-1]);return self.reply(job or {'error':'索引任务不存在'},200 if job else 404)
            if path.startswith('/api/runs/'):
                return self.reply({'error':'AI 选片已移除。'},410)
            if path.startswith('/api/preview/'):
                _,_,_,sid,cid=path.split('/');session=STORE.session(sid)
                with STORE.connect() as c:row=c.execute('SELECT data FROM captures WHERE session=? AND id=?',(sid,cid)).fetchone()
                if not session or not row:return self.reply({'error':'照片不存在'},404)
                try:
                    params=parse_qs(url.query);size=int(params.get('size',['480'])[0]);decode=params.get('decode',['0'])[0]=='1'
                    record=json.loads(row[0]);payload=media.preview_bytes(record,session['root'],DATA/'previews',lambda:media.find_exiftool(ROOT),size,decode)
                    if params.get('v',[''])[0]==record['fingerprint']:self.preview_cache='private, max-age=31536000, immutable'
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
            for job in STORE.folder_state()['scans']:
                if job['status'] in ('queued','running'):STORE.cancel_scan(job['id'])
            self.reply({'ok':True});threading.Thread(target=self.server.shutdown,daemon=True).start();return
        if path=='/api/desktop/canon':
            secret=os.environ.get('FRAMEKEEP_DESKTOP_TOKEN')
            if not secret or not secrets.compare_digest(self.headers.get('X-Framekeep-Desktop',''),secret):return self.reply({'error':'桌面实例不匹配'},403)
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=65536:raise ValueError('无效的照片通知')
                body=json.loads(self.rfile.read(length))
                if CANON is None:raise ValueError('Canon 接入服务未启动')
                return self.reply(CANON.receive(body.get('paths')))
            except (ValueError,OSError) as exc:return self.reply({'error':str(exc)},400)
        if bridge:
            if self.headers.get('X-Framekeep-Bridge')!=BRIDGE_TOKEN:return self.reply({'error':'桥接令牌无效'},403)
        elif self.headers.get('X-Framekeep-Token')!=TOKEN or self.headers.get('Origin',f'http://127.0.0.1:{self.server.server_port}')!=f'http://127.0.0.1:{self.server.server_port}':return self.reply({'error':'请从本机工作台发起操作'},403)
        try:
            length=int(self.headers.get('Content-Length','0'))
            maximum=280_000_000 if path=='/api/sku/metadata/import' else 30_000_000 if path=='/api/sku/import' else 16_000_000
            if not 0<length<=maximum:return self.reply({'error':'请求过大或为空'},400)
            body=json.loads(self.rfile.read(length))
            if path=='/api/reference/task':
                if REFERENCES is None:raise ValueError('参考资源服务未启动')
                project=SKU.project() if SKU else None
                return self.reply(REFERENCES.sync_task(STORE.active_id(),project.get('id') if project else None))
            if path.startswith('/api/reference/'):
                return self.reply(reference_api.local_post(REFERENCES,path.removeprefix('/api/reference/'),body))
            if path=='/api/video-preview':
                if CATALOG is None:raise ValueError('归档服务未启动')
                result=CATALOG.video.configure(body.get('frame_number'))
                if PREVIEWS:PREVIEWS.reconcile_archive(videos_only=True)
                return self.reply(result)
            if path=='/api/preview-cache':
                if STORAGE:return self.reply(STORAGE.configure(body.get('limit_gb')))
                result=cache_for(DATA/'previews').budget.configure(body.get('limit_gb'))
                return self.reply(STORAGE.state() if STORAGE else result)
            if path=='/api/preview-cache/manage':
                if STORAGE is None:raise ValueError('缓存管理服务未启动')
                return self.reply(STORAGE.start(body.get('action'),body.get('path','')))
            if path=='/api/canon':
                if CANON is None:raise ValueError('Canon 接入服务未启动')
                return self.reply(CANON.configure(body.get('enabled'),body.get('follow_eos',False)))
            if path=='/api/canon/destination':
                if CANON is None:raise ValueError('Canon 接入服务未启动')
                return self.reply(CANON.configure_destination(body.get('folder')))
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
            if path=='/api/catalog/add':return self.reply(REFERENCES.add_root(body['folder']) if REFERENCES else CATALOG.add_root(body['folder']))
            if path=='/api/catalog/video/stop':
                CATALOG.playback.stop(body.get('session',''));return self.reply({'ok':True})
            if path=='/api/catalog/scan':return self.reply(REFERENCES.rescan(body['root_id']) if REFERENCES else CATALOG.scan(body['root_id']))
            if path=='/api/catalog/metadata':
                if ARCHIVE is None:raise ValueError('元数据服务未启动')
                from photo_metadata import save
                return self.reply(save(ARCHIVE,CATALOG,body['item_id'],body.get('metadata'),body.get('token')))
            if path=='/api/catalog/assign':
                operation=None
                if ARCHIVE and CATALOG.item(body['item_id'])['media_type']=='image':
                    from photo_metadata import assign_sku
                    operation=assign_sku(ARCHIVE,CATALOG,body['item_id'],body['sku'])
                item=CATALOG.assign(body['item_id'],body['sku'])
                if operation:item['metadata_operation']=operation['operation']['id']
                return self.reply(item)
            if path=='/api/catalog/rating':
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
            if path=='/api/reveal-file':return self.reply(reveal_capture(STORE,body['session_id'],body['capture_id'],body.get('component')))
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
                    for service in (SKU, ANALYSIS, RAWLINK):
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
                elif path=='/api/parsing/control':
                    if body.get('action') not in ('cancel','retry'):raise ValueError('任务操作无效')
                    if channel=='ocr' and SKU:SKU.configure_tasks(action=body['action'],job_id=body['job_id'])
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
                return self.reply({'assets':[ARCHIVE.asset(ARCHIVE.ensure(body['session_id'],cid)) for cid in dict.fromkeys(ids)]})
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
                    return self.reply(select_images(SKU,body.get('project_id'),body.get('choices')))
                if path=='/api/sku/metadata/export':
                    from sku_metadata_backup import export_metadata
                    return self.reply(export_metadata(SKU))
                if path=='/api/sku/metadata/import':
                    from sku_metadata_backup import import_metadata
                    from sku_metadata_sync import attach
                    try:content=base64.b64decode(body['content_base64'],validate=True)
                    except Exception:raise ValueError('元数据文件内容无效') from None
                    imported=import_metadata(SKU,content,sync=attach(SKU,CATALOG,RAWLINK))
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
                if path=='/api/sku/activate':return self.reply(SKU.activate(body['project_id']))
                if path=='/api/sku/recognize':return self.reply(SKU.enqueue(body['session_id'],body['capture_id']))
                if path=='/api/sku/review':return self.reply(SKU.review(body['job_id'],body['action'],body.get('row_id')))
                if path=='/api/sku/export':
                    p=SKU.project(body.get('project_id'))
                    if not p:raise ValueError('对账任务不存在')
                    return self.reply({'path':str(SKU.export(p['id'])),'project_id':p['id'],'name':shooting_export_name(p['name'])})
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
                try:folder=filedialog.askdirectory(title='选择照片文件夹',parent=root)
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
        except Exception:return self.reply({'error':'本机操作失败，已有会话保持不变'},500)

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
    global STORE,MOUSE,WATCH,MODELS,SKU,ARCHIVE,ANALYSIS,RAWLINK,FILESEARCH,UPDATES,CATALOG,COMPONENTS,DRIVER,PREVIEWS,CANON,STORAGE,REFERENCES
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
    import gpu_media,gpu_video
    from video_preview import find_tool
    gpu_media.warmup()
    if find_tool('ffmpeg'):gpu_video.warmup(find_tool('ffmpeg'))
    STORE=Store(DATA,ROOT)
    CATALOG=AssetCatalog(DATA)
    STORAGE=CacheStorage(DATA,STORE,CATALOG)
    PREVIEWS=PreviewWarmer(cache_for(DATA/'previews'),lambda:media.find_exiftool(ROOT),catalog=CATALOG,autostart=False)
    STORE.on_previews=PREVIEWS.enqueue
    CATALOG.on_previews=PREVIEWS.enqueue
    MOUSE=MouseRemote(settings_path=DATA/'mouse-remote.json')
    WATCH=FolderWatch(STORE)
    MODELS=Providers(DATA,legacy=assistant_config.load_config)
    COMPONENTS=ComponentManager(DATA,seeds=[ROOT/'runtime/components',ROOT/'.runtime/component-seed',ROOT/'.runtime',ROOT.parent.parent/'.runtime'])
    DRIVER=DriverSetup(COMPONENTS,DATA,activator=MOUSE.activate_driver)
    local_inference.configure(ROOT,DATA,COMPONENTS)
    if MODELS.public().get('builtin_enabled'):local_inference.preload(MODELS.selected('ocr'))
    scheduler=VisionScheduler()
    ARCHIVE=AssetArchive(STORE,autostart=False)
    CATALOG.metadata.start()
    # 后台归档可能长时间持锁读取照片，须先完成共享评级初始化。
    for session in STORE.sessions():ARCHIVE.ratings.seed_session(session['id'])
    FILESEARCH=EverythingSearch(ARCHIVE)
    STORE.discovery=FILESEARCH
    CATALOG.discovery=FILESEARCH
    REFERENCES=ReferenceLibrary(DATA,FILESEARCH,PREVIEWS,catalog=CATALOG)
    PREVIEWS.reconcile_archive()
    SKU=SkuService(STORE,MODELS,archive=ARCHIVE,scheduler=scheduler,require_sku_choice=True)
    ARCHIVE.sku=SKU
    ANALYSIS=AssetAnalysis(ARCHIVE,MODELS,scheduler)
    RAWLINK=RawReconcile(ARCHIVE)
    UPDATES=UpdateClient(ROOT,DATA,args.port)
    def catalog_changed(sid):
        ARCHIVE.ratings.seed_session(sid)
        RAWLINK.request(sid,discover=True);ANALYSIS.request_auto(sid);PIPELINE.changed(sid)
    PIPELINE=ArchivePipeline(CATALOG,ARCHIVE,SKU,RAWLINK,autostart=False)
    from sku_metadata_sync import attach
    attach(SKU,CATALOG,RAWLINK)
    STORE.on_catalog=catalog_changed
    STORE.on_external_metadata=ARCHIVE.ratings.observe_sidecars
    ARCHIVE.thread.start()
    ARCHIVE.ratings.sync.thread.start()
    PIPELINE.thread.start()
    CANON=CanonLink(STORE,WATCH,os.environ.get('FRAMEKEEP_DESKTOP_EXE',str(ROOT/'末帧选片.exe')),
                    allow_registration=bool(os.environ.get('FRAMEKEEP_DESKTOP_PID')) and os.environ.get('FRAMEKEEP_CANON_REGISTRATION','1')!='0')
    watch_desktop_parent(server)
    if args.folder:STORE.scan(args.folder)
    # 完成评级恢复等初始化后再全速预热，避免 28 路缓存写入争抢启动磁盘。
    PREVIEWS.start()
    url=f'http://127.0.0.1:{args.port}/';print('末帧工作台：'+url,flush=True)
    if not args.no_browser and (args.browser or getattr(sys,'frozen',False)):
        import webbrowser
        webbrowser.open(url)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        REFERENCES.close()
        if getattr(SKU,'metadata_sync',None):SKU.metadata_sync.close()
        CANON.close()
        PIPELINE.close()
        RAWLINK.close()
        ANALYSIS.close()
        SKU.close()
        local_inference.close()
        COMPONENTS.close()
        ARCHIVE.close()
        WATCH.close()
        STORAGE.close()
        PREVIEWS.close()
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
