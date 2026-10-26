"""在最终工作副本应用列名并附加全部款号汇总，保留原 XML 部件。"""
import posixpath
import re
from xml.etree import ElementTree as E
from xml.sax.saxutils import escape,unescape,quoteattr
from zipfile import ZipFile,ZIP_DEFLATED

from sku_workbook import NS,N,RID,REL,col_name,col_number
from sku_workbook_pictures import PKG,CT,append_node,attach_images
from sku_presentation import FIXED_HEADERS,header_key

PART='xl/framekeep-presentation.xml'


def read_layout(archive):
    if PART not in archive.namelist():return {}
    doc=E.fromstring(archive.read(PART))
    if doc.tag!='framekeep-presentation' or doc.get('version')!='1':return {}
    return {s.get('path'):s for s in doc.findall('sheet') if s.get('path','').startswith('xl/worksheets/')}


def canonical_values(values,layout):
    if layout is None:return {}
    aliases={}
    for column in layout.findall('column'):
        ref=column.get('ref','');canonical=column.get('canonical');title=column.get('title');key=column.get('key')
        if canonical is None or values.get(ref)!=title:continue
        values[ref]=canonical
        if key and title and title!=canonical:
            if key.startswith('column:'):key='column:'+layout.get('name','')+':'+re.sub(r'\d','',ref)
            aliases[key]=title
    return aliases


def _patch_headers(raw,header_row,updates):
    xml=raw.decode('utf-8-sig');prefix=re.search(r'<((?:\w+:)?)worksheet\b',xml)[1]
    def patch(match):
        if int(re.search(r'\br=["\'](\d+)["\']',match['attrs'])[1])!=header_row:return match[0]
        body=match['body']
        for index,title in updates.items():
            ref=col_name(index)+str(header_row)
            pattern=r'<(?:\w+:)?c\b(?P<attrs>[^>]*\br=["\']'+ref+r'["\'][^>]*?)(?:/>|>.*?</(?:\w+:)?c>)'
            cell=re.search(pattern,body,re.S)
            attrs=re.sub(r'\s+t=["\'][^"\']*["\']','',cell['attrs']) if cell else f' r="{ref}"'
            node=f'<{prefix}c{attrs} t="inlineStr"><{prefix}is><{prefix}t xml:space="preserve">{escape(title)}</{prefix}t></{prefix}is></{prefix}c>'
            if cell:body=body[:cell.start()]+node+body[cell.end():]
            else:
                later=next((m.start() for m in re.finditer(r'<(?:\w+:)?c\b[^>]*\br=["\']([A-Z]+)\d+["\']',body) if col_number(m[1])>index),len(body))
                body=body[:later]+node+body[later:]
        return '<'+match['tag']+match['attrs']+'>'+body+'</'+match['tag']+'>'
    return re.sub(r'<(?P<tag>(?:\w+:)?row)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>',patch,xml,flags=re.S).encode('utf8')


def _positions(plan):
    shift=lambda i:i+int(not plan.get('image_col') and i>=plan['status_col']+1)
    last=max([shift(c['index']) for c in plan.get('columns',[])]+[shift(plan['time_col']),plan['status_col']+1])
    notes=[shift(c['index']) for c in plan.get('columns',[]) if c['title'].strip()=='备注']
    order=[i for i in range(1,last+1) if i not in notes]+notes
    moved={old:new for new,old in enumerate(order,1)}
    entries=[(moved[shift(c['index'])],header_key(plan,c),c['title']) for c in plan.get('columns',[])]
    entries.extend([(moved[plan['status_col']],'status','拍摄状态'),(moved[plan['status_col']+1],'photo','款号图片'),(moved[shift(plan['time_col'])],'time','拍摄完成时间')])
    return entries


def _structured_references(parts,tables):
    """列名改变时同步 Excel Table 引用，字符串和普通 A1 引用保持。"""
    def column(value):return ''.join("'"+ch if ch in "[]#'@" else ch for ch in value)
    def tokens(text,renames):
        mapping={token:replacement for old,new in renames.items() for token,replacement in
                 [('['+column(old)+']','['+column(new)+']'),('[@'+column(old)+']','[@'+column(new)+']')]}
        if not mapping:return text
        return re.sub('|'.join(re.escape(k) for k in sorted(mapping,key=len,reverse=True)),lambda m:mapping[m[0]],text)
    by_name={name.casefold():table for table in tables for name in table['names']}
    named=re.compile(r'(?<![\w.])('+'|'.join(re.escape(k) for k in sorted(by_name,key=len,reverse=True))+r')\[',re.I)
    def expression(text,local):
        def segment(piece):
            result=[];start=0
            for match in named.finditer(piece):
                if match.start()<start:continue
                index=match.end()-1;depth=0
                while index<len(piece):
                    ch=piece[index]
                    if ch=="'" and index+1<len(piece):index+=2;continue
                    if ch=='[':depth+=1
                    elif ch==']':
                        depth-=1
                        if not depth:break
                    index+=1
                if depth:continue
                result.extend([tokens(piece[start:match.start()],local),piece[match.start():match.end()-1],tokens(piece[match.end()-1:index+1],by_name[match[1].casefold()]['renames'])]);start=index+1
            return ''.join(result)+tokens(piece[start:],local)
        return ''.join(piece if i%2 else segment(piece) for i,piece in enumerate(re.split(r'("(?:[^"]|"")*")',text)))
    formula=re.compile(r'<(?P<tag>(?:\w+:)?(?:f|calculatedColumnFormula|totalsRowFormula|definedName))\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>',re.S)
    def formulas(raw,local):
        def patch(m):
            old=unescape(m['body'],{'&quot;':'"','&apos;':"'"});new=expression(old,local)
            return m[0] if old==new else '<'+m['tag']+m['attrs']+'>'+escape(new)+'</'+m['tag']+'>'
        return formula.sub(patch,raw)
    for part,raw in list(parts.items()):
        if not part.endswith('.xml') or b'[' not in raw:continue
        local=next((t['renames'] for t in tables if t['part']==part),{})
        scoped=[t for t in tables if t['sheet']==part]
        xml=raw.decode('utf-8-sig')
        if scoped:
            def cell(m):
                address=re.search(r'\br=["\']([A-Z]+)(\d+)["\']',m['attrs'])
                table=next((t for t in scoped if address and t['bounds'][0]<=col_number(address[1])<=t['bounds'][2] and t['bounds'][1]<=int(address[2])<=t['bounds'][3]),None)
                return '<'+m['tag']+m['attrs']+'>'+formulas(m['body'],table['renames'] if table else {})+'</'+m['tag']+'>'
            xml=re.sub(r'<(?P<tag>(?:\w+:)?c)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>',cell,xml,flags=re.S)
        else:xml=formulas(xml,local)
        parts[part]=xml.encode('utf8')


def finalize(path,plans,headers,groups,images):
    with ZipFile(path) as archive:
        parts={name:archive.read(name) for name in archive.namelist()};prior=read_layout(archive)
    manifest=E.Element('framekeep-presentation',version='1');tables=[]
    for plan in plans:
        if not plan.get('rows'):continue
        entries=_positions(plan);updates={i:headers[key] for i,key,_ in entries if key in headers}
        if updates:parts[plan['path']]=_patch_headers(parts[plan['path']],plan['header_row'],updates)
        element=E.SubElement(manifest,'sheet',path=plan['path'],name=plan['name'])
        for i,key,canonical in entries:E.SubElement(element,'column',ref=col_name(i)+str(plan['header_row']),key=key,canonical=canonical,title=headers.get(key,canonical))
        # Excel Table 的列名与表头保持一致，结构化引用随后同步。
        relpath=posixpath.join(posixpath.dirname(plan['path']),'_rels',posixpath.basename(plan['path'])+'.rels')
        if relpath in parts and updates:
            for rel in E.fromstring(parts[relpath]):
                if rel.get('TargetMode')=='External' or not rel.get('Type','').endswith('/table'):continue
                target=rel.get('Target','');part=target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join(posixpath.dirname(plan['path']),target))
                if part not in parts:continue
                doc=E.fromstring(parts[part]);start=col_number(doc.get('ref').split(':')[0]);columns=doc.find('m:tableColumns',N)
                if columns is None:continue
                titles=[updates.get(start+i,c.get('name')) for i,c in enumerate(columns)]
                if len({v.casefold() for v in titles})!=len(titles):raise ValueError('Excel 表格内列名称不能重复')
                renames={c.get('name'):title for c,title in zip(columns,titles) if c.get('name')!=title}
                if renames:
                    ends=doc.get('ref').split(':');addresses=[re.fullmatch(r'([A-Z]+)(\d+)',ref) for ref in (ends[0],ends[-1])]
                    tables.append({'part':part,'sheet':plan['path'],'names':{doc.get('name'),doc.get('displayName')}-{None},'renames':renames,
                                   'bounds':(col_number(addresses[0][1]),int(addresses[0][2]),col_number(addresses[1][1]),int(addresses[1][2]))})
                for c,title in zip(columns,titles):c.set('name',title)
                parts[part]=E.tostring(doc,encoding='utf-8',xml_declaration=True)
    if tables:_structured_references(parts,tables)
    _summary(parts,manifest,prior,headers,groups,images)
    parts[PART]=E.tostring(manifest,encoding='utf-8',xml_declaration=True)
    relpart='xl/_rels/workbook.xml.rels';rels=E.fromstring(parts[relpart])
    if not any(r.get('Type')==REL+'/customXml' and r.get('Target')=='framekeep-presentation.xml' for r in rels):
        rid='framekeepPresentation'
        while any(r.get('Id')==rid for r in rels):rid+='x'
        parts[relpart]=append_node(parts[relpart],'Relationships',f'<Relationship xmlns="{PKG}" Id="{rid}" Type="{REL}/customXml" Target="framekeep-presentation.xml"/>')
    types=E.fromstring(parts['[Content_Types].xml'])
    if not any(node.get('PartName')=='/'+PART for node in types):parts['[Content_Types].xml']=append_node(parts['[Content_Types].xml'],'Types',f'<Override xmlns="{CT}" PartName="/{PART}" ContentType="application/xml"/>')
    temporary=path.with_suffix('.presentation.xlsx')
    try:
        with ZipFile(temporary,'w',ZIP_DEFLATED) as z:
            for part,data in parts.items():
                if part.endswith(('.xml','.rels')):E.fromstring(data)
                z.writestr(part,data)
        temporary.replace(path)
    finally:temporary.unlink(missing_ok=True)


def _summary(parts,manifest,prior,headers,groups,images):
    book=E.fromstring(parts['xl/workbook.xml']);sheets=book.find('m:sheets',N)
    rels=E.fromstring(parts['xl/_rels/workbook.xml.rels'])
    existing=next((s for s in prior.values() if s.get('summary')=='1'),None)
    path=existing.get('path') if existing is not None else 'xl/worksheets/framekeep-all-skus.xml'
    if existing is None:
        suffix=0
        while path in parts:suffix+=1;path=f'xl/worksheets/framekeep-all-skus-{suffix}.xml'
        name='全部款号';names={s.get('name') for s in sheets};suffix=1
        while name in names:name='全部款号汇总'+str(suffix);suffix+=1
        rid='framekeepAllSkus'
        while any(r.get('Id')==rid for r in rels):rid+='x'
        sid=max([int(s.get('sheetId')) for s in sheets]+[0])+1
        parts['xl/workbook.xml']=append_node(parts['xl/workbook.xml'],'workbook','')
        xml=parts['xl/workbook.xml'].decode('utf8');prefix=re.search(r'<((?:\w+:)?)sheets\b',xml)[1]
        node=f'<{prefix}sheet name={quoteattr(name)} sheetId="{sid}" xmlns:fkr="{REL}" fkr:id="{rid}"/>'
        xml=xml.replace(f'</{prefix}sheets>',node+f'</{prefix}sheets>',1);parts['xl/workbook.xml']=xml.encode('utf8')
        parts['xl/_rels/workbook.xml.rels']=append_node(parts['xl/_rels/workbook.xml.rels'],'Relationships',f'<Relationship xmlns="{PKG}" Id="{rid}" Type="{REL}/worksheet" Target="{path.removeprefix("xl/")}"/>')
        parts['[Content_Types].xml']=append_node(parts['[Content_Types].xml'],'Types',f'<Override xmlns="{CT}" PartName="/{path}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
    else:name=existing.get('name')
    keys=['seq','row','sku','photo','source'];defaults=['序号','原表行','款号','图片','所属工作表']
    element=E.SubElement(manifest,'sheet',path=path,name=name,summary='1')
    values=[[headers.get(k,d) for k,d in zip(keys,defaults)]]
    for i,g in enumerate(groups,1):values.append([str(i),'；'.join(r['sheet']+' · '+str(r['excel_row']) for r in g['rows']),g['sku'],'','、'.join(g['memberships'])])
    for i,(key,canonical) in enumerate(zip(keys,defaults),1):E.SubElement(element,'column',ref=col_name(i)+'1',key=key,canonical=canonical,title=headers.get(key,canonical))
    rows=[]
    for r,values_row in enumerate(values,1):
        cells=''.join(f'<c r="{col_name(c)}{r}" t="inlineStr"><is><t xml:space="preserve">{escape(v)}</t></is></c>' for c,v in enumerate(values_row,1))
        rows.append(f'<row r="{r}"'+(' ht="78" customHeight="1"' if r>1 else '')+'>'+cells+'</row>')
    # 复用旧关系可让重复导出替换同一张汇总表与图锚点。
    old=E.fromstring(parts[path]) if path in parts else None
    drawing=old.find('m:drawing',N) if old is not None else None
    drawing_xml=E.tostring(drawing,encoding='unicode') if drawing is not None else ''
    parts[path]=(f'<worksheet xmlns="{NS}"><dimension ref="A1:E{len(values)}"/><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="1" width="9" customWidth="1"/><col min="2" max="2" width="36" customWidth="1"/><col min="3" max="3" width="26" customWidth="1"/><col min="4" max="4" width="21" customWidth="1"/><col min="5" max="5" width="36" customWidth="1"/></cols><sheetData>'+''.join(rows)+'</sheetData>'+drawing_xml+'</worksheet>').encode('utf8')
    mapping={}
    for i,g in enumerate(groups,2):
        for source in g['rows']:
            key=(source['sheet'],source['excel_row'])
            if key in images:mapping[(name,i)]=images[key];break
    attach_images(parts,{'path':path,'name':name,'rows':[{'excel_row':i} for i in range(2,len(values)+1)]},4,mapping)
