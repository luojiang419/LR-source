"""工作副本插入款号图片列并嵌入缩略图，原件始终只读。"""
import hashlib
import io
import posixpath
import re
from xml.etree import ElementTree as E
from xml.sax.saxutils import escape,unescape,quoteattr
from zipfile import ZipFile,ZIP_DEFLATED

from PIL import Image,ImageOps
from sku_workbook import NS,N,DRAW,ART,REL,RID,col_name,col_number,_column_width

PKG='http://schemas.openxmlformats.org/package/2006/relationships'
CT='http://schemas.openxmlformats.org/package/2006/content-types'


def append_node(raw,tag,child):
    xml=raw.decode('utf-8-sig');root=re.search(r'<((?:\w+:)?)'+tag+r'\b[^>]*>',xml);prefix=root.group(1)
    if root.group(0).endswith('/>'):xml=xml[:root.start()]+root.group(0)[:-2]+'>'+child+f'</{prefix}{tag}>'+xml[root.end():]
    else:xml=xml.replace(f'</{prefix}{tag}>',child+f'</{prefix}{tag}>',1)
    return xml.encode('utf8')


def insert_columns(parts,plans,column_maps=None):
    """迁移旧状态／时间布局；标准单元格、公式、范围与图锚点随插入列平移。"""
    shifts={p['name']:p['status_col']+1 for p in plans if not p.get('image_col')} if column_maps is None else {name:1 for name in column_maps}
    def mapped(column,name):
        if column_maps is not None:return column_maps.get(name,{}).get(column,column)
        at=shifts.get(name);return column+int(bool(at and column>=at))
    contexts={p['path']:p['name'] for p in plans};sheets=[]
    workbook=E.fromstring(parts['xl/workbook.xml'])
    rels={r.get('Id'):r.get('Target') for r in E.fromstring(parts['xl/_rels/workbook.xml.rels'])}
    for sheet in workbook.find('m:sheets',N):
        target=rels[sheet.get(RID)];path=target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/'+target)
        contexts[path]=sheet.get('name');sheets.append(sheet.get('name'))
    for path,name in list(contexts.items()):
        relpath=posixpath.join(posixpath.dirname(path),'_rels',posixpath.basename(path)+'.rels')
        if relpath in parts:
            for rel in E.fromstring(parts[relpath]):
                if rel.get('TargetMode')=='External':continue
                target=rel.get('Target','');part=target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join(posixpath.dirname(path),target))
                if '/drawings/' in part or '/tables/' in part:contexts[part]=name
    reference=re.compile(r"(?<![\w.!\[\]])(?:(?P<sheet>'(?:[^']|'')+'|[\w\u0080-\uffff]+)!)?(?P<a>\$?[A-Z]{1,3})(?P<r>\$?\d+)(?::(?P<b>\$?[A-Z]{1,3})(?P<s>\$?\d+))?(?![\w.(])")
    full_columns=re.compile(r"(?<![\w.!\[\]])(?:(?P<sheet>'(?:[^']|'')+'|[\w\u0080-\uffff]+)!)?(?P<a>\$?[A-Z]{1,3}):(?P<b>\$?[A-Z]{1,3})(?![\w.])")
    def shift_refs(text,context):
        def replace(m):
            name=m['sheet'];name=name[1:-1].replace("''", "'") if name and name.startswith("'") else name
            name=name or context;at=shifts.get(name)
            if not at:return m.group(0)
            a=col_number(m['a'].lstrip('$'));b=col_number(m['b'].lstrip('$')) if m['b'] else None
            left=mapped(a,name);right=mapped(b,name) if b else None
            if b and column_maps is not None:
                values=[mapped(i,name) for i in range(min(a,b),max(a,b)+1)];left=min(values);right=max(values)
            def column(v,index):return ('$' if v.startswith('$') else '')+col_name(index)
            return ((m['sheet']+'!') if m['sheet'] else '')+column(m['a'],left)+(m.groupdict().get('r') or '')+(':'+column(m['b'],right)+(m.groupdict().get('s') or '') if m['b'] else '')
        # 字符串常量中的 A1 文本不作为引用修改。
        pieces=re.split(r'("(?:[^"]|"")*")',text)
        return ''.join(full_columns.sub(replace,reference.sub(replace,piece)) if i%2==0 else piece for i,piece in enumerate(pieces))
    for part,raw in list(parts.items()):
        if not part.endswith('.xml'):continue
        xml=raw.decode('utf-8-sig');context=contexts.get(part);at=shifts.get(context)
        if at and part.startswith('xl/worksheets/'):
            xml=re.sub(r'\b(r|ref|sqref)="([A-Z$][A-Z0-9$ :]+)"',lambda m:m[1]+'="'+shift_refs(m[2],context)+'"',xml)
            def cols(m):
                tag=m[0]
                return re.sub(r'\b(min|max)="(\d+)"',lambda v:v[1]+'="'+str(int(v[2])+int(int(v[2])>=at))+'"',tag)
            if column_maps is None:xml=re.sub(r'<(?:\w+:)?col\b[^>]*/>',cols,xml)
            else:
                def reorder_cols(m):
                    prefix=re.search(r'<((?:\w+:)?)cols\b',m[0])[1];doc=E.fromstring(m[0].replace('<'+prefix+'cols','<'+prefix+'cols xmlns:'+prefix[:-1]+'="'+NS+'"',1) if prefix else m[0]);descriptors={}
                    for child in doc:
                        attrs=dict(child.attrib)
                        for i in range(int(attrs.pop('min')),int(attrs.pop('max'))+1):descriptors[mapped(i,context)]=attrs
                    nodes=[]
                    for i,attrs in sorted(descriptors.items()):
                        if nodes and nodes[-1][2]==attrs and nodes[-1][1]+1==i:nodes[-1][1]=i
                        else:nodes.append([i,i,attrs])
                    return f'<{prefix}cols>'+''.join(f'<{prefix}col min="{lo}" max="{hi}" '+ ' '.join(k+'='+quoteattr(v) for k,v in attrs.items())+'/>' for lo,hi,attrs in nodes)+f'</{prefix}cols>'
                xml=re.sub(r'<(?:\w+:)?cols\b[^>]*>.*?</(?:\w+:)?cols>',reorder_cols,xml,flags=re.S)
                def reorder_row(m):
                    pattern=r'<(?:\w+:)?c\b[^>]*\br="([A-Z]+\d+)"[^>]*(?:/>|>.*?</(?:\w+:)?c>)';cells=list(re.finditer(pattern,m['body'],re.S))
                    body=''.join(c[0] for c in sorted(cells,key=lambda c:col_number(c[1])))+re.sub(pattern,'',m['body'],flags=re.S)
                    return '<'+m['tag']+m['attrs']+'>'+body+'</'+m['tag']+'>'
                xml=re.sub(r'<(?P<tag>(?:\w+:)?row)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>',reorder_row,xml,flags=re.S)
            xml=re.sub(r'\bspans="(\d+):(\d+)"',lambda m:'spans="'+str(min(mapped(i,context) for i in range(int(m[1]),int(m[2])+1)))+':'+str(max(mapped(i,context) for i in range(int(m[1]),int(m[2])+1)))+'"',xml)
        elif at and '/tables/' in part:
            table=E.fromstring(raw);ref=table.get('ref','');ends=ref.split(':');lo=col_number(ends[0]);hi=col_number(ends[-1])
            if column_maps is None and lo<at<=hi:
                columns=table.find('m:tableColumns',N)
                if columns is not None:
                    if any(c.get('name')=='款号图片' for c in columns):raise ValueError('Excel 表格已有“款号图片”列，请先为原产品图列改名，避免表格内列名重复')
                    pid=max([int(c.get('id','0')) for c in columns]+[0])+1
                    match=re.search(r'<(?P<tag>(?:\w+:)?tableColumns)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>',xml,re.S)
                    cells=list(re.finditer(r'<(?:\w+:)?tableColumn\b[^>]*(?:/>|>.*?</(?:\w+:)?tableColumn>)',match['body'],re.S))
                    index=at-lo;offset=cells[index].start() if index<len(cells) else len(match['body'])
                    body=match['body'][:offset]+f'<tableColumn xmlns="{NS}" id="{pid}" name="款号图片"/>'+match['body'][offset:]
                    attrs=re.sub(r'\bcount="\d+"',f'count="{len(columns)+1}"',match['attrs'])
                    xml=xml[:match.start()]+'<'+match['tag']+attrs+'>'+body+'</'+match['tag']+'>'+xml[match.end():]
            elif column_maps is not None:
                match=re.search(r'<(?P<tag>(?:\w+:)?tableColumns)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>',xml,re.S)
                if match:
                    cells=re.findall(r'<(?:\w+:)?tableColumn\b[^>]*(?:/>|>.*?</(?:\w+:)?tableColumn>)',match['body'],re.S)
                    cells=[value for _,value in sorted((mapped(lo+i,context),value) for i,value in enumerate(cells))]
                    xml=xml[:match.start()]+'<'+match['tag']+match['attrs']+'>'+''.join(cells)+'</'+match['tag']+'>'+xml[match.end():]
            xml=re.sub(r'\bref="([A-Z0-9:$]+)"',lambda m:'ref="'+shift_refs(m[1],context)+'"',xml)
        elif at and '/drawings/' in part:
            xml=re.sub(r'(<(?:\w+:)?col>)(\d+)(</(?:\w+:)?col>)',lambda m:m[1]+str(mapped(int(m[2])+1,context)-1)+m[3],xml)
        def formula(m):
            local=re.search(r'\blocalSheetId="(\d+)"',m['attrs']);ctx=sheets[int(local[1])] if local else context
            value=escape(shift_refs(unescape(m['body'],{'&quot;':'"','&apos;':"'"}),ctx))
            return '<'+m['tag']+m['attrs']+'>'+value+'</'+m['tag']+'>'
        xml=re.sub(r'<(?P<tag>(?:\w+:)?(?:f|formula|formula1|formula2|definedName|calculatedColumnFormula|totalsRowFormula))\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>',formula,xml,flags=re.S)
        if xml!=raw.decode('utf-8-sig'):parts[part]=xml.encode('utf8')


def add_pictures(path,sheets,images):
    from sku_workbook import inspect_workbook
    plans=[dict(p) for p in sheets if p.get('rows')]
    with ZipFile(path) as z:parts={name:z.read(name) for name in z.namelist()}
    insert_columns(parts,plans)
    for plan in plans:
        part=plan['path'];column=plan['status_col']+1;xml=parts[part].decode('utf8');prefix=re.search(r'<((?:\w+:)?)worksheet\b',xml)[1]
        def patch_row(m):
            number=int(re.search(r'\br=["\'](\d+)["\']',m['attrs'])[1]);body=m['body'];attrs=m['attrs']
            value='款号图片' if number==plan['header_row'] else ('图片暂不可用' if (plan['name'],number) in images and images[plan['name'],number] is None else '')
            neighbor=re.search(r'<(?:\w+:)?c\b[^>]*\br="'+col_name(column-1)+str(number)+r'"[^>]*>',body)
            style=re.search(r'\bs="(\d+)"',neighbor[0]) if neighbor else None
            cell=f'<{prefix}c r="{col_name(column)}{number}"'+(f' s="{style[1]}"' if style else '')+f' t="inlineStr"><{prefix}is><{prefix}t>{escape(value)}</{prefix}t></{prefix}is></{prefix}c>'
            pattern=r'<(?:\w+:)?c\b[^>]*\br="'+col_name(column)+str(number)+r'"[^>]*(?:/>|>.*?</(?:\w+:)?c>)'
            if re.search(pattern,body,re.S):body=re.sub(pattern,lambda _:cell,body,count=1,flags=re.S)
            else:
                following=re.search(r'<(?:\w+:)?c\b[^>]*\br="'+col_name(column+1)+str(number)+r'"',body)
                index=following.start() if following else len(body);body=body[:index]+cell+body[index:]
            if isinstance(images.get((plan['name'],number)),bytes):
                height=re.search(r'\bht="([\d.]+)"',attrs)
                attrs=re.sub(r'\s+(?:ht|customHeight)="[^"]*"','',attrs)+f' ht="{max(76,float(height[1]) if height else 0):g}" customHeight="1"'
            return '<'+m['tag']+attrs+'>'+body+'</'+m['tag']+'>'
        xml=re.sub(r'<(?P<tag>(?:\w+:)?row)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>',patch_row,xml,flags=re.S)
        xml=_column_width(xml,prefix,column,19)
        parts[part]=xml.encode('utf8')
        attach_images(parts,plan,column,images)
    # 备注随整列移动，保留列宽、公式引用、表格顺序和图片锚点；源文件仍只读。
    moves={}
    for plan in plans:
        notes=[c['index'] for c in plan.get('columns',[]) if c['title'].strip()=='备注']
        if not notes:continue
        if not plan.get('image_col'):notes=[i+int(i>=plan['status_col']+1) for i in notes]
        doc=E.fromstring(parts[plan['path']]);last=max(col_number(c.get('r')) for c in doc.findall('m:sheetData/m:row/m:c',N))
        order=[i for i in range(1,last+1) if i not in notes]+notes
        mapping={old:index for index,old in enumerate(order,1)}
        if any(old!=new for old,new in mapping.items()):moves[plan['name']]=mapping
    if moves:insert_columns(parts,plans,column_maps=moves)
    temporary=path.with_suffix('.pictures.xlsx')
    try:
        with ZipFile(temporary,'w',ZIP_DEFLATED) as z:
            for part,raw in parts.items():
                if part.endswith('.xml') or part.endswith('.rels'):E.fromstring(raw)
                z.writestr(part,raw)
        inspect_workbook(temporary,allow_empty=True)
        temporary.replace(path)
    finally:temporary.unlink(missing_ok=True)


def attach_images(parts,plan,column,images):
    part=plan['path'];relpart=posixpath.join(posixpath.dirname(part),'_rels',posixpath.basename(part)+'.rels')
    sheet=E.fromstring(parts[part]);drawing=sheet.find('m:drawing',N);token=hashlib.sha256(part.encode()).hexdigest()[:16]
    if drawing is not None:
        rel=next(r for r in E.fromstring(parts[relpart]) if r.get('Id')==drawing.get(RID));target=rel.get('Target')
        drawingpart=target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join(posixpath.dirname(part),target))
    else:
        drawingpart=f'xl/drawings/framekeep-sku-{token}.xml';rid='framekeepSkuPictures'
        existing=parts.get(relpart,f'<Relationships xmlns="{PKG}"/>'.encode())
        while any(r.get('Id')==rid for r in E.fromstring(existing)):rid+='x'
        parts[relpart]=append_node(existing,'Relationships',f'<Relationship xmlns="{PKG}" Id="{rid}" Type="{REL}/drawing" Target="../drawings/{posixpath.basename(drawingpart)}"/>')
        prefix=re.search(r'<((?:\w+:)?)worksheet\b',parts[part].decode())[1]
        node=f'<{prefix}drawing xmlns:fkrel="{REL}" fkrel:id="{rid}"/>'
        xml=parts[part].decode('utf8');following=re.search(r'<(?:\w+:)?(?:legacyDrawing|legacyDrawingHF|picture|oleObjects|controls|webPublishItems|tableParts|extLst)\b',xml)
        parts[part]=(xml[:following.start()]+node+xml[following.start():]).encode() if following else append_node(parts[part],'worksheet',node)
        parts[drawingpart]=f'<xdr:wsDr xmlns:xdr="{DRAW}" xmlns:a="{ART}" xmlns:r="{REL}"/>'.encode()
    drawingrels=posixpath.join(posixpath.dirname(drawingpart),'_rels',posixpath.basename(drawingpart)+'.rels')
    relationships=E.fromstring(parts.get(drawingrels,f'<Relationships xmlns="{PKG}"/>'.encode()))
    xml=parts[drawingpart].decode('utf8');prefix=re.search(r'<((?:\w+:)?)wsDr\b',xml)[1]
    # 只替换本功能创建的图锚点；原有浮动图、WPS 图片和其他关系保留。
    xml=re.sub(r'<(?P<tag>(?:\w+:)?(?:oneCellAnchor|twoCellAnchor))\b[^>]*>.*?</(?P=tag)>',lambda m:'' if 'name="FramekeepSkuRow' in m[0] else m[0],xml,flags=re.S)
    used={int(m) for m in re.findall(r'<(?:\w+:)?cNvPr\b[^>]*\bid="(\d+)"',xml)}
    for row in plan['rows']:
        number=row['excel_row'];data=images.get((plan['name'],number))
        if not isinstance(data,bytes):continue
        with Image.open(io.BytesIO(data)) as source:
            image=ImageOps.exif_transpose(source).convert('RGB');image.thumbnail((144,92));width,height=image.size
            output=io.BytesIO();image.save(output,'JPEG',quality=82)
        media=f'xl/media/framekeep-sku-{token}-{number}.jpg';parts[media]=output.getvalue();rid=f'framekeepSkuRow{number}'
        prior=next((r for r in relationships if r.get('Id')==rid),None)
        if prior is not None:relationships.remove(prior)
        E.SubElement(relationships,'{'+PKG+'}Relationship',Id=rid,Type=REL+'/image',Target='../media/'+posixpath.basename(media))
        pid=max(used or {0})+1;used.add(pid);cx=width*9525;cy=height*9525
        anchor=f'<xdr:oneCellAnchor xmlns:xdr="{DRAW}" xmlns:a="{ART}" xmlns:r="{REL}"><xdr:from><xdr:col>{column-1}</xdr:col><xdr:colOff>38100</xdr:colOff><xdr:row>{number-1}</xdr:row><xdr:rowOff>38100</xdr:rowOff></xdr:from><xdr:ext cx="{cx}" cy="{cy}"/><xdr:pic><xdr:nvPicPr><xdr:cNvPr id="{pid}" name="FramekeepSkuRow{number}"/><xdr:cNvPicPr/></xdr:nvPicPr><xdr:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></xdr:blipFill><xdr:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr></xdr:pic><xdr:clientData/></xdr:oneCellAnchor>'
        xml=append_node(xml.encode(),'wsDr',anchor).decode()
    parts[drawingpart]=xml.encode();parts[drawingrels]=E.tostring(relationships,encoding='utf-8',xml_declaration=True)
    types=parts['[Content_Types].xml'];root=E.fromstring(types)
    for tag,attrs in [('Override',{'PartName':'/'+drawingpart,'ContentType':'application/vnd.openxmlformats-officedocument.drawing+xml'}),('Default',{'Extension':'jpg','ContentType':'image/jpeg'})]:
        key='PartName' if tag=='Override' else 'Extension'
        if not any(node.get(key)==attrs[key] for node in root):
            types=append_node(types,'Types','<'+tag+' xmlns="'+CT+'" '+ ' '.join(key+'='+quoteattr(value) for key,value in attrs.items())+'/>');root=E.fromstring(types)
    parts['[Content_Types].xml']=types
