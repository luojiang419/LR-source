"""多模态资产检索服务：持久任务、版本校验、内容归组及可解释查询。"""
import hashlib
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from PIL import Image

from asset_analysis import vision
from retrieval_schema import DIMENSIONS, PROMPT, SCHEMA, custom_dimensions, digest, dumps, normalized, parse_result
import retrieval_store as index
from sku_match import normalize
from sku_rules import DEFAULT_RULES, length_allowed, match_rules
from visual_features import APPEARANCE, CLIP, ClipRuntime, appearance, decode_upload

DEFAULTS = {'enabled': False, 'paused': False, 'auto_group': True, 'custom_dimensions': [],
            'visual_backend': 'appearance', 'material_root': r'D:\Program Files\MaterialSearchWindows',
            'shortcut': 'Ctrl+Shift+Space', 'video_frames': 3}


class AssetIntelligence:
    def __init__(self, catalog, providers, sku=None, *, autostart=True):
        self.catalog = catalog;self.providers = providers;self.sku = sku
        self.lock = threading.RLock();self.closed = threading.Event();self.wake = threading.Event()
        self.last_error = '';self.seed_cursor = 0;self.seed_done = False
        with catalog.connect() as c:
            index.install(c)
            c.execute("UPDATE intelligence_jobs SET status='queued',reason='重启后恢复在途任务' WHERE status='running'")
            row = c.execute("SELECT value FROM intelligence_settings WHERE key='config'").fetchone()
        self.config = {**DEFAULTS, **(json.loads(row[0]) if row else {})}
        self.clip = ClipRuntime(self.config['material_root'])
        catalog.intelligence = self
        self.threads = [threading.Thread(target=self._run, daemon=True, name='catalog-vlm-'+str(i)) for i in range(2)]
        self.discovery = threading.Thread(target=self._discover, daemon=True, name='catalog-vlm-discovery')
        if autostart:
            for worker in self.threads:worker.start()
            self.discovery.start()

    def configure(self, body):
        with self.lock:
            config = dict(self.config)
            for name in ('enabled', 'paused', 'auto_group'):
                if name in body:
                    if type(body[name]) is not bool:raise ValueError('开关参数无效')
                    config[name] = body[name]
            if 'custom_dimensions' in body:config['custom_dimensions'] = custom_dimensions(body['custom_dimensions'])
            if 'visual_backend' in body:
                if body['visual_backend'] not in ('appearance', 'clip'):raise ValueError('图像索引类型无效')
                config['visual_backend'] = body['visual_backend']
            if 'material_root' in body:
                if not isinstance(body['material_root'], str) or len(body['material_root']) > 1000:raise ValueError('模型路径无效')
                config['material_root'] = body['material_root']
            if 'video_frames' in body:
                if type(body['video_frames']) is not int or not 1 <= body['video_frames'] <= 8:raise ValueError('视频采样帧数应为 1–8')
                config['video_frames'] = body['video_frames']
            if 'shortcut' in body:
                if body['shortcut'] not in ('Ctrl+Shift+Space', 'Alt+Space', 'Ctrl+Alt+F', 'off'):raise ValueError('快捷键无效')
                config['shortcut'] = body['shortcut']
            if config['enabled']:
                self._provider()
            with self.catalog.connect() as c:
                c.execute("INSERT OR REPLACE INTO intelligence_settings VALUES('config',?)", (dumps(config),))
            changed = any(config[name] != self.config[name] for name in ('custom_dimensions', 'visual_backend', 'video_frames'))
            if config['material_root'] != self.config['material_root']:
                self.clip.close();self.clip = ClipRuntime(config['material_root']);changed = True
            self.config = config
            if changed:self.seed_cursor = 0;self.seed_done = False
        self.wake.set()
        return self.state()

    def _provider(self):
        provider = self.providers.purpose('asset_analysis') or self.providers.selected('multimodal')
        if not provider or provider['category'] != 'multimodal':raise ValueError('请先在模型设置选用多模态模型')
        self.providers.check_inference(provider)
        return provider

    def _config_key(self):
        provider = self._provider()
        return digest([1, provider['id'], provider['model'], provider['protocol'], provider['url'],
                       self.config['custom_dimensions'], self.config['visual_backend'], self.config['video_frames']])

    def enqueue(self, item_ids=None, root='', force=False):
        if item_ids is not None and (not isinstance(item_ids, list) or not 1 <= len(item_ids) <= 2000):
            raise ValueError('每次请选择 1–2000 个资产')
        key = self._config_key();now = time.time();queued = 0
        with self.lock, self.catalog.connect() as c:
            if item_ids:
                rows = c.execute('SELECT id,fingerprint FROM capture_items WHERE id IN ('+','.join('?' for _ in item_ids)+')', item_ids).fetchall()
                if len(rows) != len(set(item_ids)):raise ValueError('部分资产不存在或已离线')
            else:
                # 大范围请求仅标记待处理，不在 HTTP 请求中展开图像或建立全量 Python 列表。
                clause = ' AND root=?' if root else ''
                c.execute('INSERT OR IGNORE INTO intelligence_dirty SELECT id FROM capture_items WHERE media_type IN (\'image\',\'video\')'+clause, [root] if root else [])
                if force:
                    c.execute("UPDATE intelligence_jobs SET status='queued',attempt=0,due=?,updated=? WHERE status NOT IN ('running','queued') AND item IN (SELECT id FROM items WHERE 1=1"+clause+')', [now, now]+([root] if root else []))
                self.wake.set();return {'accepted': True, 'message': '已标记索引任务，后台按批调度'}
            for row in rows:
                if force:
                    c.execute("DELETE FROM intelligence_jobs WHERE item=? AND fingerprint=? AND config=? AND status!='running'", (row['id'], row['fingerprint'], key))
                queued += c.execute('INSERT OR IGNORE INTO intelligence_jobs VALUES(?,?,?,?,\'queued\',0,\'\',?,?,?)',
                                    (uuid.uuid4().hex, row['id'], row['fingerprint'], key, now, now, now)).rowcount
        self.wake.set();return {'queued': queued}

    def discover_tick(self):
        if not self.config['enabled'] or self.config['paused']:return
        key = self._config_key();now = time.time()
        with self.lock, self.catalog.connect() as c:
            if c.execute("SELECT count(*) FROM intelligence_jobs WHERE status IN ('queued','running')").fetchone()[0] >= 1000:return
            if not self.seed_done:
                rows = c.execute('SELECT rowid,id FROM items WHERE rowid>? ORDER BY rowid LIMIT 256', (self.seed_cursor,)).fetchall()
                c.executemany('INSERT OR IGNORE INTO intelligence_dirty VALUES(?)', [(r['id'],) for r in rows])
                if rows:self.seed_cursor = rows[-1]['rowid']
                self.seed_done = len(rows) < 256
            dirty = c.execute('SELECT item FROM intelligence_dirty LIMIT 128').fetchall()
            for entry in dirty:
                row = c.execute("SELECT i.id,i.fingerprint FROM capture_items i JOIN roots r ON r.id=i.root WHERE i.id=? AND r.removed=0 AND i.media_type IN ('image','video')", (entry['item'],)).fetchone()
                if row:
                    c.execute('INSERT OR IGNORE INTO intelligence_jobs VALUES(?,?,?,?,\'queued\',0,\'\',?,?,?)',
                              (uuid.uuid4().hex, row['id'], row['fingerprint'], key, now, now, now))
                c.execute('DELETE FROM intelligence_dirty WHERE item=?', (entry['item'],))

    def _discover(self):
        previous = ''
        while not self.closed.is_set():
            try:
                if self.config['enabled']:
                    key = self._config_key()
                    if previous and key != previous:self.seed_cursor = 0;self.seed_done = False
                    previous = key
                self.discover_tick()
            except Exception as exc:self.last_error = str(exc)[:400]
            self.closed.wait(1)

    def _run(self):
        while not self.closed.is_set():
            try:worked = self.tick()
            except Exception as exc:self.last_error = str(exc)[:400];worked = False
            if not worked:self.wake.wait(.5);self.wake.clear()

    def _current(self, c, job):
        row = c.execute('SELECT i.fingerprint,i.online,r.removed,j.status FROM items i JOIN roots r ON r.id=i.root JOIN intelligence_jobs j ON j.item=i.id WHERE j.id=?', (job['id'],)).fetchone()
        return bool(row and row['fingerprint'] == job['fingerprint'] and row['online'] and not row['removed'] and row['status'] == 'running')

    def _images(self, row):
        path = Path(row['path']);stat = path.stat()
        # 目录记录的 fingerprint 与 size/mtime 共同验证；提交前再检查一次。
        if stat.st_size != row['size'] or abs(stat.st_mtime-row['modified']) > .001:
            raise ValueError('源文件已变化，请刷新归档索引')
        if row['media_type'] == 'video':
            from video_preview import find_tool
            import subprocess
            ffmpeg = find_tool('ffmpeg');probe = find_tool('ffprobe')
            if not ffmpeg or not probe:raise ValueError('视频多维解析需要 FFmpeg / ffprobe')
            info = subprocess.run([str(probe), '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(path)],
                                  capture_output=True, timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=True)
            duration = float(json.loads(info.stdout)['format']['duration'])
            positions = [(i+.5)*duration/self.config['video_frames'] for i in range(self.config['video_frames'])]
            result = []
            for seconds in positions:
                payload = subprocess.run([str(ffmpeg), '-v', 'error', '-ss', str(seconds), '-i', str(path), '-frames:v', '1',
                    '-vf', 'scale=1280:1280:force_original_aspect_ratio=decrease', '-f', 'image2pipe', '-vcodec', 'mjpeg', '-threads', '1', 'pipe:1'],
                    capture_output=True, timeout=40, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=True).stdout
                if not payload or len(payload) > 8_000_000:raise ValueError('视频采样帧无法解码')
                result.append((seconds, payload))
            return result
        payload = self.catalog.preview(row['id'], 1600, read=True)
        if isinstance(payload, tuple):payload = payload[0]
        if not payload:raise ValueError('图片预览不可用')
        return [(0, payload)]

    def tick(self):
        with self.lock:
            if self.closed.is_set() or self.config['paused'] or not self.config['enabled']:return False
            provider = self._provider();config_key = self._config_key();config = dict(self.config)
            with self.catalog.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                row = c.execute("SELECT * FROM intelligence_jobs WHERE status='queued' AND due<=? ORDER BY created LIMIT 1", (time.time(),)).fetchone()
                if not row:return False
                job = dict(row)
                c.execute("UPDATE intelligence_jobs SET status='running',attempt=attempt+1,updated=? WHERE id=?", (time.time(), job['id']))
        try:
            if job['config'] != config_key:raise ValueError('解析配置已变化，旧任务失效')
            row = self.catalog.item(job['item']);self.catalog._root(row['root'])
            if row['fingerprint'] != job['fingerprint']:raise ValueError('文件版本已变化，旧任务失效')
            def valid():
                if self.closed.is_set() or self.config['paused'] or not self.config['enabled']:return False
                with self.catalog.connect() as c:return self._current(c, job)
            frames = [];vectors = []
            with self.providers.inference_slot(provider, cancel=self.closed, valid=valid):
                for number, (seconds, payload) in enumerate(self._images(row)):
                    if not valid():raise ValueError('任务已暂停或取消')
                    prompt = PROMPT+'\n额外自定义维度（在 custom 中逐项返回）：'+dumps(config['custom_dimensions'])
                    raw = vision(provider, payload, prompt, SCHEMA)
                    value = parse_result(raw, config['custom_dimensions'])
                    frames.append({'seconds': round(seconds, 3), 'result': value})
                    vectors.append((number, APPEARANCE, appearance(payload)))
                    if config['visual_backend'] == 'clip':vectors.append((number, CLIP, self.clip.encode(image=payload)))
            # 不持数据库锁调用模型。提交事务内重新校验文件、配置和任务状态。
            stat = Path(row['path']).stat()
            if stat.st_size != row['size'] or abs(stat.st_mtime-row['modified']) > .001:raise ValueError('源文件已变化，请刷新归档索引')
            with self.lock, self.catalog.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                if not self._current(c, job) or self._config_key() != job['config']:raise ValueError('版本或配置已变化，结果已丢弃')
                if not valid():raise ValueError('任务已暂停或取消')
                index.save_result(c, row['id'], row['fingerprint'], job['config'], provider['model'], frames, vectors)
                c.execute("UPDATE intelligence_jobs SET status='completed',reason='',updated=? WHERE id=?", (time.time(), job['id']))
            self.propose_group(row['root'], row['folder'])
            self.last_error = ''
        except Exception as exc:
            message = str(exc)[:600];self.last_error = message
            transient = any(word in message for word in ('HTTP 429', 'HTTP 5', '超时', '不可用'))
            status = 'queued' if (transient and job['attempt'] < 2) or self.config['paused'] or not self.config['enabled'] else 'failed'
            if '变化' in message or '失效' in message:status = 'stale'
            with self.catalog.connect() as c:
                c.execute("UPDATE intelligence_jobs SET status=?,reason=?,due=?,updated=? WHERE id=? AND status='running'",
                          (status, message, time.time()+min(300, 10*2**job['attempt']), time.time(), job['id']))
        return True

    def control(self, action, job_id=''):
        if action not in ('retry', 'cancel', 'pause', 'resume'):raise ValueError('任务操作无效')
        if action in ('pause', 'resume'):return self.configure({'paused': action == 'pause'})
        with self.lock, self.catalog.connect() as c:
            if job_id and not c.execute('SELECT 1 FROM intelligence_jobs WHERE id=?', (job_id,)).fetchone():raise ValueError('任务不存在')
            clause = ' AND id=?' if job_id else ''
            if action == 'cancel':
                c.execute("UPDATE intelligence_jobs SET status='cancelled',updated=? WHERE status IN ('queued','running')"+clause, [time.time()]+([job_id] if job_id else []))
            else:
                c.execute("UPDATE intelligence_jobs SET status='queued',reason='',attempt=0,due=?,updated=? WHERE status IN ('failed','cancelled')"+clause, [time.time(), time.time()]+([job_id] if job_id else []))
        self.wake.set();return self.state()

    def state(self):
        with self.catalog.connect() as c:
            counts = {r[0]:r[1] for r in c.execute('SELECT status,count(*) FROM intelligence_jobs GROUP BY status')}
            coverage = c.execute('SELECT count(*) FROM intelligence_results r JOIN items i ON i.id=r.item AND i.fingerprint=r.fingerprint JOIN roots ON roots.id=i.root WHERE i.online=1 AND roots.removed=0').fetchone()[0]
            jobs = [dict(r) for r in c.execute('SELECT j.*,i.name FROM intelligence_jobs j LEFT JOIN items i ON i.id=j.item ORDER BY j.updated DESC LIMIT 80')]
            groups = [dict(r) for r in c.execute("SELECT * FROM intelligence_groups WHERE status!='rejected' ORDER BY CASE WHEN status IN ('review','ready') THEN 0 ELSE 1 END,updated DESC LIMIT 50")]
            vectors = {r[0]:r[1] for r in c.execute('SELECT backend,count(*) FROM intelligence_vectors GROUP BY backend')}
        for group in groups:group['evidence'] = json.loads(group['evidence'])
        return {'config': dict(self.config), 'dimensions': DIMENSIONS, 'counts': counts, 'coverage': coverage,
                'jobs': jobs, 'groups': groups, 'vectors': vectors, 'clip_available': self.clip.available(),
                'error': self.last_error, 'discovering': not self.seed_done}

    def details(self, item):
        self.catalog.item(item)
        with self.catalog.connect() as c:
            row = c.execute('SELECT r.*,i.fingerprint AS current FROM intelligence_results r JOIN items i ON i.id=r.item WHERE r.item=?', (item,)).fetchone()
        if not row:return {'parsed': False, 'frames': []}
        return {'parsed': True, 'stale': row['current'] != row['fingerprint'], 'provider': row['provider'],
                'created': row['created'], 'frames': json.loads(row['frames'])}

    def search(self, **query):
        return index.query(self.catalog, **query)

    def image_search(self, body):
        backend = CLIP if self.config['visual_backend'] == 'clip' else APPEARANCE
        if body.get('text') and backend != CLIP:
            raise ValueError('文字语义检索需要在检索设置启用 Chinese-CLIP 并完成图像索引')
        if body.get('item_id'):
            row = self.catalog.item(body['item_id'])
            with self.catalog.connect() as c:
                saved = c.execute('SELECT vector FROM intelligence_vectors WHERE item=? AND backend=? AND fingerprint=? ORDER BY frame LIMIT 1', (row['id'], backend, row['fingerprint'])).fetchone()
            if saved:
                import array
                vector = array.array('f');vector.frombytes(saved['vector'])
            else:
                payload = self._images(row)[0][1]
                vector = self.clip.encode(image=payload) if backend == CLIP else appearance(payload)
        elif body.get('text') and backend == CLIP:
            text = body['text']
            if not isinstance(text, str) or len(text) > 300:raise ValueError('语义检索文字过长')
            vector = self.clip.encode(text=text)
        else:
            payload = decode_upload(body.get('image', ''))
            vector = self.clip.encode(image=payload) if backend == CLIP else appearance(payload)
        response = index.image_query(self.catalog, vector, backend, limit=body.get('limit', 60),
                                     root=body.get('root', ''), threshold=body.get('threshold', 0))
        if body.get('text'):
            lexical = self.search(text=body['text'], root=body.get('root', ''), limit=60)['rows']
            merged = {};scores = {}
            for lane in (lexical, response['rows']):
                for rank, row in enumerate(lane):
                    item = row['id'];scores[item] = scores.get(item, 0)+1/(60+rank)
                    if item not in merged:merged[item] = dict(row)
                    else:
                        merged[item]['reasons'] = list(dict.fromkeys(merged[item]['reasons']+row['reasons']))
                        if 'score' in row:merged[item]['score'] = row['score']
            ordered = sorted(merged, key=lambda item: (merged[item].get('priority', 2), -scores[item], item))
            response['rows'] = [merged[item] for item in ordered[:max(1,min(100,int(body.get('limit',60))))]]
            response['fusion'] = 'sku_priority+reciprocal_rank'
        return response

    def facets(self, key=''):
        allowed = list(DIMENSIONS)+self.config['custom_dimensions']
        if key and key not in allowed:raise ValueError('维度名称无效')
        keys = [key] if key else ['category','color','material','scene','style']
        rows = [];partial = False
        with self.catalog.connect() as c:
            deadline = time.monotonic()+1
            c.set_progress_handler(lambda:int(time.monotonic()>deadline),5000)
            try:
                for dimension in keys:
                    after = ''
                    # 沿 (key,normalized) 做不同值的游标跳跃；不为筛选建议统计整库数千万维度。
                    for _ in range(40):
                        row = c.execute('''SELECT normalized,value FROM intelligence_dimensions
                            WHERE key=? AND normalized>? ORDER BY normalized LIMIT 1''',(dimension,after)).fetchone()
                        if not row:break
                        after = row['normalized']
                        valid = c.execute('''SELECT 1 FROM intelligence_dimensions d
                            JOIN intelligence_results r ON r.item=d.item JOIN items i ON i.id=d.item
                            JOIN roots ON roots.id=i.root WHERE d.key=? AND d.normalized=?
                            AND r.fingerprint=i.fingerprint AND i.online=1 AND roots.removed=0 LIMIT 1''',
                            (dimension,after)).fetchone()
                        if valid:rows.append({'key':dimension,'value':row['value'],'count':None})
            except sqlite3.OperationalError as exc:
                if 'interrupt' not in str(exc):raise
                partial = True
        return {'rows':rows,'partial':partial}

    def backup(self):
        folder = self.catalog.data/'backups';folder.mkdir(parents=True,exist_ok=True)
        target = folder/('asset-catalog-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.sqlite3')
        with self.catalog.connect() as source, closing(sqlite3.connect(target)) as destination:
            source.backup(destination, pages=256, sleep=.01)
            if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('备份完整性校验失败，请保留现有数据库')
        return {'ok':True,'path':str(target),'bytes':target.stat().st_size}

    def propose_group(self, root, folder):
        with self.lock, self.catalog.connect() as c:
            rows = [dict(r) for r in c.execute('''SELECT i.*,r.frames,r.fingerprint AS parsed_version FROM capture_items i
                LEFT JOIN intelligence_results r ON r.item=i.id WHERE i.root=? AND i.folder=? AND i.media_type='image'
                ORDER BY i.id LIMIT 2001''', (root, folder))]
        if not rows:return None
        config = self.sku.sku_rules() if self.sku else {'rules': DEFAULT_RULES, 'length_filter': {'min_exclusive': None, 'max_exclusive': None}}
        values = [];codes = [];unparsed = 0;ambiguous = 0
        for row in rows:
            if row['fingerprint'] != row['parsed_version'] or not row['frames']:unparsed += 1;continue
            result = json.loads(row['frames'])[0]['result'];attributes = result['attributes']
            identity = normalized(result['product_identity']);category = normalized(attributes['category']);color = normalized(attributes['color'])
            if result['subject_count'] != 1 or result['confidence'] < .88 or len(identity) < 8 or not category or not color:ambiguous += 1
            values.append((identity, category, color))
            readings = [{'text': result['visible_text'], 'codes': result['sku_candidates']}, {'text': Path(row['path']).parent.name}]
            codes.extend(code for code in match_rules(readings, config['rules'])['exact'] if length_allowed(code, config['length_filter']))
            if row['sku']:codes.append(row['sku'])
        codes = [code for code in codes if match_rules([{'text':code}],config['rules'])['exact'] and length_allowed(code,config['length_filter'])]
        unique = {normalize(code):code for code in codes if normalize(code)}
        sku = next(iter(unique.values())) if len(unique) == 1 else ''
        signature = digest([(r['id'], r['fingerprint'], r['sku'], r['source']) for r in rows])
        safe = len(rows) >= 2 and len(rows) <= 2000 and not unparsed and not ambiguous and len(set(values)) == 1 and bool(sku)
        reasons = []
        if unparsed:reasons.append(f'{unparsed} 张尚未完成当前版本解析')
        if ambiguous:reasons.append(f'{ambiguous} 张商品身份不明确或置信度不足')
        if len(set(values)) > 1:reasons.append('款式、品类或主体颜色存在差异')
        if len(unique) > 1:reasons.append('存在多个 SKU 证据')
        if not sku:reasons.append('缺少符合公司规则的唯一 SKU 证据')
        if len(rows) < 2:reasons.append('至少两张图片才能验证文件夹一致性')
        if len(rows) > 2000:reasons.append('目录超过自动归组上限，请分批复核')
        status = 'ready' if safe else 'review';reason = '完整覆盖且款式、品类、颜色与 SKU 证据一致' if safe else '；'.join(reasons)
        evidence = {'total': len(rows), 'parsed': len(values), 'identities': sorted(set(values)),
                    'sku_candidates': list(unique.values()), 'items': [r['id'] for r in rows]}
        group_id = digest([root, folder])[:32]
        with self.catalog.connect() as c:
            old = c.execute('SELECT status,signature FROM intelligence_groups WHERE id=?', (group_id,)).fetchone()
            if old and old['signature'] == signature and old['status'] in ('confirmed', 'rejected'):return dict(old)
            c.execute('INSERT OR REPLACE INTO intelligence_groups VALUES(?,?,?,?,?,?,?,?,?)',
                      (group_id, root, folder, signature, sku, status, reason, dumps(evidence), time.time()))
        if safe and self.config['auto_group']:self.confirm_group(group_id, signature, automatic=True)
        return {'id': group_id, 'signature': signature, 'status': status, 'reason': reason}

    def confirm_group(self, group_id, signature, *, automatic=False, reject=False, sku=''):
        with self.lock, self.catalog.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            group = c.execute('SELECT * FROM intelligence_groups WHERE id=?', (group_id,)).fetchone()
            if not group or group['signature'] != signature:raise ValueError('归组提案已更新，请重新查看')
            self.catalog._root(group['root'])
            rows = [dict(r) for r in c.execute("SELECT * FROM capture_items WHERE root=? AND folder=? AND media_type='image' ORDER BY id LIMIT 2001", (group['root'], group['folder']))]
            current = digest([(r['id'], r['fingerprint'], r['sku'], r['source']) for r in rows])
            if current != signature:raise ValueError('目录内容或 SKU 已变化，请重新解析归组')
            if reject:
                c.execute("UPDATE intelligence_groups SET status='rejected' WHERE id=?", (group_id,));return {'ok': True}
            if automatic and group['status'] != 'ready':raise ValueError('当前提案需要人工复核')
            value = sku.strip() if sku else group['sku']
            if not value or not normalize(value) or len(value) > 80:raise ValueError('请先填写正确 SKU')
            for row in rows:
                # 人工修正绝不被自动推断覆盖；图片配对的其他原片保持同一索引归属。
                members = c.execute('SELECT id,sku,source FROM items WHERE pair_key=?', (row['pair_key'],)).fetchall()
                for member in members:
                    if member['source'] == 'manual' and normalize(member['sku']) != normalize(value):
                        raise ValueError('归组与已有人工 SKU 冲突，请逐张核对')
                    if automatic and member['sku'] and normalize(member['sku']) != normalize(value):
                        raise ValueError('归组与已有 SKU 冲突')
                    if member['source'] == 'manual':continue
                    source = 'vlm_group' if automatic else 'manual'
                    c.execute('UPDATE items SET sku=?,normalized=?,source=?,confidence=? WHERE id=?',
                              (value, normalize(value), source, .9 if automatic else 1, member['id']))
                    c.execute('INSERT INTO corrections VALUES(?,?,?,?,?,?)',
                              (uuid.uuid4().hex, member['id'], member['sku'], value, source, time.time()))
            c.execute("UPDATE intelligence_groups SET status='confirmed',sku=?,updated=? WHERE id=?", (value, time.time(), group_id))
        return {'ok': True, 'sku': value, 'count': len(rows)}

    def close(self):
        self.closed.set();self.wake.set();self.clip.close()
        for thread in [*self.threads, self.discovery]:
            if thread.is_alive():thread.join(timeout=2)
