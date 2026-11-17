"""在已有真实样片的全新副本上验证归档前处理开/关，不触碰首次测试成果。"""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from datetime import datetime

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'选片助手'))
from metadata_writer import MetadataWriter,sha
from sku_folder_archive import SkuFolderArchive,files_under


def snapshot(root):
    return {str(p.relative_to(root)):sha(p) for p in root.rglob('*') if p.is_file() and '.验证资料' not in p.parts}


def verify():
    baseline=ROOT/'测试文件夹'
    run=baseline/'sku库'/'.模式验证'/datetime.now().strftime('%Y%m%d-%H%M%S')
    run.mkdir(parents=True,exist_ok=False)
    summaries=[]
    baseline_before={str(p):sha(p) for folder in ('小档','大档') for p in (baseline/folder).rglob('*') if p.is_file()}
    for enabled,name in [(False,'关闭'),(True,'开启-默认')]:
        case=run/name;case.mkdir()
        small,final,target=[case/n for n in ('小档','大档','sku库')]
        small.mkdir();target.mkdir();shutil.copytree(baseline/'大档',final)
        database=small/'现场SKU.sqlite'
        with closing(sqlite3.connect(baseline/'小档/现场SKU.sqlite')) as src, closing(sqlite3.connect(database)) as c:
            src.backup(c)
            rows=c.execute('SELECT id,source FROM captures').fetchall()
            for id_,old in rows:
                relative=Path(old).relative_to(baseline/'小档')
                new=small/relative;new.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(old,new)
                c.execute('UPDATE captures SET source=? WHERE id=?',(str(new),id_))
            c.commit()
        data=small/'.验证资料';data.mkdir()
        writer=MetadataWriter(ROOT,data)
        small_before,final_before=snapshot(small),snapshot(final)
        try:
            archive=SkuFolderArchive(writer,database)
            # 默认开启，不为开启用例显式传参，验证服务默认值。
            plan=archive.plan(small,final,target,include_label_images=False) if enabled else archive.plan(small,final,target,preprocess=False,include_label_images=False)
            assert plan['preprocess'] is enabled
            result=archive.execute(plan)
            assert result['verified']==8 and result['xmp']==4 and result['deleted_reference_count']==2
            with closing(sqlite3.connect(database)) as c:
                onsite=c.execute('SELECT source,sku FROM captures').fetchall()
            if enabled:
                assert all(Path(path)==small/sku[:4]/sku/Path(path).name for path,sku in onsite)
                assert all(Path(r['source'])==final/r['parent_sku']/r['sku']/Path(r['source']).name for r in result['items'])
                assert len(result['preprocess_moves'])==17  # 小档8、大档8、已有XMP1。
                assert all(sha(entry['after'])==entry['sha256'] and not Path(entry['before']).exists() for entry in result['preprocess_moves'])
                assert all(Path(r['reference']).is_file() for r in result['skipped_reference_records'])
            else:
                assert small_before==snapshot(small) and final_before==snapshot(final)
                assert not result.get('preprocess_moves')
            # 重新扫描已整理来源，再执行不能套重母目录或改变已归档文件身份。
            target_before={str(p):sha(p) for p in target.rglob('*') if p.is_file()}
            second_plan=archive.plan(small,final,target,preprocess=enabled,include_label_images=False)
            repeated=archive.execute(second_plan)
            assert all(r['status']=='identical_verified_skip' for r in repeated['items'])
            assert target_before=={str(p):sha(p) for p in target.rglob('*') if p.is_file()}
            if enabled:assert repeated['preprocess_moves']==[]
            for item in result['items']:archive.verify_item(item)
            (target/'归档结果.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
            summaries.append({'mode':name,'preprocess':enabled,'passed':True,'small_files':len(onsite),
                              'final_files':8,'xmp_files':4,'deleted_onsite_retained':2,
                              'moved_components':len(result.get('preprocess_moves',[])),
                              'repeat_verified_skip':8,'case_root':str(case)})
            print(json.dumps(summaries[-1],ensure_ascii=False),flush=True)
        finally:writer.close()
    assert baseline_before=={str(p):sha(p) for folder in ('小档','大档') for p in (baseline/folder).rglob('*') if p.is_file()}
    path=run/'两种模式验证.json'
    path.write_text(json.dumps({'passed':True,'baseline_unchanged':True,'cases':summaries},ensure_ascii=False,indent=2),encoding='utf8')
    (ROOT/'.runtime/last-archive-modes.json').write_text(json.dumps({'run':str(run),'summary':str(path),'cases':summaries},ensure_ascii=False,indent=2),encoding='utf8')
    print(str(path),flush=True)


if __name__=='__main__':verify()
