"""构建只供本地安装的无模型安装包；不需要或替换正式发布签名密钥。"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat

from build_tools import ROOT,CSC,render_animation
import release


def archive_bundle(output):
    output=Path(output);archive=output.with_suffix('.zip')
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as bundle:
        for file in sorted(output.rglob('*')):
            if file.is_file():bundle.write(file,file.relative_to(output.parent))
    return archive


def build(output=None,*,notes=None,title='末帧选片本地安装包'):
    config=json.loads((ROOT/'更新系统/update-config.json').read_text('utf8'))
    output=Path(output) if output else ROOT/'dist'/('末帧选片-本地安装包-无模型-'+config['releaseId'])
    # 私钥仅在本次构建进程内使用；仅本安装器的内嵌 worker 信任其公钥。
    key=Ed25519PrivateKey.generate()
    notes=notes or '包含当前源码编译的末帧选片程序，沿用已有资料和模型；缺失组件按需校验下载。'
    release.build(notes,output=output,local_key=key)
    work=ROOT/'build/local-installer'/config['releaseId'];work.mkdir(parents=True,exist_ok=True)
    local_config={**config,'publicKey':key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()}
    (work/'update-config.json').write_text(json.dumps(local_config,ensure_ascii=False,indent=2),'utf8')
    command=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--log-level','WARN','--onefile','--console',
        '--name','local-install-worker','--distpath',str(work),'--workpath',str(work/'pyinstaller'),
        '--specpath',str(work),'--add-data',str(work/'update-config.json')+';更新系统']
    for module in ('tkinter','torch','numpy','PIL','scipy','matplotlib','pytest','IPython','PyQt5','PySide6','PyQt6'):
        command+=['--exclude-module',module]
    subprocess.run(command+[str(ROOT/'更新系统/updater.py')],check=True,cwd=ROOT)
    (output/'components').mkdir();(output/'components/.keep').touch()
    installer=output/'末帧选片安装器.exe'
    common=[str(CSC),'/nologo','/target:winexe','/platform:anycpu','/optimize+','/define:INSTALLER,COMPLETE',
        '/win32icon:'+str(ROOT/'桌面程序/icons/icon.ico'),'/resource:'+str(render_animation())+',animation.png',
        '/resource:'+str(work/'local-install-worker.exe')+',worker.exe',
        *['/reference:'+n for n in ('System.Windows.Forms.dll','System.Drawing.dll','Accessibility.dll','System.Web.Extensions.dll')]]
    subprocess.run(common+['/out:'+str(installer),str(ROOT/'更新系统/SharedTheme.cs'),str(ROOT/'更新系统/Program.cs')],check=True)
    (output/'安装说明.txt').write_text(title+' · 无模型安装包\n\n'
        '请完整解压后运行“末帧选片安装器.exe”，保留软件 ZIP、envelope.json 和 components 目录。\n'
        '包含软件程序，不包含模型权重和 llama.cpp 推理运行时。安装时优先复用现有用户资料中的组件，缺失项从魔搭校验下载。\n'
        '沿用既有安装目录和资料。保存并正常退出正在运行的末帧后再安装。\n'
        '已有模型选用保持；如队列显示等待选用模型，在设置 → AI 助手配置打开“使用内置模型”。\n'
        '本次更新：'+notes+'\n'
        '此本地包由独立构建公钥校验，程序内的正式更新公钥保持不变；未发布到在线更新源。\n'
        '详细使用方式见安装后的完整使用说明.md。\n',encoding='utf-8-sig')
    forbidden=[p for p in output.rglob('*') if p.is_file() and (p.suffix.lower() in ('.gguf','.sqlite3','.db') or 'signing-key' in p.name)]
    if forbidden:raise ValueError('无模型安装包混入用户资料或模型')
    report={'releaseId':config['releaseId'],'published':False,'includesModels':False,
        'remotePublicKeyUnchanged':config['publicKey']!=local_config['publicKey'],
        'installer':{'size':installer.stat().st_size,'sha256':release.sha256(installer)},
        'package':{'size':(output/'Framekeep-Windows-x64.zip').stat().st_size,'sha256':release.sha256(output/'Framekeep-Windows-x64.zip')}}
    (output/'构建信息.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8')
    print(archive_bundle(output));return output


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path);parser.add_argument('--notes');parser.add_argument('--title',default='末帧选片本地安装包')
    args=parser.parse_args();build(args.output,notes=args.notes,title=args.title)
