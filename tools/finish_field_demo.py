"""在真实引擎页面核验最终界面、下载实际软件导出的拍摄记录，并回读Windows属性。"""
from pathlib import Path
import json,sys,time
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1];out=Path(sys.argv[1]).resolve();url=sys.argv[2]
sys.path.insert(0,str(ROOT/'选片助手'))
from sku_workbook import inspect_workbook
manifest=json.loads((out/'manifest.json').read_text(encoding='utf8'));gold=json.loads((out/'visual-review.json').read_text(encoding='utf8'))['labels']
report={'started':time.time(),'errors':[]}
with sync_playwright() as pw:
    browser=pw.chromium.launch(channel='msedge',headless=True)
    page=browser.new_page(viewport={'width':1680,'height':1050},accept_downloads=True)
    page.add_init_script('window.showSaveFilePicker=undefined');page.on('pageerror',lambda e:report['errors'].append(str(e)))
    page.goto(url);page.wait_for_function('()=>window.FramekeepBoot&&!FramekeepBoot.loading',timeout=60000)
    page.locator('#startup-screen').wait_for(state='detached');page.locator('#sku-open').click()
    page.wait_for_function('()=>document.querySelector("#progress-shot")?.textContent.trim()==="56"&&document.querySelectorAll("#sku-rows .sku-metadata-status").length===0',timeout=60000)
    report['tab']=page.locator('#sku-open').inner_text();report['summary']=page.locator('.progress-folder-summary').inner_text()
    report['rows']=page.locator('#sku-rows [data-sku-row]').count();page.screenshot(path=str(out/'progress-verified.png'))
    with page.expect_download(timeout=90000) as download:page.locator('#sku-export').click()
    destination=out/'全量拍摄记录.xlsx';download.value.save_as(str(destination))
    page.wait_for_function('()=>document.querySelector("#shooting-export-dialog").dataset.status==="complete"',timeout=60000)
    report['export_status']=page.locator('#shooting-export-status').inner_text();page.screenshot(path=str(out/'export-verified.png'))
    browser.close()
sheets=inspect_workbook(destination)
exported=[r['sku'] for sheet in sheets if not sheet.get('skipped') for r in sheet['rows'] if r['baseline']=='已拍摄']
report['export_rows']=len(exported);report['export_skus_match']=set(c.upper() for c in exported)=={r['visual_sku'].upper() for r in gold}
import win32com.client
shell=win32com.client.Dispatch('Shell.Application');folder=shell.NameSpace(str(Path(manifest['photos_root'])))
report['windows_properties']=[]
for index in (0,2,12,43,49,55):
    row=gold[index];item=folder.ParseName(row['label']);subject=item.ExtendedProperty('System.Subject');keywords=item.ExtendedProperty('System.Keywords')
    report['windows_properties'].append({'label':row['label'],'sku':row['visual_sku'],'subject':subject,'keywords':keywords,
                                         'passed':str(subject).upper()==row['visual_sku'].upper() and any(str(k).upper()=='SKU:'+row['visual_sku'].upper() for k in keywords or [])})
report['finished']=time.time();report['passed']=bool(report['rows']==56 and report['export_rows']==56 and report['export_skus_match'] and not report['errors'] and all(x['passed'] for x in report['windows_properties']))
(out/'demo-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps(report,ensure_ascii=False,indent=2));raise SystemExit(0 if report['passed'] else 1)
