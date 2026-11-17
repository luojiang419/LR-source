"""比较JPEG编码数据，跳过Exif/XMP APP1；相同编码无需完整像素解码。"""
import hashlib

def jpeg_image_digest(content):
    if not content.startswith(b'\xff\xd8'):raise ValueError('不是有效JPEG')
    digest=hashlib.sha256();digest.update(b'\xff\xd8');position=2;length=len(content)
    while position<length:
        start=position
        if content[position]!=255:raise ValueError('JPEG段边界无效')
        while position<length and content[position]==255:position+=1
        if position>=length:raise ValueError('JPEG被截断')
        marker=content[position];position+=1
        if marker==0xD9:
            digest.update(content[start:]);return digest.hexdigest()
        if marker in (0xD8,0x01) or 0xD0<=marker<=0xD7:
            digest.update(content[start:position]);continue
        if position+2>length:raise ValueError('JPEG段长度缺失')
        size=int.from_bytes(content[position:position+2],'big')
        if size<2 or position+size>length:raise ValueError('JPEG段被截断')
        position+=size
        if marker!=0xE1:digest.update(content[start:position])
        if marker==0xDA:
            entropy_start=position
            while position<length:
                position=content.find(b'\xff',position)
                if position<0:raise ValueError('JPEG扫描缺少结束标记')
                probe=position+1
                while probe<length and content[probe]==255:probe+=1
                if probe>=length:raise ValueError('JPEG扫描被截断')
                if content[probe]==0 or 0xD0<=content[probe]<=0xD7:position=probe+1;continue
                break
            digest.update(content[entropy_start:position])
    raise ValueError('JPEG缺少结束标记')
