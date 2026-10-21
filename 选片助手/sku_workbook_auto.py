"""自动记录工作簿：只新增自有工作表，原工作表和图片 ZIP 部件按字节保留。"""
import io
from pathlib import Path
import re
from xml.etree import ElementTree as E
from xml.sax.saxutils import quoteattr
from zipfile import ZipFile,ZIP_DEFLATED
from sku_workbook import NS,REL,RID,col_name

PKG='http://schemas.openxmlformats.org/package/2006/relationships'
CT='http://schemas.openxmlformats.org/package/2006/content-types'


def create_book(name='现场记录'):
    output=io.BytesIO()
    with ZipFile(output,'w',ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml',f'<Types xmlns="{CT}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>')
        z.writestr('_rels/.rels',f'<Relationships xmlns="{PKG}"><Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml',f'<workbook xmlns="{NS}"><sheets/></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels',f'<Relationships xmlns="{PKG}"/>')
    return replace_sheet(output.getvalue(),name,[])[0]


def replace_sheet(content,name,rows):
    with ZipFile(io.BytesIO(content)) as src:
        entries={info.filename:(info,src.read(info)) for info in src.infolist()}
    workbook=E.fromstring(entries['xl/workbook.xml'][1]);sheets=workbook.find('{'+NS+'}sheets')
    rels=E.fromstring(entries['xl/_rels/workbook.xml.rels'][1]);types=E.fromstring(entries['[Content_Types].xml'][1])
    existing=next((sheet for sheet in sheets if sheet.get('name')==name),None)
    package_updates={}
    if existing is None:
        number=max([int(sheet.get('sheetId','0')) for sheet in sheets]+[0])+1
        rid='framekeep'+str(number)
        while any(rel.get('Id')==rid for rel in rels):rid+='x'
        part='xl/worksheets/framekeep'+str(number)+'.xml'
        while part in entries:part=part.replace('.xml','x.xml')
        # 只插入节点字符串，保留 WPS/Excel 的命名空间前缀及 mc:Ignorable 等扩展。
        xml=entries['xl/workbook.xml'][1].decode('utf-8-sig');container=re.search(r'<((?:\w+:)?)sheets\b[^>]*>',xml)
        prefix=container.group(1);sheet_xml=f'<{prefix}sheet name={quoteattr(name)} sheetId="{number}" xmlns:fkrel="{REL}" fkrel:id="{rid}"/>'
        if container.group(0).endswith('/>'):xml=xml[:container.start()]+container.group(0)[:-2]+'>'+sheet_xml+f'</{prefix}sheets>'+xml[container.end():]
        else:xml=xml.replace(f'</{prefix}sheets>',sheet_xml+f'</{prefix}sheets>',1)
        package_updates['xl/workbook.xml']=xml.encode('utf-8')
        def append_node(part_name,root_name,tag,attributes):
            xml=entries[part_name][1].decode('utf-8-sig');root=re.search(r'<((?:\w+:)?)'+root_name+r'\b[^>]*>',xml);prefix=root.group(1)
            child='<'+prefix+tag+' '+ ' '.join(key+'='+quoteattr(value) for key,value in attributes.items())+'/>'
            if root.group(0).endswith('/>'):xml=xml[:root.start()]+root.group(0)[:-2]+'>'+child+'</'+prefix+root_name+'>'+xml[root.end():]
            else:xml=xml.replace('</'+prefix+root_name+'>',child+'</'+prefix+root_name+'>',1)
            return xml.encode('utf-8')
        package_updates['xl/_rels/workbook.xml.rels']=append_node('xl/_rels/workbook.xml.rels','Relationships','Relationship',{'Id':rid,'Type':REL+'/worksheet','Target':part[3:]})
        package_updates['[Content_Types].xml']=append_node('[Content_Types].xml','Types','Override',{'PartName':'/'+part,'ContentType':'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'})
    else:
        rel=next(rel for rel in rels if rel.get('Id')==existing.get(RID));target=rel.get('Target');part=target.lstrip('/') if target.startswith('/') else 'xl/'+target
    sheet=E.Element('worksheet',xmlns=NS);E.SubElement(sheet,'dimension',ref=f'A1:F{len(rows)+1}')
    views=E.SubElement(sheet,'sheetViews');view=E.SubElement(views,'sheetView',workbookViewId='0');E.SubElement(view,'pane',ySplit='1',topLeftCell='A2',activePane='bottomLeft',state='frozen')
    cols=E.SubElement(sheet,'cols')
    for index,width in enumerate((26,24,26,26,16,28),1):E.SubElement(cols,'col',min=str(index),max=str(index),width=str(width),customWidth='1')
    data=E.SubElement(sheet,'sheetData');headers=['款号','来源文件','现场文件夹','记录时间','拍摄状态','拍摄完成时间']
    for i,values in enumerate([headers]+rows,1):
        row=E.SubElement(data,'row',r=str(i))
        for j,value in enumerate(values,1):
            cell=E.SubElement(row,'c',r=col_name(j)+str(i),t='inlineStr');E.SubElement(E.SubElement(cell,'is'),'t').text=str(value or '')
    updated={**package_updates,part:E.tostring(sheet,encoding='utf-8',xml_declaration=True)}
    output=io.BytesIO()
    with ZipFile(output,'w',ZIP_DEFLATED) as dest:
        for name,(info,raw) in entries.items():dest.writestr(info,updated.pop(name,raw))
        for name,raw in updated.items():dest.writestr(name,raw)
    return output.getvalue(),part
