"""从国内 ModelScope 下载指定 GGUF，校验 SHA-256，失败保留断点。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request

REPOS={
    'qwen':('ggml-org/Qwen2.5-VL-7B-Instruct-GGUF',[
        'Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf','mmproj-Qwen2.5-VL-7B-Instruct-Q8_0.gguf']),
    'ocr':('ggml-org/GLM-OCR-GGUF',['GLM-OCR-f16.gguf','mmproj-GLM-OCR-Q8_0.gguf'])}

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(8*1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def download(root,kind):
    repo,names=REPOS[kind]
    with urllib.request.urlopen(f'https://modelscope.cn/api/v1/models/{repo}/repo/files?Recursive=true',timeout=30) as r:metadata=json.load(r)
    rows={r['Name']:r for r in metadata['Data']['Files']}
    folder=root/kind;folder.mkdir(parents=True,exist_ok=True)
    manifest=[]
    for name in names:
        row=rows[name];target=folder/name;expected=row['Sha256'];size=row['Size']
        if target.is_file() and target.stat().st_size==size and digest(target)==expected:
            print('已校验',name,flush=True)
        else:
            url=f'https://modelscope.cn/api/v1/models/{repo}/repo?Revision=master&FilePath='+urllib.parse.quote(name)
            partial=target.with_suffix('.part');start=partial.stat().st_size if partial.exists() else 0
            if start>size:raise ValueError('断点大小异常：'+name)
            req=urllib.request.Request(url,headers={'Range':f'bytes={start}-'} if start else {})
            print('下载',name,'断点',start,'总字节',size,flush=True)
            with urllib.request.urlopen(req,timeout=90) as response:
                if start and response.status!=206:raise ValueError('镜像不支持断点，请保留文件并人工检查')
                with partial.open('ab' if start else 'wb') as f:
                    last=time.monotonic()
                    while chunk:=response.read(4*1024*1024):
                        f.write(chunk);start+=len(chunk)
                        if time.monotonic()-last>15:
                            print(name,round(start/size*100,1),'%',flush=True);last=time.monotonic()
            if partial.stat().st_size!=size or digest(partial)!=expected:raise ValueError('模型完整性校验失败：'+name)
            partial.replace(target)
            print('下载且 SHA-256 通过',name,flush=True)
        manifest.append({'file':str(target.resolve()),'sha256':expected,'bytes':size,'repo':repo,'source':'https://modelscope.cn','revision':row['Revision']})
    (folder/'download-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
    return manifest

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path('.runtime/models'));p.add_argument('--model',choices=['qwen','ocr','all'],default='all');a=p.parse_args()
    kinds=list(REPOS) if a.model=='all' else [a.model]
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda k:download(a.root,k),kinds))
