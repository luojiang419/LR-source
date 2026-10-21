"""Everything 官方 SDK/IPC 只读适配。同步 DLL 查询在可终止子进程执行。"""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import asset_search

EXTENSIONS='jpg;jpeg;png;tif;tiff;webp;heic;cr3;cr2;arw;nef;nrw;dng;raf;orf;rw2;pef;srw'


def literal_query(text):
    if not isinstance(text,str) or not 1<=len(text.strip())<=180:raise ValueError('请输入 1–180 字符的文件名或路径片段')
    if any(ord(ch)<32 or ch in '"<>|*?' for ch in text):raise ValueError('文件检索不支持通配符、引号或 Everything 表达式')
    # quoted token 作为单个字面条件；扩展名限制仅由程序添加。
    return f'file: "{text.strip()}" ext:{EXTENSIONS}'


def worker(dll_path,payload):
    dll=ctypes.WinDLL(str(Path(dll_path).resolve()))
    for name,restype,args in [
        ('Everything_SetSearchW',None,[ctypes.c_wchar_p]),
        ('Everything_SetMatchPath',None,[ctypes.c_int]),
        ('Everything_SetMax',None,[ctypes.c_uint32]),
        ('Everything_SetOffset',None,[ctypes.c_uint32]),
        ('Everything_SetSort',None,[ctypes.c_uint32]),
        ('Everything_QueryW',ctypes.c_int,[ctypes.c_int]),
        ('Everything_IsDBLoaded',ctypes.c_int,[]),
        ('Everything_GetMajorVersion',ctypes.c_uint32,[]),
        ('Everything_GetMinorVersion',ctypes.c_uint32,[]),
        ('Everything_GetRevision',ctypes.c_uint32,[]),
        ('Everything_GetBuildNumber',ctypes.c_uint32,[]),
        ('Everything_GetLastError',ctypes.c_uint32,[]),
        ('Everything_GetNumResults',ctypes.c_uint32,[]),
        ('Everything_GetResultFullPathNameW',ctypes.c_uint32,[ctypes.c_uint32,ctypes.c_wchar_p,ctypes.c_uint32]),
    ]:
        fn=getattr(dll,name);fn.restype=restype;fn.argtypes=args
    if not dll.Everything_IsDBLoaded():return {'available':False,'message':'Everything 未运行、索引尚未就绪或当前用户无法访问 IPC'}
    version='.'.join(str(getattr(dll,'Everything_Get'+s)()) for s in ['MajorVersion','MinorVersion','Revision','BuildNumber'])
    if payload.get('probe'):return {'available':True,'version':version,'paths':[]}
    dll.Everything_SetSearchW(literal_query(payload['text']))
    dll.Everything_SetMatchPath(True)
    dll.Everything_SetMax(payload['limit']+1);dll.Everything_SetOffset(payload['offset'])
    dll.Everything_SetSort(3) # EVERYTHING_SORT_PATH_ASCENDING
    if not dll.Everything_QueryW(True):return {'available':False,'message':f'Everything IPC 查询失败（{dll.Everything_GetLastError()}）'}
    paths=[]
    for i in range(dll.Everything_GetNumResults()):
        buffer=ctypes.create_unicode_buffer(32768)
        length=dll.Everything_GetResultFullPathNameW(i,buffer,len(buffer))
        if length>=len(buffer):return {'available':False,'message':'Everything 返回路径超长'}
        paths.append(buffer.value)
    return {'available':True,'version':version,'paths':paths[:payload['limit']],'has_more':len(paths)>payload['limit']}


def worker_main(dll_path):
    try:result=worker(dll_path,json.loads(sys.stdin.buffer.read(8192).decode('utf8')))
    except Exception as exc:result={'available':False,'message':'Everything SDK 不可用：'+str(exc)[:240]}
    sys.stdout.buffer.write(json.dumps(result,ensure_ascii=False).encode('utf8'))


class EverythingSearch:
    def __init__(self,archive,timeout=1.5):
        self.archive=archive;self.timeout=timeout;self.gate=threading.Lock();self.cached=None;self.checked=0
        name='Everything64.dll' if ctypes.sizeof(ctypes.c_void_p)==8 else 'Everything32.dll'
        self.dll=next((p/name for p in [archive.store.root/'runtime/everything',archive.store.root/'.runtime/everything'] if (p/name).is_file()),archive.store.root/'.runtime/everything'/name)
    def _request(self,payload):
        if sys.platform!='win32':return {'available':False,'message':'Everything 仅支持 Windows，已使用本机备案库'}
        if not self.dll.is_file():return {'available':False,'message':'未安装 Everything SDK 适配组件，已使用本机备案库'}
        if not self.gate.acquire(blocking=False):return {'available':False,'message':'Everything 正忙，已使用本机备案库'}
        try:
            command=[sys.executable,'--everything-worker',str(self.dll)] if getattr(sys,'frozen',False) else [sys.executable,str(Path(__file__).resolve()),'--worker',str(self.dll)]
            p=subprocess.run(command,input=json.dumps(payload).encode('utf8'),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=self.timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if p.returncode:raise ValueError('适配进程退出')
            result=json.loads(p.stdout.decode('utf8'))
        except (OSError,ValueError,subprocess.TimeoutExpired):result={'available':False,'message':'Everything 查询超时或失败，已使用本机备案库'}
        finally:self.gate.release()
        self.cached={k:v for k,v in result.items() if k not in ('paths','has_more')};self.checked=time.monotonic()
        return result
    def status(self):
        if self.cached is None or time.monotonic()-self.checked>10:self._request({'probe':True})
        return self.cached or {'available':False,'message':'Everything 正忙'}
    def search(self,text,offset=0,limit=60):
        literal_query(text);offset=int(offset);limit=max(1,min(100,int(limit)))
        if not 0<=offset<=10000:raise ValueError('文件搜索分页范围超限，请缩小文件名或目录条件')
        started=time.perf_counter();result=self._request({'text':text,'offset':offset,'limit':limit})
        source='everything'
        if not result.get('available'):
            source='archive';fallback=asset_search.local_paths(self.archive,text.strip(),offset,limit)
            message=result.get('message','');result.update(fallback)
            result['message']=message+('；'+fallback['message'] if fallback.get('message') else '')
        known=asset_search.known_paths(self.archive,result.get('paths',[]))
        rows=[{'path':p,'asset':known.get(p.casefold()),'identity_verified':False} for p in result.pop('paths',[])]
        return {**result,'rows':rows,'source':source,'offset':offset,'elapsed_ms':round((time.perf_counter()-started)*1000,2),'coverage':'仅 Everything 当前已索引位置' if source=='everything' else '仅本机已备案位置'}


if __name__=='__main__':worker_main(sys.argv[2])
