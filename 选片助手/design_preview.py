"""只读PSD/PSB资源缩略图；不展开图层，不修改设计源文件。"""
import io
from pathlib import Path
import struct

from PIL import Image
from preview_errors import PreviewIgnored


def embedded_thumbnail(path):
    def read(stream,length):
        value=stream.read(length)
        if len(value)!=length:raise ValueError('PSD结构不完整')
        return value
    with Path(path).open('rb') as stream:
        total=stream.seek(0,2);stream.seek(0)
        header=read(stream,26)
        if header[:4]!=b'8BPS' or struct.unpack('>H',header[4:6])[0] not in (1,2):raise ValueError('PSD文件头无效')
        color_size=struct.unpack('>I',read(stream,4))[0]
        if stream.tell()+color_size+4>total:raise ValueError('PSD颜色段越界')
        stream.seek(color_size,1);length=struct.unpack('>I',read(stream,4))[0];end=stream.tell()+length
        if end>total:raise ValueError('PSD资源段越界')
        blocks=0
        while stream.tell()<end:
            blocks+=1
            if blocks>65536:raise ValueError('PSD资源过多')
            if read(stream,4)!=b'8BIM':raise ValueError('PSD资源签名无效')
            ident=struct.unpack('>H',read(stream,2))[0];name_length=read(stream,1)[0]
            stream.seek(name_length+(1+name_length)%2,1)
            length=struct.unpack('>I',read(stream,4))[0];start=stream.tell();next_block=start+length+length%2
            if next_block>end:raise ValueError('PSD资源长度越界')
            if ident in (1033,1036) and 28<=length<=16*1024**2:
                kind,width,height,row_bytes,_,compressed,bits,planes=struct.unpack('>6I2H',read(stream,28))
                if not 0<width*height<=16_000_000 or bits!=24 or planes!=1:raise ValueError('PSD缩略图参数无效')
                if kind==1 and 0<compressed<=length-28:
                    with Image.open(io.BytesIO(read(stream,compressed))) as image:
                        if image.format!='JPEG' or image.size!=(width,height):raise ValueError('PSD缩略图尺寸不一致')
                        result=image.convert('RGB')
                    if ident==1033:r,g,b=result.split();result=Image.merge('RGB',(b,g,r))
                    return result
                if kind==0 and width*3<=row_bytes<=width*3+3 and row_bytes*height<=length-28:
                    return Image.frombytes('RGB',(width,height),read(stream,row_bytes*height),'raw','BGR' if ident==1033 else 'RGB',row_bytes)
            stream.seek(next_block)
    return None


def preview(path,size):
    path=Path(path)
    if path.suffix.lower() not in ('.psd','.psb'):raise PreviewIgnored('unsupported','此设计格式暂不支持图像预览，已保留源文件与资料')
    try:image=embedded_thumbnail(path)
    except (ValueError,OSError) as error:
        if isinstance(error,OSError) and error.errno is not None:raise
        # 资源缩略图坏损仍可尝试Pillow支持的PSD合成图。
        image=None
    if image is None:
        try:
            with Image.open(path) as source:
                if source.format!='PSD':raise ValueError('不是受支持的PSD合成图')
                if source.width*source.height>64_000_000:raise PreviewIgnored('unsupported','设计文件无内嵌预览，合成图超出安全解码尺寸')
                source.thumbnail((size,size));image=source.convert('RGB')
        except (ValueError,OSError) as error:
            if isinstance(error,OSError) and error.errno is not None:raise
            raise PreviewIgnored('unsupported','PSD/PSB没有可用内嵌缩略图，合成图版本或压缩方式暂不支持') from error
    image.thumbnail((size,size));return image
