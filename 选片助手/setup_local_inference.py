"""把独立运行时和已校验权重登记为本软件内置模型；可选添加模型卡片。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import urllib.request
import zipfile

RUNTIME_FILES={
    'llama-b11773-bin-win-cuda-12.4-x64.zip':'1c09542a78b1b3fe59ebbc3c4a4087e8feb52846c1141409c6c2ff36eeac57ef',
    'cudart-llama-bin-win-cuda-12.4-x64.zip':'8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6'}

def download_runtime(root):
    folder=Path(root)/'.runtime/llama.cpp';folder.mkdir(parents=True,exist_ok=True)
    def fetch(item):
        name,expected=item;target=folder/name
        if not target.is_file():
            temp=target.with_suffix('.part')
            with urllib.request.urlopen('https://github.com/ggml-org/llama.cpp/releases/download/b11773/'+name,timeout=90) as response,temp.open('wb') as out:
                while chunk:=response.read(4*1024*1024):out.write(chunk)
            temp.replace(target)
        with target.open('rb') as f:actual=hashlib.file_digest(f,'sha256').hexdigest()
        if actual!=expected:raise ValueError('运行时 SHA-256 校验失败：'+name)
        with zipfile.ZipFile(target) as archive:
            for member in archive.infolist():
                (folder/member.filename).resolve().relative_to(folder.resolve())
            archive.extractall(folder)
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(fetch,RUNTIME_FILES.items()))


def setup(root,data=None,register=False,with_multimodal=False):
    from component_setup import ComponentManager
    root=Path(root).resolve()
    manager=ComponentManager(root/'.runtime',seeds=[root/'.runtime'])
    groups=['pp-ocrv6-medium-local']+(['qwen2.5-vl-7b-q4'] if with_multimodal else [])
    try:manager.ensure(groups)
    finally:manager.close()
    config=manager.root/'local-models.json'
    folder=root/'.runtime/local-inference';folder.mkdir(parents=True,exist_ok=True)
    (folder/'local-models.json').write_bytes(config.read_bytes())
    if register:
        from model_providers import Providers,BUILTIN_MODELS
        providers=Providers(data or root/'.framekeep')
        for category in (('ocr','multimodal') if with_multimodal else ('ocr',)):
            model,name=BUILTIN_MODELS[category]
            if any(p['protocol']=='local' and p['model']==model for p in providers.public()['providers']):continue
            providers.save({'name':name,'category':category,'protocol':'local','model':model,'timeout':300,'max_tokens':1200,'image_size':1600,'json_mode':category!='ocr'})
        print('已添加 CPU OCR 卡片；原 API 配置和选用保持。')
    print('内置模型配置：',config)
    return config

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]);p.add_argument('--data',type=Path);p.add_argument('--register',action='store_true');p.add_argument('--download-runtime',action='store_true');p.add_argument('--with-multimodal',action='store_true');a=p.parse_args()
    if a.download_runtime:download_runtime(a.root)
    setup(a.root,a.data,a.register,a.with_multimodal)
