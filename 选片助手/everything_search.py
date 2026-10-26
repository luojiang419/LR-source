"""Everything 官方 SDK/IPC 只读适配。同步 DLL 查询在可终止子进程执行。"""
import ctypes
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import threading
import time
import stat

import asset_search
from asset_catalog import MEDIA_EXTENSIONS
from file_safety import atomic_write

# Everything 仅提供首批和命名检索加速；媒体归档的完整性仍由本机扫描快照保证。
EXTENSIONS=';'.join(sorted(extension.removeprefix('.') for extension in MEDIA_EXTENSIONS))


def network_mappings():
    """读取本用户已有映射，不连接共享、不更改 Windows 配置。"""
    if sys.platform!='win32':return []
    get_connection=ctypes.windll.mpr.WNetGetConnectionW
    get_connection.argtypes=[ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.POINTER(ctypes.c_uint32)]
    get_connection.restype=ctypes.c_uint32
    drives=ctypes.windll.kernel32.GetLogicalDrives();result=[]
    for index in range(26):
        if not drives & (1<<index):continue
        drive=chr(65+index)+':';size=ctypes.c_uint32(32768);buffer=ctypes.create_unicode_buffer(size.value)
        if get_connection(drive,buffer,ctypes.byref(size))==0:result.append((drive+'\\',buffer.value))
    return result


def root_aliases(root):
    requested=PureWindowsPath(root);aliases=[str(requested)]
    for drive,remote in network_mappings():
        for source,target in ((drive,remote),(remote,drive)):
            try:value=str(PureWindowsPath(target)/requested.relative_to(PureWindowsPath(source)))
            except ValueError:continue
            if value.casefold() not in {item.casefold() for item in aliases}:aliases.append(value)
    return aliases


def canonical_result(path, root, aliases):
    for alias in aliases:
        try:return str(PureWindowsPath(root)/PureWindowsPath(path).relative_to(PureWindowsPath(alias)))
        except ValueError:continue
    raise ValueError('Everything 返回目录范围之外的路径')


def folder_query(root, recursive=True):
    """仅程序提供的绝对目录；尾分隔符防止 photo 匹配 photo2。"""
    value=str(Path(root).absolute()).replace('/','\\').rstrip('\\')+'\\'
    if any(ord(ch)<32 or ch in '"<>|*?' for ch in value):raise ValueError('目录不支持 Everything 查询，使用目录扫描')
    return f'file: {"" if recursive else "parent:"}"{value}" ext:{EXTENSIONS}'


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
        ('Everything_GetTotResults',ctypes.c_uint32,[]),
        ('Everything_SetRequestFlags',None,[ctypes.c_uint32]),
        ('Everything_GetResultSize',ctypes.c_int,[ctypes.c_uint32,ctypes.POINTER(ctypes.c_int64)]),
        ('Everything_GetResultDateModified',ctypes.c_int,[ctypes.c_uint32,ctypes.POINTER(ctypes.c_uint64)]),
        ('Everything_GetResultFullPathNameW',ctypes.c_uint32,[ctypes.c_uint32,ctypes.c_wchar_p,ctypes.c_uint32]),
    ]:
        fn=getattr(dll,name);fn.restype=restype;fn.argtypes=args
    if not dll.Everything_IsDBLoaded():return {'available':False,'message':'Everything 未运行、索引尚未就绪或当前用户无法访问 IPC'}
    version='.'.join(str(getattr(dll,'Everything_Get'+s)()) for s in ['MajorVersion','MinorVersion','Revision','BuildNumber'])
    if payload.get('probe'):return {'available':True,'version':version,'paths':[]}
    aliases=root_aliases(payload['root']) if payload.get('root') else []
    dll.Everything_SetMatchPath(True)
    dll.Everything_SetRequestFlags(1|2|(16|64 if payload.get('metadata') else 0))
    dll.Everything_SetMax(payload['limit']+1);dll.Everything_SetOffset(payload['offset'])
    dll.Everything_SetSort(3) # EVERYTHING_SORT_PATH_ASCENDING
    queries=[folder_query(alias,payload.get('recursive',True))+(' '+literal_query(payload['text']) if payload.get('text') else '') for alias in aliases] if aliases else [literal_query(payload['text'])]
    # 同一共享可能按盘符或 UNC 被索引；选取覆盖最多的同源表示，避免重复式结果。
    best_query=None;best_total=-1
    for query in queries:
        dll.Everything_SetSearchW(query)
        if not dll.Everything_QueryW(True):return {'available':False,'message':f'Everything IPC 查询失败（{dll.Everything_GetLastError()}）'}
        total=dll.Everything_GetTotResults()
        if total>=best_total:best_query,best_total=query,total
    if best_query!=queries[-1]:
        dll.Everything_SetSearchW(best_query)
        if not dll.Everything_QueryW(True):return {'available':False,'message':'Everything IPC 查询失败'}
    paths=[];records=[]
    for i in range(dll.Everything_GetNumResults()):
        buffer=ctypes.create_unicode_buffer(32768)
        length=dll.Everything_GetResultFullPathNameW(i,buffer,len(buffer))
        if length>=len(buffer):return {'available':False,'message':'Everything 返回路径超长'}
        try:path=canonical_result(buffer.value,payload['root'],aliases) if aliases else buffer.value
        except ValueError:continue
        paths.append(path)
        if payload.get('metadata'):
            size=ctypes.c_int64();modified=ctypes.c_uint64()
            if dll.Everything_GetResultSize(i,ctypes.byref(size)) and dll.Everything_GetResultDateModified(i,ctypes.byref(modified)):
                records.append({'path':path,'size':size.value,'modified_ns':(modified.value-116444736000000000)*100})
    return {'available':True,'version':version,'paths':paths[:payload['limit']],'records':records[:payload['limit']],
            'total':dll.Everything_GetTotResults(),'has_more':dll.Everything_GetNumResults()>payload['limit']}


def worker_main(dll_path):
    try:result=worker(dll_path,json.loads(sys.stdin.buffer.read(8192).decode('utf8')))
    except Exception as exc:result={'available':False,'message':'Everything SDK 不可用：'+str(exc)[:240]}
    sys.stdout.buffer.write(json.dumps(result,ensure_ascii=False).encode('utf8'))


class EverythingSearch:
    def __init__(self,archive,timeout=1.5):
        self.archive=archive;self.timeout=timeout;self.gate=threading.Lock();self.cached=None;self.checked=0
        self.settings_path=Path(getattr(archive.store,'data',archive.store.root/'.framekeep'))/'everything-settings.json'
        self.enabled=json.loads(self.settings_path.read_text(encoding='utf8')).get('enabled',True) if self.settings_path.exists() else True
        name='Everything64.dll' if ctypes.sizeof(ctypes.c_void_p)==8 else 'Everything32.dll'
        self.dll=next((p/name for p in [archive.store.root/'runtime/everything',archive.store.root/'.runtime/everything'] if (p/name).is_file()),archive.store.root/'.runtime/everything'/name)
    def _request(self,payload):
        def unavailable(message):
            self.cached={'available':False,'message':message};self.checked=time.monotonic();return dict(self.cached)
        if not self.enabled or os.environ.get('FRAMEKEEP_DISABLE_EVERYTHING')=='1':return unavailable('已停用 Everything 接入，使用本机快速索引')
        if sys.platform!='win32':return unavailable('Everything 仅支持 Windows，已使用本机快速索引')
        if not self.dll.is_file():return unavailable('未安装 Everything SDK，使用本机快速索引')
        if not self.gate.acquire(timeout=self.timeout):return {'available':False,'message':'Everything 正忙，稍后重试'}
        try:
            if not self.enabled:return unavailable('已停用 Everything 接入，使用本机快速索引')
            command=[sys.executable,'--everything-worker',str(self.dll)] if getattr(sys,'frozen',False) else [sys.executable,str(Path(__file__).resolve()),'--worker',str(self.dll)]
            p=subprocess.run(command,input=json.dumps(payload).encode('utf8'),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=self.timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if p.returncode:raise ValueError('适配进程退出')
            result=json.loads(p.stdout.decode('utf8'))
        except (OSError,ValueError,subprocess.TimeoutExpired):result={'available':False,'message':'Everything 查询超时或失败，已使用本机备案库'}
        finally:self.gate.release()
        self.cached={k:v for k,v in result.items() if k not in ('paths','records','total','has_more')};self.checked=time.monotonic()
        return result
    def status(self):
        if self.cached is None or time.monotonic()-self.checked>10:self._request({'probe':True})
        return {**(self.cached or {'available':False,'message':'Everything 正忙'}),'enabled':self.enabled,'forced_off':os.environ.get('FRAMEKEEP_DISABLE_EVERYTHING')=='1','automatic':self.enabled,'mode':'已有索引直接查询，未覆盖目录回退'}

    def set_enabled(self,enabled):
        if not isinstance(enabled,bool):raise ValueError('Everything 调用开关必须为布尔值')
        # 等待已经开始的有界请求完成；返回后不会再启动被禁用的 SDK 查询。
        with self.gate:
            atomic_write(self.settings_path,json.dumps({'enabled':enabled}).encode('utf8'))
            self.enabled=enabled;self.cached=None;self.checked=0
        return self.status()

    def candidates(self,root,*,recursive=True,exclude=(),limit=2048,cancel=None):
        """有界首批加速。索引永不作为完整性或照片身份的证据。"""
        root=Path(root).absolute();limit=max(1,min(2048,int(limit)))
        if cancel is not None and cancel.is_set():return
        try:folder_query(root)
        except ValueError:return
        if self.cached and not self.cached.get('available') and time.monotonic()-self.checked<10:return
        result=self._request({'root':str(root),'offset':0,'limit':limit})
        if not result.get('available'):return
        excluded={Path(p).absolute() for p in exclude};seen=set();parents={root:True}
        for value in result.get('paths',[])[:limit]:
            if cancel is not None and cancel.is_set():return
            try:
                path=Path(value).absolute();relative=path.relative_to(root)
                if not relative.parts or (not recursive and len(relative.parts)!=1):continue
                if any(part.startswith('.') for part in relative.parts):continue
                if path.suffix.lower().lstrip('.') not in EXTENSIONS.split(';'):continue
                if any(path==p or path.is_relative_to(p) for p in excluded):continue
                # 不穿过重解析目录；缓存祖先检查的规模受候选条数限制。
                valid=True
                for parent in reversed(path.parents):
                    if parent==root or not parent.is_relative_to(root):continue
                    if parent not in parents:parents[parent]=not parent.is_symlink() and not parent.is_junction()
                    if not parents[parent]:valid=False;break
                info=path.lstat()
                if not valid or not stat.S_ISREG(info.st_mode) or path.is_symlink() or path.is_junction():continue
                key=str(path).casefold()
                if key in seen:continue
                seen.add(key);yield path
            except (OSError,ValueError):continue
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
