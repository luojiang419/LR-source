"""可确认的预览跳过原因；网络、权限、缓存与组件故障仍须重试。"""
from pathlib import Path
import hashlib


class PreviewIgnored(RuntimeError):
    def __init__(self,code,message):
        super().__init__(message);self.code=code


def skip_reason(record,root,error):
    if isinstance(error,PreviewIgnored):return error
    values=record.get('_portable_paths') or [record.get('_design') or record.get('_video')] or []
    if not any(values):values=[str(Path(root)/record[k]) for k in ('jpg','raw') if record.get(k)]
    if record.get('_fallback_raw'):values=[*values,record['_fallback_raw']]
    paths=list(dict.fromkeys(Path(v) for v in values if v));stats=[];missing=0
    if not paths:return None
    for path in paths:
        try:stats.append((path,path.stat()))
        except FileNotFoundError:missing+=1
        except OSError:return None
    # 共享根不可访问时不能将整台离线NAS永久标成不支持。
    if missing==len(paths):
        try:reachable=Path(root).is_dir() or Path(root).anchor and Path(Path(root).anchor).is_dir()
        except OSError:reachable=False
        return PreviewIgnored('missing','源文件已不存在，保留索引与资料') if reachable else None
    if stats and all(stat.st_size==0 for _,stat in stats):return PreviewIgnored('empty','源文件为空，没有可读取的图像')
    expected=record.get('_portable_versions')
    if expected and not missing:
        actual=[hashlib.sha256(f'{str(path).casefold()}:{stat.st_size}:{stat.st_mtime_ns}'.encode()).hexdigest() for path,stat in stats]
        if actual!=expected:return None
    if not missing and any(text in str(error).lower() for text in ('broken data stream','image file is truncated')):
        return PreviewIgnored('corrupt','图片数据损坏，现有解码器无法完整读取')
    return None
