"""生成带签名程序包和全部模型依赖的离线安装目录，不发布远端。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime,timezone

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'选片助手'))
from component_setup import digest, inside
from build_tools import CSC, OUTPUT, render_animation
import release


def source_for(record):
    relative=record['path']
    candidates=[ROOT/'.runtime/component-seed'/relative,ROOT/'.runtime/component-offline'/relative]
    if relative.startswith('models/'):candidates.append(ROOT/'.runtime'/relative)
    if relative.startswith('runtime/'):candidates.append(ROOT/'.runtime/llama.cpp'/Path(relative).name)
    source=next((p for p in candidates if p.is_file() and p.stat().st_size==record['size'] and digest(p)==record['sha256']),None)
    if source is None:raise ValueError('构建缺少已验证组件：'+relative)
    return source


def build(output=None):
    output=Path(output) if output else ROOT/'dist/末帧选片完整安装包'
    output.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((ROOT/'选片助手/component-manifest.json').read_text(encoding='utf8'))
    for r in manifest['assets']:
        source=source_for(r);target=inside(output/'components',r['path']);target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():
            if digest(target)!=r['sha256']:raise ValueError('输出组件与构建源不一致，请使用新输出目录：'+str(target))
        else:
            try:os.link(source,target)
            except OSError:shutil.copy2(source,target)
    package_dir=ROOT/'build/complete-installer-packages'/datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    package_dir=release.build('修复内置识别组件自动补齐，安装阶段准备全模型及依赖，支持新电脑鼠标驱动自动安装。',output=package_dir)
    for name in ('Framekeep-Windows-x64.zip','envelope.json'):shutil.copy2(package_dir/name,output/name)
    common=[str(CSC),'/nologo','/target:winexe','/platform:anycpu','/optimize+',
        '/define:INSTALLER,COMPLETE','/win32icon:'+str(ROOT/'桌面程序/icons/icon.ico'),
        '/resource:'+str(render_animation())+',animation.png','/resource:'+str(OUTPUT/'update-worker.exe')+',worker.exe',
        *['/reference:'+n for n in ('System.Windows.Forms.dll','System.Drawing.dll','Accessibility.dll','System.Web.Extensions.dll')]]
    installer=output/'末帧选片完整安装器.exe'
    subprocess.run(common+['/out:'+str(installer),str(ROOT/'更新系统/SharedTheme.cs'),str(ROOT/'更新系统/Program.cs')],check=True)
    (output/'安装说明.txt').write_text('末帧选片完整安装包\n\n请保留所有文件和 components 目录，双击“末帧选片完整安装器.exe”。\n本包包含全部内置 OCR、多模态模型及运行依赖，安装时自动准备，无需 Python 或手工部署。\n新电脑缺少鼠标输入驱动时，Windows 将请求管理员授权；首次安装驱动后可能需要重新插接接收器或重启电脑。安装器不会自动重启。\n已有评级、照片和设置保留，模型资料在卸载后同样保留。已配置外部 API 的用户保留原选用；全新资料默认使用内置模型。\n',encoding='utf-8-sig')
    report={'installer':str(installer),'sha256':digest(installer),'componentFiles':len(manifest['assets']),
            'componentBytes':sum(r['size'] for r in manifest['assets']),'applicationPackage':digest(output/'Framekeep-Windows-x64.zip'),'published':False}
    (output/'构建信息.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(report,ensure_ascii=False));return output


if __name__=='__main__':build()
