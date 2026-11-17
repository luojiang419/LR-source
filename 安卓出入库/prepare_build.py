"""本地读取指定 OCR 配置，不输出或提交密钥。"""
from pathlib import Path
import re
ROOT=Path(__file__).resolve().parent
source=ROOT.parent/'DOC/OCR.txt'
text=source.read_text(encoding='utf-8-sig')
def find(pattern,default=''):
    m=re.search(pattern,text,re.I|re.M)
    return m.group(1).strip() if m else default
url=find(r'^URL\s*[：:]\s*(.+)')
if url and not url.startswith(('http://','https://')):url='http://'+url
key=find(r'^KEY\s*[：:]\s*(.+)')
if not url or not key:raise SystemExit('OCR 配置缺少 URL 或 KEY')
model=find(r'^.*?模型\s*[：:]\s*(.+)','glm-ocr')
def prop(s):return s.replace('\\','\\\\').replace('\n','').replace('\r','')
(ROOT/'ocr.local.properties').write_text('\n'.join(f'{k}={prop(v)}' for k,v in [('url',url),('key',key),('model',model)])+'\n',encoding='utf-8')
(ROOT/'local.properties').write_text('sdk.dir=D:/Android/Sdk\n',encoding='utf-8')
print('已准备本机 SDK 与 OCR 私有配置（密钥不输出）。')
