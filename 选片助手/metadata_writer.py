"""受控 XMP 写入；内存生成与校验、原文件直接写回、字段级恢复。"""
import hashlib
import json
import os
from pathlib import Path
import base64
import io
import subprocess
import re
import threading
import tempfile
import queue
import weakref
import uuid
from contextlib import contextmanager,nullcontext
from PIL import Image
import media_engine as media
from file_safety import write_verified
from metadata_history import save_record
from rating_xmp import sidecar_paths,load_sidecar,sidecar_metadata

CONFIG='''%Image::ExifTool::UserDefined = (
 'Image::ExifTool::XMP::Main' => { fk => { SubDirectory => { TagTable => 'Image::ExifTool::UserDefined::Framekeep' } } },
 'Image::ExifTool::Exif::Main' => {
  0x9c9e => { Name => 'XPKeywords', Format => 'undef', Writable => 'int8u', WriteGroup => 'IFD0',
   ValueConv => '$self->Decode($val,"UTF16","II")', ValueConvInv => '$self->Encode($val,"UTF16","II") . "\\0\\0"' },
  0x9c9f => { Name => 'XPSubject', Format => 'undef', Writable => 'int8u', WriteGroup => 'IFD0',
   ValueConv => '$self->Decode($val,"UTF16","II")', ValueConvInv => '$self->Encode($val,"UTF16","II") . "\\0\\0"' },
 },
);
%Image::ExifTool::UserDefined::Framekeep = (
 GROUPS => { 0 => 'XMP', 1 => 'XMP-fk', 2 => 'Image' },
 NAMESPACE => { fk => 'https://framekeep.local/ns/asset/1.0/' }, WRITABLE => 'string',
 AssetId => {}, FileId => {}, SKU => {}, ShootType => {}, ProjectId => {}, SegmentId => {}, MetadataRevision => {}, Multidimensional => {},
 BaseSKU => {}, ParentSKU => {}, ShootVersion => {}, PhotoRole => {}, ShootDate => {}, AssignmentState => {},
 DecisionId => {}, DecisionParents => {}, DecisionSourceId => {}, MetadataSchemaVersion => {}, CatalogScopeId => {},
 Customer => {}, Brand => {}, GroupRevision => {}, GroupProductCount => {}, GroupLabelCount => {}, GroupFileCount => {}, GroupFilesDigest => {}, GroupMembersDigest => {}, GroupManifestScopeId => {},
);
1;
'''
FIELDS=['AssetId','FileId','SKU','ShootType','ProjectId','SegmentId','MetadataRevision','Multidimensional']
FIELDS += ['BaseSKU','ParentSKU','ShootVersion','PhotoRole','ShootDate','AssignmentState','DecisionId','DecisionParents','DecisionSourceId',
           'MetadataSchemaVersion','CatalogScopeId','Customer','Brand','GroupRevision','GroupProductCount','GroupLabelCount','GroupFileCount','GroupFilesDigest','GroupMembersDigest','GroupManifestScopeId']
DESCRIPTIVE={'Title':'XMP-dc:Title','Description':'XMP-dc:Description','Label':'XMP-xmp:Label'}
def field_text(value):return '' if value is None else str(value)

def windows_text(path):
    """Windows XP属性实际为UTF-16LE；通用ExifTool的UCS2读取会丢失扩展字符。"""
    if Path(path).suffix.lower() not in ('.jpg','.jpeg'):return {}
    try:
        with Image.open(path) as image:exif=image.getexif()
        result={}
        for tag,name in ((40094,'XPKeywords'),(40095,'XPSubject')):
            value=exif.get(tag)
            if isinstance(value,bytes):result['IFD0:'+name]=value.decode('utf-16-le').rstrip('\0')
        return result
    except (OSError,ValueError):return {}


def metadata_json_types(value,tag=''):
    """JSONQ 保全可编辑文本；其它字段沿用 ExifTool 默认 JSON 数字／布尔类型。"""
    if isinstance(value,dict):return {key:metadata_json_types(item,key) for key,item in value.items()}
    if isinstance(value,list):return [metadata_json_types(item,tag) for item in value]
    if not isinstance(value,str) or tag.startswith(('XMP-fk:','XMP-dc:')) or tag in ('IFD0:XPSubject','IFD0:XPKeywords','XMP-xmp:Label','SourceFile'):return value
    if value.lower() in ('true','false'):return value.lower()=='true'
    if re.fullmatch(r'-?(\d|[1-9]\d{1,14})(\.\d{1,16})?(e[-+]?\d{1,3})?',value,re.I):
        return float(value) if re.search(r'[.e]',value,re.I) else int(value)
    return value
EMBEDDED_RAW={'.cr3','.arw'}

def raw_integrity(path,*,suffix=None):
    """校验感光数据和相机内嵌预览；CR3 另校验全部 mdat 负载。"""
    import rawpy
    try:
        with rawpy.imread(io.BytesIO(path) if isinstance(path,bytes) else str(path)) as raw:
            sensor=raw.raw_image
            result={'shape':list(sensor.shape),'dtype':str(sensor.dtype),'sensor':hashlib.sha256(sensor.tobytes()).hexdigest()}
            thumb=raw.extract_thumb();data=thumb.data if isinstance(thumb.data,bytes) else thumb.data.tobytes()
            result['preview']=hashlib.sha256(data).hexdigest()
    except Exception:raise ValueError('RAW 感光数据或内嵌预览无法验证，未写入') from None
    if (suffix or ('' if isinstance(path,bytes) else Path(path).suffix)).lower()=='.cr3':
        digest=hashlib.sha256();sizes=[];total=len(path) if isinstance(path,bytes) else Path(path).stat().st_size
        with (io.BytesIO(path) if isinstance(path,bytes) else open(path,'rb')) as f:
            pos=0
            while pos<total:
                f.seek(pos);header=f.read(8)
                if len(header)!=8:raise ValueError('CR3 容器不完整')
                size=int.from_bytes(header[:4],'big');head=8
                if size==1:size=int.from_bytes(f.read(8),'big');head=16
                elif size==0:size=total-pos
                if size<head or pos+size>total:raise ValueError('CR3 数据块长度无效')
                if header[4:]==b'mdat':
                    remaining=size-head;sizes.append(remaining)
                    while remaining:
                        block=f.read(min(1024*1024,remaining));digest.update(block);remaining-=len(block)
                        if not block:raise ValueError('CR3 图像块读取不完整')
                pos+=size
        if not sizes:raise ValueError('CR3 缺少图像数据块')
        result.update(mdat_sizes=sizes,mdat=digest.hexdigest())
    return result

def sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def obsolete_reserved_keywords(journal):
    """由旧版已校验日志判断遗留受管关键词；不接管用户显式编辑过的关键词。"""
    before=journal.get('before') or {};after=journal.get('after') or {}
    if journal.get('restore') or 'Keywords' in after:return False
    subjects=journal.get('subjects_before') or [];subjects=subjects if isinstance(subjects,list) else [subjects]
    for key,prefix in (('SKU','SKU:'),('ShootType','ShootType:')):
        known={prefix+str(v) for v in (before.get(key),after.get(key)) if v}
        if any(isinstance(value,str) and value.startswith(prefix) and value not in known for value in subjects):return True
    return any(value.strip().startswith('SKU:') and value.strip()!='SKU:'+str(after.get('SKU') or '')
               for value in str(after.get('WindowsKeywords') or '').split(';'))


def jpeg_thumbnail(source,metadata):
    """ExifTool 已解析的 JPEG 绝对缩略图范围；不能证明范围时由原提取器处理。"""
    locations=[(metadata.get(group+':ThumbnailOffset'),metadata.get(group+':ThumbnailLength')) for group in ('IFD0','IFD1') if group+':ThumbnailOffset' in metadata]
    if len(locations)!=1:return None
    offset,length=locations[0]
    if type(offset) is not int or type(length) is not int or offset<0 or not 4<=length<=8_000_000:return None
    if isinstance(source,bytes):
        if not source.startswith(b'\xff\xd8') or offset+length>len(source):return None
        content=source[offset:offset+length]
    else:
        if Path(source).suffix.lower() not in ('.jpg','.jpeg'):return None
        with open(source,'rb') as stream:
            if stream.read(2)!=b'\xff\xd8':return None
            stream.seek(offset);content=stream.read(length)
    return content if len(content)==length and content.startswith(b'\xff\xd8') and content.endswith(b'\xff\xd9') else None

class MetadataWriter:
    def __init__(self,root,data):
        self.root=Path(root);self.data=Path(data);self.config=self.data/'framekeep-exiftool.config'
        self.config.write_text(CONFIG,encoding='utf8');self.lock=threading.RLock()
        self._input_snapshot=threading.local()
        self.path_locks={}
        from archive_resources import PREVIEW_WORKERS
        self.metadata_workers=PREVIEW_WORKERS
        self._tool=None;self._read_sessions=[];self._read_pool=queue.LifoQueue();self._edit_pool=queue.LifoQueue();self._edit_sessions=[]
        self._read_cleanup=weakref.finalize(self,self._close_read_sessions,self._read_sessions)
        self._edit_cleanup=weakref.finalize(self,self._close_read_sessions,self._edit_sessions)
    @staticmethod
    def _close_read_sessions(sessions):
        for session in sessions:session.close()
    def close(self):
        self._close_read_sessions(self._read_sessions)
        self._close_read_sessions(self._edit_sessions)
    def _memory_edit(self,args,content):
        from metadata_memory import MemoryEditor
        prepared=[]
        for arg in args:
            match=re.fullmatch(r'(-[\w:.-]+)<=(.*)',str(arg))
            prepared.append(match[1]+'='+Path(match[2]).read_bytes().decode('utf8') if match else str(arg))
        with self.lock:
            if not self._edit_sessions:
                for _ in range(self.metadata_workers):
                    editor=MemoryEditor(self._exiftool(),self.config)
                    self._edit_sessions.append(editor);self._edit_pool.put(editor)
        editor=self._edit_pool.get(timeout=180)
        try:
            if editor.available():return editor.edit(prepared,content)
        finally:self._edit_pool.put(editor)
    def _exiftool(self):
        with self.lock:
            if self._tool is None:self._tool=media.find_exiftool(self.root).resolve()
            return self._tool
    def _read_request(self,args):
        # 仅只读查询复用进程；图片编辑仍在内存中使用原有独立执行及完整验证。
        args=list(map(str,args))
        if any('\n' in a or '\r' in a for a in args):raise ValueError('元数据参数不能包含换行')
        args[-1]=str(Path(args[-1]).resolve())
        with self.lock:
            if not self._read_sessions:
                for _ in range(self.metadata_workers):
                    session=media.ExifToolSession(self._exiftool(),startup_args=('-config',str(self.config.resolve())))
                    self._read_sessions.append(session);self._read_pool.put(session)
        session=self._read_pool.get(timeout=180)
        try:return session.execute(['-charset','filename=UTF8',*args])
        finally:self._read_pool.put(session)
    @contextmanager
    def file_lock(self,path,*,timeout=None):
        # RAW/JPG 及其 XMP 共用锁；不同图片的慢盘 I/O 不互相阻塞。
        key=Path(path).absolute()
        if key.suffix.lower()=='.xmp':key=key.with_suffix('')
        if key.suffix.lower() in media.IMAGE_EXTS|media.RAW_EXTS|{'.webp'}:key=key.with_suffix('')
        key=str(key).casefold()
        with self.lock:
            entry=self.path_locks.setdefault(key,[threading.RLock(),0]);entry[1]+=1
        acquired=False
        try:
            acquired=entry[0].acquire() if timeout is None else entry[0].acquire(timeout=timeout)
            if not acquired:
                from file_safety import MetadataBusyError
                raise MetadataBusyError('同张图片仍有文件写回，已让行其它图片：'+str(path))
            yield
        finally:
            if acquired:entry[0].release()
            with self.lock:
                entry[1]-=1
                if not entry[1]:self.path_locks.pop(key,None)
    @staticmethod
    def discard_prepared(temporary):
        """兼容旧 journal 的工作目录，只删除程序登记的临时路径。"""
        temporary=Path(temporary)
        if temporary.parent.name.startswith('.framekeep-write-'):
            temporary.unlink(missing_ok=True)
            if temporary.parent.is_dir():temporary.parent.rmdir()
        elif temporary.name.startswith('.framekeep-write-'):
            temporary.unlink(missing_ok=True)
    def run(self,*args):
        env=os.environ.copy()
        for name in ['LANG','LC_ALL','LC_CTYPE']:env.pop(name,None)
        payload='\n'.join(map(str,args))+'\n'
        if any('\n' in str(a) or '\r' in str(a) for a in args):raise ValueError('元数据参数不能包含换行')
        result=subprocess.run([str(self._exiftool()),'-config',str(self.config),'-charset','filename=UTF8','-@','-'],input=payload.encode('utf8'),capture_output=True,timeout=180,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:raise ValueError('ExifTool 写入/读取失败：'+result.stderr.decode('utf8',errors='replace')[-240:])
        return result.stdout.decode('utf-8-sig')
    def binary(self,*args,input_data):
        """ExifTool 标准输入/输出只传内存图像；磁盘仅保存 UTF-8 参数文本。"""
        payload='\n'.join(map(str,args))+'\n'
        if any('\n' in str(a) or '\r' in str(a) for a in args):raise ValueError('元数据参数不能包含换行')
        if input_data is None and args and args[0]=='-b':return self._read_request(args)
        compatible=input_data is not None and (input_data.startswith(b'\xff\xd8') or input_data.lstrip(b'\xef\xbb\xbf \r\n\t').startswith(b'<'))
        if compatible and len(input_data)<=536870912 and args[:2]==('-o','-') and args[-1]=='-':
            prepared=self._memory_edit(args[2:-1],input_data)
            if prepared is not None:return prepared
        env=os.environ.copy()
        for name in ['LANG','LC_ALL','LC_CTYPE']:env.pop(name,None)
        with tempfile.NamedTemporaryFile(dir=self.data,prefix='.framekeep-args-',suffix='.txt',delete=False) as stream:
            arguments=Path(stream.name);stream.write(payload.encode('utf8'))
        try:
            result=subprocess.run([str(self._exiftool()),'-config',self.config.name,'-charset','filename=UTF8','-@',arguments.name],cwd=self.data,input=input_data,capture_output=True,timeout=180,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if result.returncode:raise ValueError('ExifTool 内存处理失败：'+result.stderr.decode('utf8',errors='replace')[-240:])
            return result.stdout
        finally:arguments.unlink(missing_ok=True)
    def read(self,path,full=False):
        if not isinstance(path,bytes) and Path(path).suffix.lower()=='.xmp':load_sidecar(path)
        tags=['-all:all'] if full else ['-XMP-fk:all','-XMP-dc:all','-XMP-xmp:Rating','-XMP-xmp:Label','-XPSubject','-XPKeywords','-DateTimeOriginal','-Orientation']
        memory_input=isinstance(path,bytes) and (path.startswith(b'\xff\xd8') or path.lstrip(b'\xef\xbb\xbf \r\n\t').startswith(b'<'))
        value=self._memory_edit(['__FRAMEKEEP_READ__',*[tag.lstrip('-') for tag in tags]],path) if memory_input else None
        if value is None:
            # JSONQ 防止 "123.00"、"1e3"、"TRUE" 等真实文本被 JSON 改写。
            value=self.binary('-G1','-s','-json','-api','StructFormat=JSONQ',*tags,'-',input_data=path) if isinstance(path,bytes) else self._read_request(['-G1','-s','-json','-api','StructFormat=JSONQ',*tags,path])
            result=metadata_json_types(json.loads(value.decode('utf-8-sig'))[0])
        else:result=json.loads(value.decode('utf-8-sig'))[0]
        if result.get('ExifTool:Error'):raise ValueError('ExifTool 读取失败：'+str(result['ExifTool:Error'])[:240])
        return result
    def edit(self,target,args):
        target=Path(target)
        snapshot=getattr(self._input_snapshot,'value',None)
        content=snapshot[1] if snapshot and snapshot[0]==target else target.read_bytes() if target.exists() else b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about=""/></rdf:RDF></x:xmpmeta>'
        result=self.binary('-o','-',*args,'-',input_data=content)
        if not result:raise ValueError('ExifTool 内存结果为空，未写回')
        return result
    @contextmanager
    def _snapshot_for_edit(self,path,content):
        previous=getattr(self._input_snapshot,'value',None);self._input_snapshot.value=(Path(path),content)
        try:yield
        finally:self._input_snapshot.value=previous
    @staticmethod
    def commit(payload,target,journal):
        if hashlib.sha256(payload).hexdigest()!=journal['after_hash']:raise ValueError('内存元数据结果已变化，未写回')
        write_verified(payload,target,journal['before_hash'])
    def verify_committed(self,journal):
        """以磁盘文件回读作为成功边界，内存校验不能代替实际落盘。"""
        for entry in [journal,*journal.get('sidecars',[])]:
            actual=self.read(entry['target']);wanted=entry['after']
            if any(field_text(actual.get('XMP-fk:'+key))!=field_text(wanted.get(key)) for key in FIELDS):
                raise ValueError('文件写后回读 SKU/备案字段不一致')
            if 'Rating' in wanted and actual.get('XMP-xmp:Rating')!=wanted['Rating']:
                raise ValueError('文件写后回读星级不一致')
            if 'WindowsSubject' in wanted and Path(entry['target']).suffix.lower() in ('.jpg','.jpeg'):
                if any(field_text(actual.get(tag))!=field_text(wanted.get(key)) for key,tag in [('WindowsSubject','IFD0:XPSubject'),('WindowsKeywords','IFD0:XPKeywords')]):
                    raise ValueError('文件写后回读 Windows 图片属性不一致')
    def read_asset(self,path,*,embedded=None):
        path=Path(path);value=dict(embedded) if embedded is not None else self.read(path)
        file_id=value.get('XMP-fk:FileId')
        for side in sidecar_paths(path):
            metadata=self.read(side)
            # 同名 stem.xmp 可属于 RAW；JPG 的文件身份只能来自自身内嵌
            # 或完整文件名侧车，不能把 RAW 的组件身份借给尚未补写的 JPG。
            if path.suffix.lower() in ('.jpg','.jpeg') and side==path.with_suffix('.xmp') and metadata.get('XMP-fk:FileId'):
                # JPG-only目录的新版XMP可完整恢复FileId；存在配对RAW时，
                # stem.xmp属于RAW，JPG仍只使用自己的内嵌/完整文件名侧车身份。
                raw_peer=bool(file_id)
                if not raw_peer:
                    try:
                        raw_peer=any(Path(e.name).stem.casefold()==path.stem.casefold() and Path(e.name).suffix.lower() in media.RAW_EXTS for e in os.scandir(path.parent))
                    except OSError:raw_peer=True
                if raw_peer:metadata.pop('XMP-fk:FileId',None)
            value.update({k:v for k,v in metadata.items() if k.startswith(('XMP-fk:','XMP-xmp:','XMP-dc:'))})
        if file_id and path.suffix.lower() in ('.jpg','.jpeg'):value['XMP-fk:FileId']=file_id
        return value
    def write_rating(self,path,rating):
        """评级结果在内存校验后直接写回，不创建临时整图。"""
        path=Path(path)
        if type(rating) is not int or rating not in range(6):raise ValueError('星级须为 0–5')
        if not path.is_file():raise ValueError('照片离线或不存在，星级已入库，等待文件恢复')
        targets=list(dict.fromkeys([path.with_suffix('.xmp'),*sidecar_paths(path)]))
        if path.suffix.lower() in ('.jpg','.jpeg'):targets.append(path)
        with self.file_lock(path):
            # 款号可能先于星级写入 JPG；后建的评级侧车也须带上已确认款号。
            embedded=self.read(path) if path.suffix.lower() in ('.jpg','.jpeg') else {}
            source_sku=embedded.get('XMP-fk:SKU') or sidecar_metadata(path,grouped=True).get('XMP-fk:SKU')
            for target in targets:
                before_hash=sha(target) if target.exists() else None
                before=self.read(target) if target.exists() else {}
                sku=source_sku if target!=path and not before.get('XMP-fk:SKU') else None
                if before.get('XMP-xmp:Rating')==rating and not sku:continue
                args=[f'-XMP-xmp:Rating={rating}']
                old={'Rating':before.get('XMP-xmp:Rating')};new={'Rating':rating}
                if sku:
                    subjects=before.get('XMP-dc:Subject',[])
                    subjects=subjects if isinstance(subjects,list) else [subjects]
                    keywords=list(dict.fromkeys([s for s in subjects if not s.startswith('SKU:')]+[f'SKU:{sku}']))
                    args += [f'-XMP-fk:SKU={sku}',*[f'-XMP-dc:Subject={s}' for s in keywords]]
                    old.update(SKU=before.get('XMP-fk:SKU'),Keywords=subjects)
                    new.update(SKU=sku,Keywords=keywords)
                pixels=None
                if target==path:
                    with Image.open(path) as im:pixels=hashlib.sha256(im.tobytes()).digest()
                payload=self.edit(target,args)
                actual=self.read(payload)
                if actual.get('XMP-xmp:Rating')!=rating:raise ValueError('星级写入读回不一致')
                if sku:
                    subjects=actual.get('XMP-dc:Subject',[])
                    subjects=subjects if isinstance(subjects,list) else [subjects]
                    if actual.get('XMP-fk:SKU')!=sku or subjects!=keywords:raise ValueError('星级侧车款号写入读回不一致')
                if pixels is not None:
                    with Image.open(io.BytesIO(payload)) as im:
                        if hashlib.sha256(im.tobytes()).digest()!=pixels:raise ValueError('图像数据发生变化，未写回星级')
                journal={'before':old,'after':new,
                         'before_hash':before_hash,'after_hash':hashlib.sha256(payload).hexdigest(),'target':str(target),
                         'mode':'embedded' if target==path else 'sidecar','write_method':'direct-memory-v1'}
                identity=uuid.uuid4().hex
                save_record(self.data,identity,journal,source=path)
                self.commit(payload,target,journal)
                save_record(self.data,identity,journal,source=path,status='applied')
    @contextmanager
    def exclusive(self,path):
        if os.name!='nt':
            yield;return
        import ctypes as C
        from ctypes import wintypes as W
        kernel=C.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD,W.DWORD,W.HANDLE];kernel.CreateFileW.restype=W.HANDLE
        kernel.CloseHandle.argtypes=[W.HANDLE]
        handle=kernel.CreateFileW(str(path),0x80000000,5,None,3,0,None)
        if handle==C.c_void_p(-1).value:
            error=C.WinError(C.get_last_error());error.filename=str(path);raise error
        try:yield
        finally:kernel.CloseHandle(handle)
    def capability(self,path,mode):
        if Path(path).suffix.lower() in ('.jpg','.jpeg'):return 'embedded'
        if Path(path).suffix.lower() in media.IMAGE_EXTS|{'.webp'}:return 'sidecar'
        if Path(path).suffix.lower() in media.RAW_EXTS and mode=='sidecar':return 'sidecar'
        if Path(path).suffix.lower() in EMBEDDED_RAW:return 'embedded'
        return 'unsupported'
    @staticmethod
    def owned(meta):return {k:meta.get('XMP-fk:'+k) for k in FIELDS}
    @staticmethod
    def mirrored_sidecars(path,fields=None):
        path=Path(path);sides=sidecar_paths(path)
        if not sides and path.suffix.lower() in ('.jpg','.jpeg'):
            # 业务随行信息使用常见同名XMP；RAW配对及既有描述编辑保留完整文件名规则。
            descriptive=any(key in (fields or {}) for key in (*DESCRIPTIVE,'Keywords'))
            raw_peer=any(path.with_suffix(ext).is_file() for suffix in media.RAW_EXTS for ext in ((suffix,) if os.name=='nt' else (suffix,suffix.upper())))
            sides=[Path(str(path)+'.xmp') if descriptive or raw_peer else path.with_suffix('.xmp')]
        return sides
    def write_sidecars(self,path,fields,expected_sha,*,targets):
        """源图字段已一致时只准备侧车，仍沿用身份保护及整批落盘校验。"""
        prepared=[]
        for side in targets:
            prior=self.read(side) if side.exists() else {};mirror=dict(fields)
            if prior.get('XMP-fk:AssetId') and str(prior['XMP-fk:AssetId']).replace('-','')!=str(fields.get('AssetId')).replace('-',''):
                raise ValueError('侧车已属于其他资产，未覆盖：'+side.name)
            if prior.get('XMP-fk:FileId'):mirror['FileId']=prior['XMP-fk:FileId']
            prepared.append(self.write(path,mirror,expected_sha,_target=side))
        payload,target,journal=prepared[0];journal['sidecars']=[]
        for content,_,entry in prepared[1:]:
            entry['content']=base64.b64encode(content).decode('ascii');journal['sidecars'].append(entry)
        return payload,target,journal
    def write(self,path,fields,expected_sha,*,mode='sidecar',restore=None,applied=None,sidecars_restore=None,_target=None,jpeg_fast=False):
        path=Path(path);cap='sidecar' if _target is not None else self.capability(path,mode)
        if cap=='unsupported':raise ValueError('此格式尚未开放内嵌写入；RAW 可明确选择 XMP 伴随模式')
        with self.file_lock(path):
            original=path.read_bytes() if cap=='embedded' and path.suffix.lower() in ('.jpg','.jpeg') and path.stat().st_size<=64*1024*1024 else None
            original_hash=hashlib.sha256(original).hexdigest() if original is not None else sha(path)
            if original_hash!=expected_sha:raise ValueError('文件已被其他程序修改，请重新预览')
            sides=self.mirrored_sidecars(path,fields) if _target is None else sidecar_paths(path)
            target=Path(_target) if _target is not None else (sides[-1] if sides else path.with_suffix('.xmp')) if cap=='sidecar' else path
            # RAW/JPG 共用同名 sidecar 时不覆盖不属于该 RAW 的身份。
            before=self.read(original if original is not None and target==path else target,full=True) if target.exists() else {}
            prior_id=before.get('XMP-fk:FileId')
            if cap=='sidecar' and prior_id and str(prior_id).replace('-','')!=str(fields.get('FileId')).replace('-',''):raise ValueError('同名 XMP 已属于其他文件，请先处理冲突')
            before_hash=original_hash if target==path else sha(target) if target.exists() else None
            subjects=before.get('XMP-dc:Subject',[]);subjects=subjects if isinstance(subjects,list) else [subjects]
            old=self.owned(before)
            descriptive={key for key in DESCRIPTIVE if key in fields or restore is not None and key in restore}
            for key in descriptive:old[key]=before.get(DESCRIPTIVE[key]) or ''
            managed=[f'SKU:{old["SKU"]}' if old.get('SKU') else None,f'ShootType:{old["ShootType"]}' if old.get('ShootType') else None]
            manages_keywords='Keywords' in fields or restore is not None and 'Keywords' in restore
            prefixes=tuple(prefix for key,prefix in (('SKU','SKU:'),('ShootType','ShootType:')) if key in fields)
            intended={prefix+str(fields[key]) for key,prefix in (('SKU','SKU:'),('ShootType','ShootType:')) if fields.get(key)}
            if restore is None and not manages_keywords and prefixes and any(isinstance(s,str) and s.startswith(prefixes) and s not in managed and s not in intended for s in subjects):
                # 旧副本可能只有关键词、缺少私有SKU字段。完整记录旧关键词以支持字段级撤销。
                fields=dict(fields,Keywords=[s for s in subjects if s not in managed and not (isinstance(s,str) and s.startswith(prefixes))]);manages_keywords=True
            if manages_keywords:old['Keywords']=[s for s in subjects if s not in managed]
            new={**old,**fields}
            manages_rating='Rating' in fields or (restore is not None and 'Rating' in restore)
            if manages_rating:
                old['Rating']=before.get('XMP-xmp:Rating')
                if fields.get('Rating') is not None and (type(fields['Rating']) is not int or fields['Rating'] not in range(6)):raise ValueError('星级须为 0–5')
            if restore is not None:
                if any((old.get(k)!=applied.get(k)) if k=='Rating' else (field_text(old.get(k))!=field_text(applied.get(k))) for k in old):raise ValueError('备案字段已被后来修改，不能直接撤销')
                new=restore
            new=dict(new)
            # Windows 图片属性不展示自定义 XMP-fk 命名空间。JPG 同步标准
            # XP 字段；字段级日志同时保存原值，撤销不能遗留旧款号。
            windows=cap=='embedded' and path.suffix.lower() in ('.jpg','.jpeg') and (restore is None or 'WindowsSubject' in restore)
            if windows:
                # ExifTool JSON 会把纯数字文本（包括 "0"）返回为数值；XP 字段仍按文本比较和保存。
                old['WindowsSubject']=field_text(before.get('IFD0:XPSubject'))
                old['WindowsKeywords']=field_text(before.get('IFD0:XPKeywords'))
                if restore is not None:
                    if any(old[k]!=field_text(applied.get(k)) for k in ('WindowsSubject','WindowsKeywords')):
                        raise ValueError('Windows 属性已被后来修改，不能直接撤销')
                else:
                    keywords=[s.strip() for s in old['WindowsKeywords'].split(';') if s.strip()]
                    if 'SKU' in fields:keywords=[s for s in keywords if not s.startswith('SKU:')]
                    elif old.get('SKU'):keywords=[s for s in keywords if s!=f'SKU:{old["SKU"]}']
                    if new.get('SKU'):keywords.append(f'SKU:{new["SKU"]}')
                    new['WindowsSubject']=new.get('SKU') or ('' if 'SKU' in fields and old['WindowsSubject']==str(old.get('SKU') or '') else old['WindowsSubject'])
                    new['WindowsKeywords']=';'.join(dict.fromkeys(keywords))
            for identity in ('AssetId','FileId'):
                if new.get(identity):new[identity]=str(uuid.UUID(str(new[identity])))
            managed=[f'SKU:{old["SKU"]}' if old.get('SKU') else None,f'ShootType:{old["ShootType"]}' if old.get('ShootType') else None]
            subjects=list(new.get('Keywords') or []) if manages_keywords else [s for s in subjects if s not in managed]
            subjects+=([f'SKU:{new["SKU"]}'] if new.get('SKU') else [])+([f'ShootType:{new["ShootType"]}'] if new.get('ShootType') else [])
            journal={'before':old,'after':new,'before_hash':before_hash,'target':str(target),'mode':cap,
                     'subjects_before':before.get('XMP-dc:Subject',[]),'write_method':'direct-memory-v1','keyword_policy':2}
            if restore is not None:journal.update(restore=restore,applied=applied)
            try:
                raw_before=raw_integrity(path) if cap=='embedded' and path.suffix.lower() in EMBEDDED_RAW else None
                args=[]
                args += [f'-XMP-fk:{k}={field_text(new.get(k))}' for k in FIELDS]
                if windows:
                    args += [f'-XPSubject={new.get("WindowsSubject") or ""}',f'-XPKeywords={new.get("WindowsKeywords") or ""}']
                    # 撤销最后两个 XP 字段时 ExifTool 会清理空 IFD；保留既有
                    # 分辨率等字段，避免附带删除或导致完整性检查拒绝撤销。
                    if restore is not None:
                        args += [f'-{tag}={before["IFD0:"+tag]}' for tag in ('XResolution','YResolution','ResolutionUnit','YCbCrPositioning') if 'IFD0:'+tag in before]
                if manages_rating:args.append('-XMP-xmp:Rating='+('' if new.get('Rating') is None else str(new['Rating'])))
                args+=([f'-XMP-dc:Subject={s}' for s in dict.fromkeys(subjects)] or ['-XMP-dc:Subject='])
                text_files=[]
                try:
                    for key in descriptive:
                        value=new.get(key) or '';tag=DESCRIPTIVE[key]+('-x-default' if key in ('Title','Description') else '')
                        if value:
                            with tempfile.NamedTemporaryFile(dir=self.data,prefix='.framekeep-text-',suffix='.txt',delete=False) as stream:
                                text_file=Path(stream.name);text_files.append(text_file);stream.write(value.encode('utf8'))
                            args.append(f'-{tag}<={text_file}')
                        else:args.append(f'-{tag}=')
                    with self._snapshot_for_edit(path,original) if original is not None else nullcontext():payload=self.edit(target,args)
                finally:
                    for text_file in text_files:text_file.unlink(missing_ok=True)
                after=self.read(payload,full=True)
                different=[k for k in FIELDS if str(after.get('XMP-fk:'+k) if after.get('XMP-fk:'+k) is not None else '')!=str(new.get(k) if new.get(k) is not None else '')]
                if different:raise ValueError('写入字段读回不一致：'+','.join(different))
                if windows and any(field_text(after.get(tag))!=field_text(new.get(key)) for key,tag in [('WindowsSubject','IFD0:XPSubject'),('WindowsKeywords','IFD0:XPKeywords')]):
                    raise ValueError('Windows 图片属性读回不一致')
                if manages_rating and after.get('XMP-xmp:Rating')!=new.get('Rating'):raise ValueError('星级读回不一致')
                for key in descriptive:
                    if (after.get(DESCRIPTIVE[key]) or '')!=(new.get(key) or ''):raise ValueError('描述元数据读回不一致：'+key)
                if manages_keywords:
                    actual=after.get('XMP-dc:Subject',[]);actual=actual if isinstance(actual,list) else [actual]
                    if actual!=list(dict.fromkeys(subjects)):raise ValueError('关键词读回不一致')
                mp_offsets={key for key in before if re.fullmatch(r'MPImage(?:[2-9]|[1-9][0-9]+):MPImageStart',key) and before[key]!=after.get(key)}
                thumbnail_offsets={key for key in ('IFD0:ThumbnailOffset','IFD1:ThumbnailOffset') if key in before and before[key]!=after.get(key)}
                if thumbnail_offsets:
                    thumbnail=jpeg_thumbnail(original if original is not None else path,before);generated=jpeg_thumbnail(payload,after)
                    if thumbnail is None or generated is None:
                        thumbnail=self.binary('-b','-ThumbnailImage',str(path),input_data=None)
                        generated=self.binary('-b','-ThumbnailImage','-',input_data=payload)
                    if not thumbnail or thumbnail!=generated:raise ValueError('内嵌缩略图内容校验失败，未提交')
                    journal['thumbnail_integrity']=hashlib.sha256(thumbnail).hexdigest()
                for key in mp_offsets:
                    group=key.split(':')[0]
                    tag=group+(':PreviewImage' if group+':PreviewImage' in before else ':'+group)
                    original_mp=self.binary('-b','-'+tag,str(path),input_data=None)
                    generated=self.binary('-b','-'+tag,'-',input_data=payload)
                    if not original_mp or original_mp!=generated:
                        raise ValueError('MPF 附加图像内容校验失败，未提交')
                    journal.setdefault('mp_integrity',{})[group]=hashlib.sha256(original_mp).hexdigest()
                for key in ['XMP-xmp:Rating','ExifIFD:DateTimeOriginal','IFD0:Orientation']:
                    if key=='XMP-xmp:Rating' and manages_rating:continue
                    if key in {DESCRIPTIVE[name] for name in descriptive}:continue
                    if before.get(key)!=after.get(key):raise ValueError('非目标元数据发生变化，未提交')
                for key,value in before.items():
                    if windows and key in ('IFD0:XPSubject','IFD0:XPKeywords'):continue
                    if key in thumbnail_offsets:continue
                    if key in mp_offsets:continue
                    if key=='SourceFile' or key in ('XMP-dc:Subject','XMP-x:XMPToolkit') or key.startswith(('File:','System:','ExifTool:','Composite:','XMP-fk:')):continue
                    if key=='XMP-xmp:Rating' and manages_rating:continue
                    if key in {DESCRIPTIVE[name] for name in descriptive}:continue
                    if raw_before and key in ('QuickTime:MediaDataOffset','QuickTime:MediaDataSize','QuickTime:OtherFormat'):continue
                    if raw_before and key.split(':')[-1] in ('StripOffsets','TileOffsets','PreviewImageStart','JPEGInterchangeFormat','JpgFromRawStart','HiddenDataOffset','SR2SubIFDOffset','SR2SubIFDLength','ThumbnailOffset'):continue
                    if after.get(key)!=value:raise ValueError('非目标字段发生变化，未提交：'+key)
                if cap=='embedded':
                    if raw_before:
                        if raw_integrity(payload,suffix=path.suffix)!=raw_before:raise ValueError('RAW 感光数据/内嵌预览/媒体负载变化，未提交')
                        journal['raw_integrity']=raw_before
                    else:
                        encoded_equal=False
                        if jpeg_fast and path.suffix.lower() in ('.jpg','.jpeg'):
                            from jpeg_archive_integrity import jpeg_image_digest
                            encoded_equal=jpeg_image_digest(original if original is not None else path.read_bytes())==jpeg_image_digest(payload)
                            if encoded_equal:journal['jpeg_integrity']='identical_encoded_image'
                        if not encoded_equal:
                            with Image.open(io.BytesIO(original) if original is not None else path) as a,Image.open(io.BytesIO(payload)) as b:
                                if a.size!=b.size or a.convert('RGB').tobytes()!=b.convert('RGB').tobytes():raise ValueError('图片像素验证失败，未提交')
                actual_source_hash=sha(path)
                actual_target_hash=actual_source_hash if target==path else sha(target) if target.exists() else None
                if actual_source_hash!=expected_sha or actual_target_hash!=before_hash:raise ValueError('提交前检测到外部修改，未覆盖')
                journal['after_hash']=hashlib.sha256(payload).hexdigest()
                if _target is None:
                    journal['sidecars']=[]
                    previous={entry['target']:entry for entry in sidecars_restore or []}
                    for side in sides:
                        if side==target:continue
                        prior=self.read(side) if side.exists() else {}
                        mirror=dict(new)
                        if prior.get('XMP-fk:AssetId') and str(prior['XMP-fk:AssetId']).replace('-','')!=str(new.get('AssetId')).replace('-',''):
                            raise ValueError('侧车已属于其他资产，未覆盖：'+side.name)
                        # RAW/JPG 共用侧车时保留该侧车已登记的组件身份。
                        if prior.get('XMP-fk:FileId'):mirror['FileId']=prior['XMP-fk:FileId']
                        undo=previous.get(str(side)) if restore is not None else None
                        restore_fields=dict(undo['before'],AssetId=mirror.get('AssetId'),FileId=mirror.get('FileId'),MetadataRevision=mirror.get('MetadataRevision')) if undo else restore
                        prepared,_,entry=self.write(path,mirror,expected_sha,mode=mode,restore=restore_fields,
                                                    applied=undo['after'] if undo else applied,_target=side)
                        entry['content']=base64.b64encode(prepared).decode('ascii')
                        journal['sidecars'].append(entry)
                return payload,target,journal
            except BaseException:
                self.discard_sidecars(journal)
                raise

    @staticmethod
    def discard_sidecars(journal):
        """仅清理本批登记、内容未变的临时副本。"""
        for entry in journal.get('sidecars',[]):
            if not entry.get('prepared'):continue
            temporary=Path(entry['prepared'])
            if temporary.is_file() and sha(temporary)==entry['after_hash']:
                MetadataWriter.discard_prepared(temporary)

    @staticmethod
    def check_sidecars(journal):
        for entry in journal.get('sidecars',[]):
            target=Path(entry['target'])
            if (sha(target) if target.exists() else None)!=entry['before_hash']:
                raise ValueError('侧车被其他程序修改，未覆盖：'+target.name)

    @staticmethod
    def commit_sidecars(journal):
        """侧车内容小型持久化，重启可继续；旧 journal 只读取既有准备文件。"""
        for entry in journal.get('sidecars',[]):
            target=Path(entry['target']);current=sha(target) if target.exists() else None
            if current!=entry['after_hash']:
                if current!=entry['before_hash']:raise ValueError('侧车被其他程序修改，未覆盖：'+target.name)
                if entry.get('content'):
                    if len(entry['content'])>11_000_000:raise ValueError('侧车内容超过限制，未写回')
                    payload=base64.b64decode(entry['content'],validate=True)
                elif entry.get('prepared') and Path(entry['prepared']).is_file():payload=Path(entry['prepared']).read_bytes()
                else:raise ValueError('侧车内容缺失，请重新提交元数据：'+target.name)
                if len(payload)>8_000_000 or hashlib.sha256(payload).hexdigest()!=entry['after_hash']:raise ValueError('侧车内容校验失败，未写回：'+target.name)
                write_verified(payload,target,entry['before_hash'])
            if sha(target)!=entry['after_hash']:raise ValueError('侧车写回后发生外部修改：'+target.name)
            if entry.get('prepared'):
                temporary=Path(entry['prepared'])
                if not temporary.exists() or sha(temporary)==entry['after_hash']:MetadataWriter.discard_prepared(temporary)
