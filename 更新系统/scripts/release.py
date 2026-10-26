"""构建不可变签名包；上传魔搭后匿名回读，最后激活 latest。"""
from datetime import datetime,timezone
from pathlib import Path
import argparse
import hashlib
import json
import sys
import urllib.parse
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'更新系统'))
from updater import read_json,write_json,sha256,safe_path,verify_envelope,TRUST,version
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def file_url(relative):
    return f"https://modelscope.cn/api/v1/models/{TRUST['repository']}/repo?"+urllib.parse.urlencode({'Revision':'master','FilePath':relative})


def prepare():
    config=read_json(ROOT/'更新系统/update-config.json')
    config['releaseId']=datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    write_json(ROOT/'更新系统/update-config.json',config)
    print(config['releaseId'])


def build(notes=None, output=None, *, local_key=None):
    release=ROOT/'build/tauri-release/末帧选片'
    config=read_json(release/'update-config.json')
    output=Path(output) if output else ROOT/'build/updates'/f"{config['version']}-{config['releaseId']}"
    output.mkdir(parents=True,exist_ok=False)
    package=output/'Framekeep-Windows-x64.zip';records=[]
    with zipfile.ZipFile(package,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for file in sorted(release.rglob('*')):
            if not file.is_file():continue
            relative=file.relative_to(release).as_posix();safe_path(relative)
            if file.suffix.lower() in ('.sqlite3','.db') or file.name in ('api key.md','.env'):raise ValueError('发行包含用户数据')
            archive.write(file,relative)
            records.append({'path':relative,'size':file.stat().st_size,'sha256':sha256(file)})
    manifest={'protocol':1,'product':'framekeep','displayName':'末帧选片','platform':'win32-x64',
        'version':config['version'],'releaseId':config['releaseId'],'minVersion':'1.0.0',
        'publishedAt':datetime.now(timezone.utc).isoformat(),
        'notes':notes or '新增独立自动更新器、魔搭签名更新源与轻量安装器；更新前保存评级，失败自动回滚。',
        'package':{'name':package.name,'size':package.stat().st_size,'sha256':sha256(package),
            'url':file_url(f"releases/{config['version']}/{config['releaseId']}/{package.name}")},'files':records}
    key=local_key or Ed25519PrivateKey.from_private_bytes((ROOT/'.update-private/signing-key').read_bytes())
    payload=json.dumps(manifest,ensure_ascii=False,separators=(',',':'))
    envelope={'payload':payload,'signature':key.sign(payload.encode()).hex()}
    if local_key is None:verify_envelope(envelope)
    else:key.public_key().verify(bytes.fromhex(envelope['signature']),payload.encode('utf8'))
    write_json(output/'manifest.json',manifest);write_json(output/'envelope.json',envelope)
    print(output)
    return output


def remote_bytes(url):
    request=urllib.request.Request(url,headers={'Range':'bytes=0-'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=180) as response:return response.read()


def remote_digest(url):
    request=urllib.request.Request(url,headers={'Range':'bytes=0-'})
    result=hashlib.sha256()
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=180) as response:
        while chunk:=response.read(8*1024*1024):result.update(chunk)
    return result.hexdigest()


def publish_components(api,existing):
    components=read_json(ROOT/'选片助手/component-manifest.json');published=[]
    items=[(item,ROOT/'.runtime/component-archives'/item['name']) for item in components['archives']]
    items.extend((item,ROOT/'.runtime/component-offline'/item['path']) for item in components['assets'] if item['group']=='webview')
    for item,local in items:
        parsed=urllib.parse.urlsplit(item['url']);relative=urllib.parse.parse_qs(parsed.query).get('FilePath',[''])[0]
        if parsed.hostname!='modelscope.cn' or '/'+TRUST['repository']+'/' not in parsed.path or not relative.startswith('components/'+item['sha256']+'/'):raise ValueError('运行时发布目标与固定内容地址不一致')
        # 已发布的内容地址以匿名回读为准，发布程序更新不要求保留每份历史压缩包。
        # 只有需要新上传时才依赖本地文件；现有本地文件仍须与清单匹配。
        if local.is_file() and sha256(local)!=item['sha256']:raise ValueError('运行时镜像构建已变化，未发布')
        if relative not in existing:
            if not local.is_file():raise ValueError('远端组件缺失且本地没有可上传镜像：'+item.get('name',relative))
            api.upload_file(path_or_fileobj=str(local),path_in_repo=relative,repo_id=TRUST['repository'],commit_message='components: verified inference runtime')
        if remote_digest(item['url'])!=item['sha256']:raise ValueError('运行时镜像匿名校验失败，未激活更新')
        published.append({'path':relative,'sha256':item['sha256'],'size':item['size']})
    return published


def publish(output):
    from modelscope.hub.api import HubApi,ModelScopeConfig
    from modelscope.hub.errors import HTTPError
    account=ModelScopeConfig.get_user_info()
    if not account or account[0]!=TRUST['repository'].split('/')[0]:raise ValueError('魔搭登录账号不匹配')
    api=HubApi();repo=TRUST['repository']
    envelope=read_json(output/'envelope.json');manifest=verify_envelope(envelope)
    embedded=next((r for r in manifest['files'] if r['path']=='engine/_internal/component-manifest.json'),None)
    if not embedded or embedded['sha256']!=sha256(ROOT/'选片助手/component-manifest.json'):raise ValueError('组件定义与签名程序包不一致，请重新构建后发布')
    try:api.get_model(repo)
    except HTTPError as error:
        if getattr(error.response,'status_code',None)!=404:raise
        api.create_model(repo,visibility=5,license='Apache License 2.0',chinese_name='末帧选片 桌面更新')
    existing={i['Path'] for i in api.get_model_files(repo,recursive=True) if i['Type']=='blob'}
    components=publish_components(api,existing)
    prefix=f"releases/{manifest['version']}/{manifest['releaseId']}"
    for file,relative in ((output/manifest['package']['name'],prefix+'/'+manifest['package']['name']),
                          (ROOT/'dist/末帧选片安装器.exe',prefix+'/Framekeep-Setup.exe')):
        expected=sha256(file)
        if file==output/manifest['package']['name'] and expected!=manifest['package']['sha256']:raise ValueError('本地包已改变')
        if relative in existing:
            if hashlib.sha256(remote_bytes(file_url(relative))).hexdigest()!=expected:raise ValueError('禁止覆盖不可变远端产物')
        else:api.upload_file(path_or_fileobj=str(file),path_in_repo=relative,repo_id=repo,commit_message='release: 末帧选片 '+manifest['releaseId'])
        if hashlib.sha256(remote_bytes(file_url(relative))).hexdigest()!=expected:raise ValueError('匿名下载哈希不一致，未激活')
    if 'public/latest.json' in existing:
        previous=verify_envelope(json.loads(remote_bytes(TRUST['manifestUrl'])))
        if (version(previous['version']),previous['releaseId'])>(version(manifest['version']),manifest['releaseId']):raise ValueError('禁止回退公网最新版')
    readme=output/'README.md'
    readme.write_text('# 末帧选片 Windows 更新\n\n此仓库仅提供末帧选片桌面程序和更新资源，不包含用户照片、评级或密钥。\n\n'
        f"版本：{manifest['version']} · {manifest['releaseId']}\n\n[下载轻量安装器]({file_url(prefix+'/Framekeep-Setup.exe')})\n\n"
        f"本次更新：{manifest['notes']}\n\n"
        '安装器自动准备软件、内置 OCR、推理依赖、WebView2 和鼠标输入驱动；当前仅启用 OCR，多模态作为后续开发预留，不下载约 5.54 GB 的 Qwen 权重。OCR 权重及压缩推理运行时合计约 2.96 GB，支持断点恢复，无需 Python 或 Node.js。驱动首次安装需 Windows 授权，可能需要重启。\n'
        '选片右栏可直接导入表格、绑定目录并开启全自动 OCR，与设置页同步；逐张识别并按精确或唯一高相似度匹配自动入库，冲突留作复核。公共分类去重展示跨工作表重复款号，保留各表独立进度。\n'
        '客户端验证 Ed25519 签名和 SHA-256；下载及校验后保存并正常退出，启动失败自动恢复旧版。\n','utf8')
    api.upload_file(path_or_fileobj=str(readme),path_in_repo='README.md',repo_id=repo)
    api.upload_file(path_or_fileobj=str(output/'envelope.json'),path_in_repo='public/latest.json',repo_id=repo,commit_message='activate: '+manifest['releaseId'])
    activated=json.loads(remote_bytes(TRUST['manifestUrl']))
    if activated!=envelope or verify_envelope(activated)!=manifest:raise ValueError('激活清单匿名回读不一致')
    write_json(output/'publication.json',{'repository':repo,'releaseId':manifest['releaseId'],'anonymousVerification':True,
        'installerUrl':file_url(prefix+'/Framekeep-Setup.exe'),'packageSize':manifest['package']['size'],'components':components})
    print('魔搭包、安装器及签名清单匿名回读全部通过')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['prepare','build','publish']);parser.add_argument('--snapshot',type=Path);parser.add_argument('--notes')
    args=parser.parse_args()
    if args.command=='prepare':prepare()
    elif args.command=='build':build(args.notes)
    else:publish(args.snapshot.resolve(strict=True))
