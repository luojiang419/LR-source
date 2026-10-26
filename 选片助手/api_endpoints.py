"""用户 API 地址规范化；模型发现与推理共用同一个基址。"""
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit


def url_candidates(value):
    raw=str(value or '').strip().rstrip('/')
    if not raw or any(c.isspace() or ord(c)<32 for c in raw):
        raise ValueError('请填写有效接口地址，可输入 IP:端口、域名或完整 HTTP/HTTPS 地址')
    explicit='://' in raw
    try:
        parsed=urlsplit(raw if explicit else '//'+raw.removeprefix('//'))
        host=parsed.hostname;port=parsed.port
        if explicit and parsed.scheme not in ('http','https'):raise ValueError()
        if not host or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:raise ValueError()
        # 域名转IDNA以便urllib处理，IPv6保留标准方括号。
        host=host.encode('idna').decode('ascii')
        if any(c in host for c in '/\\@?#%'):raise ValueError()
        authority=('['+host+']') if ':' in host else host
        if port is not None:authority+=':'+str(port)
        path=parsed.path.rstrip('/')
        if '\\' in path:raise ValueError()
    except (ValueError,UnicodeError):
        raise ValueError('接口地址无效：使用 HTTP/HTTPS，端口为 0–65535，地址不能包含认证信息、查询串或片段') from None
    if explicit:schemes=[parsed.scheme]
    else:
        try:ipaddress.ip_address(host);is_ip=True
        except ValueError:is_ip=False
        first='https' if port==443 or not (is_ip or host=='localhost' or port is not None) else 'http'
        schemes=[first,'http' if first=='https' else 'https']
    return [urlunsplit((scheme,authority,path,'','')) for scheme in schemes]


def validated_url(value):return url_candidates(value)[0]


def api_base(value):
    url=validated_url(value)
    for suffix in ('/chat/completions','/responses','/models'):
        if url.endswith(suffix):return url[:-len(suffix)]
    return url if urlsplit(url).path else url+'/v1'


def model_bases(value):
    """优先标准路径，保留用户前缀；不猜测服务商或修改主机。"""
    groups=[]
    for url in url_candidates(value):
        base=api_base(url);paths=[base]
        if not urlsplit(url).path:paths.append(url)
        elif not re.search(r'/v\d+(?:beta\d*)?$',urlsplit(base).path):paths.append(base+'/v1')
        groups.append(list(dict.fromkeys(paths)))
    return groups


def api_endpoint(value,protocol):
    return api_base(value)+{'chat':'/chat/completions','responses':'/responses'}[protocol]
