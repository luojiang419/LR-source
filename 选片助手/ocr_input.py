"""OCR 只读模型输入：复用清晰缓存，缺失时在内存取图，不生成浏览预览。"""
from dataclasses import dataclass
import io
from pathlib import Path

from PIL import Image
import media_engine as media
from preview_cache import cache_for
from preview_codec import embedded_jpeg


@dataclass
class OcrInput:
    payload: bytes
    cached: bool
    path: object = None
    version: object = None
    pair_key: str = ''
    members: tuple = ()

    def validate(self,catalog=None):
        if self.path is not None:
            current=self.path.stat()
            if (current.st_size,current.st_mtime_ns)!=self.version:raise ValueError('解析期间源图片发生变化，未保存旧结果')
        if self.members:
            with catalog.connect() as c:
                current=tuple(tuple(r) for r in c.execute('''SELECT i.id,i.fingerprint FROM items i JOIN roots r ON r.id=i.root
                    WHERE i.pair_key=? AND i.online=1 AND r.removed=0 ORDER BY i.extension,i.path''',(self.pair_key,)))
            if current!=self.members:raise ValueError('归档图片版本已变化，未保存旧 OCR 结果')


def _memory_image(path,record,root,tool,edge,expected=None):
    before=path.stat()
    if expected and (before.st_size!=expected['size'] or abs(before.st_mtime-expected['modified'])>.001):
        raise ValueError('归档源图已变化，请刷新索引')
    if path.suffix.lower() in media.RAW_EXTS:
        raw=dict(record,raw=path.name,jpg=None);raw.pop('_fallback_raw',None)
        image=media._raw_preview(raw,str(path.parent),tool,edge)
    elif before.st_size<=64*1024**2:
        payload=path.read_bytes()
        direct=embedded_jpeg(payload,edge,record.get('orientation'))
        if direct is not None:
            prepared=OcrInput(direct,False,path,(before.st_size,before.st_mtime_ns));prepared.validate();return prepared
        image=media._read_preview(io.BytesIO(payload),edge,record.get('orientation'))
    else:image=media._read_preview(path,edge,record.get('orientation'))
    image.thumbnail((edge,edge),Image.Resampling.LANCZOS)
    output=io.BytesIO();image.convert('RGB').save(output,'JPEG',quality=92)
    prepared=OcrInput(output.getvalue(),False,path,(before.st_size,before.st_mtime_ns));prepared.validate();return prepared


def record_input(record,root,cache,tool,edge=1600,*,expected=None,progress=lambda phase:None):
    edge=max(480,min(2400,int(edge)))
    progress('cache_lookup')
    hit=cache_for(cache).input_bytes(record,edge)
    if hit is not None:
        progress('cached_input')
        direct=embedded_jpeg(hit,edge)
        if direct is None:
            image=media._read_preview(io.BytesIO(hit),edge)
            image.thumbnail((edge,edge),Image.Resampling.LANCZOS)
            output=io.BytesIO();image.convert('RGB').save(output,'JPEG',quality=92);direct=output.getvalue()
        return OcrInput(direct,True)
    errors=[]
    # JPG 可直接供 OCR 使用，不因存在配对 RAW 而先访问 RAW 容器。
    for name in dict.fromkeys(record.get(key) for key in ('jpg','raw','_fallback_raw') if record.get(key)):
        path=Path(name) if Path(name).is_absolute() else Path(root)/name
        known=expected.get(str(path)) if expected else None
        try:progress('source_input');return _memory_image(path,record,root,tool,edge,known)
        except (OSError,RuntimeError) as exc:errors.append(exc)
    if errors:raise errors[-1]
    raise ValueError('没有可用于 OCR 的图片文件')


def catalog_input(catalog,row,edge=1600,*,result_only=False,progress=lambda phase:None):
    with catalog.connect() as c:
        members=[dict(r) for r in c.execute('''SELECT i.* FROM items i JOIN roots r ON r.id=i.root
            WHERE i.pair_key=? AND i.online=1 AND r.removed=0 ORDER BY i.extension,i.path''',(row['pair_key'],))]
    if not members or not any(r['id']==row['id'] and r['fingerprint']==row['fingerprint'] for r in members):
        raise ValueError('归档图片版本已变化，未读取旧图片')
    signature=tuple((r['id'],r['fingerprint']) for r in members)
    if result_only:prepared=OcrInput(b'',True)
    else:
        root,record=catalog.preview_record(row,members)
        tool=lambda:media.find_exiftool(Path(__file__).resolve().parents[1])
        prepared=record_input(record,root,catalog.previews,tool,edge,expected={r['path']:r for r in members},progress=progress)
    prepared.pair_key=row['pair_key'];prepared.members=signature
    return prepared
