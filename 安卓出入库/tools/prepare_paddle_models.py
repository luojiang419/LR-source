"""下载并校验官方固定模型；只把已选模型复制到 APK 的生成资源目录。"""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import tarfile
import urllib.request

ROOT=Path(__file__).resolve().parents[2]
MANIFEST=Path(__file__).with_name('paddle-models.json')

def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()

def prepare(output):
    manifest=json.loads(MANIFEST.read_text(encoding='utf-8'))
    cache=ROOT/'build/inventory-paddle-ocr/models'
    cache.mkdir(parents=True,exist_ok=True)
    for name in manifest.get('bundled',manifest['selected'].values()):
        item=next(row for row in manifest['models'] if row['name']==name)
        directory=cache/name
        if not all((directory/filename).is_file() and sha(directory/filename)==info['sha256'] for filename,info in item['files'].items()):
            archive=cache/(name+'.tar')
            if not archive.is_file() or sha(archive)!=item['archive_sha256']:
                temporary=archive.with_suffix('.download')
                urllib.request.urlretrieve(item['url'],temporary)
                if sha(temporary)!=item['archive_sha256']:raise RuntimeError(name+' 官方模型校验不匹配')
                temporary.replace(archive)
            directory.mkdir(exist_ok=True)
            with tarfile.open(archive) as package:
                for member in package.getmembers():
                    filename=Path(member.name).name
                    if member.isfile() and filename in item['files']:
                        (directory/filename).write_bytes(package.extractfile(member).read())
        for filename,info in item['files'].items():
            source=directory/filename
            if sha(source)!=info['sha256']:raise RuntimeError(name+'/'+filename+' 校验失败')
            target=output/'paddle-models'/name/filename
            target.parent.mkdir(parents=True,exist_ok=True)
            if not target.is_file() or sha(target)!=info['sha256']:shutil.copyfile(source,target)
    metadata=output/'paddle-models/selected.json'
    metadata.write_text(json.dumps(manifest['selected'],ensure_ascii=False),encoding='utf-8')
    print('已准备并校验模型：'+', '.join(manifest.get('bundled',manifest['selected'].values())))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    prepare(parser.parse_args().output.resolve())
