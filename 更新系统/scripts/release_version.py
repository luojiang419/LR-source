"""正式构建前统一递增 SemVer 补丁版本和严格单调的发布号。"""
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import re


def identity(value):
    match=re.fullmatch(r'(\d+)\.(\d+)\.(\d+)',value)
    if not match:raise ValueError('正式版本必须使用 major.minor.patch')
    return tuple(map(int,match.groups()))


def prepare(root,published,*,now=None):
    root=Path(root);config_path=root/'更新系统/update-config.json'
    config=json.loads(config_path.read_text('utf-8-sig'))
    current=max(identity(config['version']),identity(published['version']))
    next_version='.'.join(map(str,(*current[:2],current[2]+1)))
    previous=max(config['releaseId'],published['releaseId'])
    previous_time=datetime.strptime(previous,'%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
    stamp=max(now or datetime.now(timezone.utc),previous_time+timedelta(seconds=1)).strftime('%Y%m%d%H%M%S')
    next_config={**config,'version':next_version,'releaseId':stamp}
    files={config_path:json.dumps(next_config,ensure_ascii=False,indent=2)+'\n'}
    cargo=root/'桌面程序/Cargo.toml';lock=root/'桌面程序/Cargo.lock';tauri=root/'桌面程序/tauri.conf.json'
    for file,pattern in ((cargo,r'(?m)^(version\s*=\s*")[^"]+("\s*\r?$)'),
                         (lock,r'(name = "framekeep-desktop"\r?\nversion = ")[^"]+(\")')):
        text=file.read_bytes().decode('utf8')
        # 锁文件只更新本项目条目，不能改动同版本号的依赖。
        result,count=re.subn(pattern,lambda m:m[1]+next_version+(m[2] or ''),text,count=1)
        if count!=1:raise ValueError('无法更新版本：'+str(file))
        files[file]=result
    text=tauri.read_bytes().decode('utf8')
    result,count=re.subn(r'("version"\s*:\s*")[^"]+(\")',lambda m:m[1]+next_version+m[2],text,count=1)
    if count!=1:raise ValueError('无法更新 Tauri 版本')
    files[tauri]=result
    theme=root/'更新系统/SharedTheme.cs';text=theme.read_bytes().decode('utf8')
    result,count=re.subn(r'(AssemblyFileVersion\(")[^"]+("\))',lambda m:m[1]+next_version+'.0'+m[2],text,count=1)
    if count!=1:raise ValueError('无法更新原生更新工具版本')
    files[theme]=result
    before={file:file.read_bytes() for file in files}
    try:
        for file,text in files.items():file.write_bytes(text.encode('utf8'))
    except BaseException:
        for file,content in before.items():file.write_bytes(content)
        raise
    return next_config


def validate_build(root,stage):
    """打包前拒绝配置、桌面 PE 版本或冻结业务模块仍来自旧构建。"""
    import marshal
    import types
    import pefile
    from PyInstaller.archive.readers import CArchiveReader
    root=Path(root);stage=Path(stage);config=json.loads((root/'更新系统/update-config.json').read_text('utf-8-sig'))
    for relative in ('update-config.json','engine/_internal/更新系统/update-config.json','update/_internal/更新系统/update-config.json'):
        value=json.loads((stage/relative).read_text('utf-8-sig'))
        if (value['version'],value['releaseId'])!=(config['version'],config['releaseId']):raise ValueError('编译版本未统一：'+relative)
    with pefile.PE(str(stage/'SHIYING 数字资产.exe')) as pe:
        info=pe.VS_FIXEDFILEINFO[0]
        actual=(info.FileVersionMS>>16,info.FileVersionMS&65535,info.FileVersionLS>>16)
    if actual!=identity(config['version']):raise ValueError('桌面程序未按新版本重新编译')
    def normal(code):return code.replace(co_filename='',co_consts=tuple(normal(c) for c in code.co_consts)) if isinstance(code,types.CodeType) else code
    archive=CArchiveReader(str(stage/'engine/framekeep-engine.exe'));pyz=archive.open_embedded_archive('PYZ.pyz')
    if normal(marshal.loads(archive.extract('server')))!=normal(compile((root/'选片助手/server.py').read_bytes(),'server.py','exec')):raise ValueError('冻结入口不是当前源码')
    for name in pyz.toc:
        source=root/'选片助手'/(name+'.py')
        if source.is_file() and normal(pyz.extract(name))!=normal(compile(source.read_bytes(),str(source),'exec')):raise ValueError('冻结模块未重新编译：'+name)
    return config
