"""实际浏览器验证报告回放、日期任务、样片读回、筛选和手机布局。"""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright


def verify(report):
    report = Path(report).resolve()
    output = report.parent / 'browser-verification'
    output.mkdir(exist_ok=True)
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 1080}, reduced_motion='reduce')
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(report.as_uri(), wait_until='load')
            page.wait_for_function("()=>document.querySelectorAll('#nodes .machine').length===4")
            data = json.loads(page.locator('#evidence').text_content())
            assert page.locator('#tests tr').count() == len(data['results'])
            assert page.locator('#raw-cases tr').count() == len(data['raw_cases'])
            assert page.locator('#performance-table tr').count() == 4
            assert '3 / 3' in page.locator('#stat-progress').inner_text()
            page.screenshot(path=str(output / 'desktop-overview.png'))
            for index in range(len(data['stages'])):
                page.locator('#stage').fill(str(index))
                assert page.locator('#stage-title').inner_text() == data['stages'][index]['name']
                assert page.locator('#nodes .machine').count() == 4
            page.locator('#nodes').screenshot(path=str(output / 'four-nodes.png'))
            page.locator('#project-view').select_option('-C.xlsx')
            assert page.locator('#nodes').inner_text().count('2026-10-20-C.xlsx') == 4
            page.locator('#project-view').select_option('全部拍摄')
            page.locator('[data-photo="0"]').click()
            assert page.locator('#photo-dialog').is_visible()
            actual = json.loads(page.locator('#photo-json').inner_text())
            assert any(f['extension'] == '.cr3' for f in actual)
            page.keyboard.press('Escape')
            assert not page.locator('#photo-dialog').is_visible()
            page.locator('#test-category').select_option('JPG→RAW')
            assert page.locator('#tests tr').count() == len(data['raw_cases'])
            page.locator('#test-category').select_option('检索性能')
            assert page.locator('#tests tr').count() == 1
            page.locator('#test-category').select_option('全部')
            page.set_viewport_size({'width': 375, 'height': 900})
            page.evaluate('window.scrollTo(0,0)')
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'), '窄屏存在页面横向溢出'
            page.screenshot(path=str(output / 'mobile-overview.png'))
            assert not errors, errors
            result = dict(status='passed', desktop='1440×1080', mobile='375×900', stages=len(data['stages']), nodes=4,
                javascript_errors=errors, verified=['全部回放阶段', '四机数据库面板', 'C 日期任务', 'JPG/RAW 读回详情', 'RAW/性能类别筛选', '手机无页面横向溢出'])
            (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            return result
        finally:
            browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.report), ensure_ascii=False))
