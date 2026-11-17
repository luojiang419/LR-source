"""正式主页面/HTTP处理器的隔离实图验收环境，不启动拍摄、模型或生产资料。"""
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'选片助手'))


def main():
    labels=json.loads((ROOT/'.runtime/last-archive-labels.json').read_text(encoding='utf8'))
    baseline=Path(next(c['case_root'] for c in labels['cases'] if not c['preprocess']))
    case=ROOT/'测试文件夹'/'sku库'/'.主项目验收'/datetime.now().strftime('%Y%m%d-%H%M%S')
    case.mkdir(parents=True)
    small,final,target=[case/n for n in ('小档','大档','sku库')]
    for source,dest in ((baseline/'小档',small),(baseline/'大档',final)):
        shutil.copytree(source,dest,ignore=shutil.ignore_patterns('.验证资料','*.sqlite','*.json'))
    target.mkdir()
    os.environ['FRAMEKEEP_DATA']=str(case/'.资料')
    import server
    from catalog_store import Store
    from asset_archive import AssetArchive
    from asset_catalog import AssetCatalog
    from model_providers import Providers
    from sku_service import SkuService
    from asset_intelligence import AssetIntelligence
    server.DATA=case/'.资料'
    server.DATA.mkdir(exist_ok=True)
    # 旧独立验证人为重建了AssetId，正式验收保留对应原件的既有永久身份。
    from metadata_writer import MetadataWriter,sha
    from sku_folder_archive import files_under,camera_metadata,capture_key
    writer=MetadataWriter(ROOT,server.DATA)
    try:
        final_ids={capture_key(camera_metadata(meta)):meta['XMP-fk:AssetId'] for file in files_under(final) if (meta:=writer.read(file,full=True)).get('XMP-fk:AssetId')}
        for file in files_under(small):
            meta=writer.read(file,full=True);aid=final_ids.get(capture_key(camera_metadata(meta)))
            if aid and aid!=meta.get('XMP-fk:AssetId'):
                content,destination,_=writer.write(file,{'AssetId':aid},sha(file));destination.write_bytes(content)
    finally:writer.close()
    server.STORE=Store(server.DATA,ROOT)
    server.ARCHIVE=AssetArchive(server.STORE,autostart=False)
    server.CATALOG=AssetCatalog(server.DATA)
    server.MODELS=Providers(server.DATA)
    server.SKU=SkuService(server.STORE,server.MODELS,archive=server.ARCHIVE,autostart=False)
    server.INTELLIGENCE=AssetIntelligence(server.CATALOG,server.MODELS,server.SKU,autostart=False)
    server.ARCHIVE.sku=server.SKU
    server.CATALOG.add_root(str(small))
    # 现场清单也使用正式Store扫描，随后验收源路径同步。
    scan=server.STORE.scan(str(small),True)
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        if server.STORE.jobs[scan]['status'] in ('completed','failed','cancelled'):break
        time.sleep(.05)
    if server.STORE.jobs[scan]['status']!='completed':raise RuntimeError('隔离来源扫描未完成')
    roots=server.CATALOG.roots()
    server.WATCH=None;server.MOUSE=None;server.ANALYSIS=None;server.RAWLINK=None;server.UPDATES=None;server.PREVIEWS=None;server.REFERENCES=None;server.CANON=None;server.INVENTORY=None
    http=server.ThreadingHTTPServer(('127.0.0.1',18828),server.Handler);http.daemon_threads=True
    info={'url':'http://127.0.0.1:18828/','case':str(case),'small':str(small),'final':str(final),'target':str(target),'root_id':roots[0]['id'],'pid':os.getpid()}
    (ROOT/'.runtime/main-archive-acceptance.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(info,ensure_ascii=False),flush=True)
    try:http.serve_forever()
    finally:
        if hasattr(http,'folder_archives'):http.folder_archives.close()
        http.server_close();server.INTELLIGENCE.close();server.SKU.close();server.ARCHIVE.close();server.CATALOG.close()


if __name__=='__main__':main()
