"""受控 XMP 写入；同卷副本、读回、版本比较、字段级恢复。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import tempfile
import uuid
from contextlib import contextmanager
from PIL import Image
import media_engine as media
from file_safety import replace_verified
from metadata_history import save_record
from rating_xmp import sidecar_paths,load_sidecar

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
DESCRIPTIVE={'Title':'XMP-dc:Title','Description':'XMP-dc:Description','Label':'XMP-xmp:Label'}
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

def volume_root(path):
    """获取实际挂载卷根目录，兼容 Windows 挂载点和网络共享。"""
    path=Path(path).resolve()
    if os.name=='nt':
        import ctypes as C
        from ctypes import wintypes as W
        kernel=C.WinDLL('kernel32',use_last_error=True)
        kernel.GetVolumePathNameW.argtypes=[W.LPCWSTR,W.LPWSTR,W.DWORD]
        kernel.GetVolumePathNameW.restype=W.BOOL
        buffer=C.create_unicode_buffer(32768)
        if not kernel.GetVolumePathNameW(str(path),buffer,len(buffer)):raise C.WinError(C.get_last_error())
        return Path(buffer.value)
    while path.parent!=path and path.parent.stat().st_dev==path.stat().st_dev:path=path.parent
    return path

class MetadataWriter:
    def __init__(self,root,data):
        self.root=Path(root);self.data=Path(data);self.config=self.data/'framekeep-exiftool.config'
        self.config.write_text(CONFIG,encoding='utf8');self.lock=threading.RLock()
    def prepare_file(self,target):
        """照片目录之外的同卷独占临时文件，避免资源管理器预览占用。"""
        target=Path(target)
        device=target.parent.stat().st_dev
        folder=self.data/'metadata-staging'
        if self.data.stat().st_dev!=device:
            owner=hashlib.sha256(str(self.data.resolve()).casefold().encode('utf8')).hexdigest()[:16]
            folder=volume_root(target.parent)/'.framekeep-staging'/owner/'metadata-staging'
        folder.mkdir(parents=True,exist_ok=True)
        if folder.is_symlink() or folder.is_junction() or folder.stat().st_dev!=device:
            raise ValueError('照片所在卷的软件暂存目录不可用，未改写原文件')
        fd,name=tempfile.mkstemp(dir=folder,prefix='.framekeep-write-',suffix=target.suffix)
        os.close(fd)
        return Path(name)
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
        result=subprocess.run([str(media.find_exiftool(self.root)),'-config',str(self.config),'-charset','filename=UTF8','-@','-'],input=payload.encode('utf8'),capture_output=True,timeout=180,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:raise ValueError('ExifTool 写入/读取失败：'+result.stderr.decode('utf8',errors='replace')[-240:])
        return result.stdout.decode('utf-8-sig')
    def read(self,path,full=False):
        if Path(path).suffix.lower()=='.xmp':load_sidecar(path)
        tags=['-all:all'] if full else ['-XMP-fk:all','-XMP-dc:all','-XMP-xmp:Rating','-XMP-xmp:Label','-DateTimeOriginal','-Orientation']
        return json.loads(self.run('-G1','-s','-json',*tags,path))[0]
    def read_asset(self,path):
        path=Path(path);value=self.read(path)
        file_id=value.get('XMP-fk:FileId')
        for side in sidecar_paths(path):
            metadata=self.read(side)
            value.update({k:v for k,v in metadata.items() if k.startswith(('XMP-fk:','XMP-xmp:','XMP-dc:'))})
        if file_id and path.suffix.lower() in ('.jpg','.jpeg'):value['XMP-fk:FileId']=file_id
        return value
    def write_rating(self,path,rating):
        """仅变更评级；同卷临时副本、读回及替换前指纹核对。"""
        path=Path(path)
        if type(rating) is not int or rating not in range(6):raise ValueError('星级须为 0–5')
        if not path.is_file():raise ValueError('照片离线或不存在，星级已入库，等待文件恢复')
        targets=list(dict.fromkeys([path.with_suffix('.xmp'),*sidecar_paths(path)]))
        if path.suffix.lower() in ('.jpg','.jpeg'):targets.append(path)
        with self.lock:
            for target in targets:
                before_hash=sha(target) if target.exists() else None
                if target.exists() and self.read(target).get('XMP-xmp:Rating')==rating:continue
                old_rating=self.read(target).get('XMP-xmp:Rating') if target.exists() else None
                temp=self.prepare_file(target)
                try:
                    if target.exists():shutil.copy2(target,temp)
                    pixels=None
                    if target==path:
                        with Image.open(path) as im:pixels=hashlib.sha256(im.tobytes()).digest()
                    self.run('-overwrite_original',f'-XMP-xmp:Rating={rating}',temp)
                    if self.read(temp).get('XMP-xmp:Rating')!=rating:raise ValueError('星级写入读回不一致')
                    if pixels is not None:
                        with Image.open(temp) as im:
                            if hashlib.sha256(im.tobytes()).digest()!=pixels:raise ValueError('图像数据发生变化，未提交星级')
                    if (sha(target) if target.exists() else None)!=before_hash:raise ValueError('文件被其他程序修改，稍后重试星级同步')
                    journal={'before':{'Rating':old_rating},'after':{'Rating':rating},
                             'before_hash':before_hash,'after_hash':sha(temp),'target':str(target),
                             'mode':'embedded' if target==path else 'sidecar'}
                    identity=uuid.uuid4().hex
                    save_record(self.data,identity,journal,source=path)
                    replace_verified(temp,target,before_hash)
                    if sha(target)!=journal['after_hash']:raise ValueError('星级提交后文件发生变化，恢复信息已保留')
                    save_record(self.data,identity,journal,source=path,status='applied')
                finally:
                    self.discard_prepared(temp)
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
        if Path(path).suffix.lower() in media.IMAGE_EXTS|{'.webp'}:return 'sidecar'
        if Path(path).suffix.lower() in media.RAW_EXTS and mode=='sidecar':return 'sidecar'
        if Path(path).suffix.lower() in EMBEDDED_RAW:return 'embedded'
        return 'unsupported'
    @staticmethod
    def owned(meta):return {k:meta.get('XMP-fk:'+k) for k in FIELDS}
    def write(self,path,fields,expected_sha,*,mode='sidecar',restore=None,applied=None,sidecars_restore=None,_target=None):
        path=Path(path);cap='sidecar' if _target is not None else self.capability(path,mode)
        if cap=='unsupported':raise ValueError('此格式尚未开放内嵌写入；RAW 可明确选择 XMP 伴随模式')
        with self.lock:
            if sha(path)!=expected_sha:raise ValueError('文件已被其他程序修改，请重新预览')
            sides=sidecar_paths(path)
            if _target is None and not sides and path.suffix.lower() in ('.jpg','.jpeg') and any(key in fields for key in (*DESCRIPTIVE,'Keywords')):
                sides=[Path(str(path)+'.xmp')]
            target=Path(_target) if _target is not None else (sides[-1] if sides else path.with_suffix('.xmp')) if cap=='sidecar' else path
            # RAW/JPG 共用同名 sidecar 时不覆盖不属于该 RAW 的身份。
            before=self.read(target,full=True) if target.exists() else {}
            prior_id=before.get('XMP-fk:FileId')
            if cap=='sidecar' and prior_id and str(prior_id).replace('-','')!=str(fields.get('FileId')).replace('-',''):raise ValueError('同名 XMP 已属于其他文件，请先处理冲突')
            before_hash=sha(target) if target.exists() else None
            subjects=before.get('XMP-dc:Subject',[]);subjects=subjects if isinstance(subjects,list) else [subjects]
            old=self.owned(before)
            descriptive={key for key in DESCRIPTIVE if key in fields or restore is not None and key in restore}
            for key in descriptive:old[key]=before.get(DESCRIPTIVE[key]) or ''
            managed=[f'SKU:{old["SKU"]}' if old.get('SKU') else None,f'ShootType:{old["ShootType"]}' if old.get('ShootType') else None]
            manages_keywords='Keywords' in fields or restore is not None and 'Keywords' in restore
            if manages_keywords:old['Keywords']=[s for s in subjects if s not in managed]
            new={**old,**fields}
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
            subjects=list(new.get('Keywords') or []) if manages_keywords else [s for s in subjects if s not in managed]
            subjects+=([f'SKU:{new["SKU"]}'] if new.get('SKU') else [])+([f'ShootType:{new["ShootType"]}'] if new.get('ShootType') else [])
            temp=self.prepare_file(target)
            journal={'before':old,'after':new,'before_hash':before_hash,'target':str(target),'mode':cap,
                     'subjects_before':before.get('XMP-dc:Subject',[])}
            if restore is not None:journal.update(restore=restore,applied=applied)
            try:
                raw_before=raw_integrity(path) if cap=='embedded' and path.suffix.lower() in EMBEDDED_RAW else None
                if target.exists():shutil.copy2(target,temp)
                args=['-overwrite_original']
                args += [f'-XMP-fk:{k}={new.get(k) or ""}' for k in FIELDS]
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
                    self.run(*args,temp)
                finally:
                    for text_file in text_files:text_file.unlink(missing_ok=True)
                after=self.read(temp,full=True)
                if any(str(after.get('XMP-fk:'+k) or '')!=str(new.get(k) or '') for k in FIELDS):raise ValueError('写入字段读回不一致')
                if manages_rating and after.get('XMP-xmp:Rating')!=new.get('Rating'):raise ValueError('星级读回不一致')
                for key in descriptive:
                    if (after.get(DESCRIPTIVE[key]) or '')!=(new.get(key) or ''):raise ValueError('描述元数据读回不一致：'+key)
                if manages_keywords:
                    actual=after.get('XMP-dc:Subject',[]);actual=actual if isinstance(actual,list) else [actual]
                    if actual!=list(dict.fromkeys(subjects)):raise ValueError('关键词读回不一致')
                for key in ['XMP-xmp:Rating','ExifIFD:DateTimeOriginal','IFD0:Orientation']:
                    if key=='XMP-xmp:Rating' and manages_rating:continue
                    if key in {DESCRIPTIVE[name] for name in descriptive}:continue
                    if before.get(key)!=after.get(key):raise ValueError('非目标元数据发生变化，未提交')
                for key,value in before.items():
                    if key=='SourceFile' or key in ('XMP-dc:Subject','XMP-x:XMPToolkit') or key.startswith(('File:','System:','ExifTool:','Composite:','XMP-fk:')):continue
                    if key=='XMP-xmp:Rating' and manages_rating:continue
                    if key in {DESCRIPTIVE[name] for name in descriptive}:continue
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
                        journal['sidecars'].append(entry)
                return temp,target,journal
            except BaseException:
                self.discard_sidecars(journal)
                self.discard_prepared(temp);raise

    @staticmethod
    def discard_sidecars(journal):
        """仅清理本批登记、内容未变的临时副本。"""
        for entry in journal.get('sidecars',[]):
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
        """主文件提交后逐份提交已准备侧车；重启时跳过已提交项。"""
        for entry in journal.get('sidecars',[]):
            target=Path(entry['target']);temporary=Path(entry['prepared'])
            current=sha(target) if target.exists() else None
            if current!=entry['after_hash']:
                if current!=entry['before_hash']:raise ValueError('侧车被其他程序修改，未覆盖：'+target.name)
                if not temporary.is_file() or sha(temporary)!=entry['after_hash']:raise ValueError('侧车准备文件缺失或已变化，请重新提交元数据：'+target.name)
                replace_verified(temporary,target,entry['before_hash'])
            if sha(target)!=entry['after_hash']:raise ValueError('侧车提交后发生外部修改：'+target.name)
            if not temporary.exists() or sha(temporary)==entry['after_hash']:
                MetadataWriter.discard_prepared(temporary)
