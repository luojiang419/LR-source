"""构建原生独立窗口、冻结事务环境及轻量单文件安装器。"""
from pathlib import Path
import hashlib
import json
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/'更新系统'
OUTPUT=ROOT/'build/update-tools'
CSC=Path('C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe')


def render_animation():
    from playwright.sync_api import sync_playwright
    from PIL import Image
    destination=ROOT/'build/update-art/animation.png'
    destination.parent.mkdir(parents=True,exist_ok=True)
    fingerprint=hashlib.sha256((SOURCE/'assets/photo-flow.svg').read_bytes()).hexdigest()
    cache=destination.with_suffix('.sha256')
    if destination.exists() and cache.exists() and cache.read_text()==fingerprint:return destination
    strip=Image.new('RGBA',(240,180*48))
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        page=browser.new_page(viewport={'width':240,'height':180},device_scale_factor=1)
        page.goto((SOURCE/'assets/photo-flow.svg').as_uri())
        page.evaluate('document.documentElement.pauseAnimations()')
        import io
        for frame in range(48):
            page.evaluate('(t)=>document.documentElement.setCurrentTime(t)',frame/12)
            png=page.screenshot(omit_background=True)
            strip.paste(Image.open(io.BytesIO(png)),(0,frame*180))
        browser.close()
    strip.save(destination,optimize=True);cache.write_text(fingerprint)
    return destination


def build(skip_worker=False):
    OUTPUT.mkdir(parents=True,exist_ok=True)
    animation=render_animation()
    command=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--log-level','WARN','--onefile','--console',
        '--name','update-worker','--distpath',str(OUTPUT),'--workpath',str(ROOT/'build/update-worker'),
        '--specpath',str(ROOT/'build'),'--add-data',str(SOURCE/'update-config.json')+';更新系统']
    for module in ('tkinter','torch','numpy','PIL','scipy','matplotlib','pytest','IPython','PyQt5','PySide6','PyQt6'):
        command+=['--exclude-module',module]
    if not skip_worker:subprocess.run(command+[str(SOURCE/'updater.py')],check=True,cwd=ROOT)
    common=[str(CSC),'/nologo','/target:winexe','/platform:anycpu','/optimize+',
        '/win32icon:'+str(ROOT/'桌面程序/icons/icon.ico'),'/resource:'+str(animation)+',animation.png',
        *['/reference:'+name for name in ('System.Windows.Forms.dll','System.Drawing.dll','Accessibility.dll','System.Web.Extensions.dll')]]
    sources=[str(SOURCE/'SharedTheme.cs'),str(SOURCE/'Program.cs')]
    subprocess.run(common+['/out:'+str(OUTPUT/'末帧更新器.exe')]+sources,check=True)
    subprocess.run(common+['/define:UNINSTALLER','/resource:'+str(OUTPUT/'update-worker.exe')+',worker.exe','/out:'+str(OUTPUT/'卸载末帧选片.exe')]+sources,check=True)
    installer=ROOT/'dist/末帧选片安装器.exe';installer.parent.mkdir(exist_ok=True)
    subprocess.run(common+['/define:INSTALLER','/resource:'+str(OUTPUT/'update-worker.exe')+',worker.exe','/out:'+str(installer)]+sources,check=True)
    report={p.name:{'size':p.stat().st_size,'sha256':hashlib.file_digest(p.open('rb'),'sha256').hexdigest()} for p in (OUTPUT/'末帧更新器.exe',OUTPUT/'卸载末帧选片.exe',OUTPUT/'update-worker.exe',installer)}
    (ROOT/'build/update-tools-build.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':build('--native-only' in sys.argv)
