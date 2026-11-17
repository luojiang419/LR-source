"""构建原生独立窗口、冻结事务环境及轻量单文件安装器。"""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/'更新系统'
OUTPUT=ROOT/'build/update-tools'
CSC=Path('C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe')


def pack_worker(distribution,output):
    """原生 EXE 携带存储型归档；worker 本身不再进行 onefile 自解压。"""
    distribution=Path(distribution);output=Path(output);output.mkdir(parents=True,exist_ok=True)
    archive=output/'worker-runtime.zip';records=[]
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_STORED) as package:
        for file in sorted(distribution.rglob('*')):
            if not file.is_file():continue
            if file.is_symlink():raise ValueError('运行时不能包含链接')
            name=file.relative_to(distribution).as_posix()
            if name!='update-worker.exe' and not name.startswith('_internal/'):raise ValueError('运行时文件路径无效')
            package.write(file,name)
            records.append({'path':name,'size':file.stat().st_size,'sha256':hashlib.sha256(file.read_bytes()).hexdigest()})
    if not any(r['path']=='_internal/python312.dll' for r in records):raise ValueError('缺少独立 Python 运行库')
    manifest={'schema':1,'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'files':records}
    (output/'worker-manifest.json').write_text(json.dumps(manifest,separators=(',',':')),encoding='utf8')
    shutil.copy2(distribution/'update-worker.exe',output/'update-worker.exe')
    shutil.copytree(distribution/'_internal',output/'_internal',dirs_exist_ok=True)
    return manifest


def worker_resources(folder=OUTPUT):
    return ['/resource:'+str(Path(folder)/name)+','+name for name in ('worker-runtime.zip','worker-manifest.json')]


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
    command=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--log-level','WARN','--onedir','--console',
        '--name','update-worker','--distpath',str(ROOT/'build/update-worker-dist'),'--workpath',str(ROOT/'build/update-worker'),
        '--specpath',str(ROOT/'build'),'--add-data',str(SOURCE/'update-config.json')+';更新系统']
    for module in ('tkinter','torch','numpy','PIL','scipy','matplotlib','pytest','IPython','PyQt5','PySide6','PyQt6'):
        command+=['--exclude-module',module]
    if not skip_worker:
        subprocess.run(command+[str(SOURCE/'updater.py')],check=True,cwd=ROOT)
        pack_worker(ROOT/'build/update-worker-dist/update-worker',OUTPUT)
    common=[str(CSC),'/nologo','/target:winexe','/platform:anycpu','/optimize+',
        '/win32icon:'+str(ROOT/'桌面程序/icons/icon.ico'),'/resource:'+str(ROOT/'桌面程序/icons/icon.png')+',brand.png','/resource:'+str(animation)+',animation.png',
        *['/reference:'+name for name in ('System.Windows.Forms.dll','System.Drawing.dll','Accessibility.dll','System.Web.Extensions.dll','System.IO.Compression.dll')]]
    sources=[str(SOURCE/'SharedTheme.cs'),str(SOURCE/'Program.cs')]
    subprocess.run(common+['/out:'+str(OUTPUT/'SHIYING 更新器.exe')]+sources,check=True)
    subprocess.run(common+['/define:UNINSTALLER',*worker_resources(),'/out:'+str(OUTPUT/'卸载SHIYING 数字资产.exe')]+sources,check=True)
    installer=ROOT/'dist/SHIYING 数字资产安装器.exe';installer.parent.mkdir(exist_ok=True)
    subprocess.run(common+['/define:INSTALLER',*worker_resources(),'/out:'+str(installer)]+sources,check=True)
    report={p.name:{'size':p.stat().st_size,'sha256':hashlib.file_digest(p.open('rb'),'sha256').hexdigest()} for p in (OUTPUT/'SHIYING 更新器.exe',OUTPUT/'卸载SHIYING 数字资产.exe',OUTPUT/'update-worker.exe',installer)}
    (ROOT/'build/update-tools-build.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':build('--native-only' in sys.argv)
