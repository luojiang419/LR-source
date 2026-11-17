from pathlib import Path
import json,os,shutil,socket,subprocess,sys,time,urllib.request,sqlite3
from PIL import Image
from playwright.sync_api import sync_playwright

base=Path(__file__).resolve().parent;source=base/'source'
sys.path.insert(0,str(source/'选片助手'))
from catalog_store import Store
from model_providers import Providers
from sku_service import SkuService
import media_engine as media

out=source/'build/frozen-variants-final';out.mkdir()
photos=out/'photos';photos.mkdir();data=out/'data'
store=Store(data,source);sid=store.ensure_live_session(photos);store.activate(sid);ids=[]
for index in range(4):
    photo=photos/f'{index}.jpg'
    image=Image.open(base/'cpu-label.jpg') if index%2 else Image.new('RGB',(900,600),('red' if index==0 else 'blue'))
    exif=Image.Exif();exif[36867]=f'2026:11:09 10:00:0{index}'
    image.save(photo,quality=98,exif=exif)
    record=media.capture_record(photos,photo,None,{'DateTimeOriginal':exif[36867]})
    store.append_live(sid,[record]);ids.append(record['id'])
session=store.session(sid);store.save(sid,session['revision'],{'overrides':{ids[1]:2,ids[3]:2}})
providers=Providers(data);providers.set_builtin(True)
sku=SkuService(store,providers,autostart=False,require_sku_choice=True)
try:
    sku.configure_tasks(mode='manual');sku.live_arrivals(sid,store.records(sid))
    sku.set_sku_rules([{'kind':'contains','value':'LC'}]);pid=sku.project()['id']
finally:sku.close()
installed=source/'build/signed-qa/installed';engine=installed/'engine/framekeep-engine.exe'
with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
env=os.environ.copy();env.update(FRAMEKEEP_DATA=str(data),FRAMEKEEP_CANON_REGISTRATION='0',
    FRAMEKEEP_LOCAL_MODELS=str(source/'build/signed-qa/ocr-new/data/components/local-models.json'),
    FRAMEKEEP_EXIFTOOL=str(media.find_exiftool(source)))
process=subprocess.Popen([str(engine),'--port',str(port),'--no-browser'],env=env,creationflags=subprocess.CREATE_NO_WINDOW)
url=f'http://127.0.0.1:{port}';token='';report={}
def api(path,body=None):
    request=urllib.request.Request(url+path,data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type':'application/json','X-Framekeep-Token':token})
    with urllib.request.urlopen(request,timeout=30) as response:return json.load(response)
try:
    deadline=time.monotonic()+45
    while True:
        try:report['health']=api('/api/health');break
        except OSError:
            if process.poll() is not None or time.monotonic()>deadline:raise
            time.sleep(.25)
    token=api('/api/state')['request_token']
    deadline=time.monotonic()+60
    while True:
        state=api('/api/sku');shot=[r for r in state['rows'] if r['status']=='已拍摄']
        if len(shot)==2:break
        assert time.monotonic()<deadline,state['jobs'];time.sleep(.3)
    codes={r['sku'] for r in shot};assert codes=={'LC7832773-P805','LC7832773-P805-A'},codes
    assert {r['completion']['capture_id'] for r in shot}=={ids[1],ids[3]}
    report['separate_skus']=sorted(codes);report['actual_cpu_auto_labels']=True
    with sync_playwright() as pw:
        browser=pw.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page(viewport={'width':1500,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading')
            page.locator('#sku-open').click();page.locator('[data-all-view]').click()
            page.wait_for_function('()=>document.querySelectorAll("#sku-rows [data-edit-sku]").length===2')
            assert '正在更新' not in page.locator('.app-header').inner_text()
            page.screenshot(path=str(out/'separate-frozen.png'))
            assert errors==[],errors
            report['browser_errors']=errors;report['header_no_refresh_notice']=True
        finally:browser.close()
    variant=next(r for r in shot if r['sku'].endswith('-A'))
    api('/api/sku/row/rename',{'project_id':pid,'row_id':variant['id'],'old_sku':variant['sku'],'sku':'LC7832773-P805-BLUE'})
    deadline=time.monotonic()+40
    while True:
        with sqlite3.connect(data/'asset_archive.sqlite3') as c:
            pending=c.execute("SELECT count(*) FROM metadata_jobs WHERE status IN ('queued','running','writing')").fetchone()[0]
            failed=c.execute("SELECT count(*) FROM metadata_jobs WHERE status='failed'").fetchone()[0]
        if not pending:break
        assert time.monotonic()<deadline;time.sleep(.25)
    assert failed==0
    from metadata_writer import MetadataWriter
    writer=MetadataWriter(source,data)
    try:
        assigned=[writer.read(photos/f'{i}.jpg')['XMP-fk:SKU'] for i in range(4)]
    finally:writer.close()
    assert assigned==['LC7832773-P805']*2+['LC7832773-P805-BLUE']*2,assigned
    report['actual_file_skus']=assigned;report['ok']=True
finally:
    process.terminate();process.wait(timeout=20);report['process_stopped']=process.poll() is not None
    (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps(report,ensure_ascii=False),flush=True)
