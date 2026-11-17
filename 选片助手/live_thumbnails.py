"""现场小图直接读取源文件，不依赖持久预览索引或写盘队列。"""
from io import BytesIO
from PIL import Image
import media_engine as media
from archive_thumbnails import jpeg_thumbnail
from preview_codec import save_preview


def thumbnail_bytes(record,root,exiftool,size=480):
    size=max(160,min(480,int(size)))
    with media._PREVIEW_SLOTS:
        image=None
        if record.get('jpg'):
            try:
                path=media.resolved_file(root,record['jpg'])
                embedded=jpeg_thumbnail(path) if path.suffix.lower() in ('.jpg','.jpeg') else None
                if embedded:
                    try:image=media._read_preview(BytesIO(embedded[0]),size,embedded[1])
                    except (OSError,ValueError):pass
                if image is None:image=media._read_preview(path,size,record.get('orientation'))
            except (OSError,ValueError):
                if not record.get('raw'):raise
        if image is None:image=media._raw_preview(record,root,exiftool,size)
        image.thumbnail((size,size),Image.Resampling.LANCZOS)
        stream=BytesIO();save_preview(image,stream);return stream.getvalue()
