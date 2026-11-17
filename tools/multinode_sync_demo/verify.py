"""真实图片副本、真实 HTTP 和实际业务库的可复现验收。"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import secrets
import shutil
import sys
import time
from urllib.error import HTTPError, URLError
import uuid

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import (ROOT, Node, Relay, RelayServer, SimulatedCrash, canonical, connect,
                  signed_event, stable, sha, validate_event, read_capture_identity, strong_raw_match, visual_fingerprint, visual_match, normalize)

DEFAULT_SOURCE = Path(r'X:\细节图\源图片备份（勿动）\2026\2026-10-20')


def pixels(path):
    with Image.open(path) as image:
        return hashlib.sha256(image.convert('RGB').tobytes()).hexdigest()


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


class Verification:
    def __init__(self, run, source):
        self.run, self.source = Path(run).resolve(), Path(source)
        self.run.mkdir(parents=True)
        self.results, self.stages, self.samples = [], [], []
        self.raw_case_evidence = []
        self.workspace, self.project = uuid.uuid4().hex, uuid.uuid4().hex
        self.identities = {}
        for alias in ('A', 'B', 'C', 'O'):
            identity = dict(id=uuid.uuid4().hex, alias=alias, archive_writer=alias == 'O', token=secrets.token_urlsafe(32), secret=secrets.token_urlsafe(32))
            self.identities[identity['id']] = identity
        self.a_id, self.b_id, self.c_id, self.office_id = list(self.identities)
        self.plan = [{'sku': '演示-5433', 'shoot_type': '细节图'}, {'sku': '演示-5433', 'shoot_type': '模特图'},
                     {'sku': '演示-5391', 'shoot_type': '模特图'}]
        self.nodes = []
        self.server = None

    def check(self, title, fn, category='联动'):
        started = time.perf_counter()
        try:
            detail = fn()
            result = dict(title=title, category=category, status='passed', seconds=round(time.perf_counter() - started, 3), detail=detail or '断言通过')
        except Exception as exc:
            result = dict(title=title, category=category, status='failed', seconds=round(time.perf_counter() - started, 3), detail=f'{type(exc).__name__}: {exc}')
            self.results.append(result)
            save_json(self.run / 'verification-partial.json', self.results)
            print('FAIL ' + title + ': ' + result['detail'], flush=True)
            raise
        self.results.append(result)
        print('PASS ' + title, flush=True)
        return result

    def prepare(self):
        base = self.run / 'baseline'
        base.mkdir()
        names = ['XSY_5433.JPG', 'XSY_5212.JPG', 'XSY_5391.JPG', 'XSY_5250.JPG', 'XSY_5433.CR3', 'XSY_5212.CR3']
        for name in names:
            source = self.source / name
            if not source.is_file():
                raise FileNotFoundError('真实样片缺失：' + str(source))
            before = sha(source)
            target = base / name
            shutil.copy2(source, target)
            assert sha(target) == before == sha(source)
            self.samples.append(dict(name=name, source=str(source), baseline=str(target), bytes=source.stat().st_size,
                                     sha256=before, pixels=pixels(target) if source.suffix.lower() == '.jpg' else None))
        save_json(self.run / 'source-manifest.json', self.samples)
        save_json(self.run / '.local-credentials.json', dict(workspace=self.workspace, project=self.project, identities=self.identities))
        return '4 张真实 JPG＋2 张 CR3，逐文件 SHA-256 一致；原件只读'

    def node(self, name, device, *, root=None, plan=None):
        node = Node(self.run / name, self.run, self.identities[device], self.workspace, self.project,
                    self.identities, self.plan if plan is None else plan, archive_root=root)
        self.nodes.append(node)
        return node

    def make_data(self, index, device, *, sku=None, scope=None):
        name = ['XSY_5433.JPG', 'XSY_5212.JPG', 'XSY_5391.JPG', 'XSY_5250.JPG'][index]
        names = [name]  # 所有现场机器都只知道 JPG，禁止预先借用 RAW 哈希或文件身份。
        entity = stable(self.project, 'real-shot:' + str(index))
        components = []
        for filename in names:
            p = self.run / 'baseline' / filename
            components.append(dict(file_id=stable(entity, filename), sha256=sha(p), extension=p.suffix.lower(), filename=p.name,
                                   capture_identity=read_capture_identity(p), visual_fingerprint=visual_fingerprint(p)))
        # 拍摄日期直接来自真实 EXIF；业务款号/OCR 内容明确属于演示输入。
        metadata = self.a.writer.read(self.run / 'baseline' / name)
        taken = metadata['ExifIFD:DateTimeOriginal']
        captured = datetime.strptime(taken, '%Y:%m:%d %H:%M:%S').replace(tzinfo=timezone(__import__('datetime').timedelta(hours=8))).isoformat()
        return dict(entity=entity, origin=device, captured_at=captured, active=True, components=components,
                    capture_identity=read_capture_identity(self.run / 'baseline' / name),
                    visual_fingerprint=visual_fingerprint(self.run / 'baseline' / name),
                    metadata=dict(sku=sku or ('演示-5391' if index == 2 else '演示-5433'),
                                  shoot_type=scope or ('模特图' if index >= 2 else '细节图'), rating=2,
                                  ocr_text='演示 OCR：手动指定款号，不冒充真实识别结果', description='多机同步演示，真实图片副本'))

    def stage(self, name, note):
        self.stages.append(dict(name=name, note=note, nodes=[n.snapshot() for n in (self.a, self.b, self.c, self.office)]))

    def pair(self):
        for _ in range(2):
            for node in (self.a, self.b, self.c, self.office):
                node.sync(self.server.url)

    def same_progress(self):
        a = self.a.snapshot()
        projects_a = [{k: v for k, v in p.items() if k != 'workbook'} for p in a['projects']]
        for node in (self.b, self.c, self.office):
            b = node.snapshot()
            projects_b = [{k: v for k, v in p.items() if k != 'workbook'} for p in b['projects']]
            assert projects_a == projects_b, '多端任务表或进度不一致：' + b['machine']
            assert a['archive_assets'] == b['archive_assets'], '多端归档元数据不一致'
            assert a['entities'] == b['entities'], '多端完整元数据不一致'
        return a

    def current(self, entity):
        with connect(self.a.db) as c:
            return json.loads(c.execute('SELECT data FROM sync_entities WHERE id=?', (entity,)).fetchone()[0])

    def all_rows(self, node):
        return next(p['rows'] for p in node.snapshot()['projects'] if p['name'] == '全部拍摄')

    def setup_main(self):
        self.relay = Relay(self.run / 'relay.sqlite3', self.workspace, self.project, self.identities)
        self.server = RelayServer(self.relay)
        self.nas = self.run / 'simulated-nas' / 'archive'
        self.nas.mkdir(parents=True)
        self.a = self.node('A/data', self.a_id)
        self.b = self.node('B/data', self.b_id)
        self.c = self.node('C/data', self.c_id)
        self.office = self.node('O/data', self.office_id, root=self.nas)
        self.data = [self.make_data(i, self.a_id if i == 0 else self.c_id if i == 2 else self.b_id) for i in range(4)]
        self.targets = {}
        for index, data in enumerate(self.data):
            for component in data['components']:
                # 归档可改名、换目录；网络事件中不包含本机路径。
                target = self.nas / ('RAW' if component['extension'] == '.cr3' else 'JPEG') / f"归档改名-{index + 1}{component['extension']}"
                target.parent.mkdir(exist_ok=True)
                shutil.copy2(self.run / 'baseline' / component['filename'], target)
                self.office.register_file(target)
                self.targets[component['file_id']] = target
        self.raw_target = self.nas / 'RAW' / '归档改名-1.cr3'
        self.raw_target.parent.mkdir(exist_ok=True)
        shutil.copy2(self.run / 'baseline' / 'XSY_5433.CR3', self.raw_target)
        self.office.register_file(self.raw_target)
        self.stage('初始', 'A/B/C 是仅接收 JPG 的现场机器；办公室 O 连接最终归档。每台机器都有独立业务数据库。')

    def local_offline(self):
        self.first = self.a.emit(self.data[0])
        assert self.a.snapshot()['pending_uploads'] == 1
        assert self.a.search('5433')['rows'], '本地入库后应立即可检索'
        assert not self.b.snapshot()['entities']
        self.a.write_archive()
        assert all(w['state'] == 'waiting_root' for w in self.a.snapshot()['writes'])
        self.stage('A 离线拍摄', 'A 的本地进度立即完成，仅携带 JPG 与拍摄身份；其他机器尚未收到。')
        return 'A 本地完成 1 项，待上传 1 个事件；未配置 NAS 也可记进度'

    def a_upload_b_write(self):
        self.a.sync(self.server.url)
        self.a.close()
        # A 已停止；办公室 O 从 JPG 身份配对本地 RAW，A 从未读取或上传 RAW 哈希。
        assert all(p['extension'] == '.jpg' for p in self.first['data']['components'])
        assert sha(self.run / 'baseline' / 'XSY_5433.CR3') not in canonical(self.first)
        self.office.sync(self.server.url)
        assert self.office.search('5433')['rows'], 'RAW 匹配与文件写入前应可检索已收到元数据'
        proposed = self.office.reconcile_raws()
        assert len(proposed) == 1
        self.office.sync(self.server.url)
        self.office.write_archive()
        linked = self.office.snapshot()['entities'][0]['data']
        assert linked['entity'] == self.data[0]['entity']
        for comp in linked['components']:
            target = self.targets[comp['file_id']] if comp['extension'] == '.jpg' else self.raw_target
            self.targets[comp['file_id']] = target
            actual = self.office.writer.read_asset(target)
            assert actual['XMP-fk:SKU'] == '演示-5433'
            assert actual['XMP-xmp:Rating'] == 2
            assert actual['XMP-fk:ProjectId'].replace('-', '') == self.project
            if target.suffix.lower() == '.jpg':
                assert pixels(target) == self.samples[0]['pixels']
            else:
                assert sha(target) == comp['sha256'] and target.with_suffix('.xmp').is_file()
        self.a = self.node('A/data', self.a_id)
        self.pair()
        self.same_progress()
        self.stage('现场只有 JPG，办公室配对 RAW', 'A 已关闭；办公室仅凭同步来的 JPG 相机身份配对改名 RAW，并把 RAW 绑定资料回传所有机器。')
        return '现场事件无 RAW 名称/哈希；O 依据机身序列号＋真实时间及子秒匹配改名 CR3，写 XMP 侧车；JPG 像素、CR3 字节不变'

    def b_bidirectional(self):
        self.b.emit(self.data[1])
        self.b.emit(self.data[3])
        self.pair()
        self.office.write_archive()
        state = self.same_progress()
        rows = self.all_rows(self.a)
        detail = next(r for r in rows if r['sku'] == '演示-5433' and r['sheet'] == '细节图')
        model = next(r for r in rows if r['sku'] == '演示-5433' and r['sheet'] == '模特图')
        assert detail['machines'] == ['A', 'B'] and detail['shot_count'] == 2
        assert model['shot_count'] == 1 and model['machines'] == ['B']
        assert len([r for r in rows if r['status'] == '已拍摄']) == 2
        names = {p['name'] for p in state['projects']}
        assert {'2026-10-20-A.xlsx', '2026-10-20-B.xlsx', '全部拍摄'} <= names
        self.stage('B 拍摄并回传 A', '全部拍摄按款号和类型汇总，细节图显示来源 A、B；自动生成日期＋机器任务表。')
        return '两端相同：2 个完成款型 / 3 条拍摄证据；同款细节图计 1 项、保留 A/B 两次拍摄'

    def offline_restart_ack_order(self):
        self.c.emit(self.data[2])
        try:
            self.c.sync('http://127.0.0.1:1')
        except URLError:
            pass
        else:
            raise AssertionError('预期断线失败')
        assert self.c.snapshot()['pending_uploads'] == 1
        self.c.close()
        self.c = self.node('C/data', self.c_id)
        self.server.close()
        self.relay = Relay(self.run / 'relay.sqlite3', self.workspace, self.project, self.identities)
        self.server = RelayServer(self.relay)
        revised = copy.deepcopy(self.data[2])
        revised['metadata']['description'] = '断线期间已修正，禁止旧事件覆盖'
        self.c.emit(revised)
        try:
            self.c.sync(self.server.url, lose_ack=True)
        except ConnectionError:
            pass
        else:
            raise AssertionError('未模拟确认丢失')
        with connect(self.relay.path) as c:
            before = c.execute('SELECT count(*) FROM events').fetchone()[0]
        self.c.close()
        self.c = self.node('C/data', self.c_id)
        self.c.sync(self.server.url)
        self.b.sync(self.server.url, reverse=True)
        self.pair()
        with connect(self.relay.path) as c:
            assert c.execute('SELECT count(*) FROM events').fetchone()[0] == before
        assert self.c.snapshot()['pending_uploads'] == 0
        self.same_progress()
        entity = next(e for e in self.b.snapshot()['entities'] if e['id'] == revised['entity'])
        assert entity['data']['metadata']['description'] == revised['metadata']['description']
        with connect(self.b.db) as c:
            assert c.execute('SELECT count(*) FROM sync_events WHERE projected=0').fetchone()[0] == 0
        self.office.write_archive()
        self.stage('C 断线、重启和乱序补传', '第三台现场机器 C 与中继重启，确认丢失后重传；B 先收到子版本仍正确等待父版本，四机最终一致。')
        return '客户端/中继重启可恢复；重复上传不新增；反序交付后两个版本全部投影且新值保留'

    def concurrent_conflict(self):
        a = self.current(self.data[0]['entity'])
        b = copy.deepcopy(a)
        a['metadata']['description'], b['metadata']['description'] = 'A 现场修正', 'B 室内修正'
        self.a.emit(a)
        self.b.emit(b)
        self.pair()
        self.b.sync(self.server.url)
        states = self.same_progress()
        entity = next(e for e in states['entities'] if e['id'] == a['entity'])
        assert entity['state'] == 'conflict' and len(entity['heads']) == 2
        before = {str(p): sha(p) for p in self.targets.values()}
        self.office.write_archive()
        assert all(sha(Path(p)) == digest for p, digest in before.items())
        self.stage('并发编辑触发复核', '同一拍摄证据出现两个版本头，保留共同确认版本并停止自动写文件。')
        resolved = copy.deepcopy(a)
        resolved['metadata']['description'] = '已人工合并 A 现场信息与 B 室内信息'
        self.b.emit(resolved)
        self.pair()
        self.office.write_archive()
        state = self.same_progress()
        entity = next(e for e in state['entities'] if e['id'] == a['entity'])
        assert entity['state'] == 'ready' and len(entity['heads']) == 1
        actual = self.office.writer.read_asset(self.targets[a['components'][0]['file_id']])
        assert actual['XMP-dc:Description'] == resolved['metadata']['description']
        self.stage('人工合并后收敛', '解决事件引用两条冲突版本，四机数据库与办公室归档文件更新为同一结果。')
        return '并发修改显式冲突且未写文件；合并后四机收敛，办公室文件读回一致'

    def retract_only_one(self):
        value = self.current(self.data[0]['entity'])
        value['active'] = False
        value['metadata']['description'] = '撤销 A 这一条拍摄证据'
        self.a.emit(value)
        self.pair()
        self.office.write_archive()
        self.same_progress()
        detail = next(r for r in self.all_rows(self.a) if r['sku'] == '演示-5433' and r['sheet'] == '细节图')
        assert detail['status'] == '已拍摄' and detail['machines'] == ['B'] and detail['shot_count'] == 1
        self.stage('仅撤销 A 的拍摄', 'A 证据撤销后，B 的同款细节图仍有效，不会把整款回退成未拍摄。')
        return '汇总保留 B 的有效拍摄；只撤销目标证据，历史文件不清空'

    def crash_file_recovery(self):
        value = copy.deepcopy(self.data[2])
        value['metadata']['description'] = '文件已写入后模拟进程中断'
        self.b.emit(value)
        self.pair()
        try:
            self.office.write_archive(crash_after_commit=True)
        except SimulatedCrash:
            pass
        else:
            raise AssertionError('预期文件提交后的模拟中断')
        target = self.targets[value['components'][0]['file_id']]
        committed = sha(target)
        self.office.close()
        self.office = self.node('O/data', self.office_id, root=self.nas)
        self.office.sync(self.server.url)
        self.office.write_archive()
        assert sha(target) == committed
        current = self.office.heads(value['entity'])[0]
        with connect(self.office.db) as c:
            assert c.execute('SELECT state FROM sync_writes WHERE entity=? AND version=?', (value['entity'], current)).fetchone()[0] == 'applied'
        self.same_progress()
        self.stage('办公室写后中断恢复', 'O 重启核对写后摘要并补记完成，没有再次改写已成功文件。')
        return '文件成功、数据库未标记完成的窗口可恢复；重启前后文件 SHA-256 相同'

    def expect_http(self, status, fn):
        try:
            fn()
        except HTTPError as exc:
            assert exc.code == status, (exc.code, exc.read().decode())
        else:
            raise AssertionError('请求未被拒绝')

    def boundaries(self):
        self.check('错误设备凭据被拒绝', lambda: self.expect_http(403, lambda: self.a.request(self.server.url, token='invalid')), '授权')
        self.check('其他项目读取被拒绝', lambda: self.expect_http(403, lambda: self.a.request(self.server.url, query={'project': uuid.uuid4().hex})), '授权')
        self.check('设备冒名上行被拒绝', lambda: self.expect_http(403, lambda: self.a.request(self.server.url, events=[signed_event(self.identities[self.b_id], 99, self.workspace, self.project, self.data[1], [])])), '授权')
        self.check('跨项目上行被拒绝', lambda: self.expect_http(403, lambda: self.a.request(self.server.url, events=[signed_event(self.identities[self.a_id], 99, self.workspace, uuid.uuid4().hex, self.data[0], [])])), '授权')
        self.relay.revoked.add(self.b_id)
        self.check('吊销设备后无法继续拉取', lambda: self.expect_http(403, lambda: self.b.request(self.server.url)), '授权')
        self.relay.revoked.remove(self.b_id)

        def tamper():
            event = copy.deepcopy(self.first)
            event['data']['metadata']['sku'] = '被篡改'
            self.expect_http(400, lambda: self.a.request(self.server.url, events=[event]))
            conflict = signed_event(self.identities[self.a_id], 1, self.workspace, self.project, event['data'], [])
            self.expect_http(400, lambda: self.a.request(self.server.url, events=[conflict]))
            forged = copy.deepcopy(self.first)
            forged['signature'] = '0' * 64
            self.expect_http(403, lambda: self.a.request(self.server.url, events=[forged]))
        self.check('内容篡改、同编号换内容及伪造签名被拒绝', tamper, '协议')

        def sequence():
            value = copy.deepcopy(self.data[0])
            value['entity'] = uuid.uuid4().hex
            event = signed_event(self.identities[self.a_id], 999, self.workspace, self.project, value, [])
            self.expect_http(400, lambda: self.a.request(self.server.url, events=[event]))
        self.check('设备序号跳跃被拒绝且不污染中继', sequence, '协议')

        def concurrent_duplicate():
            with connect(self.relay.path) as c:
                before = c.execute('SELECT count(*) FROM events').fetchone()[0]
            with ThreadPoolExecutor(max_workers=6) as pool:
                receipts = list(pool.map(lambda _: self.a.request(self.server.url, events=[self.first]), range(12)))
            assert all(r['receipts'][0]['duplicate'] for r in receipts)
            with connect(self.relay.path) as c:
                assert c.execute('SELECT count(*) FROM events').fetchone()[0] == before
            return '6 个并发 HTTP 连接、12 次重复上行，中继事件总数不变'
        self.check('并发重复传输不重复记账', concurrent_duplicate, '协议')

        def rollback():
            with connect(self.a.db) as c:
                before = c.execute('SELECT count(*) FROM sync_events').fetchone()[0]
                cursor = c.execute("SELECT value FROM sync_settings WHERE key='cursor'").fetchone()[0]
            data = copy.deepcopy(self.data[0])
            data['entity'] = uuid.uuid4().hex
            good = signed_event(self.identities[self.a_id], 999, self.workspace, self.project, data, [])
            bad = signed_event(self.identities[self.a_id], 1000, self.workspace, uuid.uuid4().hex, data, [])
            try:
                self.a.receive({'cursor': cursor + 2, 'events': [{'event': good}, {'event': bad}]})
            except ValueError:
                pass
            else:
                raise AssertionError('预期整批回滚')
            with connect(self.a.db) as c:
                assert c.execute('SELECT count(*) FROM sync_events').fetchone()[0] == before
                assert c.execute("SELECT value FROM sync_settings WHERE key='cursor'").fetchone()[0] == cursor
        self.check('无效下行整批回滚，游标与事件不分离', rollback, '恢复')

        def schema():
            before = self.a.snapshot()['pending_uploads']
            for change in ('path', 'time', 'identity'):
                data = copy.deepcopy(self.data[2])
                if change == 'path':
                    data['components'][0]['filename'] = '../../outside.JPG'
                elif change == 'time':
                    data['captured_at'] = '2026-11-03T12:00:00'
                else:
                    data['origin'] = self.b_id
                try:
                    self.a.emit(data)
                except ValueError:
                    pass
                else:
                    raise AssertionError('危险/不兼容事件未被阻止')
            assert self.a.snapshot()['pending_uploads'] == before
            try:
                self.a.request('http://192.0.2.1:9999')
            except ValueError:
                pass
            else:
                raise AssertionError('非回环明文 HTTP 未拒绝')
        self.check('路径注入、无时区、身份变更及公网明文传输被拒绝', schema, '协议')

    def file_case(self, name, mode):
        root = self.run / 'faults' / name / 'nas'
        root.mkdir(parents=True)
        node = self.node('faults/' + name + '/data', self.office_id, root=root)
        data = copy.deepcopy(self.data[2])
        entity = data['entity']
        self.a.request(self.server.url)  # 仍走真实 HTTP 订阅，复用同一事件流。
        node.sync(self.server.url, upload=False)
        baseline = self.run / 'baseline' / data['components'][0]['filename']
        target = root / data['components'][0]['filename']
        if mode == 'wrong_name':
            shutil.copy2(self.run / 'baseline' / 'XSY_5212.JPG', target)
            # 构成完全不属于中继已知版本的另一张图，避免它合法匹配另一条证据。
            node.writer.write_rating(target, 5)
            node.register_file(target)
        elif mode in ('duplicate', 'external', 'outside', 'late'):
            if mode != 'late':
                shutil.copy2(baseline, target)
                node.register_file(target)
            if mode == 'duplicate':
                another = root / '另一份.JPG'
                shutil.copy2(baseline, another)
                node.register_file(another)
            if mode == 'external':
                node.writer.write_rating(target, 4)
            if mode == 'outside':
                try:
                    node.register_file(baseline)
                except ValueError:
                    return '拒绝登记演示归档根之外的路径'
                raise AssertionError('越界登记成功')
        before = {str(p): sha(p) for p in root.glob('*.JPG')}
        node.write_archive()
        version = node.heads(entity)[0]
        with connect(node.db) as c:
            state = c.execute('SELECT state,reason FROM sync_writes WHERE entity=? AND version=?', (entity, version)).fetchone()
        expected = 'waiting_file' if mode in ('wrong_name', 'late') else 'review'
        assert state['state'] == expected, dict(state)
        assert all(sha(Path(p)) == digest for p, digest in before.items())
        if mode == 'late':
            shutil.copy2(baseline, target)
            node.register_file(target)
            node.write_archive()
            actual = node.writer.read_asset(target)
            assert actual['XMP-fk:SKU'] == data['metadata']['sku']
            assert pixels(target) == pixels(baseline)
            return '先持久等待图片，图片到达后重新匹配、写入并读回成功'
        return state['reason'] + '；现有文件哈希不变'

    def wrong_embedded_id(self):
        root = self.run / 'faults' / 'wrong-id' / 'nas'
        root.mkdir(parents=True)
        node = self.node('faults/wrong-id/data', self.office_id, root=root)
        source = self.run / 'baseline' / 'XSY_5391.JPG'
        target = root / source.name
        shutil.copy2(source, target)
        fields = dict(AssetId=uuid.uuid4().hex, FileId=uuid.uuid4().hex, SKU='其他资产')
        payload, destination, journal = node.writer.write(target, fields, sha(target))
        node.writer.commit(payload, destination, journal)
        node.writer.commit_sidecars(journal)
        data = copy.deepcopy(self.data[2])
        data['entity'] = uuid.uuid4().hex
        data['components'][0]['file_id'] = uuid.uuid4().hex
        data['components'][0]['sha256'] = sha(target)
        data['origin'] = self.office_id
        node.emit(data)
        node.register_file(target)
        before = sha(target)
        node.write_archive()
        with connect(node.db) as c:
            task = c.execute('SELECT * FROM sync_writes').fetchone()
        assert task['state'] == 'review' and '身份不符' in task['reason']
        assert sha(target) == before
        return '即使内容指纹一致，嵌入 AssetId/FileId 不匹配仍拒绝写入'

    def raw_case(self, mode):
        base = self.run / 'raw-cases' / mode
        root = base / 'final-archive'
        root.mkdir(parents=True)
        source_node = self.node('raw-cases/' + mode + '/onsite-data', self.a_id, plan=[])
        office_node = self.node('raw-cases/' + mode + '/office-data', self.office_id, root=root, plan=[])
        relay = Relay(base / 'relay.sqlite3', self.workspace, self.project, self.identities)
        server = RelayServer(relay)
        try:
            data = copy.deepcopy(self.data[0])
            if mode == 'missing-camera-id':
                data['capture_identity']['serial'] = data['capture_identity']['unique'] = ''
            if mode == 'different-camera':
                data['capture_identity']['serial'] = 'another-camera'
            if mode == 'same-second-burst':
                data['capture_identity']['sub'] = '88'
            if mode == 'spoofed-exif-wrong-image':
                # 元数据巧合/伪造场景：JPG 时间身份声称一致，但视觉指纹来自另一张真实图。
                data['visual_fingerprint'] = visual_fingerprint(self.run / 'baseline' / 'XSY_5212.JPG')
            data['components'][0]['capture_identity'] = copy.deepcopy(data['capture_identity'])
            data['components'][0]['visual_fingerprint'] = copy.deepcopy(data['visual_fingerprint'])
            event = source_node.emit(data)
            source_node.sync(server.url)
            office_node.sync(server.url)
            assert all(p['extension'] == '.jpg' for p in event['data']['components'])
            if mode == 'late-renamed':
                assert not office_node.reconcile_raws()
                assert office_node.snapshot()['raw_pairs'][0]['state'] == 'waiting_raw'
                office_node.close()
                office_node = self.node('raw-cases/' + mode + '/office-data', self.office_id, root=root, plan=[])
            target = root / ('已改名的真实RAW.CR3' if mode == 'late-renamed' else 'XSY_5433.CR3')
            shutil.copy2(self.run / 'baseline' / ('XSY_5212.CR3' if mode == 'same-name-wrong-shot' else 'XSY_5433.CR3'), target)
            if mode == 'other-asset':
                payload, sidecar, journal = office_node.writer.write(target, dict(AssetId=uuid.uuid4().hex, FileId=uuid.uuid4().hex, SKU='其他已确认资产'), sha(target))
                office_node.writer.commit(payload, sidecar, journal)
                office_node.writer.commit_sidecars(journal)
            office_node.register_file(target)
            if mode == 'duplicate-raw':
                second = root / '重复副本.CR3'
                shutil.copy2(target, second)
                office_node.register_file(second)
            if mode == 'concurrent-edit-during-binding':
                original_emit = office_node.emit

                def concurrent_emit(updated, *, parents=None):
                    changed = copy.deepcopy(data)
                    changed['metadata']['description'] = 'RAW 配对期间现场已修正'
                    source_node.emit(changed)
                    source_node.sync(server.url)
                    office_node.sync(server.url)
                    return original_emit(updated, parents=parents)
                office_node.emit = concurrent_emit
            before = sha(target)
            previous_sidecar = sha(target.with_suffix('.xmp')) if target.with_suffix('.xmp').exists() else None
            proposals = office_node.reconcile_raws()
            pair = office_node.snapshot()['raw_pairs'][0]
            if mode == 'concurrent-edit-during-binding':
                assert len(proposals) == 1
                office_node.sync(server.url)
                source_node.sync(server.url)
                office_node.write_archive()
                entity = office_node.snapshot()['entities'][0]
                assert entity['state'] == 'conflict' and len(entity['heads']) == 2
                assert not target.with_suffix('.xmp').exists() and sha(target) == before
                office_node.reconcile_raws()
                pair = office_node.snapshot()['raw_pairs'][0]
                detail = '配对使用读取时父版本；期间现场修正产生明确冲突，不用旧快照覆盖新元数据，不写 RAW'
            elif mode == 'late-renamed':
                assert len(proposals) == 1 and pair['state'] == 'proposed'
                office_node.write_archive()
                assert not target.with_suffix('.xmp').exists(), '中继确认前不写 RAW'
                office_node.sync(server.url)
                office_node.write_archive()
                office_node.reconcile_raws()
                source_node.sync(server.url)
                actual = office_node.writer.read_asset(target)
                assert actual['XMP-fk:SKU'] == data['metadata']['sku'] and actual['XMP-xmp:Rating'] == 2
                assert sha(target) == before
                assert len(source_node.snapshot()['entities']) == 1
                assert len(source_node.snapshot()['entities'][0]['data']['components']) == 2
                assert self.all_rows(source_node)[0]['shot_count'] == 1
                pair = office_node.snapshot()['raw_pairs'][0]
                detail = '现场仅 JPG → 元数据先到且办公室重启 → 改名 RAW 后到 → 相机身份匹配 → 绑定回传 → XMP 写回；不增加拍摄次数'
            else:
                assert not proposals
                expected = 'review' if mode in ('duplicate-raw', 'missing-camera-id', 'other-asset', 'spoofed-exif-wrong-image') else 'waiting_raw'
                assert pair['state'] == expected, pair
                office_node.write_archive()
                assert sha(target) == before
                current_sidecar = sha(target.with_suffix('.xmp')) if target.with_suffix('.xmp').exists() else None
                assert current_sidecar == previous_sidecar
                detail = pair['reason'] + '；RAW 与既有侧车保持不变'
            self.raw_case_evidence.append(dict(mode=mode, detail=detail, pair=pair,
                jpg_identity=data['capture_identity'], raw_identity=read_capture_identity(target),
                visual=visual_match(visual_fingerprint(target), data['visual_fingerprint']),
                raw_sha_before=before, raw_sha_after=sha(target), raw_bytes_preserved=sha(target) == before,
                remote_jpg_only=all(p['extension'] == '.jpg' for p in event['data']['components'])))
            return detail
        finally:
            server.close()

    def raw_authority(self):
        existing = self.current(self.data[2]['entity'])
        raw = self.run / 'baseline' / 'XSY_5391.JPG'
        # 构造字段完整但无权执行的追加事件；本地和中继都应先拒绝角色。
        proposed = copy.deepcopy(existing)
        proposed['components'].append(dict(file_id=uuid.uuid4().hex, sha256='f' * 64, extension='.cr3', filename='RAW.CR3',
                                          capture_identity=copy.deepcopy(existing['capture_identity']), visual_fingerprint=copy.deepcopy(existing['visual_fingerprint'])))
        try:
            self.a.emit(proposed)
        except ValueError:
            pass
        else:
            raise AssertionError('现场角色越权追加 RAW')
        with connect(self.a.db) as c:
            seq = c.execute("SELECT value FROM sync_settings WHERE key='seq'").fetchone()[0] + 1
        event = signed_event(self.identities[self.a_id], seq, self.workspace, self.project, proposed, self.a.heads(proposed['entity']))
        self.expect_http(400, lambda: self.a.request(self.server.url, events=[event]))
        return '现场机器不能伪造 RAW 绑定；只有配置归档角色的节点可追加，字母名称不参与授权'

    def file_identity_collision(self):
        with connect(self.a.db) as c:
            seq = c.execute("SELECT value FROM sync_settings WHERE key='seq'").fetchone()[0] + 1
        forged = copy.deepcopy(self.data[0])
        forged['entity'] = uuid.uuid4().hex
        event = signed_event(self.identities[self.a_id], seq, self.workspace, self.project, forged, [])
        self.expect_http(400, lambda: self.a.request(self.server.url, events=[event]))
        return '同一 file_id 或已登记内容指纹不能被另一资产重新占用'

    def original_integrity(self):
        from openpyxl import load_workbook
        for sample in self.samples:
            assert sha(Path(sample['source'])) == sample['sha256']
            assert sha(Path(sample['baseline'])) == sample['sha256']
        for data in self.data:
            for comp in data['components']:
                target = self.targets[comp['file_id']]
                if target.suffix.lower() == '.cr3':
                    assert sha(target) == comp['sha256']
                else:
                    assert pixels(target) == pixels(self.run / 'baseline' / comp['filename'])
        for node in (self.a, self.b, self.c, self.office):
            node.export_workbooks()
            for project in node.snapshot()['projects']:
                book = load_workbook(self.run / project['workbook'], read_only=True)
                assert list(book.active.values)[0][:4] == ('款号', '拍摄类型', '拍摄状态', '机器来源')
                assert book.active.max_row == len(project['rows']) + 1
                book.close()
            with connect(node.db) as c:
                assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            with node.archive.connect() as c:
                assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                assert not c.execute('PRAGMA foreign_key_check').fetchall()
        assert sha(self.raw_target) == next(s['sha256'] for s in self.samples if s['name'].endswith('.CR3'))
        return '6 个来源原件和基准哈希保持；4 张归档 JPG 像素保持、CR3 字节保持；四机全部工作簿可读，业务库完整性通过'

    def search_performance(self):
        from performance import benchmark
        expected = {e['id'] for e in self.a.snapshot()['entities'] if '5433' in normalize(e['data']['metadata']['sku'])}
        for node in (self.a, self.b, self.c, self.office):
            assert {r['entity'] for r in node.search('5433')['rows']} == expected
        self.performance = benchmark(self.run / 'search-performance', self.project, self.office)
        return f"10 万条规模；不同词 HTTP P95 {self.performance['http']['p95_ms']}ms，并发增量 P95 {self.performance['concurrent']['p95_ms']}ms；检索不等待归档写锁"

    def run_all(self):
        try:
            self.check('复制真实案例并验证原件与副本哈希', self.prepare, '原片保护')
            self.setup_main()
            self.check('A 无 NAS 且离线时本地记账和持久排队', self.local_offline)
            self.check('现场只有 JPG，关闭后办公室独立配对 RAW 并写入', self.a_upload_b_write)
            self.check('B 回传 A，全部拍摄区分机器并生成日期任务表', self.b_bidirectional)
            self.check('断线重启、确认丢失重传和子版本先到', self.offline_restart_ack_order, '恢复')
            self.check('并发编辑停止写入，人工合并后两端收敛', self.concurrent_conflict, '冲突')
            self.check('撤销 A 不清除 B 的同款有效拍摄', self.retract_only_one, '冲突')
            self.check('文件提交后进程中断，重启幂等补记完成', self.crash_file_recovery, '恢复')
            self.boundaries()
            for name, mode, title in [('same-name', 'wrong_name', '同名不同内容不误写'), ('duplicates', 'duplicate', '多个可靠候选停止自动写入'),
                                      ('external', 'external', '索引后外部修改不被覆盖'), ('outside', 'outside', '目标路径必须属于授权归档根'),
                                      ('late', 'late', '原图缺失持久等待，到达后自动匹配写入')]:
                self.check(title, lambda n=name, m=mode: self.file_case(n, m), '文件边界')
            self.check('指纹一致但嵌入身份错误仍拒绝写入', self.wrong_embedded_id, '文件边界')
            for mode, title in [('late-renamed', '仅 JPG 上行，改名 RAW 后到且办公室重启仍可匹配'),
                                ('same-name-wrong-shot', '同名但不同拍摄时刻的真实 RAW 不误配'),
                                ('duplicate-raw', '多个 RAW 候选满足身份条件时转复核'),
                                ('missing-camera-id', '缺机身身份时不凭同秒同名匹配 RAW'),
                                ('different-camera', '同型号另一机身的 RAW 不误配'),
                                ('same-second-burst', '同秒连拍不同子秒的 RAW 不误配'),
                                ('spoofed-exif-wrong-image', '即使 EXIF 身份声称一致，画面不符仍拒绝 RAW 绑定'),
                                ('concurrent-edit-during-binding', 'RAW 配对期间并发修正不会被旧快照覆盖'),
                                ('other-asset', 'RAW 已绑定其他资产时不改写侧车')]:
                self.check(title, lambda m=mode: self.raw_case(m), 'JPG→RAW')
            self.check('只有归档角色能追加 RAW 绑定', self.raw_authority, '授权')
            self.check('文件身份与内容指纹不能跨资产重复占用', self.file_identity_collision, '协议')
            self.check('同步后即时检索、10 万条索引与并发增量性能', self.search_performance, '检索性能')
            self.check('原件、像素、RAW、Excel 与数据库最终完整性', self.original_integrity, '原片保护')
            final = self.same_progress()
            with connect(self.relay.path) as c:
                count = c.execute('SELECT count(*) FROM events').fetchone()[0]
            file_checks = []
            for entity in self.office.snapshot()['entities']:
                data = entity['data']
                for component in data['components']:
                    target = self.targets[component['file_id']]
                    actual = self.office.writer.read_asset(target)
                    file_checks.append(dict(entity=data['entity'], filename=component['filename'], target=str(target.relative_to(self.run)),
                        origin=self.identities[data['origin']]['alias'], extension=component['extension'], original_sha=component['sha256'],
                        final_sha=sha(target), payload_unchanged=(sha(target) == component['sha256']) if target.suffix.lower() == '.cr3'
                            else pixels(target) == pixels(self.run / 'baseline' / component['filename']),
                        metadata={k: v for k, v in actual.items() if k.startswith(('XMP-fk:', 'XMP-xmp:', 'XMP-dc:'))}))
            evidence = dict(generated_at=datetime.now(timezone.utc).isoformat(), workspace=self.workspace, project=self.project,
                            results=self.results, stages=self.stages, samples=self.samples, final_A=final, final_B=self.b.snapshot(),
                            final_nodes=[n.snapshot() for n in (self.a, self.b, self.c, self.office)],
                            file_checks=file_checks, raw_cases=self.raw_case_evidence, performance=self.performance,
                            relay_events=count, transport='真实回环 HTTP；未部署公网 HTTPS，未使用两台实体机',
                            nas='本机独立目录模拟 NAS；未对生产 NAS 执行写入')
            save_json(self.run / 'evidence.json', evidence)
            return evidence
        finally:
            for node in self.nodes:
                node.close()
            if self.server:
                self.server.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--output', type=Path, default=ROOT / 'build' / 'multinode-sync-demo')
    args = parser.parse_args()
    # 使用递增运行目录；失败也保留证据，不递归删除旧数据。
    base = args.output.resolve()
    if not base.is_relative_to((ROOT / 'build').resolve()):
        parser.error('演示输出必须位于项目 build 目录')
    base.mkdir(parents=True, exist_ok=True)
    run = base / ('run-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    evidence = Verification(run, args.source).run_all()
    from report import render
    report = render(run, evidence)
    save_json(base / 'latest.json', dict(run=str(run), report=str(report)))
    print(canonical(dict(report=str(report), passed=len(evidence['results']), relay_events=evidence['relay_events'])), flush=True)


if __name__ == '__main__':
    main()
