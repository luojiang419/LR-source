"""校验最小包、上传并匿名回读，最后切换客户端实际读取的 latest。"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'更新系统'),str(Path(__file__).resolve().parent)]
import updater as u
import release


def publish(snapshot,verification):
    from modelscope.hub.api import HubApi,ModelScopeConfig
    snapshot=Path(snapshot);evidence=u.read_json(verification)
    envelope=u.read_json(snapshot/'envelope.json');manifest=u.verify_envelope(envelope)
    config=u.read_json(ROOT/'更新系统/update-config.json')
    if (config['version'],config['releaseId'])!=(manifest['version'],manifest['releaseId']):raise ValueError('发布包不是当前准备的正式版本')
    if evidence.get('status')!='passed' or (evidence.get('version'),evidence.get('releaseId'))!=(manifest['version'],manifest['releaseId']):raise ValueError('缺少当前编译版本的验收记录')
    package=snapshot/manifest['package']['name']
    if u.sha256(package)!=evidence['package_sha256'] or u.sha256(package)!=manifest['package']['sha256']:raise ValueError('验收后软件包发生变化')
    if manifest.get('kind')!='minimal':raise ValueError('发布入口只接受最小更新包')
    with zipfile.ZipFile(package) as z:
        if set(z.namelist())!={r['path'] for r in manifest['files']} or z.testzip():raise ValueError('压缩包与签名清单不一致')
        for r in manifest['files']:
            if '/runtime/' in r['path'] or '/models/' in r['path'] or r['path'].endswith(('base_library.zip','.pyd','.dll')):raise ValueError('最小包混入运行库或模型')
            if len(z.read(r['path']))!=r['size'] or hashlib.sha256(z.read(r['path'])).hexdigest()!=r['sha256']:raise ValueError('包内文件校验失败')
    repo=u.TRUST['repository'];account=ModelScopeConfig.get_user_info()
    if not account or account[0]!=repo.split('/')[0]:raise ValueError('魔搭账号与既有仓库不匹配')
    previous_envelope=json.loads(release.remote_bytes(u.TRUST['manifestUrl']));previous=u.verify_envelope(previous_envelope)
    incoming=(u.version(manifest['version']),manifest['releaseId']);before=(u.version(previous['version']),previous['releaseId'])
    if before>=incoming and previous_envelope!=envelope:raise ValueError('版本号没有递增，禁止覆盖或回退正式发布')
    # 新装安装器固定使用已经验证的完整基线，不让空目录安装最小差异包。
    bootstrap=u.read_json(snapshot/'bootstrap.json');base=u.verify_envelope(bootstrap['envelope'])
    if base.get('kind')=='minimal':raise ValueError('新装基线不能是最小差异包')
    api=HubApi();api.get_model(repo)
    existing={entry['Path'] for entry in api.get_model_files(repo,recursive=True) if entry['Type']=='blob'}
    prefix='minimal/'+manifest['releaseId'];artifacts=[]
    for file,name in ((package,package.name),(snapshot/'安装最小更新.exe','安装最小更新.exe'),(snapshot/'SHIYING-Setup.exe','SHIYING-Setup.exe'),(snapshot/'envelope.json','envelope.json')):
        relative=prefix+'/'+name;url=release.file_url(relative)
        if relative not in existing:
            print(json.dumps({'phase':'uploading','file':name,'bytes':file.stat().st_size}),flush=True)
            api.upload_file(path_or_fileobj=str(file),path_in_repo=relative,repo_id=repo,commit_message='minimal release: '+manifest['version']+' '+name)
        if name.endswith('.json'):
            remote=json.loads(release.remote_bytes(url))
            if remote!=envelope or u.verify_envelope(remote)!=manifest:raise ValueError('远端签名清单不一致')
        elif release.remote_digest(url)!=u.sha256(file):raise ValueError('匿名完整下载 SHA-256 不一致')
        artifacts.append({'path':relative,'size':file.stat().st_size,'sha256':u.sha256(file),'url':url})
    readme=snapshot/'README.md'
    readme.write_text('# SHIYING 数字资产 Windows 更新\n\n'
        f"正式版本：{manifest['version']} · 发布号：{manifest['releaseId']}\n\n"
        f"已安装用户：在软件中点击“检查更新”，自动下载约 {manifest['package']['size']/1000000:.2f} MB 的最小包。已有 OCR、模型和运行环境沿用。\n\n"
        f"[手动最小更新入口]({artifacts[1]['url']})（下载后与 [程序包]({artifacts[0]['url']})、[签名清单]({artifacts[3]['url']})放在同一目录，程序包文件名保留 SHIYING-Windows-x64.zip）。\n\n"
        f"[新电脑安装入口]({artifacts[2]['url']})：准备完整基础环境，启动后在软件中检查更新到当前版本。\n\n"
        f"本次更新：{manifest['notes']}\n\n"
        '客户端验证 Ed25519 签名和 SHA-256；覆盖前保存操作，更新失败自动回滚。最小包不包含 OCR、模型、Python 标准库、DLL 或 FFmpeg/CUDA 运行环境；新电脑仍需基础环境。\n',encoding='utf8')
    api.upload_file(path_or_fileobj=str(readme),path_in_repo='README.md',repo_id=repo,commit_message='docs: online minimal '+manifest['version'])
    if release.remote_bytes(release.file_url('README.md')).decode('utf-8-sig').replace('\r\n','\n')!=readme.read_text('utf8').replace('\r\n','\n'):raise ValueError('说明回读失败')
    base_path=snapshot/'install-envelope.json';u.write_json(base_path,bootstrap['envelope'])
    api.upload_file(path_or_fileobj=str(base_path),path_in_repo='public/install-latest.json',repo_id=repo,commit_message='installation baseline: '+base['releaseId'])
    api.upload_file(path_or_fileobj=str(snapshot/'envelope.json'),path_in_repo='public/minimal-latest.json',repo_id=repo,commit_message='minimal latest: '+manifest['releaseId'])
    if u.verify_envelope(json.loads(release.remote_bytes(release.file_url('public/install-latest.json'))))!=base:raise ValueError('新装基线回读失败')
    if json.loads(release.remote_bytes(release.file_url('public/minimal-latest.json')))!=envelope:raise ValueError('最小清单回读失败')
    current=json.loads(release.remote_bytes(u.TRUST['manifestUrl']))
    if current!=previous_envelope and current!=envelope:raise ValueError('发布期间已有其它版本激活，未覆盖')
    api.upload_file(path_or_fileobj=str(snapshot/'envelope.json'),path_in_repo='public/latest.json',repo_id=repo,commit_message='activate online update: '+manifest['version'])
    activated=json.loads(release.remote_bytes(u.TRUST['manifestUrl']))
    if activated!=envelope or u.verify_envelope(activated)!=manifest:raise ValueError('客户端入口激活回读失败')
    result={'published':True,'version':manifest['version'],'releaseId':manifest['releaseId'],'online_latest_activated':True,'runtime_uploaded':False,'package_bytes':manifest['package']['size'],'artifacts':artifacts,'previous_release':previous['releaseId'],'install_base':base['releaseId']}
    u.write_json(snapshot/'publication.json',result);print(json.dumps(result,ensure_ascii=False),flush=True)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--snapshot',type=Path,required=True);p.add_argument('--verification',type=Path,required=True);a=p.parse_args();publish(a.snapshot,a.verification)
