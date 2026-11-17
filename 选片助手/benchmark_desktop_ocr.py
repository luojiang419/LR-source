"""只读真实图片的 Small/GLM 同图基准，预期款号仅用于推理后评分。"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time

from component_setup import ComponentManager,write_json
import local_inference
from sku_vision import recognize
from sku_match import normalize

ROOT=Path(__file__).resolve().parents[1]
SMALL='pp-ocrv6-small-local'
MEDIUM='pp-ocrv6-medium-local'


def memory():
    """包含 Python、原生推理库和模型的整个工作进程内存，不冒充权重大小。"""
    if sys.platform!='win32':return {}
    import ctypes as C
    from ctypes import wintypes as W
    class Counters(C.Structure):
        _fields_=[('cb',W.DWORD),('faults',W.DWORD)]+[(k,C.c_size_t) for k in
            ('peak_working_set','working_set','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_pagefile','private')]
    value=Counters();value.cb=C.sizeof(value)
    kernel=C.WinDLL('kernel32');kernel.GetCurrentProcess.restype=W.HANDLE
    psapi=C.WinDLL('psapi');psapi.GetProcessMemoryInfo.argtypes=[W.HANDLE,C.c_void_p,W.DWORD]
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),C.byref(value),value.cb):raise C.WinError()
    return {k+'_mib':round(getattr(value,k)/1024**2,2) for k in ('peak_working_set','working_set','private')}


def cpu_worker(fixtures,config,out,model,threads,rounds):
    cases=json.loads(Path(fixtures).read_text('utf8'));config=json.loads(Path(config).read_text('utf8'))
    config['models'][model]['threads']=threads
    write_json(Path(out)/'models.json',config)
    runtime=local_inference.LocalInference(Path(out)/'models.json',Path(out)/'logs');local_inference._runtime=runtime
    images={c['path']:Path(c['path']).read_bytes() for c in cases}
    provider={'id':'cpu-benchmark','protocol':'local','url':'http://127.0.0.1','model':model}
    report={'model':model,'threads':threads,'backend':'CPUExecutionProvider','baseline_memory':memory(),'warm':[]}
    def request(case):
        start=time.perf_counter();result=recognize(provider,images[case['path']])
        return {'name':case['name'],'kind':case.get('kind','unknown'),
                'scorable':case.get('scorable',True),
                'candidate_exact':normalize(case['expected']) in [normalize(c) for c in result['codes']],
                'elapsed_ms':round((time.perf_counter()-start)*1000,3),'result':result}
    try:
        report['cold']=request(cases[0]);report['loaded_memory']=memory()
        for turn in range(rounds):
            for case in cases[turn%len(cases):]+cases[:turn%len(cases)]:
                row=request(case);row['round']=turn+1;report['warm'].append(row)
        times=sorted(r['elapsed_ms'] for r in report['warm'])
        report['summary']={'median_ms':statistics.median(times),'p95_ms':times[math.ceil(.95*len(times))-1],
                           'min_ms':min(times),'max_ms':max(times),'requests':len(times),
                           'candidate_exact':sum(r['candidate_exact'] for r in report['warm'] if r['scorable']),
                           'scorable_requests':sum(r['scorable'] for r in report['warm'])}
        report['peak_memory']=memory()
    finally:
        runtime.close();report['closed']=runtime.ocr is None
        report['inputs_unchanged']=all(Path(p).read_bytes()==raw for p,raw in images.items())
        write_json(Path(out)/'result.json',report)
    print(json.dumps({'model':model,'threads':threads,'cold_ms':report['cold']['elapsed_ms'],**report['summary'],**report['peak_memory']},ensure_ascii=False),flush=True)


def run_cpu(fixtures,out,rounds=3,threads=(2,4)):
    out=Path(out).resolve();out.mkdir(parents=True,exist_ok=True)
    manager=ComponentManager(out/'data',seeds=[ROOT/'.runtime'])
    try:manager.ensure([SMALL,MEDIUM])
    finally:manager.close()
    config=manager.root/'local-models.json'
    report={'platform':platform.platform(),'machine':platform.machine(),'fixtures':json.loads(Path(fixtures).read_text('utf8')),
            'timing_scope':'输入已预读；首次请求包含导入/加载；连续请求包含解码、检测、识别、候选提取；每个配置独立工作进程',
            'limitation':'i7-14700K 限推理线程，不能等同于低配置现场电脑实测','models':[]}
    for model in (SMALL,MEDIUM):
        for count in threads:
            target=out/(model+'-'+str(count))
            subprocess.run([sys.executable,str(Path(__file__).resolve()),'--fixtures',str(Path(fixtures).resolve()),'--out',str(target),
                            '--rounds',str(rounds),'--cpu-worker',model,'--worker-config',str(config),'--threads',str(count)],check=True)
            report['models'].append(json.loads((target/'result.json').read_text('utf8')))
            write_json(out/'cpu-benchmark.json',report)
    return report


def run(fixtures,out,rounds=3,glm_config=None):
    out=Path(out).resolve();out.mkdir(parents=True,exist_ok=True)
    cases=json.loads(Path(fixtures).read_text(encoding='utf8'))
    if not cases or rounds<1:raise ValueError('需要图像用例及至少一轮识别')
    # 先取输入字节，计时不包含 NAS/磁盘读取；哈希前后复核。
    images={c['path']:Path(c['path']).read_bytes() for c in cases}
    hashes={p:hashlib.sha256(raw).hexdigest() for p,raw in images.items()}
    manager=ComponentManager(out/'data',seeds=[ROOT/'.runtime'])
    manager.ensure([SMALL])
    small_config=json.loads((manager.root/'local-models.json').read_text('utf8'))
    config=json.loads(Path(glm_config).read_text('utf8')) if glm_config else {'models':{}}
    config['models'][SMALL]=small_config['models'][SMALL]
    write_json(out/'local-models.json',config)
    report={'platform':platform.platform(),'python':platform.python_version(),'machine':platform.machine(),
            'fixtures':[dict(c,sha256=hashes[c['path']]) for c in cases],
            'rounds':rounds,'timing_scope':'预读输入字节 → sku_vision.recognize 返回；不含模型下载、NAS、业务库写入',
            'models':[]}
    previous=local_inference._runtime
    try:
        for model in ([SMALL,'glm-ocr-local'] if glm_config else [SMALL]):
            runtime=local_inference.LocalInference(out/'local-models.json',out/'logs')
            local_inference._runtime=runtime
            provider={'id':'benchmark-'+model,'protocol':'local','url':'http://127.0.0.1',
                      'model':model,'image_size':1600,'max_tokens':512,'timeout':300}
            rows=[];row={'model':model,'backend':'Windows x64 ONNX Runtime CPU' if model==SMALL else 'llama.cpp (hardware fields below)',
                         'cold':None,'warm':rows};report['models'].append(row)
            def recognize_case(case):
                start=time.perf_counter();entry={'name':case['name']}
                try:
                    result=recognize(provider,images[case['path']]);entry['result']=result
                    # 完整候选匹配为主；全文出现仅作诊断，不把子串当款号正确。
                    entry['candidate_exact']=normalize(case.get('expected','')) in [normalize(c) for c in result['codes']]
                    entry['text_contains_expected']=case.get('expected','') in result['text']
                except Exception as exc:entry['error']=str(exc)
                entry['elapsed_ms']=round((time.perf_counter()-start)*1000,3)
                return entry
            try:
                row['cold']=recognize_case(cases[0])
                row['hardware']={k:v for k,v in (runtime.endpoint or {}).items() if k not in ('key','url')}
                for turn in range(rounds):
                    # 不连续重复同图；旋转起点防止 GLM 同图 KV 缓存冒充常规速度。
                    ordered=cases[turn%len(cases):]+cases[:turn%len(cases)]
                    for case in ordered:
                        entry=recognize_case(case);entry['round']=turn+1;rows.append(entry)
                    write_json(out/'benchmark.json',report)
                times=sorted(r['elapsed_ms'] for r in rows)
                row['summary']={'median_ms':statistics.median(times),'mean_ms':statistics.mean(times),
                                'p95_ms':times[max(0,math.ceil(.95*len(times))-1)],
                                'min_ms':min(times),'max_ms':max(times),'requests':len(rows),
                                'candidate_exact':sum(r.get('candidate_exact',False) for r in rows),
                                'errors':sum('error' in r for r in rows)}
                print(json.dumps({'model':model,'cold_ms':row['cold']['elapsed_ms'],**row['summary']},ensure_ascii=False),flush=True)
            finally:
                process=runtime.process;runtime.close()
                row['closed']=runtime.ocr is None and (process is None or process.poll() is not None)
                write_json(out/'benchmark.json',report)
    finally:
        local_inference._runtime=previous;manager.close()
        report['inputs_unchanged']=all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==value for p,value in hashes.items())
        write_json(out/'benchmark.json',report)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--fixtures',type=Path,required=True)
    parser.add_argument('--out',type=Path,default=ROOT/'build/desktop-paddle-ocr/benchmark')
    parser.add_argument('--rounds',type=int,default=3);parser.add_argument('--glm-config',type=Path)
    parser.add_argument('--cpu',action='store_true');parser.add_argument('--threads',type=int,nargs='+',default=[2,4])
    parser.add_argument('--cpu-worker',choices=[SMALL,MEDIUM]);parser.add_argument('--worker-config',type=Path)
    args=parser.parse_args()
    if args.cpu_worker:cpu_worker(args.fixtures,args.worker_config,args.out,args.cpu_worker,args.threads[0],args.rounds)
    elif args.cpu:run_cpu(args.fixtures,args.out,args.rounds,args.threads)
    else:run(args.fixtures,args.out,args.rounds,args.glm_config)
