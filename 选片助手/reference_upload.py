"""无文件体积上限的参考图流式接收；RAW 在工作站提取预览，不展开全尺寸像素。"""
import base64
import io
from pathlib import Path
import tempfile
import threading
from urllib.parse import unquote

from PIL import Image
import media_engine as media

_SLOTS=threading.BoundedSemaphore(2)
CHUNK=1024*1024
IMAGE_EXTENSIONS={'.jpg','.jpeg','.png','.webp','.tif','.tiff','.bmp','.gif','.heic','.heif'}


def prepare(path,app_root):
    path=Path(path)
    if path.suffix.lower() in media.RAW_EXTS:
        try:tool=media.find_exiftool(app_root)
        except RuntimeError:tool=None
        payload=None;orientation=1
        if tool:
            try:orientation,payload=media.raw_embedded_metadata(path,tool)
            except (OSError,ValueError,RuntimeError):pass
        if payload:
            image=media._read_preview(io.BytesIO(payload),1600,orientation)
        else:
            image=media._raw_preview({'raw':path.name},path.parent,tool,1600)
    else:
        # 不走可能整文件读取的 GPU JPEG 快捷路径；大文件只由解码器按需读。
        with Image.open(path) as source:
            orientation=source.getexif().get(274,1)
            source.draft('RGB',(1600,1600));source.thumbnail((1600,1600),Image.Resampling.LANCZOS)
            image=media._orient_preview(source.convert('RGB'),orientation)
    image.thumbnail((1600,1600),Image.Resampling.LANCZOS)
    output=io.BytesIO();image.convert('RGB').save(output,'JPEG',quality=88)
    return output.getvalue(),image.size


def receive(stream,length,filename,app_root):
    if not isinstance(length,int) or length<=0:raise ValueError('上传文件为空或长度无效')
    name=unquote(str(filename or '参考图.jpg')).replace('\\','/').rsplit('/',1)[-1][:255]
    suffix=Path(name).suffix.lower()
    if suffix and suffix not in IMAGE_EXTENSIONS|media.RAW_EXTS:raise ValueError('请选择图片或相机 RAW 文件')
    if not _SLOTS.acquire(blocking=False):raise ValueError('正在准备其他参考图，请稍后重试')
    try:
        # 每次只读取 1 MB；未设文件大小上限，也不把原文件一次读入内存。
        with tempfile.TemporaryDirectory(prefix='framekeep-reference-') as folder:
            target=Path(folder)/('reference'+(suffix or '.image'));remaining=length
            with target.open('wb') as output:
                while remaining:
                    chunk=stream.read(min(CHUNK,remaining))
                    if not chunk:raise ValueError('图片上传未完成，请重新选择文件')
                    output.write(chunk);remaining-=len(chunk)
            try:payload,size=prepare(target,app_root)
            except Exception as exc:raise ValueError('无法读取该图片或 RAW 预览，请检查文件是否完整及相机格式支持') from exc
            return {'image':'data:image/jpeg;base64,'+base64.b64encode(payload).decode('ascii'),
                    'name':name,'source_bytes':length,'width':size[0],'height':size[1]}
    finally:_SLOTS.release()
