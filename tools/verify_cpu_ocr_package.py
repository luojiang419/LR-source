"""在隔离资料目录通过真实冻结引擎 HTTP 接口验证纯 CPU OCR。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'选片助手'))
from catalog_store import Store
import media_engine as media
from model_providers import Providers
from sku_service import SkuService
from component_setup import ComponentManager,DEFAULT_MODEL_IDS


def verify(engine,config,image,out,expected,mode='direct'):
    out=Path(out).resolve();out.relative_to((ROOT/'build').resolve());out.mkdir(parents=True,exist_ok=True)
    photos=out/'photos';photos.mkdir(exist_ok=True);photo=photos/Path(image).name;shutil.copyfile(image,photo)
    before=hashlib.sha256(photo.read_bytes()).hexdigest()
    data=out/'data';store=Store(data,ROOT);sid=store.ensure_live_session(photos);store.activate(sid)
    store.append_live(sid,[media.capture_record(photos,photo,None,{})]);cid=store.records(sid)[0]['id']
    providers=Providers(data)
    providers.save({'id':'cpu-medium','name':'内置 Medium CPU','model':'pp-ocrv6-medium-local','category':'ocr','protocol':'local','select':True})
    if mode in ('new','upgrade'):providers.set_builtin(True)
    old_weight=data/'components/models/ocr/legacy-test.gguf'
    if mode=='upgrade':
        for provider in providers.data['providers']:
            if provider['id']=='cpu-medium':provider.update(model='glm-ocr-local',name='内置 GLM-OCR')
        providers._persist()
        old_weight.parent.mkdir(parents=True,exist_ok=True);old_weight.write_bytes(b'keep-old-user-model')
    if config is None:
        manager=ComponentManager(data,seeds=[ROOT/'.runtime'])
        try:manager.ensure(DEFAULT_MODEL_IDS)
        finally:manager.close()
    sku=SkuService(store,providers,autostart=False)
    try:sku.set_sku_rules([])  # 本次只验 OCR 草稿，不触发公司款号入库/元数据规则。
    finally:sku.close()
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env=os.environ.copy();env.update(FRAMEKEEP_DATA=str(data),FRAMEKEEP_CANON_REGISTRATION='0')
    if config:env['FRAMEKEEP_LOCAL_MODELS']=str(Path(config).resolve())
    else:env.pop('FRAMEKEEP_LOCAL_MODELS',None)
    env['FRAMEKEEP_EXIFTOOL']=str(media.find_exiftool(ROOT))
    process=subprocess.Popen([str(Path(engine).resolve()),'--port',str(port),'--no-browser'],env=env,
                             creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    request_token=''
    def api(path,body=None):
        req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,
            data=json.dumps(body).encode('utf8') if body is not None else None,headers={'Content-Type':'application/json','X-Framekeep-Token':request_token})
        with urllib.request.urlopen(req,timeout=30) as response:return json.load(response)
    report={}
    try:
        deadline=time.monotonic()+40
        while True:
            if process.poll() is not None:raise RuntimeError('打包引擎提前退出：'+str(process.returncode))
            try:report['health']=api('/api/health');break
            except OSError:
                if time.monotonic()>deadline:raise
                time.sleep(.25)
        request_token=api('/api/state')['request_token']
        state=api('/api/providers');selected=next(p for p in state['providers'] if p['id']==state['selected']['ocr'])
        assert selected['model']=='pp-ocrv6-medium-local',selected
        assert all(p['model']!='glm-ocr-local' for p in state['providers'] if p['protocol']=='local')
        report['default_model']=selected['model'];report['builtin_enabled']=state['builtin_enabled']
        if mode=='upgrade':
            assert selected['id']=='cpu-medium'
            report['legacy_card_id_preserved']=True;report['old_weights_preserved']=old_weight.read_bytes()==b'keep-old-user-model'
        report['provider_test']=api('/api/providers/test',{'id':'cpu-medium'})
        start=time.perf_counter();report['parse']=api('/api/sku/parse',{'session_id':sid,'capture_id':cid})
        report['parse_ms']=round((time.perf_counter()-start)*1000,2)
        reading=report['parse']['reading']
        assert expected in reading['codes'],reading
        assert reading['auxiliary']['backend']=='CPUExecutionProvider'
        report['source_unchanged']=before==hashlib.sha256(photo.read_bytes()).hexdigest()
        assert report['source_unchanged']
        capi=Path(engine).parent/'_internal/onnxruntime/capi'
        report['gpu_providers_absent']=not any(capi.glob('*providers_cuda*')) and not any(capi.glob('*providers_tensorrt*'))
        assert report['gpu_providers_absent']
        report['ok']=True
    finally:
        process.terminate();process.wait(timeout=15)
        report['test_process_stopped']=process.poll() is not None
        (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({k:v for k,v in report.items() if k!='parse'},ensure_ascii=False),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--engine',type=Path,required=True);parser.add_argument('--config',type=Path)
    parser.add_argument('--mode',choices=['direct','new','upgrade'],default='direct')
    parser.add_argument('--image',type=Path,required=True);parser.add_argument('--out',type=Path,required=True);parser.add_argument('--expected',required=True)
    args=parser.parse_args();verify(args.engine,args.config,args.image,args.out,args.expected,args.mode)
