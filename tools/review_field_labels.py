"""依据已实际查看的标签核对表，在真实拍摄进度界面完成余项；不改写OCR结果。"""
from pathlib import Path
import json,sqlite3,sys,time,re
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1];out=(Path(sys.argv[2]).resolve() if len(sys.argv)>2 else ROOT/'build/field-trial/full');data=out/'data'
url=sys.argv[1];review=json.loads((out/'visual-review.json').read_text(encoding='utf8'))['labels']
with sqlite3.connect(data/'sku.sqlite3',timeout=30) as c:
    choices=dict(c.execute('SELECT capture_id,sku FROM ocr_choices'))
    pending=c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running','waiting_model')").fetchone()[0]
if pending:raise RuntimeError(f'仍有{pending}条识别请求；请待真实OCR完成再进行人工核对')
corrections=[{**r,'before':choices.get(r['capture_id'])} for r in review if choices.get(r['capture_id'],'').upper()!=r['visual_sku'].upper()]
report={'basis':'人工视觉核对后通过拍摄进度的核对/填写款号操作；保留原OCR文字','before_choices':choices,'corrections':corrections,'started':time.time(),'errors':[]}
def save():(out/'manual-review.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
save()
with sync_playwright() as pw:
    browser=pw.chromium.launch(channel='msedge',headless=True);page=browser.new_page(viewport={'width':1680,'height':1050});page.on('pageerror',lambda e:report['errors'].append(str(e)))
    page.goto(url);page.wait_for_function('()=>window.FramekeepBoot&&!FramekeepBoot.loading',timeout=60000);page.locator('#sku-open').click()
    page.wait_for_selector('#sku-jobs [data-sku-edit-capture]',state='attached',timeout=60000)
    toggle=page.locator('#progress-sidebar-toggle')
    if toggle.count() and toggle.get_attribute('aria-expanded')=='false':toggle.click()
    if not page.locator('#sku-records').evaluate('(e)=>e.open'):page.locator('#sku-records > summary').click()
    page.screenshot(path=str(out/'before-manual-review.png'))
    for item in corrections:
        cid=item['capture_id'];start=time.time()
        normal=lambda value:re.sub('[^A-Z0-9]','',(value or '').upper())
        if item['before'] and normal(item['before'])==normal(item['visual_sku']):
            state=page.evaluate('()=>FramekeepIO.api("/api/sku")')
            row=next(r for r in state['rows'] if (r.get('completion') or {}).get('capture_id')==cid)
            page.locator(f'[data-edit-sku="{row["id"]}"]').dblclick()
            editor=page.locator('.sku-code-input');editor.fill(item['visual_sku'])
            with page.expect_response(lambda r:r.url.endswith('/api/sku/row/rename') and r.request.method=='POST',timeout=90000) as response:editor.press('Enter')
            if not response.value.ok:raise RuntimeError(response.value.text())
            item['method']='table_rename';item['confirmed_at']=time.time();item['seconds']=item['confirmed_at']-start;save();print(f"格式修正 {item['index']} {item['visual_sku']} {item['seconds']:.3f}s",flush=True);continue
        page.locator(f'[data-sku-edit-capture="{cid}"]').first.click(timeout=30000)
        page.wait_for_selector('#ocr-metadata-dialog[open]',timeout=15000)
        page.wait_for_function('()=>!document.querySelector("#ocr-metadata-save").disabled',timeout=30000)
        page.locator('#ocr-metadata-sku').fill(item['visual_sku'])
        page.locator('#ocr-metadata-save').click()
        page.wait_for_selector('#ocr-metadata-dialog[open]',state='hidden',timeout=90000)
        item['confirmed_at']=time.time();item['seconds']=item['confirmed_at']-start;save()
        print(f"人工核对 {item['index']} {item['label']} {item['before']} -> {item['visual_sku']} {item['seconds']:.3f}s",flush=True)
    page.screenshot(path=str(out/'after-manual-review.png'));report['finished']=time.time();save();browser.close()
