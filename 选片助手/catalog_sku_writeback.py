"""归档 OCR 确认结果的持久发件箱，复用既有文件元数据提交与恢复。"""
import hashlib
import json
from pathlib import Path
import time
import uuid
import sqlite3
from contextlib import closing
from copy import deepcopy

from catalog_ocr import AUTO_SOURCES,reconcile_folder
from photo_metadata import ensure_file,source
from metadata_writer import sha


def fingerprint(path):
    stat=Path(path).stat()
    return hashlib.sha256(f'{str(path).casefold()}:{stat.st_size}:{stat.st_mtime_ns}'.encode()).hexdigest(),stat


class CatalogSkuWriteback:
    def __init__(self,archive,catalog,sku):
        self.archive=archive;self.catalog=catalog;self.sku=sku;self.error=''
        self.snapshot={'counts':{},'failed':[],'paused':False,'loading':True}
        archive.catalog_writeback=self

    def current(self,item):
        with self.catalog.connect() as c:
            row=c.execute('SELECT i.*,r.removed FROM items i JOIN roots r ON r.id=i.root WHERE i.id=?',(item,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def eligible(row):
        return bool(row and row['online'] and not row['removed'] and row['sku'] and row['source'] in AUTO_SOURCES)

    def validate(self,guard):
        row=self.current(guard['item'])
        if not self.eligible(row) or row['sku']!=guard['sku'] or row['fingerprint']!=guard['fingerprint']:
            raise ValueError('OCR 款号或图片版本已更新，旧元数据任务已停止')
        if fingerprint(row['path'])[0]!=row['fingerprint']:
            raise ValueError('原文件已变化，等待重新索引和核验后写入 SKU')

    def update(self,item,status,reason='',**values):
        values.update(status=status,reason=reason,updated=time.time())
        with self.catalog.connect() as c:
            c.execute('UPDATE catalog_sku_writeback SET '+','.join(key+'=?' for key in values)+' WHERE item=?',list(values.values())+[item])

    def state(self):
        return {**deepcopy(self.snapshot),'error':self.error}

    def refresh_state(self):
        # 高频界面轮询只读内存。后台读取小队列时也不建立主库的 2GB 映射。
        with closing(sqlite3.connect(self.catalog.db.as_uri()+'?mode=ro',uri=True,timeout=.1)) as c:
            c.row_factory=sqlite3.Row
            c.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE,True)
            counts=dict(c.execute('SELECT status,count(*) FROM catalog_sku_writeback GROUP BY status'))
            failed=[dict(r) for r in c.execute("SELECT item,sku,reason FROM catalog_sku_writeback WHERE status='failed' ORDER BY updated DESC LIMIT 5")]
            paused=bool(c.execute("SELECT 1 FROM intelligence_settings WHERE key='ocr_metadata_paused' AND value='1'").fetchone())
        self.snapshot={'counts':counts,'failed':failed,'paused':paused,'loading':False}

    def retry(self):
        with self.archive.lock:
            with self.catalog.connect() as c:
                rows=c.execute("""SELECT item,operation FROM catalog_sku_writeback
 WHERE status IN ('failed','cancelled') OR (operation<>'' AND status IN ('queued','writing'))""").fetchall()
            for row in rows:
                values={}
                if row['operation']:
                    jobs=self.archive.operation(row['operation'])['jobs']
                    if jobs and all(job['status']=='cancelled' for job in jobs):values={'operation':'','request_key':''}
                    else:self.archive.retry(row['operation'])
                self.update(row['item'],'queued',**values)
            with self.catalog.connect() as c:
                c.execute("DELETE FROM intelligence_settings WHERE key IN ('ocr_metadata_paused','ocr_metadata_seed_v1')")
        self.refresh_state();return self.state()

    def tick(self):
        self.refresh_state()
        with self.catalog.connect() as c:
            if c.execute("SELECT 1 FROM intelligence_settings WHERE key='ocr_metadata_paused' AND value='1'").fetchone():return False
            row=c.execute("SELECT * FROM catalog_sku_writeback WHERE status IN ('writing','reverting') ORDER BY updated LIMIT 1").fetchone()
            if row is None:row=c.execute("SELECT * FROM catalog_sku_writeback WHERE status='queued' ORDER BY updated LIMIT 1").fetchone()
        if row is None:return False
        entry=dict(row)
        try:
            self.process(entry);self.error=''
        except Exception as error:
            self.error=str(error)[:350]
            self.update(entry['item'],'failed',self.error)
        return True

    def process(self,entry):
        item=entry['item'];current=self.current(item)
        # 跨库确认中断后先找回原操作，随后按当前归属决定接续或撤销。
        if not entry['operation'] and entry['request_key']:
            with self.archive.connect() as c:
                previous=c.execute('SELECT id FROM operations WHERE source=? LIMIT 1',(entry['request_key'],)).fetchone()
            if previous:
                entry['operation']=previous['id'];self.update(item,entry['status'],operation=previous['id'])
        if entry['operation']:
            operation=self.archive.operation(entry['operation']);jobs=operation['jobs']
            pending=any(job['status'] in ('queued','running') for job in jobs)
            failed=[job for job in jobs if job['status']=='failed']
            same=self.eligible(current) and current['sku']==entry['sku'] and current['fingerprint']==entry['fingerprint']
            if entry['status']=='reverting':
                if pending:return
                if failed:raise ValueError(failed[0]['reason'])
                self.update(item,'queued' if self.eligible(current) else 'cancelled',operation='',request_key='',
                            fingerprint=current['fingerprint'] if current else entry['fingerprint'],sku=current['sku'] if current else '')
                return
            if not same:
                # 已写入的字段通过原有日志恢复；人工后续修订由 archive.revert 的版本规则保护。
                restored=self.archive.revert(entry['operation'])
                self.update(item,'reverting',operation=restored['operation']['id'])
                return
            if pending:self.update(item,'writing');return
            if failed:raise ValueError(failed[0]['reason'])
            if jobs and all(job['status']=='applied' for job in jobs):self.update(item,'applied');return
            self.update(item,'cancelled','已有更新的资产归属，旧写入任务已取消')
            return
        if not self.eligible(current):self.update(item,'cancelled');return
        guard={key:current[key] for key in ('path','fingerprint','sku','source')};guard['item']=item
        self.validate(guard)
        same_request=entry['fingerprint']==current['fingerprint'] and entry['sku']==current['sku']
        key=entry['request_key'] if same_request and entry['request_key'] else 'catalog-ocr:'+uuid.uuid4().hex
        if key!=entry['request_key']:
            self.update(item,'queued',request_key=key,fingerprint=current['fingerprint'],sku=current['sku'])
        path,root=source(self.catalog,item)
        with self.archive.lock:
            file=ensure_file(self.archive,path,root);asset=self.archive.asset(file['asset'])
            # 每次只登记一个组件，交给同一队列串行提交，避免 RAW/JPG 互相取消修订。
            preview=self.archive.preview(None,None,current['sku'],asset['scope'] or '未分类',asset['project'],
                                         asset_ids=[file['asset']],source='ocr_folder')
            self.validate(guard)
            operation=self.archive.apply(preview['preview_id'],request_key=key,metadata_guard=guard)
        self.update(item,'writing',operation=operation['operation']['id'],fingerprint=current['fingerprint'],sku=current['sku'])

    def committed(self,job,guard,content_sha):
        """经原写入器校验的纯元数据改动，保留同一画面的 OCR 和视觉证据。"""
        current=self.current(guard['item'])
        if not current:return
        journal=json.loads(job['journal'])
        expected=journal['after_hash'] if Path(journal['target'])==Path(job['path']) else job['expected_sha']
        if sha(job['path'])!=expected or content_sha!=expected:
            raise ValueError('元数据提交后原文件发生其他变化，未沿用旧 OCR 证据')
        prior=guard['fingerprint']
        if journal.get('restore'):
            with self.catalog.connect() as c:
                saved=c.execute('SELECT fingerprint FROM catalog_sku_writeback WHERE item=?',(guard['item'],)).fetchone()
            if saved:prior=saved[0]
        new_fingerprint,stat=fingerprint(job['path'])
        if current['fingerprint'] not in (prior,new_fingerprint):
            raise ValueError('元数据提交后素材索引出现其他版本，请重新核验')
        if new_fingerprint!=prior:
            with self.catalog.connect() as c:
                c.execute('UPDATE catalog_sku_writeback SET written_fingerprint=? WHERE item=?',(new_fingerprint,guard['item']))
                for table in ('intelligence_ocr','intelligence_ocr_evidence','intelligence_results','intelligence_vectors','smart_frames'):
                    c.execute('UPDATE '+table+' SET fingerprint=? WHERE item=? AND fingerprint=?',(new_fingerprint,guard['item'],prior))
                c.execute("""UPDATE intelligence_file_fingerprints SET fingerprint=?,
 content_sha=CASE WHEN content_sha<>'' THEN ? ELSE '' END WHERE item=? AND fingerprint=?""",
                          (new_fingerprint,content_sha,guard['item'],prior))
                c.execute('UPDATE items SET size=?,modified=?,fingerprint=? WHERE id=? AND fingerprint=?',
                          (stat.st_size,stat.st_mtime,new_fingerprint,guard['item'],prior))
                # items 的版本触发器会清除派生帧索引，用已保存的视觉结果恢复。
                visual=c.execute('SELECT frames FROM intelligence_results WHERE item=? AND fingerprint=?',(guard['item'],new_fingerprint)).fetchone()
                if visual:
                    from smart_index import save_frames
                    save_frames(c,guard['item'],new_fingerprint,json.loads(visual['frames']))
                c.execute("UPDATE catalog_sku_writeback SET fingerprint=?,written_fingerprint='' WHERE item=?",(new_fingerprint,guard['item']))
                reconcile_folder(c,current['root'],current['folder'],self.sku.sku_rules())
        # 已知 item 主键，不走按路径忽略大小写的全库查找。
        with self.catalog.connect() as c:
            c.execute("UPDATE catalog_metadata SET status='queued',priority=1,error='',generation=generation+1 WHERE item=?",(guard['item'],))
        self.catalog.metadata.wake.set()
