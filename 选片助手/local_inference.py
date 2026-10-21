"""软件自管的 llama.cpp 子进程，不依赖 LM Studio 或外部模型服务。"""
import atexit
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
import urllib.request

MODEL_IDS={'qwen2.5-vl-7b-q4':'multimodal','glm-ocr-local':'ocr'}


def own_process(process):
    """Windows 引擎被强制结束时由内核收回它启动的模型进程。"""
    if os.name!='nt':return None
    import ctypes as C
    from ctypes import wintypes as W
    class Limits(C.Structure):
        _fields_=[('process_time',C.c_int64),('job_time',C.c_int64),('flags',W.DWORD),('min_working',C.c_size_t),('max_working',C.c_size_t),('active',W.DWORD),('affinity',C.c_size_t),('priority',W.DWORD),('scheduling',W.DWORD)]
    class IO(C.Structure):_fields_=[(name,C.c_uint64) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
    class Extended(C.Structure):_fields_=[('basic',Limits),('io',IO),('process_memory',C.c_size_t),('job_memory',C.c_size_t),('peak_process',C.c_size_t),('peak_job',C.c_size_t)]
    kernel=C.WinDLL('kernel32',use_last_error=True)
    kernel.CreateJobObjectW.argtypes=[C.c_void_p,W.LPCWSTR];kernel.CreateJobObjectW.restype=W.HANDLE
    kernel.SetInformationJobObject.argtypes=[W.HANDLE,C.c_int,C.c_void_p,W.DWORD]
    kernel.AssignProcessToJobObject.argtypes=[W.HANDLE,W.HANDLE]
    kernel.CloseHandle.argtypes=[W.HANDLE]
    handle=kernel.CreateJobObjectW(None,None);info=Extended();info.basic.flags=0x2000
    if not handle or not kernel.SetInformationJobObject(handle,9,C.byref(info),C.sizeof(info)) or not kernel.AssignProcessToJobObject(handle,W.HANDLE(int(process._handle))):
        if handle:kernel.CloseHandle(handle)
        raise OSError('无法建立内置模型进程退出保护')
    return lambda:kernel.CloseHandle(handle)


class LocalInference:
    def __init__(self,config,logs,components=None):
        self.config=Path(config);self.logs=Path(logs);self.lock=threading.RLock()
        self.process=None;self.active=None;self.endpoint=None;self.log=None;self.closed=False;self.release_job=None
        self.components=components

    def settings(self):
        if not self.config.is_file():raise ValueError('尚未部署内置模型，请先运行 setup_local_inference.py')
        data=json.loads(self.config.read_text(encoding='utf8'))
        if not Path(data['executable']).is_file():raise ValueError('内置推理运行时不存在，请重新部署')
        return data

    def _stop(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
            self.process=None
        if self.release_job:self.release_job();self.release_job=None
        if self.log:self.log.close();self.log=None
        self.active=None;self.endpoint=None

    def close(self):
        with self.lock:self.closed=True;self._stop()

    def _start(self,model):
        if self.closed:raise ValueError('内置推理已关闭')
        if self.active==model and self.process and self.process.poll() is None:return
        if self.components:
            self.components.ensure([model]);self.config=self.components.root/'local-models.json'
        data=self.settings();item=data.get('models',{}).get(model)
        if model not in MODEL_IDS or not item:raise ValueError('内置模型未部署：'+str(model))
        for name in ('model','mmproj'):
            if not Path(item[name]).is_file():raise ValueError('内置模型文件缺失：'+name)
        self._stop()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        key=secrets.token_urlsafe(32)
        # 不抢占其他软件的模型；显存不足时只卸载本软件的旧模型并减少 GPU 层数。
        gpu_layers=item.get('gpu_layers',99);offload=True
        try:
            output=subprocess.check_output(['nvidia-smi','--query-gpu=memory.free','--format=csv,noheader,nounits'],text=True,timeout=5,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            free=int(output.splitlines()[0]);need=8500 if MODEL_IDS[model]=='multimodal' else 3500
            if free<need:gpu_layers=8 if free>=2500 else 0;offload=False
        except (OSError,ValueError,subprocess.SubprocessError):gpu_layers=0;offload=False
        command=[data['executable'],'--model',item['model'],'--mmproj',item['mmproj'],
            '--host','127.0.0.1','--port',str(port),'--alias',model,
            '--ctx-size',str(item.get('context',8192)),'--parallel','1','--n-gpu-layers',str(gpu_layers),
            '--threads','8','--batch-size','512','--ubatch-size','256','--jinja','--no-webui',
            '--flash-attn','on','--image-max-tokens','4096']
        if not offload:command+=['--no-mmproj-offload']
        self.logs.mkdir(parents=True,exist_ok=True);self.log=(self.logs/(model+'.log')).open('ab')
        env=os.environ.copy();env['LLAMA_API_KEY']=key
        try:
            self.process=subprocess.Popen(command,stdout=self.log,stderr=self.log,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.release_job=own_process(self.process)
            endpoint={'url':f'http://127.0.0.1:{port}','key':key,'protocol':'chat','gpu_layers':gpu_layers,'mmproj_offload':offload,'local_runtime':True}
            until=time.monotonic()+180
            while time.monotonic()<until:
                if self.process.poll() is not None:raise ValueError('内置模型启动失败，请查看本地推理日志')
                try:
                    req=urllib.request.Request(endpoint['url']+'/health',headers={'Authorization':'Bearer '+key})
                    with urllib.request.urlopen(req,timeout=2) as response:
                        if response.status==200:self.active=model;self.endpoint=endpoint;return
                except OSError:time.sleep(.25)
            raise ValueError('内置模型加载超时，请查看本地推理日志')
        except Exception:self._stop();raise

    @contextmanager
    def resolve(self,provider):
        # 整个请求持锁，另一队列不能在推理中途卸载模型。
        with self.lock:
            self._start(provider['model'])
            yield {**provider,**self.endpoint}


_runtime=None

def configure(root,data,components=None):
    global _runtime
    if _runtime:_runtime.close()
    config=os.environ.get('FRAMEKEEP_LOCAL_MODELS')
    if not config:
        candidates=[Path(data)/'components/local-models.json',Path(root)/'runtime/local-inference/local-models.json',Path(root)/'.runtime/local-inference/local-models.json']
        # 项目内发行程序沿用项目级模型包；便携部署优先程序旁 runtime。
        candidates+=[Path(root).parent.parent/'.runtime/local-inference/local-models.json']
        config=next((p for p in candidates if p.is_file()),candidates[0])
    _runtime=LocalInference(config,Path(data)/'local-inference-logs',components)
    return _runtime

def runtime():
    global _runtime
    if _runtime is None:
        import sys
        root=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]
        configure(root,root/'.framekeep')
    return _runtime

@contextmanager
def resolve(provider):
    if provider.get('protocol')=='local':
        with runtime().resolve(provider) as value:yield value
    else:yield provider

def close():
    if _runtime:_runtime.close()

atexit.register(close)
