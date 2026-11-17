"""归档浏览缩略图选项与不经磁盘缓存的文件内嵌图读取。"""
from collections import OrderedDict
from concurrent.futures import Future
from pathlib import Path
import io,threading



class ArchiveThumbnails:
    def __init__(self,data):
        self.path=Path(data)/'archive-thumbnail-settings.json';self.lock=threading.RLock();self.cache_enabled=False;self.rows=OrderedDict()
        # 进程内有界编码结果：重绘、预取后再显示及返回页面不再重读NAS原片。
        # 只在内存，不写磁盘缩略图；键含路径与源文件指纹，文件变化后自然失效。
        self.encoded=OrderedDict();self.encoded_bytes=0;self.encoded_limit=96*1024*1024
        # 同一缩略图同时只生成一次：后台预热与浏览器请求撞上时后到者等待结果。
        self.inflight={}

    def produce(self,key,build,timeout=60):
        hit=self.encoded_get(key)
        if hit is not None:return hit
        with self.lock:
            hit=self.encoded.get(key)
            if hit is not None:self.encoded.move_to_end(key);return hit
            waiter=self.inflight.get(key)
            if waiter is None:
                waiter=self.inflight[key]=Future();leader=True
            else:leader=False
        if not leader:return waiter.result(timeout)
        try:
            payload=build()
        except BaseException as exc:
            waiter.set_exception(exc)
            raise
        else:
            self.encoded_put(key,payload);waiter.set_result(payload)
            return payload
        finally:
            with self.lock:self.inflight.pop(key,None)

    def encoded_get(self,key):
        with self.lock:
            payload=self.encoded.get(key)
            if payload is not None:self.encoded.move_to_end(key)
            return payload

    def encoded_put(self,key,payload):
        if len(payload)>2*1024*1024:return
        with self.lock:
            previous=self.encoded.pop(key,None)
            if previous is not None:self.encoded_bytes-=len(previous)
            self.encoded[key]=payload;self.encoded_bytes+=len(payload)
            while self.encoded_bytes>self.encoded_limit and self.encoded:
                _,old=self.encoded.popitem(last=False);self.encoded_bytes-=len(old)

    def state(self):return {'cache_enabled':self.cache_enabled}

    def configure(self,enabled):
        if type(enabled) is not bool:raise ValueError('缩略图缓存开关必须为布尔值')
        if enabled:raise ValueError('当前版本仅支持文件直读，不再构建缩略图缓存')
        return self.state()

    def remember(self,rows):
        with self.lock:
            for row in rows:
                self.rows.pop(row['id'],None)
                self.rows[row['id']]={k:row[k] for k in ('id','path','media_type','fingerprint')}
            # 大目录连续浏览的一整屏列表都应命中行缓存，不为每张缩略图回查大索引。
            while len(self.rows)>8192:self.rows.popitem(last=False)

    def row(self,item):
        with self.lock:return self.rows.get(item)


def _exif_thumbnail(segment):
    if not segment.startswith(b'Exif\0\0'):return None
    data=segment[6:];order='little' if data[:2]==b'II' else 'big' if data[:2]==b'MM' else None
    if not order or len(data)<8:return None
    def number(offset,size):
        if offset<0 or offset+size>len(data):raise ValueError('EXIF偏移越界')
        return int.from_bytes(data[offset:offset+size],order)
    def directory(offset):
        count=number(offset,2)
        if count>4096 or offset+2+count*12+4>len(data):raise ValueError('EXIF目录越界')
        values={}
        for pos in range(offset+2,offset+2+count*12,12):
            tag,kind,amount=number(pos,2),number(pos+2,2),number(pos+4,4)
            if amount==1 and kind in (3,4):values[tag]=number(pos+8,2 if kind==3 else 4)
        return values,number(offset+2+count*12,4)
    try:
        if number(2,2)!=42:return None
        primary,next_ifd=directory(number(4,4))
        if not next_ifd:return None
        thumb,_=directory(next_ifd);offset,length=thumb.get(0x201,0),thumb.get(0x202,0)
        if not offset or length<4 or offset+length>len(data):return None
        payload=data[offset:offset+length]
        if not payload.startswith(b'\xff\xd8') or not payload.endswith(b'\xff\xd9'):return None
        orientation=thumb.get(0x112,primary.get(0x112,1))
        return payload,orientation if orientation in range(1,9) else 1
    except ValueError:return None


def jpeg_thumbnail(path):
    """只读JPEG头部APP段；不为读取内嵌图下载完整JPEG扫描数据。"""
    with Path(path).open('rb') as stream:
        if stream.read(2)!=b'\xff\xd8':return None
        for _ in range(128):
            if stream.tell()>512*1024:return None
            if stream.read(1)!=b'\xff':return None
            marker=stream.read(1)
            while marker==b'\xff':marker=stream.read(1)
            if not marker or marker in (b'\xda',b'\xd9'):return None
            if marker==b'\x01' or 0xd0<=marker[0]<=0xd8:continue
            length=int.from_bytes(stream.read(2),'big')-2
            if length<0:return None
            if marker==b'\xe1':
                found=_exif_thumbnail(stream.read(length))
                if found:return found
            else:stream.seek(length,1)
    return None


def file_thumbnail(row,size,exiftool,frame=3):
    import media_engine as media
    from preview_codec import save_preview
    path=Path(row['path']);path=media.resolved_file(path.parent,path.name);size=max(160,min(480,int(size)))
    if row['media_type']=='image':
        embedded=jpeg_thumbnail(path) if path.suffix.lower() in ('.jpg','.jpeg') else None
        if embedded:image=media._read_preview(io.BytesIO(embedded[0]),size,embedded[1])
        elif path.suffix.lower() in media.RAW_EXTS:
            orientation,payload=media.raw_thumbnail_metadata(path,exiftool())
            image=media._read_preview(io.BytesIO(payload),size,orientation) if payload else media._raw_preview({'raw':path.name,'orientation':orientation,'_catalog':True},path.parent,exiftool,size)
        else:image=media._read_preview(path,size)
    elif row['media_type']=='design':
        from design_preview import preview
        image=preview(path,size)
    else:
        from video_preview import extract_frame,validate_frame
        image=extract_frame(path,size,validate_frame(frame))
    image.thumbnail((size,size));output=io.BytesIO();save_preview(image,output);return output.getvalue()
