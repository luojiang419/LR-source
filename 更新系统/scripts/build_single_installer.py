"""生成单个 EXE 离线全量安装器，包含 CPU OCR 和依赖，排除 Qwen 权重。"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

from build_tools import ROOT,CSC,OUTPUT,render_animation,worker_resources
from build_complete_installer import source_for

sys.path.insert(0,str(ROOT/'更新系统'))
from updater import verify_envelope,sha256


def build(snapshot,output=None):
    snapshot=Path(snapshot).resolve();envelope=json.loads((snapshot/'envelope.json').read_text('utf8'))
    manifest=verify_envelope(envelope);release_id=manifest['releaseId']
    package=snapshot/manifest['package']['name']
    if package.stat().st_size!=manifest['package']['size'] or sha256(package)!=manifest['package']['sha256']:raise ValueError('软件包与签名不匹配')
    components=json.loads((ROOT/'选片助手/component-manifest.json').read_text('utf8'))
    selected=[r for r in components['assets'] if r['group'] in ('pp-ocrv6-medium-local','pp-ocrv6-small-local','driver','webview') or r.get('bundled')]
    if any(r['path'].lower().endswith('.gguf') or 'qwen' in r['path'].lower() for r in selected):raise ValueError('全量 CPU 安装器不能包含 Qwen 权重')
    sources=[('envelope.json',snapshot/'envelope.json'),('SHIYING-Windows-x64.zip',package)]
    sources.extend(('components/'+r['path'],source_for(r)) for r in selected)
    work=ROOT/'build/single-installer'/release_id;work.mkdir(parents=True,exist_ok=True)
    archive=work/'offline-payload.zip';records=[]
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_STORED) as bundle:
        for relative,source in sources:
            bundle.write(source,relative)
            records.append({'path':relative,'size':source.stat().st_size,'sha256':sha256(source)})
    metadata=work/'offline-manifest.json'
    metadata.write_text(json.dumps({'schema':1,'archive_sha256':sha256(archive),'files':records},separators=(',',':')),encoding='utf8')
    installer=Path(output) if output else ROOT/'dist'/('SHIYING 数字资产全量安装器-修复版-'+release_id+'.exe')
    installer.parent.mkdir(parents=True,exist_ok=True)
    command=[str(CSC),'/nologo','/target:winexe','/platform:anycpu','/optimize+','/define:INSTALLER,EMBEDDED_FULL',
        '/win32icon:'+str(ROOT/'桌面程序/icons/icon.ico'),'/resource:'+str(ROOT/'桌面程序/icons/icon.png')+',brand.png',
        '/resource:'+str(render_animation())+',animation.png',*worker_resources(),
        '/resource:'+str(archive)+',offline-payload.zip','/resource:'+str(metadata)+',offline-manifest.json',
        *['/reference:'+name for name in ('System.Windows.Forms.dll','System.Drawing.dll','Accessibility.dll','System.Web.Extensions.dll','System.IO.Compression.dll')],
        '/out:'+str(installer),str(ROOT/'更新系统/SharedTheme.cs'),str(ROOT/'更新系统/Program.cs')]
    subprocess.run(command,check=True)
    report={'releaseId':release_id,'installer':str(installer),'bytes':installer.stat().st_size,'sha256':sha256(installer),
            'offline':True,'singleExe':True,'qwenWeightsIncluded':False,'glmWeightsIncluded':False,
            'components':[r['path'] for r in selected],'payloadFiles':records,'workerRuntime':'onedir','published':False}
    (work/'build-info.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('components','payloadFiles')},ensure_ascii=False),flush=True)
    return installer


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--snapshot',type=Path,required=True);parser.add_argument('--out',type=Path)
    args=parser.parse_args();build(args.snapshot,args.out)
