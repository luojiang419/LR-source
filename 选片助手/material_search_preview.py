"""图搜图工作进程的只读预览，和宿主使用相同的方向/RGB约定。"""
import io
from pathlib import Path
import subprocess

from PIL import Image, ImageOps


def preview(path, kind='image', exiftool=''):
    path = Path(path)
    if kind == 'design':
        from design_preview import preview as design
        return design(path,1600).convert('RGB')
    try:
        with Image.open(path) as source:
            source.draft('RGB',(1600,1600)); source.thumbnail((1600,1600))
            return ImageOps.exif_transpose(source).convert('RGB')
    except OSError as original:
        try:
            import rawpy
            with rawpy.imread(str(path)) as source:
                thumbnail = source.extract_thumb()
                if thumbnail.format == rawpy.ThumbFormat.JPEG:
                    with Image.open(io.BytesIO(thumbnail.data)) as image: return ImageOps.exif_transpose(image).convert('RGB')
                return Image.fromarray(thumbnail.data).convert('RGB')
        except Exception:
            if not exiftool: raise ValueError('素材没有可用图片预览') from original
            for tag in ('PreviewImage','JpgFromRaw','ThumbnailImage'):
                payload = subprocess.run([exiftool,'-b','-'+tag,str(path)],capture_output=True,timeout=30,
                                         creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)).stdout
                if payload:
                    with Image.open(io.BytesIO(payload)) as image: return ImageOps.exif_transpose(image).convert('RGB')
            raise ValueError('素材没有可用图片预览') from original
