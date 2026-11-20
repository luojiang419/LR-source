"""只读导出压缩后的目录库；原库保留，可在关闭程序后作为迁移/恢复来源。

python -X utf8 tools/compact_catalog.py --source <asset_catalog.sqlite3> --output <新路径>
先等待客户端元数据增量压缩完成；本命令不替换正在使用的数据库。
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time


def compact(source, output):
    source=Path(source).absolute();output=Path(output).absolute()
    if not source.is_file():raise ValueError('源数据库不存在')
    if source.resolve()==output.resolve() or output.exists():raise ValueError('目标必须是不存在的新文件，原库和旧备份均不可覆盖')
    if any(p.is_symlink() or p.is_junction() for p in (source,output,*source.parents,*output.parents)):
        raise ValueError('压缩路径不可经过链接目录')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent,prefix='.catalog-compact-',suffix='.sqlite3',delete=False) as stream:
        temporary=Path(stream.name)
    started=time.monotonic()
    try:
        with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as original:
            # VACUUM INTO 在一致性读快照中复制，包含已提交 WAL，不修改源文件。
            original.execute('VACUUM INTO ?', (str(temporary),))
        with closing(sqlite3.connect(temporary)) as copied:
            if copied.execute("SELECT 1 FROM sqlite_master WHERE name='catalog_metadata_fts'").fetchone():
                # 旧 FTS 删除段可能仍占用大量页；只在导出副本中重建并压实。
                copied.execute("INSERT INTO catalog_metadata_fts(catalog_metadata_fts) VALUES('rebuild')")
                copied.commit();copied.execute('VACUUM')
            if copied.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('压缩文件完整性校验未通过')
            if not copied.execute("SELECT 1 FROM sqlite_master WHERE name='items'").fetchone():raise ValueError('源文件不是目录数据库')
        if os.name=='nt':os.rename(temporary,output)
        else:os.link(temporary,output)
        wal=source.with_name(source.name+'-wal')
        return {'source':str(source),'output':str(output),'source_bytes':source.stat().st_size+(wal.stat().st_size if wal.exists() else 0),
                'output_bytes':output.stat().st_size,'seconds':round(time.monotonic()-started,3),'verified':True}
    finally:
        # 仅清理由本函数独占创建、且尚未通过校验的输出。
        temporary.unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    print(json.dumps(compact(args.source,args.output),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
