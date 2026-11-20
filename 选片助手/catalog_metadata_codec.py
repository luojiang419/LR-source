"""元数据常用字段保持 JSON 可查询，全部原始标签压缩保存、详情按需还原。"""
import base64
import json
import zlib

FORMAT = 2
KEPT_TAGS = {'XMP-fk:SKU', 'XMP-fk:ShootType', 'XMP-fk:AssetID',
             'XMP-fk:Revision', 'XMP-dc:Subject', 'XMP-xmp:Rating'}


def pack(payload):
    result = {key: value for key, value in payload.items() if key not in ('tags', '_tags_z')}
    tags = payload.get('tags', {})
    result['tags'] = {key: value for key, value in tags.items() if key in KEPT_TAGS}
    result['_tags_z'] = base64.b64encode(zlib.compress(json.dumps(tags, ensure_ascii=False,
        separators=(',', ':')).encode(), 6)).decode('ascii')
    result['_format'] = FORMAT
    return json.dumps(result, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def unpack(raw):
    result = json.loads(raw)
    if '_tags_z' in result:
        result['tags'] = json.loads(zlib.decompress(base64.b64decode(result.pop('_tags_z'), validate=True)))
    result.pop('_format', None)
    return result
