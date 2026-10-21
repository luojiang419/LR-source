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


def setup(root,data=None,register=False):
    root=Path(root).resolve();runtime=root/'.runtime/llama.cpp'
    executable=next(iter(runtime.rglob('llama-server.exe')),None)
    if executable is None:raise ValueError('请先解压官方 llama.cpp Windows CUDA 运行时到 .runtime/llama.cpp')
    models={}
    for kind,mid in [('qwen','qwen2.5-vl-7b-q4'),('ocr','glm-ocr-local')]:
        manifest=json.loads((root/f'.runtime/models/{kind}/download-manifest.json').read_text(encoding='utf8'))
        model=next(x['file'] for x in manifest if not Path(x['file']).name.startswith('mmproj'))
        mmproj=next(x['file'] for x in manifest if Path(x['file']).name.startswith('mmproj'))
        models[mid]={'model':model,'mmproj':mmproj,'context':8192,'gpu_layers':99}
    folder=root/'.runtime/local-inference';folder.mkdir(parents=True,exist_ok=True)
    config=folder/'local-models.json'
    config.write_text(json.dumps({'executable':str(executable),'models':models},ensure_ascii=False,indent=2),encoding='utf8')
    if register:
        from model_providers import Providers
        providers=Providers(data or root/'.framekeep')
        for mid,category,name in [('qwen2.5-vl-7b-q4','multimodal','内置 Qwen2.5-VL 7B Q4'),('glm-ocr-local','ocr','内置 GLM-OCR')]:
            old=next((p for p in providers.public()['providers'] if p['protocol']=='local' and p['model']==mid),{})
            providers.save({'id':old.get('id'),'name':name,'category':category,'protocol':'local','model':mid,'timeout':300,'max_tokens':1200,'image_size':1600,'json_mode':category!='ocr'})
        print('已添加内置模型卡片；原 API 卡片和选用保持，可在界面点击选用。')
    print('内置模型配置：',config)
    return config

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]);p.add_argument('--data',type=Path);p.add_argument('--register',action='store_true');p.add_argument('--download-runtime',action='store_true');a=p.parse_args()
    if a.download_runtime:download_runtime(a.root)
    setup(a.root,a.data,a.register)
