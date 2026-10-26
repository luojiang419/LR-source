"""真实浏览器与本地 OCR 的隔离端到端验收；可重复运行。"""
import hashlib
import json
from pathlib import Path
import time
from playwright.sync_api import sync_playwright
from PIL import Image

BASE = Path(__file__).resolve().parents[2] / 'build/archive-v2'
URL = 'http://127.0.0.1:8786'


def main():
    report = {'checks': [], 'screenshots': [], 'errors': []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe', headless=True)
        page = browser.new_page(viewport={'width': 1680, 'height': 1050}, device_scale_factor=1)
        page.on('pageerror', lambda error: report['errors'].append(str(error)))
        try:
            page.goto(URL, wait_until='networkidle')
            page.locator('.v2-nav [data-page="archive"]').wait_for()
            page.wait_for_function('() => document.querySelectorAll("#v2-grid .v2-photo").length === 7')
            page.screenshot(path=str(BASE / '01-全新归档.png'))
            report['screenshots'].append('01-全新归档.png')
            page.locator('#v2-scan').click()
            start = time.monotonic()
            while True:
                state = page.request.get(URL + '/api/archive-v2/state').json()
                if state['job']['status'] in ('completed', 'failed', 'cancelled'):
                    break
                if time.monotonic() - start > 250:
                    raise AssertionError('OCR 扫描超过 250 秒')
                page.wait_for_timeout(700)
            assert state['job']['status'] == 'completed', state['job']
            assert not state['job']['errors'], state['events']
            report['scan'] = state['job']
            report['checks'].append('真实 GLM-OCR 扫描全部 7 张，无解析失败')
            page.locator('[data-page="review"]').click()
            label = page.locator('#v2-grid [data-item]').filter(has=page.locator('img[alt="003-实拍标签.jpg"]'))
            label.click()
            page.locator('#v2-edit-sku').fill('LCJ81166')
            page.locator('#v2-overwrite').check()
            page.locator('#v2-save').click()
            page.wait_for_function('() => document.querySelector("#v2-message").textContent.includes("更新 3 张")')
            report['checks'].append('实拍标签确认前一段 3 张（含标签），标签不串入后一款')
            label2 = page.locator('#v2-grid [data-item]').filter(has=page.locator('img[alt="007-测试标签.jpg"]'))
            label2.click()
            page.locator('#v2-edit-sku').fill('DEMO-AW2602')
            page.locator('#v2-overwrite').check()
            page.locator('#v2-save').click()
            page.wait_for_function('() => document.querySelector("#v2-message").textContent.includes("更新 4 张")')
            report['checks'].append('第二结束标签确认后段 4 张，固定区间成员')
            page.locator('[data-page="database"]').click()
            page.locator('[data-sku="LCJ81166"]').click()
            page.wait_for_function('() => document.querySelectorAll("#v2-grid .v2-photo").length === 3')
            page.screenshot(path=str(BASE / '02-SKU数据库.png'))
            report['screenshots'].append('02-SKU数据库.png')
            page.locator('#v2-grid .v2-photo').first.click()
            page.locator('#v2-edit-sku').fill('REVIEW-9001')
            page.locator('#v2-save').click()
            page.wait_for_function('() => document.querySelector("#v2-message").textContent.includes("已确认款号")')
            page.locator('#v2-overwrite').check()
            page.locator('#v2-save').click()
            page.wait_for_function('() => document.querySelector("#v2-message").textContent.includes("更新 1 张")')
            report['checks'].append('已确认款号冲突受保护；明确覆盖后单张人工校正成功')
            page.locator('[data-sku="REVIEW9001"]').click()
            page.wait_for_function('() => document.querySelectorAll("#v2-grid .v2-photo").length === 1')
            page.locator('#v2-grid .v2-photo').click()
            sample = BASE / 'samples/2026秋冬/棚拍A/001.jpg'
            with Image.open(sample) as image:
                before_pixels = hashlib.sha256(image.convert('RGB').tobytes()).hexdigest()
            page.locator('#v2-write').click()
            page.wait_for_function('() => document.querySelector("#v2-message").textContent.includes("读回和像素校验通过")', timeout=30000)
            with Image.open(sample) as image:
                assert hashlib.sha256(image.convert('RGB').tobytes()).hexdigest() == before_pixels
            report['checks'].append('样片内部 SKU/AssetId/FileId 写入读回，JPEG 像素保持')
            page.locator('[data-inspector="evidence"]').click()
            page.screenshot(path=str(BASE / '03-识别证据.png'))
            report['screenshots'].append('03-识别证据.png')
            for width, height in ((1920,1080),(1440,900),(1024,768),(720,900),(390,844)):
                page.set_viewport_size({'width':width,'height':height})
                for mode in ('archive','database','review'):
                    page.locator(f'[data-page="{mode}"]').click()
                    if mode=='database':
                        page.locator('[data-sku="DEMOAW2602"]').click()
                    page.wait_for_timeout(120)
                    boxes = page.evaluate('''() => {const r=s=>{const b=document.querySelector(s).getBoundingClientRect();return {top:b.top,bottom:b.bottom,height:b.height,width:b.width}}; return {viewport:innerHeight, width:innerWidth, root:r('.v2'), gallery:r('.v2-center'), nav:r('.v2-header'), footer:r('.v2-statusbar'), overflow:document.documentElement.scrollWidth>innerWidth}}''')
                    assert boxes['root']['height']==height, boxes
                    assert abs(boxes['footer']['bottom']-height)<=1, boxes
                    assert boxes['gallery']['height']>=330, boxes
                    assert not boxes['overflow'], boxes
                    if width>720:
                        assert boxes['gallery']['height']>height*.65, boxes
                    report['checks'].append(f'{width}×{height} {mode} 满高、底栏贴底、无横向溢出')
                page.screenshot(path=str(BASE / f'layout-{width}.png'))
            # 留给用户一个可继续操作的双 SKU 工作台。
            page.set_viewport_size({'width':1680,'height':1050})
            page.locator('[data-page="archive"]').click()
            page.locator('[data-inspector="metadata"]').click()
            page.locator('#v2-grid .v2-photo').nth(2).click()
            page.screenshot(path=str(BASE / '04-最终工作台.png'))
            assert not report['errors'], report['errors']
            report['checks'].append('页面脚本异常为 0')
        finally:
            (BASE / 'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
            browser.close()
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
