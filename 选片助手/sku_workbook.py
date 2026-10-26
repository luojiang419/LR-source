"""xlsx 款号定位与增量状态列；只改工作表 XML，保留 WPS 原生图片等部件。"""
from pathlib import Path
import posixpath
import re
from xml.etree import ElementTree as E
from xml.sax.saxutils import escape,quoteattr
from zipfile import ZipFile,ZIP_DEFLATED

from sku_match import normalize

NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
N={'m':NS};RID='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
HEADERS={'款号','货号','SKU','产品款号','SKU款号'}
DRAW='http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'
ART='http://schemas.openxmlformats.org/drawingml/2006/main'
REL='http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def _relationships(z,part):
    relpart=posixpath.join(posixpath.dirname(part),'_rels',posixpath.basename(part)+'.rels')
    if relpart not in z.namelist():return {}
    result={}
    for rel in E.fromstring(z.read(relpart)):
        if rel.get('TargetMode')=='External':continue
        target=rel.get('Target','')
        path=target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join(posixpath.dirname(part),target))
        if path.startswith('xl/') and '..' not in path.split('/') and path in z.namelist():result[rel.get('Id')]=path
    return result


def _cell_image_catalog(z):
    part='xl/cellimages.xml'
    if part not in z.namelist():return {}
    rels=_relationships(z,part);result={}
    for pic in E.fromstring(z.read(part)).iter('{'+DRAW+'}pic'):
        props=pic.find('.//{'+DRAW+'}cNvPr');blip=pic.find('.//{'+ART+'}blip')
        if props is not None and blip is not None:
            media=rels.get(blip.get('{'+REL+'}embed'))
            if media and media.startswith('xl/media/'):result[props.get('name')]=media
    return result


def _sheet_images(z,part,doc,cells,strings,catalog):
    images=[]
    for ref,cell in cells.items():
        # 缓存文本和公式都可能带 DISPIMG；只解析引用，不执行任何公式。
        text=cell.findtext('m:f','',N)+' '+cell_value(cell,strings)
        match=re.search(r'\bDISPIMG\s*\(\s*"([^"]+)"',text,re.I)
        media=catalog.get(match.group(1)) if match else None
        if media:images.append({'cell':ref,'media':media})
    # 普通 Excel 浮动图同样按锚点所在行关联，外部链接不下载。
    rels=_relationships(z,part)
    for drawing in doc.findall('m:drawing',N):
        drawingpart=rels.get(drawing.get(RID))
        if not drawingpart:continue
        image_rels=_relationships(z,drawingpart)
        for anchor in E.fromstring(z.read(drawingpart)):
            origin=anchor.find('{'+DRAW+'}from')
            if origin is None:continue
            row=int(origin.findtext('{'+DRAW+'}row','0'))+1;col=int(origin.findtext('{'+DRAW+'}col','0'))+1
            for blip in anchor.iter('{'+ART+'}blip'):
                media=image_rels.get(blip.get('{'+REL+'}embed'))
                if media and media.startswith('xl/media/'):
                    entry={'cell':f'{col_name(col)}{row}','media':media}
                    if entry not in images:images.append(entry)
    return images


def read_image(path,entry):
    """只读取已索引的包内位图，禁止任意路径或外部 URL。"""
    if not re.fullmatch(r'xl/media/[^/\\]+\.(?:png|jpe?g|gif|webp|bmp)',entry,re.I):raise ValueError('不支持此图片格式')
    with ZipFile(path) as z:
        info=z.getinfo(entry)
        if not 0<info.file_size<=20_000_000:raise ValueError('图片过大或为空')
        data=z.read(info)
    if data.startswith(b'\x89PNG\r\n\x1a\n'):kind='image/png'
    elif data.startswith(b'\xff\xd8\xff'):kind='image/jpeg'
    elif data.startswith((b'GIF87a',b'GIF89a')):kind='image/gif'
    elif data.startswith(b'RIFF') and data[8:12]==b'WEBP':kind='image/webp'
    elif data.startswith(b'BM'):kind='image/bmp'
    else:raise ValueError('图片资源格式无法识别')
    return data,kind


def col_number(value):
    out=0
    for ch in re.match(r'[A-Z]+',value).group():out=out*26+ord(ch)-64
    return out


def col_name(number):
    value=''
    while number:number,remainder=divmod(number-1,26);value=chr(65+remainder)+value
    return value


def cell_value(cell,strings):
    value=cell.findtext('m:v','',N)
    if cell.get('t')=='s':return strings[int(value)] if value else ''
    if cell.get('t')=='inlineStr':return ''.join(cell.find('m:is',N).itertext())
    return value


def inspect_workbook(path,allow_empty=False):
    result=[]
    with ZipFile(path) as z:
        from sku_workbook_presentation import read_layout,canonical_values
        if sum(i.file_size for i in z.infolist())>150_000_000:raise ValueError('表格解压内容超过 150MB，请拆分后导入')
        layouts=read_layout(z)
        strings=[]
        if 'xl/sharedStrings.xml' in z.namelist():strings=[''.join(s.itertext()) for s in E.fromstring(z.read('xl/sharedStrings.xml')).findall('m:si',N)]
        catalog=_cell_image_catalog(z)
        rels={r.get('Id'):r.get('Target') for r in E.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
        for sheet in E.fromstring(z.read('xl/workbook.xml')).findall('m:sheets/m:sheet',N):
            target=rels[sheet.get(RID)];target=target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/'+target)
            if not target.startswith('xl/') or '..' in target.split('/'):raise ValueError('工作表引用路径无效')
            layout=layouts.get(target)
            if layout is not None and layout.get('summary')=='1':continue
            doc=E.fromstring(z.read(target));cells={c.get('r'):c for c in doc.findall('m:sheetData/m:row/m:c',N)}
            values={ref:cell_value(c,strings) for ref,c in cells.items()}
            header_aliases=canonical_values(values,layout)
            headers=[ref for ref,v in values.items() if v.strip() in HEADERS and int(re.sub('[A-Z]','',ref))<=50]
            if not headers:
                result.append({'name':sheet.get('name'),'path':target,'skipped':'未找到款号列','rows':[]});continue
            if len({col_number(ref) for ref in headers})>1:raise ValueError(f'{sheet.get("name")} 存在多列款号，需先明确唯一款号列')
            header=min(headers,key=lambda r:int(re.sub('[A-Z]','',r)));header_row=int(re.sub('[A-Z]','',header));sku_col=col_number(header)
            active=max((col_number(ref) for ref,c in cells.items() if values[ref]!='' or c.find('m:f',N) is not None),default=sku_col)
            statuses=[ref for ref,v in values.items() if v.strip()=='拍摄状态' and int(re.sub('[A-Z]','',ref))==header_row]
            if len(statuses)>1:raise ValueError(f'{sheet.get("name")} 有多个拍摄状态列，请先整理')
            status_col=col_number(statuses[0]) if statuses else active+1
            pictures=[ref for ref,v in values.items() if statuses and v.strip()=='款号图片' and int(re.sub('[A-Z]','',ref))==header_row and col_number(ref)==status_col+1]
            image_col=status_col+1 if pictures else None
            time_col=status_col+(2 if image_col else 1)
            if time_col>16384:raise ValueError('工作表没有可追加状态和完成时间的空列')
            times=[ref for ref,v in values.items() if v.strip()=='拍摄完成时间' and int(re.sub('[A-Z]','',ref))==header_row]
            if len(times)>1 or (times and col_number(times[0])!=time_col):raise ValueError(f'{sheet.get("name")} 的拍摄完成时间须紧邻拍摄状态右侧，请先整理该列')
            if not times and any(col_number(ref)==time_col and (values[ref]!='' or cell.find('m:f',N) is not None) for ref,cell in cells.items()):raise ValueError(f'{sheet.get("name")} 的拍摄状态右侧已有内容，请先插入空列，避免覆盖原数据')
            columns=[{'index':i,'letter':col_name(i),'title':values.get(f'{col_name(i)}{header_row}','').strip() or col_name(i)} for i in range(1,active+1) if i not in (status_col,time_col,image_col)]
            rows=[]
            for ref,value in values.items():
                row=int(re.sub('[A-Z]','',ref))
                if col_number(ref)!=sku_col or row<=header_row or not value.strip() or value.strip() in HEADERS:continue
                key=normalize(value)
                if not key:continue
                if len(key)>80:raise ValueError(f'{sheet.get("name")} 第 {row} 行款号过长，请检查款号列')
                status=values.get(f'{col_name(status_col)}{row}','').strip() or '未拍摄'
                rows.append({'excel_row':row,'sku':value,'normalized':key,'baseline':status,'baseline_completed_at':values.get(f'{col_name(time_col)}{row}','').strip(),
                             'cells':{col['letter']:values.get(f"{col['letter']}{row}",'') for col in columns}})
            result.append({'name':sheet.get('name'),'path':target,'header_row':header_row,'sku_col':sku_col,'status_col':status_col,
                           'status_letter':col_name(status_col),'new_status':not bool(statuses),'time_col':time_col,'time_letter':col_name(time_col),'image_col':image_col,'columns':columns,'rows':rows,
                           'images':_sheet_images(z,target,doc,cells,strings,catalog),'header_aliases':header_aliases})
    if not allow_empty and not any(s['rows'] for s in result):raise ValueError('表格中未找到有效款号行')
    return result


def _column_width(xml,prefix,column,width=16):
    def patch(match):
        cols=E.fromstring(match.group(0));parts=[];found=False
        for child in cols:
            attrs=dict(child.attrib);lo=int(attrs['min']);hi=int(attrs['max'])
            if lo<=column<=hi:
                found=True
                if lo<column:parts.append(dict(attrs,max=str(column-1)))
                parts.append(dict(attrs,min=str(column),max=str(column),width=str(width),customWidth='1'))
                if column<hi:parts.append(dict(attrs,min=str(column+1)))
            else:parts.append(attrs)
        if not found:parts.append({'min':str(column),'max':str(column),'width':str(width),'customWidth':'1'})
        parts.sort(key=lambda a:int(a['min']))
        return f'<{prefix}cols>'+''.join(f'<{prefix}col '+ ' '.join(f'{k}={quoteattr(v)}' for k,v in a.items())+'/>' for a in parts)+f'</{prefix}cols>'
    # 片段需附带主命名空间才能被 ET 单独读取；默认命名空间不会影响属性处理。
    pattern=r'<(?:\w+:)?cols\b[^>]*>.*?</(?:\w+:)?cols>'
    match=re.search(pattern,xml,re.S)
    if match:
        fragment=match.group(0)
        if prefix:fragment=fragment.replace('<'+prefix+'cols','<'+prefix+'cols xmlns:'+prefix[:-1]+'="'+NS+'"',1)
        class Fragment:
            def group(self,_):return fragment
        return xml[:match.start()]+patch(Fragment())+xml[match.end():]
    cols=f'<{prefix}cols><{prefix}col min="{column}" max="{column}" width="{width}" customWidth="1"/></{prefix}cols>'
    return re.sub(r'(<(?:\w+:)?sheetData\b)',lambda m:cols+m.group(1),xml,count=1)


def export_workbook(source,output,sheets,statuses,completed_times=None,*,capture_images=None):
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True);temp=output.with_suffix('.tmp.xlsx')
    # 旧任务布局迁移失败或源表变化时也不能绕过相邻列冲突检查。
    current={s['path']:s for s in inspect_workbook(source,allow_empty=True) if not s.get('skipped')}
    for plan in sheets:
        if plan.get('rows') and (plan['path'] not in current or plan['status_col']!=current[plan['path']]['status_col']):raise ValueError('工作表状态列布局已变化，请重新导入')
    plans={s['path']:s for s in sheets if s.get('rows')}
    with ZipFile(source) as original,ZipFile(temp,'w',ZIP_DEFLATED) as dest:
        for info in original.infolist():
            content=original.read(info.filename)
            if info.filename in plans:
                plan=plans[info.filename];xml=content.decode('utf-8-sig');root=re.search(r'<((?:\w+:)?)worksheet\b',xml)
                if not root:raise ValueError('暂不支持此工作表 XML 格式')
                prefix=root.group(1);time_col=plan.get('time_col',plan['status_col']+1)
                updates={r['excel_row']:{plan['status_col']:statuses.get((plan['name'],r['excel_row']),r['baseline']),time_col:(completed_times or {}).get((plan['name'],r['excel_row']),r.get('baseline_completed_at',''))} for r in plan['rows']}
                updates[plan['header_row']]={plan['status_col']:'拍摄状态',time_col:'拍摄完成时间'}
                def patch_row(match):
                    attrs=match.group('attrs');number=re.search(r'\br=["\'](\d+)["\']',attrs)
                    if not number or int(number.group(1)) not in updates:return match.group(0)
                    row=int(number.group(1));body=match.group('body')
                    for column,value in updates[row].items():body=patch_cell(body,row,column,value)
                    return '<'+match.group('tag')+attrs+'>'+body+'</'+match.group('tag')+'>'
                def patch_cell(body,row,column,value):
                    ref=f'{col_name(column)}{row}'
                    cells=list(re.finditer(r'<(?P<tag>(?:\w+:)?c)\b(?P<attrs>[^>]*?)(?:/>|>(?P<body>.*?)</(?P=tag)>)',body,re.S))
                    style='';cut=None;insert=len(body)
                    for c in cells:
                        addr=re.search(r'\br=["\']([A-Z]+\d+)["\']',c.group('attrs'))
                        if not addr:continue
                        pos=col_number(addr.group(1));s=re.search(r'\bs=["\'](\d+)["\']',c.group('attrs'))
                        if pos==column:cut=(c.start(),c.end());style=s.group(1) if s else style
                        elif pos<column and s:style=s.group(1)
                        elif pos>column:insert=min(insert,c.start())
                    if row==plan['header_row']:
                        for c in cells:
                            if re.search(r'\br=["\']'+col_name(plan['sku_col'])+str(row)+r'["\']',c.group('attrs')):
                                s=re.search(r'\bs=["\'](\d+)["\']',c.group('attrs'));style=s.group(1) if s else ''
                    new=f'<{prefix}c r="{ref}"'+(f' s="{style}"' if style else '')+f' t="inlineStr"><{prefix}is><{prefix}t>{escape(str(value))}</{prefix}t></{prefix}is></{prefix}c>'
                    body=body[:cut[0]]+new+body[cut[1]:] if cut else body[:insert]+new+body[insert:]
                    return body
                xml=re.sub(r'<(?P<tag>(?:\w+:)?row)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>',patch_row,xml,flags=re.S)
                xml=_column_width(xml,prefix,plan['status_col'])
                xml=_column_width(xml,prefix,time_col,38)
                def dimension(m):
                    old=m.group(1);end=old.split(':')[-1];last=max(col_number(end),time_col);row=int(re.sub('[A-Z]','',end))
                    return m.group(0).replace(old,old.split(':')[0]+':'+col_name(last)+str(row))
                xml=re.sub(r'<(?:\w+:)?dimension\b[^>]*\bref="([A-Z0-9:]+)"[^>]*/>',dimension,xml,count=1)
                content=xml.encode('utf8');E.fromstring(content)
            dest.writestr(info,content)
    if capture_images is not None:
        from sku_workbook_pictures import add_pictures
        add_pictures(temp,sheets,capture_images)
    temp.replace(output);return output
