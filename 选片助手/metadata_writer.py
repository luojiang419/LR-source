"""受控 XMP 写入；同卷副本、读回、版本比较、字段级恢复。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import uuid
from contextlib import contextmanager
from PIL import Image
import media_engine as media

CONFIG='''%Image::ExifTool::UserDefined = (
 'Image::ExifTool::XMP::Main' => { fk => { SubDirectory => { TagTable => 'Image::ExifTool::UserDefined::Framekeep' } } },
);
%Image::ExifTool::UserDefined::Framekeep = (
 GROUPS => { 0 => 'XMP', 1 => 'XMP-fk', 2 => 'Image' },
 NAMESPACE => { fk => 'https://framekeep.local/ns/asset/1.0/' }, WRITABLE => 'string',
 AssetId => {}, FileId => {}, SKU => {}, ShootType => {}, ProjectId => {}, SegmentId => {}, MetadataRevision => {},
);
1;
'''
FIELDS=['AssetId','FileId','SKU','ShootType','ProjectId','SegmentId','MetadataRevision']
EMBEDDED_RAW={'.cr3','.arw'}

def raw_integrity(path):
    """校验感光数据和相机内嵌预览；CR3 另校验全部 mdat 负载。"""
    import rawpy
    try:
        with rawpy.imread(str(path)) as raw:
            sensor=raw.raw_image
            result={'shape':list(sensor.shape),'dtype':str(sensor.dtype),'sensor':hashlib.sha256(sensor.tobytes()).hexdigest()}
            thumb=raw.extract_thumb();data=thumb.data if isinstance(thumb.data,bytes) else thumb.data.tobytes()
            result['preview']=hashlib.sha256(data).hexdigest()
    except Exception:raise ValueError('RAW 感光数据或内嵌预览无法验证，未写入') from None
    if Path(path).suffix.lower()=='.cr3':
        digest=hashlib.sha256();sizes=[];total=Path(path).stat().st_size
        with open(path,'rb') as f:
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

class MetadataWriter:
    def __init__(self,root,data):
        self.root=Path(root);self.data=Path(data);self.config=self.data/'framekeep-exiftool.config'
        self.config.write_text(CONFIG,encoding='utf8');self.lock=threading.RLock()
    def run(self,*args):
        env=os.environ.copy()
        for name in ['LANG','LC_ALL','LC_CTYPE']:env.pop(name,None)
        payload='\n'.join(map(str,args))+'\n'
        if any('\n' in str(a) or '\r' in str(a) for a in args):raise ValueError('元数据参数不能包含换行')
        result=subprocess.run([str(media.find_exiftool(self.root)),'-config',str(self.config),'-charset','filename=UTF8','-@','-'],input=payload.encode('utf8'),capture_output=True,timeout=180,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:raise ValueError('ExifTool 写入/读取失败：'+result.stderr.decode('utf8',errors='replace')[-240:])
        return result.stdout.decode('utf-8-sig')
    def read(self,path,full=False):
        tags=['-all:all'] if full else ['-XMP-fk:all','-XMP-dc:Subject','-XMP-xmp:Rating','-DateTimeOriginal','-Orientation']
        return json.loads(self.run('-G1','-s','-json',*tags,path))[0]
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
        if handle==C.c_void_p(-1).value:raise ValueError('文件仍被写入或占用，请稍后重试')
        try:yield
        finally:kernel.CloseHandle(handle)
    def capability(self,path,mode):
        if Path(path).suffix.lower() in ('.jpg','.jpeg'):return 'embedded'
        if Path(path).suffix.lower() in media.RAW_EXTS and mode=='sidecar':return 'sidecar'
        if Path(path).suffix.lower() in EMBEDDED_RAW:return 'embedded'
        return 'unsupported'
    @staticmethod
    def owned(meta):return {k:meta.get('XMP-fk:'+k) for k in FIELDS}
    def write(self,path,fields,expected_sha,*,mode='embedded',restore=None,applied=None):
        path=Path(path);cap=self.capability(path,mode)
        if cap=='unsupported':raise ValueError('此格式尚未开放内嵌写入；RAW 可明确选择 XMP 伴随模式')
        with self.lock:
            if sha(path)!=expected_sha:raise ValueError('文件已被其他程序修改，请重新预览')
            target=path.with_suffix('.xmp') if cap=='sidecar' else path
            # RAW/JPG 共用同名 sidecar 时不覆盖不属于该 RAW 的身份。
            before=self.read(target,full=True) if target.exists() else {}
            prior_id=before.get('XMP-fk:FileId')
            if cap=='sidecar' and prior_id and str(prior_id).replace('-','')!=str(fields.get('FileId')).replace('-',''):raise ValueError('同名 XMP 已属于其他文件，请先处理冲突')
            before_hash=sha(target) if target.exists() else None
            subjects=before.get('XMP-dc:Subject',[]);subjects=subjects if isinstance(subjects,list) else [subjects]
            old=self.owned(before);new=dict(fields)
            manages_rating='Rating' in fields or (restore is not None and 'Rating' in restore)
            if manages_rating:
                old['Rating']=before.get('XMP-xmp:Rating')
                if fields.get('Rating') is not None and (type(fields['Rating']) is not int or fields['Rating'] not in range(6)):raise ValueError('星级须为 0–5')
            if restore is not None:
                if any((old.get(k)!=applied.get(k)) if k=='Rating' else (str(old.get(k) or '')!=str(applied.get(k) or '')) for k in old):raise ValueError('备案字段已被后来修改，不能直接撤销')
                new=restore
            new=dict(new)
            for identity in ('AssetId','FileId'):
                if new.get(identity):new[identity]=str(uuid.UUID(str(new[identity])))
            managed=[f'SKU:{old["SKU"]}' if old.get('SKU') else None,f'ShootType:{old["ShootType"]}' if old.get('ShootType') else None]
            subjects=[s for s in subjects if s not in managed]
            subjects+=([f'SKU:{new["SKU"]}'] if new.get('SKU') else [])+([f'ShootType:{new["ShootType"]}'] if new.get('ShootType') else [])
            workspace=target.parent/('.framekeep-write-'+uuid.uuid4().hex);workspace.mkdir()
            temp=workspace/target.name
            journal={'before':old,'after':new,'before_hash':before_hash,'target':str(target),'mode':cap}
            if restore is not None:journal.update(restore=restore,applied=applied)
            try:
                raw_before=raw_integrity(path) if cap=='embedded' and path.suffix.lower() in EMBEDDED_RAW else None
                if target.exists():shutil.copy2(target,temp)
                args=['-overwrite_original']
                args += [f'-XMP-fk:{k}={new.get(k) or ""}' for k in FIELDS]
                if manages_rating:args.append('-XMP-xmp:Rating='+('' if new.get('Rating') is None else str(new['Rating'])))
                args+=['-XMP-dc:Subject=']+[f'-XMP-dc:Subject+={s}' for s in dict.fromkeys(subjects)]
                self.run(*args,temp)
                after=self.read(temp,full=True)
                if any(str(after.get('XMP-fk:'+k) or '')!=str(new.get(k) or '') for k in FIELDS):raise ValueError('写入字段读回不一致')
                if manages_rating and after.get('XMP-xmp:Rating')!=new.get('Rating'):raise ValueError('星级读回不一致')
                for key in ['XMP-xmp:Rating','ExifIFD:DateTimeOriginal','IFD0:Orientation']:
                    if key=='XMP-xmp:Rating' and manages_rating:continue
                    if before.get(key)!=after.get(key):raise ValueError('非目标元数据发生变化，未提交')
                for key,value in before.items():
                    if key=='SourceFile' or key in ('XMP-dc:Subject','XMP-x:XMPToolkit') or key.startswith(('File:','System:','ExifTool:','Composite:','XMP-fk:')):continue
                    if key=='XMP-xmp:Rating' and manages_rating:continue
                    if raw_before and key in ('QuickTime:MediaDataOffset','QuickTime:MediaDataSize','QuickTime:OtherFormat'):continue
                    if raw_before and key.split(':')[-1] in ('StripOffsets','TileOffsets','PreviewImageStart','JPEGInterchangeFormat','JpgFromRawStart','HiddenDataOffset','SR2SubIFDOffset','SR2SubIFDLength','ThumbnailOffset'):continue
                    if after.get(key)!=value:raise ValueError('非目标字段发生变化，未提交：'+key)
                if cap=='embedded':
                    if raw_before:
                        if raw_integrity(temp)!=raw_before:raise ValueError('RAW 感光数据/内嵌预览/媒体负载变化，未提交')
                        journal['raw_integrity']=raw_before
                    else:
                        with Image.open(path) as a,Image.open(temp) as b:
                            if a.size!=b.size or a.convert('RGB').tobytes()!=b.convert('RGB').tobytes():raise ValueError('图片像素验证失败，未提交')
                if sha(path)!=expected_sha or (sha(target) if target.exists() else None)!=before_hash:raise ValueError('提交前检测到外部修改，未覆盖')
                journal['after_hash']=sha(temp)
                journal['prepared']=str(temp)
                return temp,target,journal
            except BaseException:
                temp.unlink(missing_ok=True);workspace.rmdir();raise
