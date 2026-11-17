"""为既有正式安装构建最小签名包；不重新分发 OCR 和运行时。"""
import argparse
from datetime import datetime,timezone
import json
import io
import hashlib
import re
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'更新系统'),str(ROOT/'更新系统/scripts')]
import updater as u
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from build_tools import CSC
import release


def native_digest(name,content):
    if name.endswith('SharedTheme.cs'):content=re.sub(rb'AssemblyFileVersion\("[^"]+"\)',b'AssemblyFileVersion("<release>")',content)
    return hashlib.sha256(content).hexdigest()


def runtime_baselines(baselines):
    variants={}
    for rows in baselines.values():
        for r in rows:
            if '/runtime/' in r['path'] or r['path'].endswith(('base_library.zip','.dll','.pyd')) or r['path']=='update/卸载SHIYING 数字资产.exe':
                variants.setdefault(r['path'],{})[r['sha256']]={'size':r['size'],'sha256':r['sha256']}
    result={}
    for release_id,rows in baselines.items():
        result[release_id]=[]
        for r in rows:
            alternatives=[entry for digest,entry in variants.get(r['path'],{}).items() if digest!=r['sha256']]
            result[release_id].append({**r,**({'compatible':alternatives} if alternatives else {})})
    return result


def build_bootstrap(output,baseline,version_info):
    snapshot=Path(baseline['snapshot']);setup=snapshot/'SHIYING-Setup.exe'
    meta={'envelope':u.read_json(snapshot/'envelope.json'),'url':release.file_url(f"releases/{baseline['version']}/{baseline['releaseId']}/SHIYING-Setup.exe"),'size':setup.stat().st_size,'sha256':u.sha256(setup)}
    u.write_json(output/'bootstrap.json',meta)
    subprocess.run([str(CSC),'/nologo','/target:winexe','/platform:anycpu','/optimize+','/reference:System.Windows.Forms.dll','/reference:System.Web.Extensions.dll','/resource:'+str(output/'bootstrap.json')+',bootstrap.json','/out:'+str(output/'SHIYING-Setup.exe'),str(version_info),str(ROOT/'更新系统/BootstrapInstaller.cs')],check=True)


def build(base_snapshots,output,stage,*,notes=None):
    output=Path(output);stage=Path(stage)
    from release_version import validate_build
    config=validate_build(ROOT,stage)
    staged=u.read_json(stage/'update-config.json')
    if (staged['version'],staged['releaseId'])!=(config['version'],config['releaseId']):raise ValueError('版本准备后必须重新编译并暂存程序，不能在打包时修改旧程序身份')
    baseline={};baseline_versions={};old_workers=[];latest=None;latest_full=None
    native_sources={name:native_digest(name,(ROOT/name).read_bytes()) for name in ('更新系统/Program.cs','更新系统/SharedTheme.cs','更新系统/updater.py','更新系统/installation.py','更新系统/process_control.py')}
    snapshots=[(Path(p),u.verify_envelope(u.read_json(Path(p)/'envelope.json'))) for p in base_snapshots]
    for snapshot,old in sorted(snapshots,key=lambda item:(u.version(item[1]['version']),item[1]['releaseId'])):
        if u.version(config['version'])<=u.version(old['version']):raise ValueError('正式发布必须先运行 release.py prepare 自动递增版本并重新编译')
        rows={r['path']:r for r in old['files']}
        full='engine/_internal/python312.dll' in rows
        if not full:
            if latest_full is None:raise ValueError('最小历史快照需要先提供完整基线')
            rows={**{r['path']:r for r in latest_full['files']},**rows}
        baseline[old['releaseId']]=list(rows.values());baseline_versions[old['releaseId']]=old['version']
        with zipfile.ZipFile(snapshot/old['package']['name']) as archive:
            if 'update/worker-manifest.json' in archive.namelist():old_workers.append(json.loads(archive.read('update/worker-manifest.json').decode('utf-8-sig')))
        if full and (latest_full is None or old['releaseId']>latest_full['releaseId']):latest_full={**old,'snapshot':snapshot}
        if latest is None or old['releaseId']>latest['releaseId']:latest=old
    # PyInstaller 会重建标准库 ZIP；内容相同则复用既有字节，不分发时间戳差异。
    with zipfile.ZipFile(latest_full['snapshot']/latest_full['package']['name']) as archive:
        for name in ('engine/_internal/base_library.zip','update/_internal/base_library.zip'):
            old_bytes=archive.read(name);current=stage/name
            with zipfile.ZipFile(io.BytesIO(old_bytes)) as a,zipfile.ZipFile(current) as b:
                if set(a.namelist())==set(b.namelist()) and all(a.read(n)==b.read(n) for n in a.namelist()):current.write_bytes(old_bytes)
        previous_sources=latest_full.get('nativeSources')
        sidecar=latest_full['snapshot']/'native-sources.json'
        if previous_sources is None and sidecar.is_file():previous_sources=u.read_json(sidecar)
        if previous_sources==native_sources:
            # 卸载入口内嵌旧更新环境；业务源码没变时不因构建时间或版本数据重新分发它。
            name='update/卸载SHIYING 数字资产.exe'
            (stage/name).write_bytes(archive.read(name))
    output.mkdir(parents=True,exist_ok=False)
    for name in ('update-config.json','engine/_internal/更新系统/update-config.json','update/_internal/更新系统/update-config.json'):
        existing=u.read_json(stage/name)
        if (existing['version'],existing['releaseId'])!=(config['version'],config['releaseId']):raise ValueError('编译版本未统一：'+name)
    worker=u.read_json(stage/'update/worker-manifest.json')
    for record in worker['files']:
        path=stage/'update'/record['path'];record.update(size=path.stat().st_size,sha256=u.sha256(path))
        if record['path'].startswith('_internal/') and not record['path'].endswith('update-config.json'):
            alternatives={}
            for old_worker in old_workers:
                for item in old_worker['files']:
                    if item['path']==record['path']:
                        for entry in [item,*item.get('compatible',[])]:
                            if (entry['size'],entry['sha256'])!=(record['size'],record['sha256']):alternatives[entry['sha256']]={'size':entry['size'],'sha256':entry['sha256']}
            if alternatives:record['compatible']=list(alternatives.values())
    u.write_json(stage/'update/worker-manifest.json',worker)
    layout=u.read_json(stage/'update/install-layout.json')
    for record in layout['files']:
        path=stage/record['path'];record.update(size=path.stat().st_size,sha256=u.sha256(path))
    layout.update(version=config['version'],releaseId=config['releaseId']);u.write_json(stage/'update/install-layout.json',layout)
    old_by_path={r['path']:r for r in latest_full['files']}
    changed=[r['path'] for r in layout['files'] if r!=old_by_path.get(r['path'])]
    # 现有更新协议必须携带这些小型入口；不携带未变化的运行库和依赖。
    required=['SHIYING 数字资产.exe','engine/framekeep-engine.exe','update/update-worker.exe','update/SHIYING 更新器.exe','update-config.json','update/install-layout.json']
    # 较旧客户端也需要完整的一致界面及小型业务脚本，不携带二进制运行环境。
    required.extend(r['path'] for r in layout['files'] if r['path'].startswith(('engine/_internal/演示方案/','engine/_internal/material-search-runtime/')) or r['path'].endswith(('metadata_memory_worker.pl','clip_worker.py')))
    names=sorted(set(changed+required))
    if any('/runtime/' in p or '/component-seed/' in p or '/models/' in p or p.endswith(('base_library.zip','.dll','.pyd')) for p in names):raise ValueError('本次最小包出现运行时或模型变化，请单独处理基础版本升级')
    records=[{'path':name,'size':(stage/name).stat().st_size,'sha256':u.sha256(stage/name)} for name in names]
    package=output/'SHIYING-Windows-x64.zip'
    with zipfile.ZipFile(package,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for r in records:z.write(stage/r['path'],r['path'])
    notes=notes or '修复现场二星解析状态误报，接通客户端最小在线更新，并统一自动递增正式版本号。'
    manifest={'protocol':1,'product':'framekeep','displayName':'SHIYING 数字资产','platform':'win32-x64','version':config['version'],'releaseId':config['releaseId'],'minVersion':'1.0.1','kind':'minimal','baseReleaseId':latest_full['releaseId'],'nativeSources':native_sources,'publishedAt':datetime.now(timezone.utc).isoformat(),'notes':notes,'files':records,
              'package':{'name':package.name,'size':package.stat().st_size,'sha256':u.sha256(package),'url':release.file_url('minimal/'+config['releaseId']+'/'+package.name)}}
    payload=json.dumps(manifest,ensure_ascii=False,separators=(',',':'))
    key=Ed25519PrivateKey.from_private_bytes((ROOT/'.update-private/signing-key').read_bytes())
    envelope={'payload':payload,'signature':key.sign(payload.encode()).hex()};assert u.verify_envelope(envelope)==manifest
    # 公开 JSON 下载可能移除 BOM；入口内嵌哈希与实际下载字节必须一致。
    (output/'envelope.json').write_text(json.dumps(envelope,ensure_ascii=False,indent=2),encoding='utf8')
    u.write_json(output/'manifest.json',manifest)
    # 启动器核对全部基线程序文件；业务 data 目录不在程序清单中。
    meta={'version':config['version'],'releaseId':config['releaseId'],'baselineLabel':', '.join(baseline),'baselineVersions':baseline_versions,'baselines':runtime_baselines(baseline),'package':{'size':package.stat().st_size,'sha256':u.sha256(package)},'envelope':{'size':(output/'envelope.json').stat().st_size,'sha256':u.sha256(output/'envelope.json')}}
    u.write_json(output/'minimal.json',meta)
    version_info=output/'VersionInfo.cs'
    version_info.write_text('[assembly: System.Reflection.AssemblyFileVersion("'+config['version']+'.0")]','utf8')
    exe=output/'安装最小更新.exe';rsp=output/'compile.rsp'
    args=['/nologo','/target:winexe','/platform:anycpu','/optimize+','/reference:System.Windows.Forms.dll','/reference:System.Web.Extensions.dll',
          '/out:'+str(exe),'/resource:'+str(output/'minimal.json')+',minimal.json',str(version_info),str(ROOT/'更新系统/MinimalUpdateLauncher.cs')]
    rsp.write_text('\n'.join('"'+arg+'"' for arg in args),encoding='utf-8-sig')
    subprocess.run([str(CSC),'@'+str(rsp)],check=True,cwd=ROOT)
    build_bootstrap(output,latest_full,version_info)
    shutil.copy2(output/'SHIYING-Setup.exe',ROOT/'dist/SHIYING 数字资产安装器.exe')
    (output/'使用说明.txt').write_text('SHIYING '+config['version']+' · 最小更新包\n\n已安装用户可直接在软件内检查更新。手动安装支持基线：'+meta['baselineLabel']+'。\n将程序包、签名清单和“安装最小更新.exe”放在同一目录，运行入口并选择原安装目录，保存当前操作后按更新窗口执行。\n复用本机 OCR、模型和全部运行环境，不下载或重装这些依赖。更新失败由现有事务自动回滚；照片和业务资料保持。\n新电脑运行 SHIYING-Setup.exe 准备完整基础环境，启动后检查更新到当前版本。\n',encoding='utf-8-sig')
    u.write_json(output/'build-info.json',{'version':config['version'],'releaseId':config['releaseId'],'file_count':len(records),'package_bytes':package.stat().st_size,'package_sha256':u.sha256(package),'supported_baselines':list(baseline),'contains_models':False,'contains_runtime_files':False,'published':False,'compiler':str(CSC)})
    print(str(rsp),flush=True)
    return output


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',type=Path,action='append',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--stage',type=Path,required=True);p.add_argument('--notes');a=p.parse_args();build(a.base,a.output,a.stage,notes=a.notes)
