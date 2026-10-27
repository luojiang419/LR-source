"""对已启动的隔离演示进行真实 HTTP 冒烟检查，不输出或保存明文 Key。"""
import io
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from PIL import Image

URL='http://127.0.0.1:8792'
checks={}


def request(path, body=None, token=None, key=None):
    headers={}
    if token:headers['X-Framekeep-Token']=token
    if key:headers['Authorization']='Bearer '+key
    if body is not None:headers['Content-Type']='application/json'
    data=json.dumps(body).encode() if body is not None else None
    try:
        with urlopen(Request(URL+path,data=data,headers=headers),timeout=15) as response:
            raw=response.read()
            return response.status,json.loads(raw) if 'json' in response.headers.get('Content-Type','') else raw
    except HTTPError as error:
        return error.code,json.loads(error.read())


def check(name, condition):
    checks[name]=bool(condition)
    if not condition:raise AssertionError(name)


def main():
    _,state=request('/api/state')
    token=state['request_token']
    check('真实选片会话载入六张图片',state['indexed_captures']==6)
    _,config=request('/api/split/config')
    root=config['roots'][0]['id']
    status,_=request('/api/split/create-key',{'name':'bad','roots':[root]})
    check('管理请求需要工作台令牌',status==403)
    status,result=request('/api/split/create-key',{'name':'验证用密钥（已停用）','roots':[root]},token)
    check('创建真实命名密钥',status==200 and result['api_key'].startswith('fk_'))
    key,key_id=result['api_key'],result['id']
    status,_=request('/api/library/v1/roots')
    check('未携带密钥拒绝访问',status==401)
    status,public=request('/api/library/v1/roots',key=key)
    check('真实Bearer鉴权并隐藏物理路径',status==200 and all('path' not in r for r in public['roots']))
    _,sources=request('/api/split/sources')
    device=next(d for d in sources['devices'] if d.get('remote') and d['name']=='参考资料工作站')
    remote_root=device['roots'][0]['id']
    _,folders=request('/api/split/library/folders?'+urlencode({'device':device['id'],'root':remote_root}))
    check('独立设备返回层级目录',folders['folders'][0]['name']=='秋冬女装')
    _,items=request('/api/split/library/items?'+urlencode({'device':device['id'],'root':remote_root,'folder':'秋冬女装/棚拍','q':'5234'}))
    check('远端Everything真实检索',items['total']==1 and items['engine']=='Everything + 目录数据库')
    _,binary=request('/api/split/library/preview?'+urlencode({'device':device['id'],'root':remote_root,'file':items['rows'][0]['relative'],'size':420}))
    with Image.open(io.BytesIO(binary)) as image:
        check('远端传输可解码真实图片',image.width>0 and image.height>0 and max(image.size)<=420)
    status,_=request('/api/library/v1/preview?'+urlencode({'root':root,'file':'../resource-config.json'}),key=key)
    check('路径穿越被拒绝',status==400)
    status,_=request('/api/split/add-peer',{'name':'无效鉴权测试','address':'127.0.0.1:8793','api_key':'fk_invalid'},token)
    check('错误设备密钥不被保存',status==400)
    _,revealed=request('/api/split/reveal-key',{'id':key_id},token)
    check('本机查看返回同一密钥',revealed['api_key']==key)
    request('/api/split/disable-key',{'id':key_id},token)
    status,_=request('/api/library/v1/roots',key=key)
    check('停用即时撤销访问',status==401)
    output=Path(__file__).resolve().parents[1]/'build/split-capture-demo/http-verification.json'
    output.write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(checks,ensure_ascii=False,indent=2))
    print(f'通过 {len(checks)} 项真实 HTTP 检查')


if __name__=='__main__':main()
