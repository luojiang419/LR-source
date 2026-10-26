"""预览编码与格式识别；不携带原片元数据，不改变原图。"""
import io

from PIL import Image

RECIPE=6
MAX_PREVIEW_EDGE=2048
QUALITY=85
DETAIL_QUALITY=85


def image_mime(payload):
    if payload.startswith(b'\xff\xd8'):return 'image/jpeg'
    if len(payload)>=16 and payload[:4]==b'RIFF' and payload[8:12]==b'WEBP':return 'image/webp'
    raise ValueError('预览图片格式无效')


def validate_stream(stream,size):
    stream.seek(0);head=stream.read(16);kind=image_mime(head)
    if kind=='image/webp':
        if int.from_bytes(head[4:8],'little')+8!=size or head[12:16] not in (b'VP8 ',b'VP8L',b'VP8X'):
            raise ValueError('WebP 缓存不完整')
    else:
        stream.seek(-2,2)
        if stream.read()!=b'\xff\xd9':raise ValueError('JPEG 缓存不完整')
    stream.seek(0)
    return kind


def save_preview(image,path,*,decode=False):
    # 已朝正的像素快速编码；不运行 optimize，不携带源 EXIF/ICC/XMP。
    image.convert('RGB').save(path,format='JPEG',quality=QUALITY)


def embedded_jpeg(payload,size,orientation=None):
    """尺寸合适时只整理 JPEG 头，不解码/重编码扫描数据；方向交给浏览器。"""
    if not payload or not payload.startswith(b'\xff\xd8') or not payload.endswith(b'\xff\xd9'):return None
    with Image.open(io.BytesIO(payload)) as image:
        if image.format!='JPEG' or image.mode not in ('RGB','L') or max(image.size)>min(size,MAX_PREVIEW_EDGE):return None
        embedded=image.getexif().get(274)
        try:orientation=int(embedded if embedded in range(1,9) else orientation or 1)
        except (ValueError,TypeError):orientation=1
        if orientation not in range(1,9):orientation=1
    # 去掉原 EXIF/XMP/ICC/IPTC/注释，只保留压缩参数、JFIF 和 Adobe 色彩变换头。
    parts=[b'\xff\xd8'];offset=2
    if orientation!=1:
        exif=Image.Exif();exif[274]=orientation;data=exif.tobytes()
        parts.append(b'\xff\xe1'+(len(data)+2).to_bytes(2,'big')+data)
    while offset<len(payload):
        start=offset
        if payload[offset]!=255:raise ValueError('JPEG 标记无效')
        while offset<len(payload) and payload[offset]==255:offset+=1
        if offset>=len(payload):raise ValueError('JPEG 标记不完整')
        marker=payload[offset];offset+=1
        if marker==0xda:
            parts.append(payload[start:]);return b''.join(parts)
        if marker in (0xd8,0xd9,0x01) or 0xd0<=marker<=0xd7:
            parts.append(payload[start:offset]);continue
        length=int.from_bytes(payload[offset:offset+2],'big')
        if length<2 or offset+length>len(payload):raise ValueError('JPEG 段不完整')
        offset+=length
        if marker!=0xfe and not (0xe1<=marker<=0xef and marker!=0xee):parts.append(payload[start:offset])
    raise ValueError('JPEG 缺少扫描数据')


def jpeg_payload(payload,quality=95):
    """OCR/AI 的既有 JPEG 协议只在发送时转码，不写另一份磁盘缓存。"""
    if not (payload[:4]==b'RIFF' and payload[8:12]==b'WEBP'):return payload
    output=io.BytesIO()
    with Image.open(io.BytesIO(payload)) as image:
        from gpu_media import encode_jpeg
        accelerated=encode_jpeg(image,quality)
        if accelerated is not None:return accelerated
        image.convert('RGB').save(output,format='JPEG',quality=quality,optimize=True)
    return output.getvalue()


def compatible_image(payload,accept=''):
    """局域网旧客户端只识别 JPEG；转码仅用于响应，不增加磁盘副本。"""
    supported=False
    for item in accept.lower().split(','):
        parts=[p.strip() for p in item.split(';')]
        if parts[0]!='image/webp':continue
        try:supported=any(float(p[2:])>0 for p in parts[1:] if p.startswith('q=')) if any(p.startswith('q=') for p in parts[1:]) else True
        except ValueError:supported=False
        if supported:break
    if image_mime(payload)!='image/webp' or supported:return payload
    return jpeg_payload(payload,quality=85)
