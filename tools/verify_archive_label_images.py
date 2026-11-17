"""真实二星标签：仅归档最终大档内已有标签，不从小档补充。"""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'选片助手'))
from metadata_writer import MetadataWriter,sha
from rating_xmp import sidecar_metadata
from sku_folder_archive import SkuFolderArchive,camera_metadata,capture_key,metadata_fields


def verify():
    baseline=ROOT/'测试文件夹'
    label_root=Path(r'X:\细节图\源图片备份（勿动）\现场拍摄小档\2026-11-04')
    run=baseline/'sku库'/'.标签图验证'/datetime.now().strftime('%Y%m%d-%H%M%S')
    run.mkdir(parents=True,exist_ok=False)
    cases=[]
    original_hashes={str(label_root/(name+'.JPG')):sha(label_root/(name+'.JPG')) for name in ('XSY_2297','XSY_2365')}
    fixture=json.loads((baseline/'样片与测试款号对照.json').read_text(encoding='utf8'))[0]
    for enabled in (False,True):
        case=run/('开启' if enabled else '关闭');case.mkdir()
        small,final,target=[case/n for n in ('小档','大档','sku库')]
        for p in (small,final,target):p.mkdir()
        database=small/'现场SKU.sqlite'
        with closing(sqlite3.connect(database)) as c,c:
            c.execute('CREATE TABLE captures(id TEXT PRIMARY KEY,source TEXT,fingerprint TEXT,sku TEXT,scope TEXT,rating INTEGER,metadata TEXT)')
        data=small/'.验证资料';data.mkdir()
        writer=MetadataWriter(ROOT,data)
        try:
            product=small/'原目录/现场款图.JPG';product.parent.mkdir()
            shutil.copy2(fixture['onsite'],product)
            # 为测试款式图明确备案到第一张标签的SKU，验证同SKU并排关联；非原商品归属声明。
            product_record={'source':str(product),'sku':'LC7877836-P1','scope':'细节图','rating':1}
            product_key=capture_key(camera_metadata(writer.read(product,full=True)))
            product_content,product_path,_=writer.write(product,metadata_fields(product_record,product_key),sha(product))
            product_path.write_bytes(product_content)
            records=[(product,product_record['sku'],1)]
            for index,name in enumerate(('XSY_2297','XSY_2365')):
                original=label_root/(name+'.JPG');local=small/('原目录/二星标签-'+name+'.JPG')
                shutil.copy2(original,local)
                meta=sidecar_metadata(original,grouped=True)
                sku=meta.get('XMP-fk:SKU') or next(v[4:] for v in meta['XMP-dc:Subject'] if v.startswith('SKU:'))
                record={'source':str(local),'sku':sku,'scope':'细节图','rating':2}
                key=capture_key(camera_metadata(writer.read(local,full=True)))
                content,destination,_=writer.write(local,metadata_fields(record,key),sha(local))
                destination.write_bytes(content)
                records.append((local,sku,2))
                if index==1:shutil.copy2(original,final/'最终保留标签.JPG')
            for file in fixture['final']:
                shutil.copy2(file,final/Path(file).name)
            with closing(sqlite3.connect(database)) as c,c:
                for i,(p,sku,rating) in enumerate(records):
                    meta=camera_metadata(writer.read(p,full=True))
                    c.execute('INSERT INTO captures VALUES (?,?,?,?,?,?,?)',(str(i),str(p),capture_key(meta),sku,'细节图',rating,json.dumps(meta)))
            before={str(p):sha(p) for folder in (small,final) for p in folder.rglob('*') if p.suffix.lower() in ('.jpg','.cr3')}
            archive=SkuFolderArchive(writer,database)
            result=archive.execute(archive.plan(small,final,target,preprocess=enabled))
            assert result['final_expected']==3 and result['expected']==3 and result['expected_xmp']==1
            assert result['supplemental_label_count']==0 and result['label_file_count']==1 and result['deleted_reference_count']==1
            labels=[r for r in result['items'] if r['photo_role']=='label']
            assert len(labels)==1 and {r['origin'] for r in labels}=={'final'}
            for row in labels:
                assert Path(row['destination']).is_file()
                assert writer.read(row['destination'])['XMP-xmp:Rating']==2
                assert writer.read(row['destination'])['XMP-fk:SKU']==row['sku']
                assert row['thumbnail_path'] and row['xmp'] is None
            target_before={str(p):sha(p) for p in target.rglob('*') if p.is_file()}
            repeated=archive.execute(archive.plan(small,final,target,preprocess=enabled))
            assert all(r['status']=='identical_verified_skip' for r in repeated['items'])
            assert target_before=={str(p):sha(p) for p in target.rglob('*') if p.is_file()}
            if not enabled:assert before=={str(p):sha(p) for folder in (small,final) for p in folder.rglob('*') if p.suffix.lower() in ('.jpg','.cr3')}
            (target/'归档结果.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
            cases.append({'case_root':str(case),'preprocess':enabled,'passed':True,'archived':3,'final_files':3,'label_files':1,'supplemental_labels':0,'repeat_skipped':3})
            print(json.dumps(cases[-1],ensure_ascii=False),flush=True)
        finally:writer.close()
    assert all(sha(p)==digest for p,digest in original_hashes.items())
    summary={'passed':True,'original_labels_unchanged':True,'cases':cases}
    (run/'二星标签验证.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8')
    (ROOT/'.runtime/last-archive-labels.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8')


if __name__=='__main__':verify()
