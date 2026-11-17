"""归档文档复用拍摄进度自动模板及其最终排版管线。"""
import io
from pathlib import Path
import re
import tempfile


def workbook(entries, details, info):
    from openpyxl import load_workbook
    from sku_workbook_auto import create_book, replace_sheet
    from sku_workbook import inspect_workbook, export_workbook
    from sku_workbook_pictures import add_pictures
    from sku_workbook_presentation import finalize
    from sku_presentation import grouped_rows
    from sku_match import normalize

    sheets={};names={};used={'文件明细','范围与版本','全部款号'}
    for entry in entries:
        raw=entry.get('shoot_type') or '现场记录'
        if raw not in names:
            stem=re.sub(r'[\\/*?:\[\]:\x00-\x1f]', '_',raw).strip(" '")[:31] or '现场记录'
            name=stem;counter=1
            while name.casefold() in {n.casefold() for n in used}:
                counter+=1;suffix='_'+str(counter);name=stem[:31-len(suffix)]+suffix
            names[raw]=name;used.add(name)
        sheets.setdefault(names[raw],[]).append(entry)
    if not sheets:sheets['现场记录']=[]
    content=create_book(next(iter(sheets)))
    rows=[];pictures={};labels={}
    for sheet,entries in sheets.items():
        values=[]
        for number,entry in enumerate(entries,2):
            values.append([entry['sku'],entry['source'],entry['folder'],entry['added'],entry['status'],entry['completed']])
            rows.append({'id':entry['id'],'sheet':sheet,'excel_row':number,'sku':entry['sku'],'normalized':normalize(entry['sku']),
                         'baseline':entry['status'],'status':entry['status'],'completed_at':entry['completed']})
            pictures[sheet,number]=entry['picture'];labels[sheet,number]=entry['label']
        content,_=replace_sheet(content,sheet,values)
    book=load_workbook(io.BytesIO(content))
    for name,values in [('文件明细',details),('范围与版本',info)]:
        table=book.create_sheet(name)
        for values_row in values:
            table.append(values_row)
            for cell in table[table.max_row]:
                if isinstance(cell.value,str):cell.data_type='s'
        table.freeze_panes='A2';table.auto_filter.ref=table.dimensions
        for column in table.columns:table.column_dimensions[column[0].column_letter].width=24
    for table in book:
        for row in table:
            for cell in row:
                if cell.value in (None,'') and cell.data_type=='s':cell.value=None;cell.data_type='n'
    with tempfile.TemporaryDirectory(prefix='shiying-archive-document-') as folder:
        source=Path(folder)/'source.xlsx';output=Path(folder)/'document.xlsx';book.save(source);book.close()
        plans=[p for p in inspect_workbook(source,allow_empty=True) if not p.get('skipped')]
        for plan in plans:plan['rows']=[r for r in rows if r['sheet']==plan['name']]
        export_workbook(source,output,plans,{(r['sheet'],r['excel_row']):r['status'] for r in rows},
                        {(r['sheet'],r['excel_row']):r['completed_at'] for r in rows},capture_images=pictures)
        label_plans=[]
        for plan in inspect_workbook(output,allow_empty=True):
            if plan.get('skipped'):continue
            original=next(p for p in plans if p['name']==plan['name'])
            label_plans.append(dict(plan,rows=original['rows'],status_col=plan['image_col'],image_col=plan.get('label_col')))
        add_pictures(output,label_plans,labels,picture_title='标签图片',picture_kind='Label')
        finalize(output,plans,{},grouped_rows([r for r in rows if r['sku']],plans),pictures)
        return output.read_bytes()
