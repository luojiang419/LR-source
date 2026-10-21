"""用真实 RAW/JPG 的隔离副本验收现场评级到最终归档写入的完整界面链路。"""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

from PIL import Image
from playwright.sync_api import sync_playwright
from metadata_writer import MetadataWriter,raw_integrity,sha

ROOT=Path(__file__).resolve().parents[1]

def verify(samples,output,engine=None):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    samples=Path(samples).resolve();raws=[p for p in sorted(samples.glob('*.CR3')) if p.with_suffix('.JPG').is_file()][:10]
    if len(raws)!=10:raise ValueError('需要至少 10 对 CR3/JPG；仅在新建输出目录内测试')
    onsite=output/'现场JPG';onsite.mkdir();destination=output/'最终归档';card=destination/'存储卡'/'DCIM';card.mkdir(parents=True)
    baseline={str(p):sha(p) for raw in raws for p in (raw,raw.with_suffix('.JPG'))}
    for raw in raws:shutil.copy2(raw.with_suffix('.JPG'),onsite/(raw.stem+'.JPG'))
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    url=f'http://127.0.0.1:{port}';secret=secrets.token_urlsafe(24)
    env={k:v for k,v in os.environ.items() if not k.startswith('FRAMEKEEP_')}
    env.update(FRAMEKEEP_DATA=str(output/'data'),FRAMEKEEP_CONFIG=str(output/'no-config.md'),FRAMEKEEP_DESKTOP_TOKEN=secret)
    if engine:env['FRAMEKEEP_APP_ROOT']=str(ROOT/'dist/末帧选片')
    command=[str(Path(engine).resolve())] if engine else [sys.executable,'-X','utf8',str(ROOT/'选片助手/server.py')]
    log=(output/'engine.log').open('w',encoding='utf-8');process=None
    def start():
        nonlocal process
        process=subprocess.Popen(command+['--port',str(port),'--no-browser'],env=env,cwd=ROOT,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            try:
                with urllib.request.urlopen(url+'/api/health',timeout=1) as response:json.load(response)
                return
            except OSError:time.sleep(.1)
        raise AssertionError('引擎启动失败，见 engine.log')
    def stop():
        if process and process.poll() is None:
            request=urllib.request.Request(url+'/api/desktop/shutdown',data=b'{}',headers={'X-Framekeep-Desktop':secret},method='POST')
            with urllib.request.urlopen(request,timeout=5) as response:response.read()
            process.wait(timeout=20)
    def wait(fn,seconds=90):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            value=fn()
            if value:return value
            time.sleep(.15)
        raise AssertionError('验收等待超时')
    page=None;browser=None;progress=[];errors=[]
    try:
        start()
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            page=browser.new_page(viewport={'width':1680,'height':1050},reduced_motion='reduce');page.on('pageerror',lambda error:errors.append(str(error)))
            def api(path,body=None):
                return page.evaluate('async ([path,body])=>FramekeepIO.api(path,body===null?undefined:body)',[path,body])
            def wait_ui(expression,**kwargs):
                try:return page.wait_for_function(expression,**kwargs)
                except BaseException:
                    page.screenshot(path=str(output/'failure.png'))
                    debug={'message':page.locator('#analysis-message').inner_text(),'errors':errors,'workspace':page.evaluate('FramekeepWorkspace.current()')}
                    (output/'failure.json').write_text(json.dumps(debug,ensure_ascii=False,indent=2),encoding='utf-8')
                    print(json.dumps(debug,ensure_ascii=False),flush=True)
                    raise
            page.goto(url,wait_until='networkidle')
            page.locator('#open-folder').click();page.locator('#folder-path').fill(str(onsite));page.locator('#scan-folder').click()
            page.wait_for_selector('#folder-list [data-name="现场JPG"]');page.locator('#folder-list [data-name="现场JPG"]').click()
            wait_ui('()=>FRAMEKEEP_SESSION.root.includes("现场JPG") && DEMO_PHOTOS.length===10')
            sid=page.evaluate('FRAMEKEEP_SESSION.id')
            page.locator('#asset-analysis-open').click();wait_ui('()=>document.getElementById("analysis-stats").textContent.includes("并发")');page.locator('#parsing-ocr-mode').select_option('manual');wait_ui('()=>document.getElementById("analysis-message").textContent.includes("已保存")');page.locator('#analysis-close').click()
            page.locator('#current-shooting-tab').click();page.locator('#shooting-manual-type').fill('细节图');page.locator('#photo-settings-tab').click()
            ratings=[0,1,2,3,4,5,0,3,4,5];expected={}
            for raw,rating in zip(raws,ratings):
                rows=page.evaluate('DEMO_PHOTOS.map(p=>({id:p.id,jpg:p.jpg}))');cid=next(r['id'] for r in rows if r['jpg']==raw.stem+'.JPG')
                page.locator(f'#photo-grid [data-photo="{cid}"] [data-focus]').click()
                page.locator(f'#stars [data-rating="{rating}"]' if rating else '#clear-rating').click()
                expected[raw.stem]={'rating':rating,'sku':'验证-A' if len(expected)<5 else '验证-B'}
            page.evaluate('()=>FramekeepIO.flush()')
            for first,last,sku in [(0,4,'验证-A'),(5,9,'验证-B')]:
                ids=page.evaluate('DEMO_PHOTOS.map(p=>p.id)');page.locator(f'#photo-grid [data-photo="{ids[first]}"] [data-focus]').click();page.locator(f'#photo-grid [data-photo="{ids[last]}"] [data-focus]').click(modifiers=['Shift'])
                assert page.locator('#asset-count').inner_text()=='5 张'
                page.locator('#asset-sku').fill(sku);page.locator('#asset-preview').click();page.wait_for_selector('#asset-preview-dialog[open]');page.locator('#asset-apply').click()
                wait_ui('()=>document.getElementById("asset-message").textContent.includes("已内嵌 5")')
                def source_written():
                    jobs=[j for op in api('/api/metadata/operations')['operations'] for j in api('/api/metadata/operations/'+op['id'])['jobs']]
                    assert not any(j['status']=='failed' for j in jobs),jobs
                    return len(jobs)==last+1 and all(j['status']=='applied' for j in jobs)
                wait(source_written)
            # 删除测试现场 JPG，重启后仅凭保留的现场记录匹配最终存档。
            for raw in raws:
                for original in (raw,raw.with_suffix('.JPG')):shutil.copy2(original,card/original.name)
            saved_onsite=output/'已备案现场副本';shutil.copytree(onsite,saved_onsite)
            onsite.resolve().relative_to(output)
            shutil.rmtree(onsite);stop();start();page.reload(wait_until='networkidle')
            page.once('dialog',lambda d:d.accept(str(destination)));page.locator('#raw-sync-browse').click();wait_ui('()=>!document.getElementById("raw-sync-start").disabled')
            page.locator('#raw-sync-start').click();wait_ui('()=>document.getElementById("asset-analysis-open").getAttribute("aria-selected")==="true"')
            deadline=time.monotonic()+150;captured=False
            while time.monotonic()<deadline:
                batches=api('/api/parsing')['metadata']['batches'];batch=batches[0];progress.append({k:batch[k] for k in ('status','total','finished','counts')})
                if batch['counts'].get('running') and batch['finished']>0 and not captured and page.locator('#metadata-files [data-metadata-status="running"]').count() and int(page.locator('#metadata-progress').get_attribute('aria-valuenow'))>0:
                    page.emulate_media(reduced_motion='no-preference')
                    position=page.locator('#metadata-progress-fill').evaluate('(e)=>getComputedStyle(e).backgroundPosition')
                    page.wait_for_timeout(200)
                    assert page.locator('#metadata-progress-fill').evaluate('(e)=>getComputedStyle(e).animationName')=='metadata-progress-flow'
                    assert page.locator('#metadata-progress-fill').evaluate('(e)=>getComputedStyle(e).backgroundPosition')!=position
                    assert page.locator('#metadata-files [data-metadata-status]').first.get_attribute('data-metadata-status')=='running'
                    page.screenshot(path=str(output/'writing.png'));page.emulate_media(reduced_motion='reduce');captured=True
                if batch['status'] in ('completed','attention','failed'):break
                page.wait_for_timeout(250)
            assert batch['status']=='completed',batch
            assert batch['counts']=={'applied':20},batch
            assert batch['verification']['status']=='completed' and batch['verification']['valid']==20,batch
            assert captured
            wait_ui('()=>document.querySelectorAll("#metadata-files [data-metadata-status=applied]").length===10')
            wait_ui('()=>document.getElementById("metadata-percent").textContent==="100%"')
            page.screenshot(path=str(output/'completed.png'))
            for theme in ('dark','light','pure-white','hardcore'):
                page.evaluate('(t)=>FramekeepTheme.set(t)',theme)
                for width in (1680,1024,375,320):
                    page.set_viewport_size({'width':width,'height':1050});assert not page.evaluate('document.documentElement.scrollWidth>innerWidth'),(theme,width)
            page.set_viewport_size({'width':1680,'height':1050});page.evaluate('FramekeepTheme.set("dark")')
            writer=MetadataWriter(ROOT,output/'data');verified=[]
            for raw in raws:
                for ext in ('.CR3','.JPG'):
                    target=card/(raw.stem+ext);fields=writer.read(target);want=expected[raw.stem]
                    assert fields['XMP-xmp:Rating']==want['rating'],(target,fields,want)
                    assert fields['XMP-fk:SKU']==want['sku'],(target,fields,want)
                    assert fields['XMP-fk:ShootType']=='细节图'
                    if ext=='.CR3':assert raw_integrity(raw)==raw_integrity(target)
                    else:
                        with Image.open(raw.with_suffix('.JPG')) as a,Image.open(target) as b:assert a.convert('RGB').tobytes()==b.convert('RGB').tobytes()
                    verified.append({'file':target.name,**want,'hash':sha(target)})
            assert any(p['counts'].get('running') for p in progress),progress
            assert any(0<p['finished']<20 for p in progress),progress
            # 程序重启后保持进度，重复写入仍全部完成且不改文件。
            stop();start();page.reload(wait_until='networkidle');page.locator('#asset-analysis-open').click()
            wait_ui('()=>document.querySelectorAll("#metadata-files [data-metadata-status=applied]").length===10')
            page.locator('#analysis-close').click();page.locator('#raw-sync-start').click()
            wait_ui('()=>FramekeepWorkspace.current()==="analysis"')
            wait(lambda:api('/api/parsing')['metadata']['batches'][0]['status']=='completed')
            assert [sha(card/f['file']) for f in verified]==[f['hash'] for f in verified]
            # 恢复测试现场副本，在现场改星和改 SKU；不点击归档写入，目的地应自动跟随。
            shutil.copytree(saved_onsite,onsite);page.locator('#analysis-close').click()
            ids=page.evaluate('DEMO_PHOTOS.map(p=>p.id)');page.locator(f'#photo-grid [data-photo="{ids[0]}"] [data-focus]').click();page.locator('#stars [data-rating="4"]').click();page.evaluate('()=>FramekeepIO.flush()')
            def auto_finished(rating,sku):
                batch=api('/api/parsing')['metadata']['batches'][0]
                row=next((f for f in batch['files'] if f['name'].endswith(raws[0].stem+'.CR3')),None)
                return batch['status']=='completed' and row and row['rating']==rating and row['sku']==sku and batch['verification'].get('status')=='completed'
            wait(lambda:auto_finished(4,'验证-A'))
            for extension in ('.JPG','.CR3'):assert writer.read(card/(raws[0].stem+extension))['XMP-xmp:Rating']==4
            expected[raws[0].stem]['rating']=4
            page.locator('#app-settings-open').click();page.locator('#asset-overwrite').check();page.locator('#app-settings-close').click()
            page.locator(f'#photo-grid [data-photo="{ids[0]}"] [data-focus]').click();page.locator(f'#photo-grid [data-photo="{ids[4]}"] [data-focus]').click(modifiers=['Shift']);page.locator('#asset-sku').fill('验证-A2');page.locator('#asset-preview').click();page.wait_for_selector('#asset-preview-dialog[open]');page.locator('#asset-apply').click();wait_ui('()=>!document.getElementById("asset-preview-dialog").open')
            wait(lambda:auto_finished(4,'验证-A2'))
            for raw in raws[:5]:
                expected[raw.stem]['sku']='验证-A2'
                for extension in ('.JPG','.CR3'):assert writer.read(card/(raw.stem+extension))['XMP-fk:SKU']=='验证-A2'
            page.locator('#asset-analysis-open').click()
            # 仅在本地测试副本制造 JPG 星级冲突，从进度页查看原因并明确确认后恢复。
            conflict=card/(raws[0].stem+'.JPG');writer.run('-overwrite_original','-XMP-xmp:Rating=5',conflict)
            page.locator('#analysis-close').click();page.locator('#raw-sync-start').click()
            wait_ui('()=>FramekeepWorkspace.current()==="analysis"')
            wait(lambda:api('/api/parsing')['metadata']['batches'][0]['status']=='attention')
            wait_ui('()=>!document.getElementById("metadata-review").hidden');page.screenshot(path=str(output/'conflict.png'));page.locator('#metadata-review').click()
            page.wait_for_selector('#raw-sync-dialog[open] .raw-pair')
            card_ui=page.locator('.raw-pair').filter(has=page.locator('[data-raw-review]')).first
            card_ui.locator('[data-raw-source]').select_option(index=1);card_ui.locator('[data-raw-overwrite]').check();card_ui.locator('[data-raw-review]').click();page.locator('#raw-sync-close').click()
            wait(lambda:api('/api/parsing')['metadata']['batches'][0]['counts'].get('applied')==20)
            assert writer.read(conflict)['XMP-xmp:Rating']==expected[raws[0].stem]['rating']
            wait(lambda:api('/api/parsing')['metadata']['batches'][0]['status']=='completed')
            # 模拟漏拷一张 JPG，必须在最终校验中列出缺少组件；补回后重新校验通过。
            missing=card/(raws[-1].stem+'.JPG');holding=output/'暂存漏拷JPG';missing.rename(holding)
            page.locator('#analysis-close').click();page.locator('#raw-sync-start').click();wait_ui('()=>FramekeepWorkspace.current()==="analysis"')
            wait(lambda:api('/api/parsing')['metadata']['batches'][0]['status']=='attention')
            audit=api('/api/parsing')['metadata']['batches'][0]['verification'];assert any(raws[-1].stem in s for s in audit['missing_components']),audit
            wait_ui('()=>document.getElementById("metadata-verification").textContent.includes("缺少组件")');page.screenshot(path=str(output/'missing-component.png'))
            holding.rename(missing);page.locator('#analysis-close').click();page.locator('#raw-sync-start').click();wait_ui('()=>FramekeepWorkspace.current()==="analysis"');wait(lambda:api('/api/parsing')['metadata']['batches'][0]['status']=='completed')
            wait_ui('()=>document.getElementById("metadata-percent").textContent==="100%"');page.screenshot(path=str(output/'final-verified.png'))
            assert all(sha(path)==digest for path,digest in baseline.items())
            assert not errors,errors
            for f in verified:f.update(expected[Path(f['file']).stem],hash=sha(card/f['file']))
            result={'captures':10,'files':20,'offline_source':True,'ui_rating_and_sku':True,'running_and_partial_progress':True,'percentage_animation':True,'active_file_first':True,'auto_rating_and_sku':True,'final_audit':True,'missing_component_detected':True,'restart_progress':True,'idempotent':True,'conflict_ui_recovery':True,'themes_and_widths':16,'raw_integrity':True,'jpg_pixels':True,'source_unchanged':True,'page_errors':errors,'verified':verified}
            (output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');(output/'progress.json').write_text(json.dumps(progress,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({k:v for k,v in result.items() if k!='verified'},ensure_ascii=False))
            browser.close();browser=None
    except BaseException:
        if page:
            try:page.screenshot(path=str(output/'failure.png'))
            except Exception:pass
        raise
    finally:
        if browser:
            try:browser.close()
            except Exception:pass
        stop();log.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--samples',required=True);parser.add_argument('--output',required=True);parser.add_argument('--engine');args=parser.parse_args()
    verify(args.samples,args.output,args.engine)
