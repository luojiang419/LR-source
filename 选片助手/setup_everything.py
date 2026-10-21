"""安装公开 MIT SDK 到本项目运行时，不安装/修改 Everything 软件或其索引。"""
import hashlib
from pathlib import Path
import io
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parents[1]
URL='https://www.voidtools.com/Everything-SDK.zip'
SDK_SHA256='f5716d9513cce6b462b5170a0a2e7e081e191d9bcac6774f5decc061497df443'


def install():
    data=urllib.request.urlopen(URL,timeout=30).read()
    if hashlib.sha256(data).hexdigest()!=SDK_SHA256:raise ValueError('官方 SDK 包已变化，请核对来源与许可证后更新锁定摘要')
    sdk=zipfile.ZipFile(io.BytesIO(data));target=ROOT/'.runtime/everything';target.mkdir(parents=True,exist_ok=True)
    for name in ('Everything64.dll','Everything32.dll'):(target/name).write_bytes(sdk.read('dll/'+name))
    source=sdk.read('src/Everything.c').decode('utf-8-sig')
    license='\n'.join(line.removeprefix('//').strip() for line in source.splitlines()[1:25])
    (target/'LICENSE-SDK.txt').write_text(license+'\n\nSource: '+URL+'\nSDK archive SHA256: '+hashlib.sha256(data).hexdigest()+'\n',encoding='utf8')
    print(target)


if __name__=='__main__':install()
