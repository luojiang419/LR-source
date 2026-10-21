"""只读照片元数据与按需预览。ExifTool 批量读取，LibRaw 补充 RAW 预览/解码。"""
from __future__ import annotations
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import ntpath
import queue
import time
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageOps, ImageDraw

RAW_EXTS={'.cr3','.cr2','.nef','.nrw','.arw','.srf','.sr2','.raf','.rw2','.orf','.dng','.pef','.3fr','.fff','.rwl','.iiq','.srw'}
IMAGE_EXTS={'.jpg','.jpeg','.tif','.tiff','.png','.heic','.heif'}
_LOCKS={}
_LOCK_GUARD=threading.Lock()
_PREVIEW_SLOTS=threading.BoundedSemaphore(2)
_DECODE_SLOT=threading.BoundedSemaphore(1)
_STATS={}

class ScanCancelled(Exception):pass

def find_exiftool(root):
    import shutil
    configured=os.environ.get('FRAMEKEEP_EXIFTOOL')
    if configured and Path(configured).is_file():return Path(configured)
    system=shutil.which('exiftool')
    if system:return Path(system)
    for base in [Path(root)/'.runtime/exiftool',Path(root)/'runtime/exiftool',Path(getattr(sys,'_MEIPASS',root))/'runtime/exiftool']:
        if base.exists():
            found=next(base.rglob('exiftool.exe'),None)
            if found:return found
    raise RuntimeError('找不到 ExifTool，请运行安装依赖脚本或设置 FRAMEKEEP_EXIFTOOL。')

class ExifToolSession:
    """串行复用 ExifTool；取消或关闭后，下次请求自动启动干净进程。"""
    def __init__(self,exiftool,timeout=180):
        self.exiftool=Path(exiftool);self.timeout=timeout
        self._lock=threading.Lock();self._state_lock=threading.Lock()
        self._process=None;self._output=None;self._reader=None;self._sequence=0

    def _start(self):
        with self._state_lock:
            if self._process is None:
                process=subprocess.Popen([str(self.exiftool),'-stay_open','True','-@','-'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                output=queue.Queue()
                def read_output():
                    try:
                        for line in iter(process.stdout.readline,b''):output.put(line)
                    finally:output.put(None)
                reader=threading.Thread(target=read_output,name='exiftool-output',daemon=True)
                self._process=process;self._output=output;self._reader=reader;reader.start()
            return self._process,self._output

    def _discard(self,expected=None):
        with self._state_lock:
            if expected is not None and self._process is not expected:return
            process=self._process;reader=self._reader
            self._process=None;self._output=None;self._reader=None
        if process is not None:
            if process.poll() is None:
                try:process.kill()
                except OSError:pass
            process.wait()
            process.stdin.close()
            if reader is not None:reader.join()
            process.stdout.close()

    def close(self):
        """立即释放当前进程，也可中断正在读取的请求。"""
        self._discard()

    def execute(self,args,cancel=None):
        started=time.monotonic();acquired=False;process=None
        def check():
            if cancel is not None and cancel.is_set():raise ScanCancelled()
            if time.monotonic()-started>self.timeout:raise RuntimeError('读取拍摄时间超时，请检查网络目录后重试。')
        try:
            while not acquired:
                check();acquired=self._lock.acquire(timeout=.05)
            check();process,output=self._start();self._sequence+=1
            number=self._sequence;marker=('{ready'+str(number)+'}').encode()
            process.stdin.write(('\n'.join(args)+f'\n-execute{number}\n').encode('utf8'));process.stdin.flush()
            chunks=[]
            while True:
                check()
                try:line=output.get(timeout=.05)
                except queue.Empty:continue
                if line is None:raise RuntimeError('ExifTool 已退出或读取被关闭，请重试。')
                if line.rstrip(b'\r\n')==marker:return b''.join(chunks)
                chunks.append(line)
        except BaseException:
            if process is not None:self._discard(process)
            raise
        finally:
            if acquired:self._lock.release()


def read_metadata(paths,exiftool,cancel=None,*,session=None):
    if not paths:return []
    args=[str(exiftool),'-charset','filename=UTF8','-fast','-json','-n','-DateTimeOriginal','-SubSecDateTimeOriginal','-SubSecTimeOriginal','-OffsetTimeOriginal','-Make','-Model','-SerialNumber','-BodySerialNumber','-InternalSerialNumber','-ImageUniqueID','-OriginalRawFileName','-RawFileName','-FileNumber','-ImageNumber','-ISO','-FNumber','-ExposureTime','-Orientation','-ImageWidth','-ImageHeight','-Rating','-@','-']
    # stdin 参数文件保留中文路径，禁止调用 shell。
    from time import monotonic
    if session is not None:
        stdout=session.execute(args[1:-2]+[str(p) for p in paths],cancel=cancel)
    else:
        with subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)) as process:
            started=monotonic();payload=('\n'.join(str(p) for p in paths)+'\n').encode('utf8')
            try:
                while True:
                    if cancel is not None and cancel.is_set():raise ScanCancelled()
                    if monotonic()-started>180:raise RuntimeError('读取拍摄时间超时，请检查网络目录后重试。')
                    try:stdout,_=process.communicate(input=payload,timeout=.3);break
                    except subprocess.TimeoutExpired:payload=None
            except BaseException:
                process.kill();process.communicate();raise
    try:data=json.loads(stdout.decode('utf-8-sig'))
    except (ValueError,UnicodeError):raise RuntimeError('ExifTool 未返回有效元数据。') from None
    if len(data)!=len(paths):raise RuntimeError('元数据条目数与源文件不一致。')
    return data

def parse_time(meta):
    value=meta.get('SubSecDateTimeOriginal') or meta.get('DateTimeOriginal')
    if not isinstance(value,str):return None
    value=value[:10].replace(':','-')+'T'+value[11:]
    try:return datetime.fromisoformat(value).isoformat()
    except ValueError:return None

def fingerprint(paths):
    stats=[_STATS.get(str(p)) or p.stat() for p in paths]
    return hashlib.sha256(json.dumps([(str(p),s.st_size,s.st_mtime_ns) for p,s in zip(paths,stats)],ensure_ascii=False).encode()).hexdigest()

def discover(root,recursive=True,cancel=None):
    root=Path(root).resolve(strict=True)
    if not root.is_dir():raise ValueError('请选择照片文件夹。')
    grouped={}
    stack=[root]
    while stack:
        if cancel is not None and cancel.is_set():raise ScanCancelled()
        with os.scandir(stack.pop()) as entries:
            for entry in entries:
                if cancel is not None and cancel.is_set():raise ScanCancelled()
                if entry.name.startswith('.framekeep-'):continue
                if entry.is_symlink():continue
                if entry.is_dir(follow_symlinks=False):
                    if recursive and not entry.name.startswith('.') and not Path(entry.path).is_junction():stack.append(Path(entry.path))
                    continue
                p=Path(entry.path)
                if p.suffix.lower() not in RAW_EXTS|IMAGE_EXTS or not entry.is_file(follow_symlinks=False):continue
                _STATS[str(p)]=entry.stat(follow_symlinks=False)
                relative=p.relative_to(root)
                grouped.setdefault(str(relative.with_suffix('')).casefold(),[]).append(p)
    shots=[]
    for _,files in sorted(grouped.items()):
        raws=sorted(p for p in files if p.suffix.lower() in RAW_EXTS)
        images=sorted((p for p in files if p.suffix.lower() in IMAGE_EXTS),key=lambda p:(p.suffix.lower() not in ('.jpg','.jpeg'),str(p)))
        if raws:
            for i,raw in enumerate(raws):shots.append((raw,images[0] if i==0 and images else None))
            # 除已配对 JPG 外其他文件分别保留，避免把不同格式静默丢弃。
            for image in images[1:]:shots.append((image,None))
        else:
            for image in images:shots.append((image,None))
    return root,shots

def capture_record(root,primary,companion,meta):
    raw=primary.suffix.lower() in RAW_EXTS
    rel=str(primary.relative_to(root)).replace('\\','/')
    paths=[primary]+([companion] if companion else [])
    captured=parse_time(meta)
    # 无 EXIF 不用文件修改时间冒充拍摄时间，单独标记并保守保留。
    return {'id':hashlib.sha256(rel.casefold().encode()).hexdigest()[:24],'name':primary.stem,
            'time':captured,'missingTime':captured is None,'camera':str(meta.get('Model','未知相机')),
            'captureIdentity':{k:str(meta[k]) for k in ('Make','Model','SerialNumber','BodySerialNumber','InternalSerialNumber','DateTimeOriginal','SubSecTimeOriginal','SubSecDateTimeOriginal','OffsetTimeOriginal','ImageUniqueID','OriginalRawFileName','RawFileName','FileNumber','ImageNumber') if meta.get(k) is not None},
            'cameraKey':str(meta.get('Model','未知相机'))+':'+str(meta.get('SerialNumber','')),
            'raw':rel if raw else None,'jpg':str(companion.relative_to(root)).replace('\\','/') if companion else (None if raw else rel),
            'iso':meta.get('ISO'),'aperture':meta.get('FNumber'),'shutter':meta.get('ExposureTime'),
            'orientation':meta.get('Orientation',1),'width':meta.get('ImageWidth'),'height':meta.get('ImageHeight'),
            'existingRating':max(0,min(5,int(meta.get('Rating',0) or 0))),
            'fingerprint':fingerprint(paths),'fileCount':len(paths),'metadataError':meta.get('Error')}

def resolved_file(root,relative):
    if not relative or not isinstance(relative,str):raise ValueError('照片路径为空。')
    if Path(relative).is_absolute() or '\\' in relative or ':' in relative or any(x in ('','..','.') for x in relative.split('/')):raise ValueError('照片相对路径非法。')
    p=(Path(root)/relative).resolve(strict=True)
    p.relative_to(Path(root).resolve())
    return p

def _orient_preview(image,orientation):
    try:orientation=int(orientation)
    except (ValueError,TypeError):orientation=1
    transform={2:Image.Transpose.FLIP_LEFT_RIGHT,3:Image.Transpose.ROTATE_180,4:Image.Transpose.FLIP_TOP_BOTTOM,5:Image.Transpose.TRANSPOSE,6:Image.Transpose.ROTATE_270,7:Image.Transpose.TRANSVERSE,8:Image.Transpose.ROTATE_90}.get(orientation)
    return image.transpose(transform) if transform is not None else image


def _read_preview(source,size,orientation=None):
    with Image.open(source) as image:
        embedded_orientation=image.getexif().get(274)
        try:embedded_orientation=int(embedded_orientation)
        except (ValueError,TypeError):embedded_orientation=None
        if embedded_orientation not in range(1,9):embedded_orientation=None
        # JPEG 在解码阶段先缩小，避免展开全尺寸像素后才缩放。
        image.draft('RGB',(size,size))
        result=image.convert('RGB')
    # 明确的内嵌 1 表示像素已朝正，不能再用 RAW/目录的 6 重复旋转。
    return _orient_preview(result,embedded_orientation if embedded_orientation is not None else orientation)


def _embedded_preview(path,exiftool,tags,size,orientation):
    for tag in tags:
        try:
            # 与批量元数据读取一样使用 UTF-8 参数流，Windows argv 会损坏中文路径。
            result=subprocess.run([str(exiftool),'-charset','filename=UTF8','-fast','-b','-'+tag,'-@','-'],input=(str(path)+'\n').encode('utf8'),capture_output=True,timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if result.stdout.startswith(b'\xff\xd8'):
                return _read_preview(io.BytesIO(result.stdout),size,orientation)
        except (OSError,ValueError,subprocess.TimeoutExpired):
            # 缺失、损坏或无法提取时继续尝试下一种内嵌图，不自动解码 RAW。
            continue
    return None


def _companion_preview(record,root,size):
    if record.get('jpg'):
        try:return _read_preview(resolved_file(root,record['jpg']),size,record.get('orientation'))
        except (OSError,ValueError):pass
    return None


def _raw_preview(record,root,exiftool,size):
    path=resolved_file(root,record['raw']);orientation=record.get('orientation')
    if size<=480:
        image=_embedded_preview(path,exiftool,('ThumbnailImage',),size,orientation)
        if image is not None:return image
    image=_companion_preview(record,root,size)
    if image is not None:return image
    tags=('PreviewImage','JpgFromRaw') if size<=480 else ('PreviewImage','JpgFromRaw','ThumbnailImage')
    image=_embedded_preview(path,exiftool,tags,size,orientation)
    if image is not None:return image
    import rawpy
    # 只打开容器并展开内嵌图；不调用 unpack/postprocess 展开 RAW 像素。
    with rawpy.RawPy() as source:
        source.open_file(str(path));thumb=source.extract_thumb()
        if thumb.format==rawpy.ThumbFormat.JPEG:
            return _read_preview(io.BytesIO(thumb.data),size,orientation)
        return _orient_preview(Image.fromarray(thumb.data).convert('RGB'),orientation)


def preview(record,root,cache,exiftool,size=480,decode=False):
    size=max(160,min(4096,int(size)))
    key=hashlib.sha256((record['fingerprint']+f':{size}:{decode}:{record.get("orientation",1)}:v3').encode()).hexdigest()
    target=Path(cache)/(key+'.jpg');target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists():return target
    with _LOCK_GUARD:lock=_LOCKS.setdefault(key,threading.Lock())
    with lock:
        if target.exists():return target
        with _PREVIEW_SLOTS,(_DECODE_SLOT if decode else nullcontext()):
            image=None
            if record.get('raw'):
                if decode:
                    import rawpy
                    with rawpy.imread(str(resolved_file(root,record['raw']))) as source:
                        image=Image.fromarray(source.postprocess(use_camera_wb=True,half_size=True))
                else:image=_raw_preview(record,root,exiftool,size)
            elif not decode:image=_companion_preview(record,root,size)
            if image is None:raise RuntimeError('无可用预览；可在单张视图按需解码 RAW。')
            image.thumbnail((size,size),Image.Resampling.LANCZOS)
            temp=target.with_suffix('.tmp');image.save(temp,format='JPEG',quality=85,optimize=True);temp.replace(target)
            return target

def placeholder():
    image=Image.new('RGB',(480,320),(43,43,43));draw=ImageDraw.Draw(image);draw.text((100,145),'Preview unavailable',fill=(190,190,190));out=io.BytesIO();image.save(out,format='JPEG');return out.getvalue()

def path_aliases(root):
    """查询已存在映射：X:\\path 与同机 UNC 的等价路径，不按文件名猜目录。"""
    root=str(root);aliases=[root]
    if os.name!='nt':return aliases
    import ctypes
    def connection(drive):
        length=ctypes.c_ulong(32768);buffer=ctypes.create_unicode_buffer(length.value)
        result=ctypes.windll.mpr.WNetGetConnectionW(drive,buffer,ctypes.byref(length))
        return buffer.value if result==0 else None
    normalized=ntpath.normpath(root);drive,tail=ntpath.splitdrive(normalized)
    if len(drive)==2 and drive[1]==':':
        share=connection(drive)
        if share:aliases.append(ntpath.join(share,tail.lstrip('\\/')))
    else:
        for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
            share=connection(letter+':')
            if share:
                prefix=ntpath.normpath(share).rstrip('\\')
                if normalized.casefold()==prefix.casefold() or normalized.casefold().startswith(prefix.casefold()+'\\'):
                    aliases.append(letter+':\\'+normalized[len(prefix):].lstrip('\\'))
    return list(dict.fromkeys(aliases))
