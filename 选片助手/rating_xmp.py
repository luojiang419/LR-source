"""标准 XMP 评级读取；不执行来源 XML 中的实体。"""
from pathlib import Path
import hashlib
import json
import xml.etree.ElementTree as ET

RATING='{http://ns.adobe.com/xap/1.0/}Rating'
NAMESPACES={
    'http://ns.adobe.com/xap/1.0/':'XMP-xmp',
    'http://purl.org/dc/elements/1.1/':'XMP-dc',
    'https://framekeep.local/ns/asset/1.0/':'XMP-fk',
}


def sidecar_paths(path):
    """按覆盖顺序返回实际侧车；Windows 文件系统同时兼容 .XMP 大小写。"""
    path=Path(path)
    candidates=(path.with_suffix('.xmp'),Path(str(path)+'.xmp'))
    return [p for p in dict.fromkeys(candidates) if p.is_file() and not p.is_symlink() and not p.is_junction()]


def sidecar_signature(paths):
    sides=dict.fromkeys(side for path in paths for side in sidecar_paths(path))
    return tuple((str(side),side.stat().st_size,side.stat().st_mtime_ns) for side in sides)


def sidecar_fingerprint(paths):
    return hashlib.sha256(json.dumps(sidecar_signature(paths),ensure_ascii=False).encode()).hexdigest()


def load_sidecar(path):
    path=Path(path)
    if path.is_symlink() or path.is_junction():raise ValueError('XMP 侧车不能是链接')
    if path.stat().st_size>8_000_000:raise ValueError('XMP 侧车超过 8 MB，未读取或覆盖')
    content=path.read_bytes();declarations=content.replace(b'\x00',b'').upper()
    if b'<!DOCTYPE' in declarations or b'<!ENTITY' in declarations:raise ValueError('XMP 包含实体声明，未读取或覆盖')
    try:return ET.fromstring(content)
    except ET.ParseError:raise ValueError('XMP 侧车格式损坏，未覆盖：'+path.name) from None


def sidecar_metadata(path,*,grouped=False):
    """只读常用描述/备案字段；不解释 Camera Raw 的调色参数。"""
    result={}
    def field(tag,value):
        if not tag.startswith('{'):return
        uri,name=tag[1:].split('}',1)
        group=NAMESPACES.get(uri)
        if group is None:return
        if group=='XMP-xmp' and name=='Rating':
            try:value=int(value)
            except (ValueError,TypeError):return
            if value not in range(-1,6):return
        if group=='XMP-dc':
            name={'subject':'Subject','title':'Title','description':'Description'}.get(name)
            if name is None:return
        result[group+':'+name if grouped else name]=value
    for target in sidecar_paths(path):
        try:
            root=load_sidecar(target)
            for node in root.iter():
                for tag,value in node.attrib.items():field(tag,value)
                if node.tag.startswith('{') and node.tag[1:].split('}',1)[0] in NAMESPACES:
                    leaves=[child for child in node.iter() if child.tag=='{http://www.w3.org/1999/02/22-rdf-syntax-ns#}li']
                    if leaves:
                        default=next((child for child in leaves if child.get('{http://www.w3.org/XML/1998/namespace}lang')=='x-default'),leaves[0])
                        value=[child.text or '' for child in leaves] if node.tag.endswith('}subject') else default.text or ''
                    else:value=node.text
                    if value is not None:field(node.tag,value)
        except (OSError,ValueError,ET.ParseError):continue
    return result


def sidecar_rating(path):
    return sidecar_metadata(path).get('Rating')
