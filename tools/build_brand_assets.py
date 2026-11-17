"""由统一 SVG 生成 Windows / Android 品牌图标，不修改 AI 原始设计稿。"""
from pathlib import Path
import io
import shutil
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]


def build():
    source=ROOT/'演示方案/brand.svg'
    with sync_playwright() as pw:
        browser=pw.chromium.launch(channel='msedge',headless=True)
        page=browser.new_page(viewport={'width':512,'height':512},device_scale_factor=1)
        page.goto(source.as_uri())
        png=page.screenshot(omit_background=True)
        browser.close()
    image=Image.open(io.BytesIO(png)).convert('RGBA')
    icons=ROOT/'桌面程序/icons'
    image.save(icons/'icon.png')
    image.save(icons/'icon.ico',sizes=[(16,16),(20,20),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
    # Android 位图 drawable 的文件名保持不变，使既有 manifest 无需迁移。
    drawable=ROOT/'安卓出入库/app/src/main/res/drawable'
    (drawable/'ic_launcher.xml').unlink(missing_ok=True)
    image.save(drawable/'ic_launcher.png')
    shutil.copy2(source,ROOT/'安卓出入库/app/src/main/assets/brand.svg')
    shutil.copy2(source,ROOT/'桌面程序/frontend/brand.svg')
    print('已生成 Windows ICO / PNG、Android launcher 及共用 SVG')


if __name__=='__main__':build()
