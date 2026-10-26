"""用真实 JPEG 经软件现有适配层验证内置模型，输出原始结果和耗时。"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

from asset_analysis import vision
import local_inference
from sku_vision import recognize

SCHEMA={'type':'object','properties':{'garment_type':{'type':'string'},'color':{'type':'string'},'visible_text':{'type':'string'},'uncertainty':{'type':'string'}},'required':['garment_type','color','visible_text','uncertainty'],'additionalProperties':False}
PROMPT='描述图中服装的品类和颜色，用中文填写 garment_type 和 color。visible_text 只逐字写实际可读的产品款号，没有可读款号填空字符串；不推测品牌或隐藏内容。uncertainty 说明看不清的细节，没有则填空字符串。'

def gpu():
    try:return subprocess.check_output(['nvidia-smi','--query-gpu=memory.used,memory.free','--format=csv,noheader,nounits'],text=True,timeout=5,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)).strip()
    except (OSError,subprocess.SubprocessError):return 'unavailable'

def benchmark(root,label,products,out,expected):
    out.mkdir(parents=True,exist_ok=True);runtime=local_inference.configure(root,out)
    paths=[label,*products];before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report={'inputs':before,'runtime':'official llama.cpp b11773 CUDA 12.4; software-owned; no LM Studio','gpu_before_used_free_mib':gpu(),'tests':[]}
    def record(model,path,task):
        provider={'id':'benchmark-'+model,'model':model,'protocol':'local','url':'http://127.0.0.1','json_mode':model!='glm-ocr-local','timeout':300,'max_tokens':768}
        t=time.monotonic();was_loaded=runtime.active==model;row={'model':model,'file':str(path),'task':task,'already_loaded':was_loaded}
        try:
            if task=='sku':row['result']=recognize(provider,path.read_bytes());row['sku_exact']=row['result']['codes']==[expected]
            else:
                row['raw']=vision(provider,path.read_bytes(),PROMPT,SCHEMA);row['result']=json.loads(row['raw'])
                from jsonschema import validate
                validate(row['result'],SCHEMA);row['schema_valid']=True
            row['runtime']={k:v for k,v in runtime.endpoint.items() if k not in ('key','url')};row['gpu_used_free_mib']=gpu()
        except Exception as e:row['error']=str(e)
        row['seconds']=round(time.monotonic()-t,3);report['tests'].append(row)
        (out/'benchmark.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps(row,ensure_ascii=False),flush=True)
    try:
        record('glm-ocr-local',label,'sku')
        record('glm-ocr-local',label,'sku')  # 同图重复，可能命中运行时 KV 缓存，单独标记。
        record('qwen2.5-vl-7b-q4',label,'sku')
        for p in products:record('qwen2.5-vl-7b-q4',p,'attributes')
        record('qwen2.5-vl-7b-q4',label,'sku')
        record('glm-ocr-local',label,'sku')  # 验证两模型切换与重载。
    finally:
        child=runtime.process;runtime.close();report['model_process_exited']=child is None or child.poll() is not None
        report['inputs_unchanged']=all(hashlib.sha256(p.read_bytes()).hexdigest()==before[str(p)] for p in paths)
        report['gpu_after_used_free_mib']=gpu()
        (out/'benchmark.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]);p.add_argument('--label',type=Path,required=True);p.add_argument('--products',type=Path,nargs='+',required=True);p.add_argument('--out',type=Path,default=Path('build/local-model-validation'));p.add_argument('--expected-code',required=True);a=p.parse_args()
    benchmark(a.root,a.label,a.products,a.out,a.expected_code)
