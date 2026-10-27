"""只读照片元数据与按需预览。ExifTool 批量读取，LibRaw 补充 RAW 预览/解码。"""
from __future__ import annotations
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import tempfile
import ntpath
import queue
import time
import atexit
import base64
import weakref
from collections import OrderedDict
from preview_cache import cache_for,cache_name,WARM_WORKERS
from contextlib import nullcontext,contextmanager
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageOps, ImageDraw
from preview_codec import MAX_PREVIEW_EDGE,embedded_jpeg

_QUICK_PREVIEWS=OrderedDict()
_QUICK_PREVIEW_GUARD=threading.Lock()


def quick_preview_bytes(record,root,size=480):
    """当前新图的有界内存预览，不等待历史缓存索引或写盘队列。"""
    if not record.get('jpg') or not 160<=size<=480:return None
    key=(str(root),record['fingerprint'],size,record.get('orientation',1))
    with _QUICK_PREVIEW_GUARD:
        if key in _QUICK_PREVIEWS:
            _QUICK_PREVIEWS.move_to_end(key);return _QUICK_PREVIEWS[key]
    path=resolved_file(root,record['jpg'])
    if path.suffix.lower() not in ('.jpg','.jpeg'):return None
    with _preview_lock('quick:'+repr(key)):
        with _QUICK_PREVIEW_GUARD:
            if key in _QUICK_PREVIEWS:return _QUICK_PREVIEWS[key]
        from preview_codec import save_preview
        image=_read_preview(path,size,record.get('orientation'))
        stream=io.BytesIO();save_preview(image,stream);payload=stream.getvalue()
        with _QUICK_PREVIEW_GUARD:
            _QUICK_PREVIEWS[key]=payload
            while len(_QUICK_PREVIEWS)>8:_QUICK_PREVIEWS.popitem(last=False)
        return payload

RAW_EXTS={'.cr3','.cr2','.nef','.nrw','.arw','.srf','.sr2','.raf','.rw2','.orf','.dng','.pef','.3fr','.fff','.rwl','.iiq','.srw'}
IMAGE_EXTS={'.jpg','.jpeg','.tif','.tiff','.png','.heic','.heif'}
_PREVIEW_LOCK_GUARD=threading.Lock()
_PREVIEW_LOCKS=weakref.WeakValueDictionary()
_PREVIEW_SLOTS=threading.BoundedSemaphore(WARM_WORKERS+2)
_DECODE_SLOT=threading.BoundedSemaphore(1)
_STATS={}

class ScanCancelled(Exception):pass


@contextmanager
def _preview_lock(key):
    # 每个预览独立互斥；等待者持有强引用，最后使用者退出后自动释放锁对象。
    with _PREVIEW_LOCK_GUARD:
        lock=_PREVIEW_LOCKS.get(key)
        if lock is None:
            lock=threading.Lock();_PREVIEW_LOCKS[key]=lock
    with lock:yield

def find_exiftool(root):
    import shutil
    configured=os.environ.get('FRAMEKEEP_EXIFTOOL')
    if configured and Path(configured).is_file():return Path(configured)
    system=shutil.which('exiftool')
    if system:return Path(system)
    app_root=Path(os.environ.get('FRAMEKEEP_APP_ROOT',root))
    for base in [Path(root)/'.runtime/exiftool',Path(root)/'runtime/exiftool',app_root/'.runtime/exiftool',app_root/'runtime/exiftool',Path(getattr(sys,'_MEIPASS',root))/'runtime/exiftool']:
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
                # 二进制提取结果末尾可能紧接 ready 标记，没有单独的换行。
                for suffix in (marker+b'\r\n',marker+b'\n'):
                    if line.endswith(suffix):return b''.join(chunks)+line[:-len(suffix)]
                chunks.append(line)
        except BaseException:
            if process is not None:self._discard(process)
            raise
        finally:
            if acquired:self._lock.release()


_PREVIEW_SESSION_LOCK=threading.Lock()
_PREVIEW_SESSIONS={}


@contextmanager
def preview_exif_session(exiftool):
    key=str(exiftool)
    with _PREVIEW_SESSION_LOCK:
        if key not in _PREVIEW_SESSIONS:
            sessions=queue.Queue()
            for _ in range(WARM_WORKERS+2):sessions.put(ExifToolSession(exiftool,timeout=30))
            _PREVIEW_SESSIONS[key]=sessions
        sessions=_PREVIEW_SESSIONS[key]
    session=sessions.get()
    try:yield session
    finally:sessions.put(session)


def close_preview_sessions():
    with _PREVIEW_SESSION_LOCK:
        for sessions in _PREVIEW_SESSIONS.values():
            for session in list(sessions.queue):session.close()
        _PREVIEW_SESSIONS.clear()


atexit.register(close_preview_sessions)


def raw_thumbnail_metadata(path,exiftool):
    """一次读取方向和内嵌小图，避免 NAS RAW 每张重复打开两次。"""
    with preview_exif_session(exiftool) as session:
        payload=session.execute(['-charset','filename=UTF8','-fast','-json','-n','-b','-Orientation','-ThumbnailImage',str(path)])
    row=json.loads(payload.decode('utf-8-sig'))[0]
    orientation=int(row.get('Orientation',1))
    if orientation not in range(1,9):orientation=1
    encoded=row.get('ThumbnailImage','')
    thumbnail=base64.b64decode(encoded[7:],validate=True) if encoded.startswith('base64:') else None
    return orientation,thumbnail


def raw_embedded_metadata(path,exiftool):
    """优先获取相机 JPEG；方向与 JPEG 同次读容器，复用常驻 ExifTool。"""
    orientation=1
    for tag in ('PreviewImage','JpgFromRaw','ThumbnailImage'):
        with preview_exif_session(exiftool) as session:
            payload=session.execute(['-charset','filename=UTF8','-fast','-json','-n','-b','-Orientation','-'+tag,str(path)])
        row=json.loads(payload.decode('utf-8-sig'))[0]
        try:orientation=int(row.get('Orientation',orientation))
        except (ValueError,TypeError):orientation=1
        if orientation not in range(1,9):orientation=1
        encoded=row.get(tag,'')
        if encoded.startswith('base64:'):
            data=base64.b64decode(encoded[7:],validate=True)
            if data.startswith(b'\xff\xd8') and data.endswith(b'\xff\xd9'):return orientation,data
    return orientation,None


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
    # 独立软件新建资料库时优先读标准侧车，包括明确取消的零星。
    from rating_xmp import sidecar_metadata
    for path,meta in zip(paths,data):
        meta.update(sidecar_metadata(path))
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

def discover(root,recursive=True,cancel=None,*,exclude=(),indexed=None,on_indexed=None,stat_snapshot=None,only_paths=None):
    root=Path(root).resolve(strict=True)
    if not root.is_dir():raise ValueError('请选择照片文件夹。')
    grouped={}
    excluded={Path(path).resolve() for path in exclude}
    from preview_cache import _LOCATIONS
    excluded.update(_LOCATIONS.values())
    if indexed is not None:
        early={}
        for p in indexed.candidates(root,recursive=recursive,exclude=excluded,limit=128,cancel=cancel):
            try:_STATS[str(p)]=p.stat()
            except OSError:continue
            early.setdefault(str(p.relative_to(root).with_suffix('')).casefold(),[]).append(p)
        if early and on_indexed:
            shots=[]
            for files in early.values():
                raws=sorted(p for p in files if p.suffix.lower() in RAW_EXTS)
                images=sorted((p for p in files if p.suffix.lower() in IMAGE_EXTS),key=lambda p:(p.suffix.lower() not in ('.jpg','.jpeg'),str(p)))
                shots.extend((p,images[0] if i==0 and images else None) for i,p in enumerate(raws))
                shots.extend((p,None) for p in (images[1:] if raws else images))
            on_indexed(root,shots)
    stack=[root]
    selected={os.path.normcase(os.path.splitext(str(p))[0]) for p in only_paths} if only_paths is not None else None
    while stack:
        if cancel is not None and cancel.is_set():raise ScanCancelled()
        with os.scandir(stack.pop()) as entries:
            for entry in entries:
                if cancel is not None and cancel.is_set():raise ScanCancelled()
                if entry.name.startswith('.framekeep-'):continue
                if entry.is_symlink():continue
                if entry.is_dir(follow_symlinks=False):
                    if selected is not None and not any(stem.startswith(os.path.normcase(entry.path)+os.sep) for stem in selected):continue
                    if Path(entry.path).resolve() in excluded:continue
                    if recursive and not entry.name.startswith('.') and not Path(entry.path).is_junction():stack.append(Path(entry.path))
                    continue
                if selected is not None:
                    stem,extension=os.path.splitext(os.path.normcase(entry.path))
                    if stem not in selected and not (extension=='.xmp' and os.path.splitext(stem)[0] in selected):continue
                p=Path(entry.path)
                if stat_snapshot is not None and p.suffix.lower() in RAW_EXTS|IMAGE_EXTS|{'.xmp'} and entry.is_file(follow_symlinks=False):
                    stat_snapshot[os.path.normcase(str(p))]=entry.stat(follow_symlinks=False)
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
    from rating_xmp import sidecar_metadata,sidecar_fingerprint
    sidecar=sidecar_metadata(primary);external_rating=sidecar.get('Rating')
    # 无 EXIF 不用文件修改时间冒充拍摄时间，单独标记并保守保留。
    return {'id':hashlib.sha256(rel.casefold().encode()).hexdigest()[:24],'name':primary.stem,
            'time':captured,'missingTime':captured is None,'camera':str(meta.get('Model','未知相机')),
            'captureIdentity':{k:str(meta[k]) for k in ('Make','Model','SerialNumber','BodySerialNumber','InternalSerialNumber','DateTimeOriginal','SubSecTimeOriginal','SubSecDateTimeOriginal','OffsetTimeOriginal','ImageUniqueID','OriginalRawFileName','RawFileName','FileNumber','ImageNumber') if meta.get(k) is not None},
            'cameraKey':str(meta.get('Model','未知相机'))+':'+str(meta.get('SerialNumber','')),
            'raw':rel if raw else None,'jpg':str(companion.relative_to(root)).replace('\\','/') if companion else (None if raw else rel),
            'iso':meta.get('ISO'),'aperture':meta.get('FNumber'),'shutter':meta.get('ExposureTime'),
            'orientation':meta.get('Orientation',1),'width':meta.get('ImageWidth'),'height':meta.get('ImageHeight'),
            'existingRating':max(0,min(5,int(external_rating if external_rating is not None else meta.get('Rating',0) or 0))),
            **({'liveRating':max(0,external_rating)} if external_rating is not None else {}),
            'fingerprint':fingerprint(paths),'sidecarFingerprint':sidecar_fingerprint(paths),'sidecarMetadata':sidecar,
            'fileCount':len(paths),'metadataError':meta.get('Error')}

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
        orientation=embedded_orientation if embedded_orientation is not None else orientation
        try:orientation=int(orientation)
        except (ValueError,TypeError):orientation=1
        if orientation not in range(1,9):orientation=1
        if size>480 and image.format=='JPEG' and image.mode in ('RGB','L') and image.fp is not None and image.width*image.height<=100_000_000:
            from gpu_media import decode_jpeg
            position=image.fp.tell();image.fp.seek(0)
            payload=image.fp.read();image.fp.seek(position)
            accelerated=decode_jpeg(payload,size,orientation)
            if accelerated is not None:return accelerated
        # JPEG 在解码阶段先缩小，避免展开全尺寸像素后才缩放。
        image.draft('RGB',(size,size))
        result=image.convert('RGB')
        # 快速预览使用 JPEG 缩小解码，避免等待后台清晰图持有的 GPU 队列。
        if size<=480:result.thumbnail((size,size),Image.Resampling.LANCZOS)
    # 明确的内嵌 1 表示像素已朝正，不能再用 RAW/目录的 6 重复旋转。
    return _orient_preview(result,orientation)


def _embedded_preview(path,exiftool,tags,size,orientation,*,pooled=False):
    for tag in tags:
        try:
            # 与批量元数据读取一样使用 UTF-8 参数流，Windows argv 会损坏中文路径。
            if pooled:
                with preview_exif_session(exiftool) as session:
                    payload=session.execute(['-charset','filename=UTF8','-fast','-b','-'+tag,str(path)])
            else:
                result=subprocess.run([str(exiftool),'-charset','filename=UTF8','-fast','-b','-'+tag,'-@','-'],input=(str(path)+'\n').encode('utf8'),capture_output=True,timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                payload=result.stdout
            if payload.startswith(b'\xff\xd8'):
                return _read_preview(io.BytesIO(payload),size,orientation)
        except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired):
            # 缺失、损坏或无法提取时继续尝试下一种内嵌图，不自动解码 RAW。
            continue
    return None


def _companion_preview(record,root,size):
    if record.get('jpg'):
        try:return _read_preview(resolved_file(root,record['jpg']),size,record.get('orientation'))
        except (OSError,ValueError):pass
    return None


def _raw_preview(record,root,exiftool,size):
    image=_companion_preview(record,root,size)
    if image is not None:return image
    path=resolved_file(root,record['raw']);orientation=record.get('orientation')
    if callable(exiftool):exiftool=exiftool()
    if size<=480:
        if record.get('_small_thumbnail'):
            try:return _read_preview(io.BytesIO(record['_small_thumbnail']),size,orientation)
            except (OSError,ValueError):pass
        image=_embedded_preview(path,exiftool,('ThumbnailImage',),size,orientation,pooled=bool(record.get('_catalog')))
        if image is not None:return image
    tags=('PreviewImage','JpgFromRaw') if size<=480 else ('PreviewImage','JpgFromRaw','ThumbnailImage')
    image=_embedded_preview(path,exiftool,tags,size,orientation,pooled=bool(record.get('_catalog')))
    if image is not None:return image
    import rawpy
    # 只打开容器并展开内嵌图；不调用 unpack/postprocess 展开 RAW 像素。
    with rawpy.RawPy() as source:
        source.open_file(str(path));thumb=source.extract_thumb()
        if thumb.format==rawpy.ThumbFormat.JPEG:
            return _read_preview(io.BytesIO(thumb.data),size,orientation)
        return _orient_preview(Image.fromarray(thumb.data).convert('RGB'),orientation)


def _jpeg_source(record,root,exiftool):
    """一次获取压缩数据；配对 RAW 优先内嵌图，避免为预览读取完整大 JPG。"""
    if record.get('_source_jpeg') is not None:return record['_source_jpeg']
    raw=record.get('raw') or record.get('_fallback_raw')
    if raw and not record.get('_jpeg_attempted'):
        record['_jpeg_attempted']=True
        try:
            tool=exiftool() if callable(exiftool) else exiftool
            path=resolved_file(root,raw)
            orientation,payload=raw_embedded_metadata(path,tool)
            if payload:
                record['orientation']=orientation;record['_source_jpeg']=payload;return payload
        except (OSError,ValueError,RuntimeError,TypeError):pass
    if record.get('jpg'):
        try:
            path=resolved_file(root,record['jpg'])
            if path.suffix.lower() in ('.jpg','.jpeg') and path.stat().st_size<=64*1024**2:
                payload=path.read_bytes()
                if payload.startswith(b'\xff\xd8'):
                    record['_source_jpeg']=payload;return payload
        except OSError:pass
    return None


def warm_previews(record,root,cache,exiftool,sizes=(420,MAX_PREVIEW_EDGE),*,cache_index=None):
    """每张一次读取；主图仅在超限时缩放，小图复用同次的压缩数据或像素。"""
    index=cache_index or cache_for(cache);record=dict(record)
    sizes=tuple(sorted({max(160,min(MAX_PREVIEW_EDGE,int(s))) for s in sizes}))
    with index.budget.generation() if index.budget else nullcontext():
        if all(index.contains([cache_name(record,edge)]) for edge in sizes):return
        largest=max(sizes);image=None
        payload=None if record.get('_video') else _jpeg_source(record,root,exiftool)
        if payload:
            if embedded_jpeg(payload,largest,record.get('orientation')) is None:
                image=_read_preview(io.BytesIO(payload),largest,record.get('orientation'))
        elif record.get('_video'):
            from video_preview import extract_frame
            image=extract_frame(record['_video'],largest,record['_video_frame'])
        elif record.get('raw'):image=_raw_preview(record,root,exiftool,largest)
        else:image=_companion_preview(record,root,largest)
        if image is not None:
            from gpu_media import resize_preview
            image=resize_preview(image,largest)
        for edge in sizes:
            current=dict(record)
            if image is not None:current['_warmed_image']=image.copy()
            # 已持有整批 generation 租约，不能再次等待迁移开关，否则迁移会互等。
            _preview(current,root,cache,exiftool,edge,cache_index=index)


def preview(record,root,cache,exiftool,size=480,decode=False,*,cache_index=None):
    index=cache_index or cache_for(cache)
    with index.budget.generation() if index.budget else nullcontext():
        return _preview(dict(record),root,cache,exiftool,size,decode,cache_index=index)


def _preview(record,root,cache,exiftool,size=480,decode=False,*,cache_index=None):
    size=max(160,min(MAX_PREVIEW_EDGE,int(size)))
    from preview_codec import save_preview
    name=cache_name(record,size,decode);key=Path(name).stem
    index=cache_index or cache_for(cache);target=index.folder/name
    hit=index.get(target.name)
    if hit is not None and not record.get('_portable_prepare'):return hit
    cached_source=index.larger(record,size,decode) if record.get('_catalog') and hit is None else None
    # 旧 JPEG 不强制批量重建；原片离线时仍可浏览已核验的历史预览。
    for version in (4,3):
        old_name=cache_name(record,size,decode,version=version)
        if not (index.folder/old_name).is_file():continue
        try:online=any(resolved_file(root,record[k]).is_file() for k in ('jpg','raw') if record.get(k))
        except OSError:online=False
        if not online:
            legacy=index.get(old_name)
            if legacy is not None:return legacy
    try:
        content=None if record.get('_video') else index.known_content(record)
        if not record.get('_video') and content is None and cached_source is None and (
                record.get('_portable_prepare') or not record.get('_background_preview') and index.has_portable() and index.candidate(record,root)):
            content=index.content_key(record,root)
    except (OSError,ValueError):content=None
    if hit is not None:
        if content:index.bind_content(content,record,size,decode,target.name)
        return hit
    portable=index.find_content(content,size,record.get('orientation',1),decode) if content else None
    if portable is not None:
        owner=index if portable.parent==index.folder else cache_for(portable.parent)
        owner.bind_content(content,record,size,decode,portable.name)
        return portable
    with _preview_lock(key):
        hit=index.get(target.name)
        if hit is not None:return hit
        with _PREVIEW_SLOTS,(_DECODE_SLOT if decode else nullcontext()):
            load_size=size
            image=None
            payload=None if decode or record.get('_video') or cached_source is not None else _jpeg_source(record,root,exiftool)
            # RAW 方向可能由上面的同次 JPEG 提取补齐，重新确定缓存身份。
            name=cache_name(record,size,decode);target=index.folder/name
            hit=index.get(name)
            if hit is not None:return hit
            direct=embedded_jpeg(payload,size,record.get('orientation')) if payload else None
            if direct is not None:pass
            elif record.get('_warmed_image') is not None:
                image=record.pop('_warmed_image')
            elif payload is not None:
                image=_read_preview(io.BytesIO(payload),size,record.get('orientation'))
            elif cached_source is not None:
                image=_read_preview(cached_source,size,1)
            elif record.get('_video'):
                from video_preview import extract_frame
                image=extract_frame(record['_video'],load_size,record['_video_frame'])
            elif record.get('raw'):
                if decode:
                    import rawpy
                    with rawpy.imread(str(resolved_file(root,record['raw']))) as source:
                        image=Image.fromarray(source.postprocess(use_camera_wb=True,half_size=True))
                else:image=_raw_preview(record,root,exiftool,size)
            elif not decode:image=_companion_preview(record,root,load_size)
            if image is None and direct is None:raise RuntimeError('无可用预览；可在单张视图按需解码 RAW。')
            if image is not None:
                if size<=480:image.thumbnail((size,size),Image.Resampling.LANCZOS)
                else:
                    from gpu_media import resize_preview
                    image=resize_preview(image,size)
            # 独占创建临时文件，不能覆盖并最终清掉用户已有的同名 .tmp。
            with tempfile.NamedTemporaryFile(dir=index.folder,prefix='.framekeep-preview-',suffix='.tmp',delete=False) as stream:
                temp=Path(stream.name)
            try:
                if direct is not None:temp.write_bytes(direct)
                else:save_preview(image,temp,decode=decode)
                index.publish(temp,target,record,size,decode)
                if not record.get('_video') and content is None and record.get('_portable_prepare'):
                    try:content=index.content_key(record,root)
                    except (OSError,ValueError):pass
                if content:index.bind_content(content,record,size,decode,target.name)
            finally:temp.unlink(missing_ok=True)
            return target


def preview_bytes(record,root,cache,exiftool,size=480,decode=False):
    """读取与淘汰互斥，防止返回 Path 后恰被其他请求淘汰。"""
    index=cache_for(cache)
    for _ in range(3):
        target=preview(record,root,cache,exiftool,size,decode,cache_index=index)
        owner=index if target.parent==index.folder else cache_for(target.parent)
        payload=owner.get(target.name,read=True)
        if payload is not None:return payload
    raise OSError('预览缓存繁忙，请重试')

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
