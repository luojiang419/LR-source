"""正式本机 HTTP 接口；沿用主服务的 Host/令牌验证。"""
import sys
from urllib.parse import parse_qs

from folder_archive_jobs import FolderArchiveJobs


def service(handler):
    host=sys.modules[handler.__class__.__module__]
    if getattr(host,'STORE',None) is None or getattr(host,'ARCHIVE',None) is None or getattr(host,'CATALOG',None) is None:
        raise ValueError('归档服务尚未就绪')
    if not hasattr(handler.server,'folder_archives'):
        # HTTP 处理器可能并发初始化，借用现有资产锁确保单实例。
        with host.ARCHIVE.lock:
            if not hasattr(handler.server,'folder_archives'):
                handler.server.folder_archives=FolderArchiveJobs(host.STORE,host.ARCHIVE,host.CATALOG,getattr(host,'WATCH',None))
    return handler.server.folder_archives


def get(handler,path,query):
    api=service(handler);q={k:v[0] for k,v in parse_qs(query,max_num_fields=10).items()}
    if path=='/api/folder-archive/state':return handler.reply(api.state(q.get('id',''),q.get('offset',0)))
    if path=='/api/folder-archive/thumbnail':return handler.reply(api.thumbnail(q.get('id',''),q.get('item','')),kind='image/jpeg')
    if path=='/api/folder-archive/report':
        content,name=api.report(q.get('id',''))
        return handler.reply(content,kind='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',filename=name)
    return handler.reply({'error':'归档接口不存在'},404)


def post(handler,path,body):
    api=service(handler)
    if path=='/api/folder-archive/scope':return handler.reply(api.sources.selection(body.get('root_id',''),body.get('folder','')))
    if path=='/api/folder-archive/prepare':return handler.reply(api.prepare(body))
    action=path.rsplit('/',1)[-1]
    if action not in ('start','pause','resume','cancel','export'):return handler.reply({'error':'归档操作不存在'},404)
    return handler.reply(getattr(api,action)(body.get('id','')))


def close(server):
    if hasattr(server,'folder_archives'):server.folder_archives.close()
