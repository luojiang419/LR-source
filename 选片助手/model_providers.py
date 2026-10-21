"""分类模型卡片和明确选用；本机密钥用 Windows DPAPI 保存。"""
import base64
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import threading
import uuid
import urllib.request
import urllib.error
from urllib.parse import urlsplit

CATEGORIES={'multimodal','ocr','text'}
PROTOCOLS={'responses','chat','ocr_json','paddleocr','local'}


def validated_url(value):
    url=str(value).strip().rstrip('/');parsed=urlsplit(url)
    if parsed.scheme!='https' and not(parsed.scheme=='http' and parsed.hostname in ('localhost','127.0.0.1','::1')):raise ValueError('接口须用 HTTPS，本机服务可用 HTTP')
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:raise ValueError('接口地址不能包含认证信息、查询串或片段')
    return url


class NoModelRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def seal(value,decode=False):
    if os.name!='nt':
        raise ValueError('密钥本机保护目前需要 Windows')
    class Blob(C.Structure):_fields_=[('size',W.DWORD),('data',C.POINTER(C.c_ubyte))]
    raw=base64.b64decode(value) if decode else value.encode('utf8')
    buf=(C.c_ubyte*len(raw)).from_buffer_copy(raw);source=Blob(len(raw),buf);target=Blob()
    crypt=C.WinDLL('crypt32',use_last_error=True);kernel=C.WinDLL('kernel32',use_last_error=True)
    fn=crypt.CryptUnprotectData if decode else crypt.CryptProtectData
    fn.argtypes=[C.POINTER(Blob),C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p,W.DWORD,C.POINTER(Blob)]
    if not fn(C.byref(source),None,None,None,None,1,C.byref(target)):raise ValueError('API 密钥本机保护失败，请重新输入密钥')
    try:result=C.string_at(target.data,target.size)
    finally:
        kernel.LocalFree.argtypes=[C.c_void_p];kernel.LocalFree(target.data)
    return result.decode('utf8') if decode else base64.b64encode(result).decode('ascii')


class Providers:
    @staticmethod
    def reserved(provider):return bool(provider and provider.get('protocol')=='local' and provider.get('category')=='multimodal')

    def __init__(self,data,legacy=None):
        self.path=Path(data)/'model-providers.json';self.lock=threading.RLock()
        self.data={'providers':[],'selected':{}}
        if self.path.is_file():self.data=json.loads(self.path.read_text(encoding='utf8'))
        elif legacy:
            try:
                c=legacy();saved=self.save({'name':'原 AI 助手','category':'multimodal','protocol':'responses','url':c['url'],'model':c['model'],'key':c['key']});self.select(saved['providers'][0]['id'])
            except (ValueError,OSError):pass

    def _persist(self):
        self.path.parent.mkdir(parents=True,exist_ok=True);temp=self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.data,ensure_ascii=False,indent=2),encoding='utf8');temp.replace(self.path)

    def public(self):
        with self.lock:
            reserved={p['id'] for p in self.data['providers'] if self.reserved(p)}
            selected={k:v for k,v in self.data['selected'].items() if v not in reserved};bindings={k:v for k,v in self.data.get('bindings',{}).items() if v not in reserved}
            if self.data.get('builtin_enabled',False):
                selected={category:p['id'] for category in ('ocr','multimodal') if (p:=self._builtin(category))}
                bindings={}
            return {'providers':[{k:v for k,v in p.items() if k!='secret'}|{'key_set':bool(p.get('secret')),'reserved':self.reserved(p)} for p in self.data['providers']],
                    'selected':selected,'bindings':bindings,'builtin_enabled':self.data.get('builtin_enabled',False)}

    def _editable(self):
        if self.data.get('builtin_enabled',False):raise ValueError('请先关闭“使用内置模型”，再切换或修改 API 配置')

    def _builtin(self,category):
        model={'ocr':'glm-ocr-local'}.get(category)
        return next((p for p in self.data['providers'] if p['category']==category and p['protocol']=='local' and p['model']==model),None)

    def set_builtin(self,enabled):
        if type(enabled) is not bool:raise ValueError('内置模型开关必须为布尔值')
        with self.lock:
            if enabled and not self.data.get('builtin_enabled',False):
                for category,model,name in [('ocr','glm-ocr-local','内置 GLM-OCR')]:
                    if not self._builtin(category):self.save({'name':name,'category':category,'protocol':'local','model':model,'timeout':300,'max_tokens':1200,'image_size':1600,'json_mode':category!='ocr'})
            self.data['builtin_enabled']=enabled;self._persist();return self.public()

    def check_inference(self,provider):
        with self.lock:
            if self.reserved(provider):raise ValueError('内置多模态作为后续开发预留，当前未启用')
            if self.data.get('builtin_enabled',False) and provider.get('protocol')!='local':raise ValueError('已开启内置模型，外部 API 任务请关闭开关后重试')
        return provider

    def save(self,body):
        with self.lock:
            self._editable()
            pid=body.get('id') or uuid.uuid4().hex;old=next((p for p in self.data['providers'] if p['id']==pid),{})
            category=body.get('category');protocol=body.get('protocol');url=validated_url(body.get('url','') if protocol!='local' else 'http://127.0.0.1')
            if category not in CATEGORIES or protocol not in PROTOCOLS:raise ValueError('请选择有效模型分类和接口协议')
            name=str(body.get('name','')).strip();model=str(body.get('model','')).strip()
            if not name or not model or len(name)>100 or len(model)>200:raise ValueError('请填写配置名称和模型标识')
            if protocol=='local':
                from local_inference import MODEL_IDS
                if MODEL_IDS.get(model)!=category:raise ValueError('请选择对应类别的内置模型：qwen2.5-vl-7b-q4 / glm-ocr-local')
            if old and old['category']!=category:raise ValueError('已有卡片不能更换分类，请新建配置')
            key=str(body.get('key','')).strip();secret='' if protocol=='local' else (seal(key) if key else old.get('secret',''))
            item={'id':pid,'name':name,'category':category,'protocol':protocol,'url':url,'model':model,'secret':secret,
                  'timeout':max(10,min(300,int(body.get('timeout',old.get('timeout',90))))),
                  'max_tokens':max(128,min(8192,int(body.get('max_tokens',old.get('max_tokens',2000))))),
                  'image_size':max(480,min(2400,int(body.get('image_size',old.get('image_size',1600))))),
                  'json_mode':bool(body.get('json_mode',old.get('json_mode',True)))}
            if body.get('select') is True and self.reserved(item):raise ValueError('内置多模态作为后续开发预留，当前未启用')
            self.data['providers']=[p for p in self.data['providers'] if p['id']!=pid]+[item]
            if body.get('select') is True:self.data['selected'][category]=pid
            self._persist();return self.public()

    def models(self,body):
        """只获取模型ID，不保存草稿、不发送图片或执行推理。"""
        with self.lock:self._editable()
        old=self.by_id(body['id']) if body.get('id') else None
        if body.get('id') and not old:raise ValueError('配置卡片不存在')
        protocol=body.get('protocol',old.get('protocol') if old else None)
        if protocol=='local':
            from local_inference import MODEL_IDS
            return {'models':[model for model,category in MODEL_IDS.items() if category=='ocr'],'message':'当前启用内置 OCR，多模态作为后续开发预留'}
        if protocol not in ('chat','responses'):raise ValueError('该协议未提供统一模型列表，请手动填写模型标识')
        url=validated_url(body.get('url',old.get('url','') if old else ''))
        for suffix in ('/chat/completions','/responses'):
            if url.endswith(suffix):url=url[:-len(suffix)]
        if not url.endswith('/v1'):url+='/v1'
        key=str(body.get('key','')).strip() or (old.get('key','') if old else '')
        if '\r' in key or '\n' in key:raise ValueError('API 密钥不能包含换行，请重新输入')
        headers={'Accept':'application/json'}
        if key:headers['Authorization']='Bearer '+key
        try:
            request=urllib.request.Request(url+'/models',headers=headers)
            with urllib.request.build_opener(NoModelRedirect()).open(request,timeout=10) as response:
                raw=response.read(2_000_001)
            if len(raw)>2_000_000:raise ValueError('模型列表响应过大，请手动填写模型标识')
            data=json.loads(raw)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in (401,403):raise ValueError(f'获取模型列表 HTTP {exc.code}，请检查 API 密钥及模型列表权限') from None
            if exc.code in (404,405):raise ValueError('服务未提供模型列表接口，请检查地址或手动填写模型标识') from None
            raise ValueError(f'获取模型列表 HTTP {exc.code}，请检查服务地址；也可手动填写模型标识') from None
        except (OSError,UnicodeError,json.JSONDecodeError):raise ValueError('获取模型列表连接超时或响应无效，请检查服务，也可手动填写模型标识') from None
        rows=data.get('data') if isinstance(data,dict) else None
        if not isinstance(rows,list):raise ValueError('模型列表格式无效，请手动填写模型标识')
        models=sorted({r['id'].strip() for r in rows if isinstance(r,dict) and isinstance(r.get('id'),str) and r['id'].strip() and len(r['id'].strip())<=200})
        return {'models':models,'message':f'获取到 {len(models)} 个模型' if models else '服务返回空模型列表，可手动填写模型标识'}

    def select(self,pid):
        with self.lock:
            self._editable()
            p=next((p for p in self.data['providers'] if p['id']==pid),None)
            if not p:raise ValueError('配置卡片不存在')
            if self.reserved(p):raise ValueError('内置多模态作为后续开发预留，当前未启用')
            self.data['selected'][p['category']]=pid;self._persist();return self.public()

    def delete(self,pid):
        with self.lock:
            self._editable()
            self.data['providers']=[p for p in self.data['providers'] if p['id']!=pid]
            self.data['selected']={k:v for k,v in self.data['selected'].items() if v!=pid}
            self.data['bindings']={k:v for k,v in self.data.get('bindings',{}).items() if v!=pid}
            self._persist();return self.public()

    def selected(self,category):
        with self.lock:
            if self.data.get('builtin_enabled',False):
                p=self._builtin(category)
                return self.by_id(p['id']) if p else None
            pid=self.data['selected'].get(category)
            p=next((dict(p) for p in self.data['providers'] if p['id']==pid),None)
            if self.reserved(p):return None
            if p:p['key']=seal(p['secret'],True) if p.get('secret') else '';p.pop('secret',None)
            return p

    def by_id(self,pid):
        with self.lock:
            p=next((dict(p) for p in self.data['providers'] if p['id']==pid),None)
            if p:p['key']=seal(p.pop('secret'),True) if p.get('secret') else '';p.pop('secret',None)
            return p

    def bind(self,purpose,pid):
        if purpose not in ('sku_label','asset_analysis'):raise ValueError('未知模型用途')
        p=self.by_id(pid)
        if not p or p['category']!='multimodal':raise ValueError('请明确选择多模态卡片')
        if self.reserved(p):raise ValueError('内置多模态作为后续开发预留，当前未启用')
        with self.lock:
            self._editable();self.data.setdefault('bindings',{})[purpose]=pid;self._persist()
        return self.public()

    def purpose(self,purpose):
        with self.lock:
            if self.data.get('builtin_enabled',False) and purpose in ('sku_label','asset_analysis'):return self.selected('multimodal')
            p=self.by_id(self.data.get('bindings',{}).get(purpose))
            return None if self.reserved(p) else p
