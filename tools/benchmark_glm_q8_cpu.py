"""隔离 GLM-OCR Q8 CPU 试验，与 Medium 使用逐字节相同输入，不修改模型配置。"""
import argparse
import base64
import ctypes as C
from ctypes import wintypes as W
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'选片助手'))
from local_inference import own_process
from paddle_ocr import PaddleOcr,candidate_text
from sku_match import extract_ocr_codes,normalize
from PIL import Image


def memory(pid):
    class Counters(C.Structure):
        _fields_=[('cb',W.DWORD),('faults',W.DWORD)]+[(k,C.c_size_t) for k in
            ('peak_working_set','working_set','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_pagefile','private')]
    kernel=C.WinDLL('kernel32');kernel.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];kernel.OpenProcess.restype=W.HANDLE
    kernel.CloseHandle.argtypes=[W.HANDLE];handle=kernel.OpenProcess(0x410,False,pid)
    if not handle:return {}
    try:
        data=Counters();data.cb=C.sizeof(data);api=C.WinDLL('psapi');api.GetProcessMemoryInfo.argtypes=[W.HANDLE,C.c_void_p,W.DWORD]
        if not api.GetProcessMemoryInfo(handle,C.byref(data),data.cb):return {}
        return {k+'_mib':round(getattr(data,k)/1024**2,2) for k in ('peak_working_set','working_set','private')}
    finally:kernel.CloseHandle(handle)


def run(out,tokens=1024,threads=4,cases=()):
    out=Path(out).resolve();out.mkdir(parents=True,exist_ok=True)
    q8=ROOT/'.runtime/models/ocr/GLM-OCR-Q8_0.gguf';projection=ROOT/'.runtime/models/ocr/mmproj-GLM-OCR-Q8_0.gguf'
    proof=json.loads((q8.parent/'q8-download-manifest.json').read_text('utf8'))
    with q8.open('rb') as stream:assert hashlib.file_digest(stream,'sha256').hexdigest()==proof['sha256']
    with projection.open('rb') as stream:assert hashlib.file_digest(stream,'sha256').hexdigest()==proof['projector']['sha256']
    root=ROOT/'.runtime/models/paddle-ocr'
    medium=PaddleOcr({'detector':root/'PP-OCRv6_medium_det/inference.onnx','recognizer':root/'PP-OCRv6_medium_rec/inference.onnx','dictionary':root/'PP-OCRv6_medium_rec/inference.yml','threads':threads})
    fixtures=json.loads((ROOT/'build/desktop-paddle-ocr/fixtures.json').read_text('utf8'));fixtures={c['name']:c for c in fixtures}
    selected=[tuple((value.rsplit('@',1)[0],int(value.rsplit('@',1)[1]))) for value in cases] if cases else [('XSY_2611',0),('XSY_3471',0),('XSY_5753',0),('XSY_2611',90),('XSY_2611',180),('XSY_3471',90)]
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    key=secrets.token_urlsafe(32);environment=os.environ.copy();environment.update(LLAMA_API_KEY=key,CUDA_VISIBLE_DEVICES='-1')
    command=[str(ROOT/'.runtime/llama.cpp/llama-server.exe'),'--model',str(q8),'--mmproj',str(projection),
        '--alias','glm-ocr','--host','127.0.0.1','--port',str(port),'--device','none','--n-gpu-layers','0',
        '--no-mmproj-offload','--fit','off','--ctx-size','4096','--parallel','1','--threads',str(threads),
        '--threads-batch',str(threads),'--image-max-tokens',str(tokens),'--flash-attn','on','--jinja','--no-cache-prompt','--no-webui']
    report={'model':proof,'cpu_only':True,'threads':threads,'image_max_tokens':tokens,'prompt_cache':False,
            'limitation':'小样本含方向变体；本机 14700K，不是客户机实测。两个模型收到同字节 JPEG；GLM 原生读图，Medium 含方向预处理。','cases':[]}
    started=time.perf_counter();log=(out/'server.log').open('wb');process=None;release=None
    try:
        process=subprocess.Popen(command,env=environment,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));release=own_process(process)
        deadline=time.monotonic()+120
        while True:
            if process.poll() is not None:raise RuntimeError('GLM Q8 服务启动失败，请查看本次独立日志')
            try:
                req=urllib.request.Request(f'http://127.0.0.1:{port}/health',headers={'Authorization':'Bearer '+key})
                with urllib.request.urlopen(req,timeout=2) as response:
                    if response.status==200:break
            except OSError:pass
            if time.monotonic()>deadline:raise TimeoutError('GLM Q8 加载超时')
            time.sleep(.25)
        report['load_ms']=(time.perf_counter()-started)*1000;report['loaded_memory']=memory(process.pid)
        print(json.dumps({'status':'loaded','load_ms':report['load_ms'],'memory':report['loaded_memory']}),flush=True)
        for name,degrees in selected:
            case=fixtures[name]
            with Image.open(case['path']) as image:
                image=image.convert('RGB')
                if degrees:image=image.transpose({90:Image.Transpose.ROTATE_90,180:Image.Transpose.ROTATE_180,270:Image.Transpose.ROTATE_270}[degrees])
                image.thumbnail((1600,1600));buffer=io.BytesIO();image.save(buffer,'JPEG',quality=92)
            payload=buffer.getvalue();row={'name':name,'degrees':degrees,'expected':case['expected'],'payload_sha256':hashlib.sha256(payload).hexdigest()}
            reading=medium.recognize(payload);codes=extract_ocr_codes(candidate_text(reading['text']))
            row['medium']={'text':reading['text'],'codes':codes,'elapsed_ms':reading['timing']['total_ms'],
                           'candidate_exact':normalize(case['expected']) in [normalize(c) for c in codes],'orientation':reading['orientation']}
            body={'model':'glm-ocr','messages':[{'role':'user','content':[{'type':'text','text':'Text Recognition:'},
                 {'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+base64.b64encode(payload).decode('ascii')}}]}],
                 'max_tokens':512,'temperature':0,'cache_prompt':False}
            request=urllib.request.Request(f'http://127.0.0.1:{port}/v1/chat/completions',json.dumps(body).encode(),
                {'Content-Type':'application/json','Authorization':'Bearer '+key})
            begin=time.perf_counter();print(json.dumps({'status':'recognizing','name':name,'degrees':degrees}),flush=True)
            try:
                with urllib.request.urlopen(request,timeout=240) as response:answer=json.load(response)
                choice=answer['choices'][0];text=choice['message']['content'];codes=extract_ocr_codes(text)
                row['q8']={'text':text,'codes':codes,'finish_reason':choice.get('finish_reason'),'usage':answer.get('usage'),
                           'server_timings':answer.get('timings'),'candidate_exact':normalize(case['expected']) in [normalize(c) for c in codes] and choice.get('finish_reason')!='length'}
            except Exception as exc:row['q8']={'error':str(exc),'candidate_exact':False}
            row['q8']['elapsed_ms']=(time.perf_counter()-begin)*1000;row['q8']['memory']=memory(process.pid)
            report['cases'].append(row);(out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8')
            print(json.dumps({'name':name,'degrees':degrees,'medium_ms':row['medium']['elapsed_ms'],'q8':row['q8']},ensure_ascii=False),flush=True)
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        if release:release()
        log.close();report['server_stopped']=process is None or process.poll() is not None
        (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,default=ROOT/'build/glm-q8-validation/cpu-1024');parser.add_argument('--image-tokens',type=int,default=1024);parser.add_argument('--threads',type=int,default=4)
    parser.add_argument('--cases',nargs='*',default=[])
    args=parser.parse_args();run(args.out,args.image_tokens,args.threads,args.cases)
