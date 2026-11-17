"""SKU小图复用归档的有界内存与并发合并机制，不生成磁盘缓存。"""
from pathlib import Path

from archive_thumbnails import ArchiveThumbnails
from direct_previews import preview_bytes

_THUMBNAILS=ArchiveThumbnails(Path('.'))


def library_preview_bytes(record,root,exiftool,size=480,decode=False):
    size=max(160,min(480,int(size))) if int(size)<=480 else int(size)
    fingerprint=record.get('fingerprint')
    if not fingerprint or decode or size>480:
        return preview_bytes(record,root,exiftool,size,decode)
    key=(str(root),record.get('id'),record.get('jpg'),record.get('raw'),fingerprint,record.get('orientation',1),size)
    return _THUMBNAILS.produce(key,lambda:preview_bytes(record,root,exiftool,size,decode))
