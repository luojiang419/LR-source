"""纯 Open XML 双图库存表，不依赖 Office 或外部图片链接。"""
import io
from xml.sax.saxutils import escape
from zipfile import ZipFile,ZIP_DEFLATED
from PIL import Image,ImageOps
from sku_workbook_auto import create_book,replace_sheet

def export(service,query):
    from sku_workbook import NS,REL
    pkg='http://schemas.openxmlformats.org/package/2006/relationships'
    rows=[];offset=0
    while True:
        page=service.records({**query,'offset':offset,'limit':200});rows.extend(page['rows']);offset+=len(page['rows'])
        if not page['has_more']:break
    # 独立新文件；不触碰现有拍摄工作簿。
    content=create_book('出入库记录')
    with ZipFile(io.BytesIO(content)) as src:parts={n:src.read(n) for n in src.namelist()}
    sheet_part=next(n for n in parts if n.startswith('xl/worksheets/') and n.endswith('.xml'))
    sheet=[f'<worksheet xmlns="{NS}" xmlns:r="{REL}"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" state="frozen"/></sheetView></sheetViews><cols>']
    for i,w in enumerate((8,28,27,27,10,29),1):sheet.append(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>')
    sheet.append('</cols><sheetData>');drawing=['<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="'+REL+'">'];rels=[f'<Relationships xmlns="{pkg}">'];number=0
    values=[['序号','款号','产品实拍图','SKU 标签图片','模式','出入库时间']]+[[i,r['sku'],'','','入库' if r['mode']=='in' else '出库',r['at']] for i,r in enumerate(rows,1)]
    for index,record in enumerate(values,1):
        sheet.append(f'<row r="{index}" ht="{28 if index==1 else 120}" customHeight="1">')
        for j,v in enumerate(record):sheet.append(f'<c r="{chr(65+j)}{index}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>')
        sheet.append('</row>')
        if index==1:continue
        r=rows[index-2]
        for j,kind in enumerate(('product','label'),2):
            number+=1;name=f'inventory{number}.jpg'
            with Image.open(io.BytesIO(service.image(r['id'],kind))) as raw:
                im=ImageOps.exif_transpose(raw).convert('RGB');im.thumbnail((360,360));output=io.BytesIO();im.save(output,'JPEG',quality=85);w,h=im.size
            parts['xl/media/'+name]=output.getvalue();scale=min(170/w,148/h);cx=round(w*scale)*9525;cy=round(h*scale)*9525
            rels.append(f'<Relationship Id="rId{number}" Type="{REL}/image" Target="../media/{name}"/>')
            drawing.append(f'<xdr:oneCellAnchor><xdr:from><xdr:col>{j}</xdr:col><xdr:colOff>38100</xdr:colOff><xdr:row>{index-1}</xdr:row><xdr:rowOff>38100</xdr:rowOff></xdr:from><xdr:ext cx="{cx}" cy="{cy}"/><xdr:pic><xdr:nvPicPr><xdr:cNvPr id="{number}" name="{name}"/><xdr:cNvPicPr/></xdr:nvPicPr><xdr:blipFill><a:blip r:embed="rId{number}"/><a:stretch><a:fillRect/></a:stretch></xdr:blipFill><xdr:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr></xdr:pic><xdr:clientData/></xdr:oneCellAnchor>')
    sheet.append(f'</sheetData><autoFilter ref="A1:F{len(values)}"/><drawing r:id="inventoryImages"/></worksheet>');parts[sheet_part]=''.join(sheet).encode()
    parts['xl/worksheets/_rels/'+sheet_part.rsplit('/',1)[1]+'.rels']=f'<Relationships xmlns="{pkg}"><Relationship Id="inventoryImages" Type="{REL}/drawing" Target="../drawings/inventory.xml"/></Relationships>'.encode()
    parts['xl/drawings/inventory.xml']=''.join(drawing+['</xdr:wsDr>']).encode();parts['xl/drawings/_rels/inventory.xml.rels']=''.join(rels+['</Relationships>']).encode()
    types=parts['[Content_Types].xml'].decode();types=types.replace('</Types>','<Default Extension="jpg" ContentType="image/jpeg"/><Override PartName="/xl/drawings/inventory.xml" ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"/></Types>');parts['[Content_Types].xml']=types.encode()
    output=io.BytesIO()
    with ZipFile(output,'w',ZIP_DEFLATED) as dest:
        for name,data in parts.items():dest.writestr(name,data)
    return output.getvalue()
