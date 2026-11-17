"""独立索引的不同关键词、真实 HTTP 与并发增量性能验证。"""
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import threading
import time
from urllib.parse import urlencode
from urllib.request import build_opener, ProxyHandler

from core import connect, normalize
from search_index import SearchIndex, SearchServer


def summary(values):
    ordered = sorted(values)
    return dict(samples=len(values), p50_ms=round(ordered[len(ordered)//2], 3),
                p95_ms=round(ordered[max(0, math.ceil(len(ordered)*.95)-1)], 3), max_ms=round(max(ordered), 3))


def benchmark(directory, project, node):
    directory = Path(directory)
    directory.mkdir(parents=True)
    index = SearchIndex(directory / 'search-benchmark.sqlite3')
    started = time.perf_counter()
    for first in range(0, 100000, 1000):
        rows = []
        for number in range(first, first + 1000):
            sku = f'LC{number:07d}P{number%20:02d}'
            rows.append(dict(entity=f'benchmark-{number}', project=project, normalized=normalize(sku), sku=sku,
                             scope='细节图' if number % 2 else '模特图', origin='ABC'[number % 3], active=1,
                             conflict=0, version='synthetic:1', ocr_text=f'COTTON SAMPLE {number}', description='模拟规模数据，不代表真实拍摄'))
        index.publish(rows)
    seed_ms = (time.perf_counter() - started) * 1000
    with connect(node.search_index.path) as c:
        actual = [dict(r) for r in c.execute('SELECT entity,project,normalized,sku,scope,origin,active,conflict,version,ocr_text,description FROM docs')]
    index.publish(actual)
    server = SearchServer(index, project)
    opener = build_opener(ProxyHandler({}))

    def request(query, mode='sku'):
        start = time.perf_counter()
        with opener.open(server.url + '/search?' + urlencode({'q': query, 'mode': mode}), timeout=3) as response:
            result = json.load(response)
        assert 'error' not in result
        return (time.perf_counter() - start) * 1000, result

    try:
        cases = []
        for i in range(30):
            number = i * 977
            cases.extend([(f'LC{number:07d}P{number%20:02d}', 'exact'), (f'LC{i:04d}', 'prefix'), (f'{1000+i*13}', 'sku')])
        cases += [('LC', 'sku'), ('7877', 'sku'), ('ZZNOMATCH999999', 'sku'), ('COTTON', 'text')]
        direct, http, examples = [], [], []
        for query, mode in cases:
            start = time.perf_counter()
            result = index.search(project, query, mode=mode)
            direct.append((time.perf_counter() - start) * 1000)
            duration, result = request(query, mode)
            http.append(duration)
            if len(examples) < 10 or query in ('7877', 'ZZNOMATCH999999', 'COTTON'):
                examples.append(dict(query=query, mode=mode, milliseconds=round(duration, 3), returned=len(result['rows'])))
        hot = [request('7877')[0] for _ in range(30)]
        publishing = threading.Event()
        writer_times = []

        def publish():
            for batch in range(40):
                if batch == 0:
                    writer_times.append(time.perf_counter())
                    publishing.set()
                index.publish([dict(entity=f'delta-{batch}-{i}', project=project, normalized=f'NEWSYNC{batch:04d}{i:03d}',
                    sku=f'NEWSYNC{batch:04d}{i:03d}', scope='细节图', origin='C', active=1, conflict=0, version='delta:1',
                    ocr_text='同步增量', description='增量更新') for i in range(25)])
                time.sleep(.004)
            writer_times.append(time.perf_counter())

        thread = threading.Thread(target=publish)
        thread.start()
        assert publishing.wait(3)
        concurrent, query_times = [], []
        for i in range(100):
            query_times.append(time.perf_counter())
            concurrent.append(request(str(1000 + i * 7))[0])
        thread.join(timeout=10)
        assert not thread.is_alive()
        overlap = sum(writer_times[0] <= stamp <= writer_times[-1] for stamp in query_times)
        assert overlap > 0

        fresh = dict(entity='fresh-visible', project=project, normalized='ZSYNC0001P01', sku='ZSYNC0001P01', scope='细节图',
                     origin='A', active=1, conflict=0, version='fresh:1', ocr_text='新入库立即可查', description='验收')
        start = time.perf_counter()
        index.publish([fresh])
        _, response = request('ZSYNC0001P01', 'exact')
        visible_ms = (time.perf_counter() - start) * 1000
        assert response['rows'][0]['entity'] == 'fresh-visible'

        # 查询路径不获取 Node.lock 或元数据写入器的锁。
        with node.lock, node.writer.lock, ThreadPoolExecutor(max_workers=1) as pool:
            start = time.perf_counter()
            response = pool.submit(node.search, '5433').result(timeout=2)
            blocked_writer_ms = (time.perf_counter() - start) * 1000
            assert response['rows']
        with connect(index.path) as busy:
            busy.execute('BEGIN IMMEDIATE')
            sqlite_lock_ms, response = request('7877')
            assert response['rows'] and sqlite_lock_ms < 150
        with connect(index.path) as c:
            plans = dict(prefix=[r['detail'] for r in c.execute('EXPLAIN QUERY PLAN SELECT entity FROM docs WHERE project=? AND normalized>=? AND normalized<? ORDER BY normalized,entity LIMIT 61', (project, 'LC', 'LD'))],
                         contains=[r['detail'] for r in c.execute('EXPLAIN QUERY PLAN SELECT d.entity FROM terms t JOIN docs d ON d.rowid=t.rowid WHERE terms MATCH ? AND d.project=? AND t.rowid>0 ORDER BY t.rowid LIMIT 61', ('normalized : "7877"', project))])
            assert any('docs_project_sku' in p for p in plans['prefix'])
            assert any('VIRTUAL TABLE INDEX' in p for p in plans['contains'])
            assert not any('TEMP B-TREE' in p for p in plans['prefix'] + plans['contains'])
            count = c.execute('SELECT count(*) FROM docs').fetchone()[0]
        direct_result, http_result, concurrent_result = summary(direct), summary(http), summary(concurrent)
        assert http_result['p95_ms'] < 150 and concurrent_result['p95_ms'] < 150
        assert direct_result['p95_ms'] < 50
        return dict(dataset='100000 条模拟资料＋本轮真实同步资产，独立持久 SQLite/FTS5 索引', initial_rows=100000 + len(actual),
                    final_rows=count, seed_ms=round(seed_ms, 3), direct=direct_result, http=http_result, same_query=summary(hot),
                    concurrent=concurrent_result, overlapping_requests=overlap, fresh_commit_to_http_ms=round(visible_ms, 3),
                    writer_lock_search_ms=round(blocked_writer_ms, 3), query_plans=plans, examples=examples,
                    sqlite_write_lock_http_ms=round(sqlite_lock_ms, 3),
                    note='不同关键词首次查询与同词复查分别记录；未清空系统文件缓存，不代表物理冷盘、生产全库或原生 UI 耗时。')
    finally:
        server.close()
