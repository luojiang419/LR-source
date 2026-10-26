"""导航任务灯的只读摘要：不访问原图、不等待业务锁、不启动任何任务。"""
from contextlib import closing
from pathlib import Path
import sqlite3

MODULES=('selection','progress','archive','database','analysis','settings')


def snapshot(store=None,sku=None,archive=None,catalog=None,analysis=None,rawlink=None,components=None,updates=None,storage=None):
    groups={key:{'active':0,'waiting':0,'paused':0,'details':[],'available':True} for key in MODULES}

    def add(key,label,count,state='active'):
        if count:
            groups[key][state]+=count
            groups[key]['details'].append(f'{label} {count} 项')

    def read(database,keys,queries):
        # 独立只读连接，避免状态指示器等待归档锁或占用业务连接。
        try:
            with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True,timeout=.05)) as c:
                for key,label,sql,state in queries:add(key,label,int(c.execute(sql).fetchone()[0] or 0),state)
        except (OSError,sqlite3.Error):
            for key in keys:groups[key]['available']=False

    if store is not None:
        scans=sum(job['status'] in ('queued','running','cancelling') for job in store.jobs.copy().values())
        add('selection','照片索引',scans)
        from preview_cache import _LOCATIONS
        cache=_LOCATIONS.get(store.data.resolve(),store.data)/'previews/index.sqlite3'
        if cache.is_file():read(cache,('selection','archive'),[
            ('selection','预览生成',"SELECT count(*) FROM pending WHERE attempts<3 AND json_extract(record,'$._catalog') IS NULL",'active'),
            ('archive','预览生成',"SELECT count(*) FROM archive_jobs WHERE state='queued'",'active')])

    if sku is not None:
        queue_state='paused' if sku.tasks_paused else 'active'
        automatic="trigger='manual' OR (trigger='full_auto' AND {full}) OR (COALESCE(trigger,'') NOT IN ('manual','full_auto') AND {auto})".format(full=int(bool(sku.full_auto.get('enabled'))),auto=int(sku.task_mode=='auto'))
        queries=[
            ('progress','OCR 识别',"SELECT COUNT(*) FROM jobs WHERE status='running'",'active'),
            ('progress','OCR 排队',f"SELECT COUNT(*) FROM jobs WHERE status='queued' AND ({automatic})",queue_state),
            ('progress','OCR 等待模式恢复',f"SELECT COUNT(*) FROM jobs WHERE status='queued' AND NOT ({automatic})",'waiting'),
            ('progress','等待模型',"SELECT COUNT(*) FROM jobs WHERE status='waiting_model'",'waiting'),
            ('analysis','标签解析',"SELECT COUNT(*) FROM parse_jobs WHERE status='running'",'active'),
            ('analysis','标签解析排队',"SELECT COUNT(*) FROM parse_jobs WHERE status='queued'",queue_state),
            ('database','SKU 入库',"SELECT COUNT(*) FROM archive_events WHERE delivered=0",'active'),
        ]
        # 解析任务页也展示 OCR 队列，两个入口均反映这份真实工作。
        queries.extend(('analysis',label,sql,state) for key,label,sql,state in queries.copy() if key=='progress')
        read(sku.db,('progress','database','analysis'),queries)

    if archive is not None:
        queries=[('archive','元数据写入',"SELECT COUNT(*) FROM metadata_jobs WHERE status IN ('queued','running')",'active')]
        if rawlink is not None:
            queries.extend([
                ('archive','归档匹配',"SELECT COUNT(*) FROM raw_session_work WHERE status IN ('queued','running')",'active'),
                ('archive','归档校验',"SELECT COUNT(*) FROM archive_sync_targets WHERE status IN ('indexing','matching','verifying')",'active'),
            ])
        # 解析任务页的元数据队列与归档页面共享后台工作。
        queries.extend(('analysis',label,sql,state) for key,label,sql,state in queries.copy())
        read(archive.db,('archive','analysis'),queries)
    if catalog is not None:
        read(catalog.db,('archive','database'),[
            (key,'资产索引',"SELECT COUNT(*) FROM roots WHERE removed=0 AND status='scanning'",'active')
            for key in ('archive','database')])
    if analysis is not None:
        queued='paused' if analysis.paused else 'active'
        auto_enabled=int(bool(analysis.auto.get('enabled')))
        read(analysis.db,('analysis',),[
            ('analysis','图片解析',"SELECT COUNT(*) FROM jobs WHERE status='running'",'active'),
            ('analysis','解析排队',f"SELECT COUNT(*) FROM jobs WHERE status='queued' AND (trigger!='auto' OR {auto_enabled})",queued),
            ('analysis','等待自动解析恢复',f"SELECT COUNT(*) FROM jobs WHERE status='queued' AND trigger='auto' AND NOT {auto_enabled}",'waiting'),
            ('analysis','准备解析图片',"SELECT COUNT(*) FROM auto_inputs WHERE status='running'",'active'),
            ('analysis','准备排队',"SELECT COUNT(*) FROM auto_inputs WHERE status='queued'",queued if auto_enabled else 'waiting'),
        ])
    if components is not None and components.state.copy().get('status')=='working':add('settings','准备识别组件',1)
    if updates is not None and updates.value.copy().get('status')=='checking':add('settings','检查更新',1)
    if storage is not None and storage.job.copy().get('status')=='running':add('settings','缓存管理',1)

    for group in groups.values():
        group['state']=('busy' if group['active'] else 'unknown' if not group['available'] else 'paused' if group['paused'] else 'waiting' if group['waiting'] else 'idle')
        group['message']='；'.join(group.pop('details')) or ('状态暂不可用' if not group['available'] else '空闲')
        if not group['available'] and group['active']:group['message']+='；部分状态暂不可用'
    return {'modules':groups}
