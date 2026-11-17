"""使用指定照片目录只读验收现场内存小图；业务资料始终写入隔离临时目录。"""
import argparse
from collections import Counter
from contextlib import ExitStack
from datetime import datetime,timedelta
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit,parse_qsl,urlencode

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'选片助手'))
from catalog_store import Store,stable_json
from folder_watch import FolderWatch
from playwright.sync_api import sync_playwright
import server


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('folder',type=Path);parser.add_argument('--output',type=Path,default=ROOT/'build/continuous-scroll-real')
    args=parser.parse_args();source=args.folder.resolve(strict=True);out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
    def manifest():
        result={}
        def visit(folder):
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.is_symlink():continue
                    if entry.is_dir(follow_symlinks=False):visit(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        stat=entry.stat(follow_symlinks=False);result[str(Path(entry.path).relative_to(source))]=(stat.st_size,stat.st_mtime_ns)
        visit(source);return result
    before=manifest();names=sorted([name for name in before if Path(name).suffix.lower() in ('.jpg','.jpeg')],key=str.casefold)
    assert names,'目录没有JPEG照片';print(json.dumps({'stage':'只读清点完成','photos':len(names),'source_files':len(before)},ensure_ascii=False),flush=True)
    # 文件夹只读：不运行监控线程、不读取或改写源XMP，不调用评级或导出。
    stamp=datetime(2026,11,4,0);records=[]
    for index,name in enumerate(names):
        size,mtime=before[name];records.append({'id':hashlib.sha256(name.encode()).hexdigest()[:24],'name':Path(name).name,'jpg':Path(name).as_posix(),'raw':None,'missingTime':False,'time':(stamp+timedelta(seconds=index)).isoformat(),'camera':'acceptance','cameraKey':'acceptance','existingRating':0,'fileCount':1,'fingerprint':f'{size}-{mtime}'})
    checks=[];errors=[];failed=[];reads=[];start=time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='isolated-',dir=out) as folder,ExitStack() as stack:
        store=Store(Path(folder)/'data',ROOT);sid=store.ensure_live_session(source);store.activate(sid)
        with store.connect() as db:db.executemany('INSERT INTO captures VALUES(?,?,?,?)',[(sid,r['id'],stable_json(r),i) for i,r in enumerate(records)])
        watch=FolderWatch(store,autostart=False);watch.start(str(source));watch.info['initializing']=False
        values=dict(STORE=store,DATA=store.data,MODELS=None,ARCHIVE=None,CATALOG=None,ANALYSIS=None,SKU=None,WATCH=watch,MOUSE=None,CANON=None,RAWLINK=None,FILESEARCH=None,REFERENCES=None,PREVIEWS=None,DIRECT=None)
        for name,value in values.items():stack.enter_context(patch.object(server,name,value))
        stack.enter_context(patch.object(server,'workbench_views',return_value=None))
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start()
        stack.callback(http.server_close);stack.callback(http.shutdown)
        with sync_playwright() as pw,pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True) as browser:
            page=browser.new_page(viewport={'width':1680,'height':1050});page.set_default_timeout(60000)
            page.on('pageerror',lambda error:errors.append(str(error)))
            def thumbnail(url):
                parts=urlsplit(url);query=dict(parse_qsl(parts.query))
                if not parts.path.startswith('/api/preview/') or int(query.get('size','2048'))>600:return None
                query.pop('instant',None);query.pop('retry',None);return parts.path+'?'+urlencode(sorted(query.items()))
            page.on('request',lambda request:reads.append(thumbnail(request.url)) if thumbnail(request.url) else None)
            page.on('response',lambda response:failed.append({'url':response.url,'status':response.status}) if thumbnail(response.url) and response.status>=400 else None)
            def save(stage):
                result={'stage':stage,'source':str(source),'photos':len(records),'elapsed_seconds':round(time.perf_counter()-start,3),'reads':len(reads),'unique_reads':len(set(reads)),'duplicate_reads':{k:v for k,v in Counter(reads).items() if v>1},'stats':page.evaluate('FramekeepThumbnailMemory.stats'),'checks':checks,'page_errors':errors,'failed_thumbnails':failed}
                (out/'progress.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8');return result
            def settle(selector):
                page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                page.wait_for_function('''selector=>{const images=[...document.querySelectorAll(selector)].filter(i=>{const r=i.getBoundingClientRect(),b=i.closest('#catalog-view,#watch-thumbnails').getBoundingClientRect();return r.width>0&&r.bottom>b.top&&r.top<b.bottom});return images.length>0&&images.every(i=>i.complete&&i.naturalWidth)&&!FramekeepThumbnailMemory.stats.active&&!FramekeepThumbnailMemory.stats.queued}''',arg=selector)
            def scroll(container,top,images):page.locator(container).evaluate('(el,top)=>el.scrollTop=top',top);settle(images)
            def sweep(container,images):
                scroll(container,0,images)
                for step in range(max(1000,len(records)*2)):
                    geometry=page.locator(container).evaluate('(el)=>({top:el.scrollTop,maximum:el.scrollHeight-el.clientHeight,step:Math.max(30,el.clientHeight*.8)})')
                    if geometry['top']>=geometry['maximum']-1:break
                    scroll(container,min(geometry['maximum'],geometry['top']+geometry['step']),images)
                    if step%100==0:
                        save('连续滚动 '+container);print(json.dumps({'stage':container,'scroll_step':step,'reads':len(reads)},ensure_ascii=False),flush=True)
                else:raise AssertionError('滚动未到末尾')
            try:
                page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>FramekeepBoot.loading===false&&FramekeepLayout.isMonitoring()')
                assert page.evaluate('DEMO_PHOTOS.length')==len(records)
                assert page.locator('.session-batchbar,[data-batch-next],[data-batch-jump]').count()==0
                target=page.evaluate('DEMO_PHOTOS[Math.floor(DEMO_PHOTOS.length*.73)].id');page.evaluate('(id)=>Framekeep.browsePhoto(id)',target);settle('#watch-thumbnails img')
                for view in ('grid','list','single','grid'):
                    page.locator(f'[data-view="{view}"]').click()
                    if view=='single':continue
                    container='#photo-grid' if view=='grid' else '#photo-list'
                    page.wait_for_function('''({container,target})=>{const el=document.querySelector(container+' .focused'),b=document.getElementById('catalog-view').getBoundingClientRect();if(!el||el.dataset.photo!==target)return false;const r=el.getBoundingClientRect();return r.top>=b.top-1&&r.bottom<=b.bottom+1}''',arg={'container':container,'target':target})
                sweep('#catalog-view','#photo-grid img');count=len(reads)
                assert count==len(records),(count,len(records))
                for view,container,images in [('grid','#catalog-view','#photo-grid img'),('list','#catalog-view','#photo-list img'),('sidebar','#watch-thumbnails','#watch-thumbnails img')]:
                    page.locator(f'[data-view="{view if view!="sidebar" else "single"}"]').click()
                    if view=='list':sweep(container,images)
                    else:
                        for fraction in (0,1,.5,.25,.75,0,1):
                            maximum=page.locator(container).evaluate('(el)=>el.scrollHeight-el.clientHeight');scroll(container,maximum*fraction,images)
                    assert len(reads)==count,(view,'回访重复读取小图',count,len(reads))
                    checks.append({'view':view,'photos':page.evaluate('DEMO_PHOTOS.length'),'reads_before_revisits':count,'reads_after_revisits':len(reads),'visible_nodes':page.locator(images).count()})
                    print(json.dumps(checks[-1],ensure_ascii=False),flush=True);save('连续滚动回访')
                for fraction in (0,.5,.999):
                    index=min(len(records)-1,int(len(records)*fraction));target=records[index]['id']
                    page.evaluate('(id)=>Framekeep.browsePhoto(id)',target);page.locator('[data-view="grid"]').click();settle('#photo-grid img')
                    assert len(reads)==count,('全目录定位回访重复读取',index)
                assert page.locator('#photo-grid img').count()<100
                page.screenshot(path=str(out/'verified.png'));result=save('完成')
                assert not errors,errors;assert not failed,failed;assert len(reads)==len(set(reads)),result['duplicate_reads']
                assert len(set(reads))==len(records),(len(set(reads)),len(records))
                assert not list(store.data.rglob('*.webp')),'不应写入持久缩略图'
                after=manifest();assert before==after,'源目录内容或文件时间发生变化'
                result.update(source_manifest_unchanged=True,source_files_checked=len(before),continuous_revisits_added_reads=0,no_pagination_controls=True)
                (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
                print(json.dumps({k:v for k,v in result.items() if k not in ('checks','duplicate_reads')},ensure_ascii=False),flush=True)
            except BaseException:
                save('失败');page.screenshot(path=str(out/'failure.png'));raise


if __name__=='__main__':main()
