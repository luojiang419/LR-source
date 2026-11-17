"""发行版自带的仓管 XLSX 报表，不依赖 Node、Office 或 Codex 运行库。"""
from datetime import datetime, timezone, timedelta
import io
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZipFile, ZIP_DEFLATED

SHANGHAI=timezone(timedelta(hours=8))
NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
HEADERS=['母SKU','子SKU','存储路径','文件缩略图','归档日期','归档前路径','归档后路径','校验结果','配套XMP路径','备注','图片类型']


def report_filename(at=None):
    return '归档校验报表-'+(at or datetime.now(timezone.utc)).astimezone(SHANGHAI).strftime('%Y%m%d%H%M')+'.xlsx'


def text_cell(column,row,value,style=0):
    # inlineStr 明确为文本，原始款号/路径即使以=开头也不会执行公式。
    return f'<c r="{column}{row}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value or ""))}</t></is></c>'


def excel_date(value):
    if not value:return None
    parsed=datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(SHANGHAI).replace(tzinfo=None)
    return (parsed-datetime(1899,12,30)).total_seconds()/86400


def build_report(report, *, at=None):
    output=io.BytesIO()
    sheets=[('归档明细',report['items'],False)]
    if report.get('skipped_reference_records'):sheets.append(('后期删除记录',report['skipped_reference_records'],True))
    if report.get('excluded_records'):sheets.append(('未入库排除记录',report['excluded_records'],True))
    if report.get('unassigned_reference_records'):sheets.append(('未选用来源记录',report['unassigned_reference_records'],True))
    media=[];sheet_parts=[];drawing_parts=[]
    for sheet_index,(name,items,deleted) in enumerate(sheets,1):
        rows=[f'<row r="1" ht="8" customHeight="1"/>',f'<row r="2" ht="24" customHeight="1">{text_cell("A",2,"归档校验报表" if not deleted else name,3)}</row>',
              f'<row r="3" ht="20" customHeight="1">{text_cell("A",3,"归档日期采用北京时间；缩略图按实际照片身份关联。")}</row>',
              f'<row r="4" ht="20" customHeight="1">{text_cell("A",4,"选用大档 "+str(report.get("final_expected",report["expected"]))+" 个，未入库排除 "+str(report.get("excluded_count",0))+" 个，二星标签 "+str(report.get("label_file_count",0))+" 张（小档补充 "+str(report.get("supplemental_label_count",0))+" 张），已验证 "+str(report.get("verified",0))+" 张，最终未选用来源 "+str(report.get("deleted_reference_count",0))+" 张。")}</row>',
              '<row r="5" ht="25" customHeight="1">'+''.join(text_cell(chr(65+i),5,h,1) for i,h in enumerate(HEADERS))+'</row>']
        if report.get('fast'):
            rows[2]=f'<row r="3" ht="20" customHeight="1">{text_cell("A",3,"极速归档：复制流计算源SHA-256，目标单次读回；整理/最终清单采用文件版本核对。日期采用北京时间。")}</row>'
        anchors=[];rels=[]
        for index,item in enumerate(items,6):
            successful=item.get('status') in ('verified','identical_verified_skip')
            target=item.get('destination') if successful and not deleted else ''
            excluded=item.get('status')=='unregistered_excluded'
            unrelated=item.get('status')=='unassigned_reference_not_selected'
            original=item.get('original_reference',item.get('reference',item.get('original_source',item.get('source','')))) if deleted else item.get('original_source',item['source'])
            status='未入库，按规则排除' if excluded else '最终大档未选用，自动跳过' if deleted else '已存在，校验一致' if item.get('status')=='identical_verified_skip' else '验证通过' if successful else '未通过，待核实'
            if unrelated:status='无SKU，与本次最终大档无关'
            note=item.get('reason','')+'；原件保持，不计入本次选用数量' if excluded else '不计入大档遗漏' if deleted else ''
            if unrelated:note=item['reason']
            if item.get('preview_note'):note+=' '+item['preview_note']+'。'
            if item.get('verification_method')=='verified_receipt_and_file_version':note+=' 文件版本未变，复用已验证回执。'
            if item.get('origin')=='onsite_label':note+=' 二星标签图从小档补充。'
            if report.get('preprocess') and not excluded and not unrelated:note+=' 整理后来源：'+item.get('reference' if deleted else 'source','')
            if item.get('status')=='identical_verified_skip' and not item.get('archived_at'):note+=' 原归档日期未知。'
            if not deleted and not successful:note+=' 计划目标：'+str(item.get('destination',''))
            values=[item.get('parent_sku',''),item.get('sku',''),str(Path(target).parent) if target else '',
                    '不适用' if deleted else '',None,original,target,status,item.get('xmp') if successful else '',note,
                    '未入库照片' if excluded else '二星标签图' if item.get('photo_role')=='label' or item.get('rating')==2 else '款式图']
            cells=[]
            for col,value in enumerate(values):
                column=chr(65+col)
                if col==4:
                    date=excel_date(item.get('archived_at')) if successful and not deleted else None
                    cells.append(f'<c r="{column}{index}" s="4">'+(f'<v>{date}</v>' if date is not None else '')+'</c>')
                else:cells.append(text_cell(column,index,value,2))
            rows.append(f'<row r="{index}" ht="100" customHeight="1">'+''.join(cells)+'</row>')
            thumb=item.get('thumbnail_path')
            if not deleted and thumb and Path(thumb).is_file():
                image=Path(thumb).read_bytes();media_index=len(media)+1;media.append(image)
                rid='rImage'+str(media_index)
                width=min(120,int(item.get('thumbnail_width',80)));height=min(120,int(item.get('thumbnail_height',120)))
                x=(130-width)//2;y=6;row=index-1
                anchor=lambda mark,ox,oy:f'<xdr:{mark}><xdr:col>3</xdr:col><xdr:colOff>{ox*9525}</xdr:colOff><xdr:row>{row}</xdr:row><xdr:rowOff>{oy*9525}</xdr:rowOff></xdr:{mark}>'
                anchors.append(f'<xdr:twoCellAnchor editAs="twoCell">{anchor("from",x,y)}{anchor("to",x+width,y+height)}<xdr:pic><xdr:nvPicPr><xdr:cNvPr id="{media_index}" name="Preview {media_index}"/><xdr:cNvPicPr/></xdr:nvPicPr><xdr:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></xdr:blipFill><xdr:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr></xdr:pic><xdr:clientData/></xdr:twoCellAnchor>')
                rels.append(f'<Relationship Id="{rid}" Type="{REL}/image" Target="../media/image{media_index}.jpg"/>')
            elif not deleted:
                # 图片缺失不能用其他款图替代。
                rows[-1]=rows[-1].replace(text_cell('D',index,'',2),text_cell('D',index,'缩略图不可用',2))
        widths=[12,19,45,19,25,51,54,23,52,56,18]
        cols=''.join(f'<col min="{i+1}" max="{i+1}" width="{w}" customWidth="1"/>' for i,w in enumerate(widths))
        end=len(items)+5
        sheet=f'<worksheet xmlns="{NS}" xmlns:r="{REL}"><sheetViews><sheetView workbookViewId="0" showGridLines="0"><pane xSplit="2" ySplit="5" topLeftCell="C6" activePane="bottomRight" state="frozen"/></sheetView></sheetViews><cols>{cols}</cols><sheetData>{"".join(rows)}</sheetData><autoFilter ref="A5:K{end}"/>'
        if anchors:
            sheet+='<drawing r:id="rDrawing"/>'
            drawing_parts.append((sheet_index,'<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="'+REL+'">'+''.join(anchors)+'</xdr:wsDr>',rels))
        sheet_parts.append(sheet+'</worksheet>')
    styles=f'''<styleSheet xmlns="{NS}"><numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm:ss"/></numFmts><fonts count="3"><font><sz val="10"/><name val="Microsoft YaHei"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="10"/><name val="Microsoft YaHei"/></font><font><b/><sz val="14"/><name val="Microsoft YaHei"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF345244"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="2"><border/><border><left style="thin"><color rgb="FF98A49B"/></left><right style="thin"><color rgb="FF98A49B"/></right><top style="thin"><color rgb="FF98A49B"/></top><bottom style="thin"><color rgb="FF98A49B"/></bottom></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf><xf numFmtId="0" fontId="0" fillId="0" borderId="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf><xf numFmtId="0" fontId="2" fillId="0" borderId="0"/><xf numFmtId="164" fontId="0" fillId="0" borderId="1" applyNumberFormat="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''
    package_ns='http://schemas.openxmlformats.org/package/2006/relationships'
    with ZipFile(output,'w',ZIP_DEFLATED) as z:
        types=['<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>','<Default Extension="xml" ContentType="application/xml"/>','<Default Extension="jpg" ContentType="image/jpeg"/>',
               '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>','<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
        relationships=[];workbook_sheets=[]
        for i,(name,_,_) in enumerate(sheets,1):
            types.append(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
            relationships.append(f'<Relationship Id="rSheet{i}" Type="{REL}/worksheet" Target="worksheets/sheet{i}.xml"/>')
            workbook_sheets.append(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rSheet{i}"/>')
            z.writestr(f'xl/worksheets/sheet{i}.xml',sheet_parts[i-1])
        relationships.append(f'<Relationship Id="rStyles" Type="{REL}/styles" Target="styles.xml"/>')
        for index,drawing,rels in drawing_parts:
            types.append(f'<Override PartName="/xl/drawings/drawing{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"/>')
            z.writestr(f'xl/drawings/drawing{index}.xml',drawing)
            z.writestr(f'xl/drawings/_rels/drawing{index}.xml.rels',f'<Relationships xmlns="{package_ns}">'+''.join(rels)+'</Relationships>')
            z.writestr(f'xl/worksheets/_rels/sheet{index}.xml.rels',f'<Relationships xmlns="{package_ns}"><Relationship Id="rDrawing" Type="{REL}/drawing" Target="../drawings/drawing{index}.xml"/></Relationships>')
        for i,image in enumerate(media,1):z.writestr(f'xl/media/image{i}.jpg',image)
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'+''.join(types)+'</Types>')
        z.writestr('_rels/.rels',f'<Relationships xmlns="{package_ns}"><Relationship Id="rWorkbook" Type="{REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/_rels/workbook.xml.rels',f'<Relationships xmlns="{package_ns}">'+''.join(relationships)+'</Relationships>')
        z.writestr('xl/workbook.xml',f'<workbook xmlns="{NS}" xmlns:r="{REL}"><bookViews><workbookView/></bookViews><sheets>'+''.join(workbook_sheets)+'</sheets></workbook>')
        z.writestr('xl/styles.xml',styles)
    return output.getvalue(),report_filename(at)
