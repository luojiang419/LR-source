"""搜索候选必须保留原代表图、成员元数据和分页语义；不读取原片。"""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from asset_catalog import AssetCatalog
from retrieval_store import install


class CatalogSearchCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.catalog=AssetCatalog(Path(self.temp.name))
        self.catalog.intelligence=object()
        with self.catalog.connect() as c:
            install(c)
            c.executemany('INSERT INTO roots(id,path,created) VALUES(?,?,0)',[('one','Z:/unavailable/one'),('two','Z:/unavailable/two')])
            self.add(c,'jpg','one','plain.jpg','pair','MAIN123')
            self.add(c,'raw','one','plain.CR3','pair','RAWONLY777')
            self.add(c,'manual-raw','one','manual.CR3','manual','RAWREP99',source='manual')
            self.add(c,'manual-jpg','one','manual.jpg','manual','JPGONLY55')
            self.add(c,'unicode','two','春夏新品/100%_"quote".jpg','unicode','')
            self.add(c,'stale','one','xy.jpg','stale','STALE999')
            self.add(c,'video','two','clip.mp4','video','VIDEO99',media='video')
            self.add(c,'offline','one','offline.jpg','offline','OFFLINE99',online=0)
            c.execute("UPDATE catalog_metadata SET search_text='标签甲乙 current metadata' WHERE item='raw'")
            c.execute("UPDATE catalog_metadata SET search_text='stale metadata',fingerprint='old' WHERE item='stale'")
            c.execute("UPDATE catalog_metadata SET search_text='offline metadata' WHERE item='offline'")
            for item,terms,version in [('jpg','blue linen','jpg'),('raw','hiddenvisual','raw'),('stale','stalevisual','old'),('video','blue motion','video')]:
                c.execute('INSERT INTO intelligence_results VALUES(?,?,?,?,?,?,0)',(item,version,'test','test','[]',terms))
                c.execute('INSERT INTO intelligence_fts(item,terms) VALUES(?,?)',(item,terms))

    def tearDown(self):self.catalog.close();self.temp.cleanup()

    @staticmethod
    def add(c,item,root,relative,pair,sku,source='unmatched',media='image',online=1):
        c.execute('''INSERT INTO items(id,root,path,relative,folder,name,extension,size,modified,fingerprint,
            pair_key,last_seen,sku,normalized,source,media_type,online) VALUES(?,?,?,?,?,?,?,1,10,?,?,1,?,?,?,?,?)''',
            (item,root,'Z:/unavailable/'+root+'/'+relative,relative,relative.rpartition('/')[0] or '/',
             relative.rpartition('/')[2],Path(relative).suffix.lower(),item,pair,sku,sku,source,media,online))

    def legacy(self,text,extra='1',values=()):
        with self.catalog.connect() as c:
            condition,args=self.catalog.search_condition(c,text)
            condition=condition.replace('CROSS JOIN items i ON','JOIN items i ON')
            return [dict(row) for row in c.execute('SELECT * FROM capture_items WHERE online=1 AND '+extra+' AND '+condition+' ORDER BY modified DESC,id',list(values)+args)]

    def test_all_search_branches_preserve_exact_representative_semantics(self):
        for visual in (True,False):
            self.catalog.intelligence=object() if visual else None
            for text in ('MAIN123','RAWONLY777','JPGONLY55','RAWREP99','.CR3','标签甲乙','stale metadata',
                         'hiddenvisual','blue','stalevisual','春夏','100%','_"quote"','%','xy','not found'):
                with self.subTest(visual=visual,text=text):
                    expected=self.legacy(text);result=self.catalog.items(text=text)
                    self.assertEqual(result['total'],len(expected))
                    self.assertEqual([{k:v for k,v in row.items() if k!='preview'} for row in result['rows']],expected)
        self.catalog.intelligence=object()
        self.assertEqual(self.catalog.items(text='RAWONLY777')['rows'],[])
        self.assertEqual(self.catalog.items(text='hiddenvisual')['rows'],[])
        self.assertEqual([r['id'] for r in self.catalog.items(text='标签甲乙')['rows']],['jpg'])
        self.assertEqual({r['id'] for r in self.catalog.items(text='.CR3')['rows']},{'jpg','manual-raw'})

    def test_short_terms_and_unavailable_fts_keep_literal_fallback(self):
        self.catalog.fast_index._ready=False;self.catalog.metadata.fts=False
        for text in ('xy','春夏','%','标签','a\0b','RAWONLY777'):
            with self.subTest(text=text):
                self.assertEqual([row['id'] for row in self.catalog.items(text=text)['rows']],
                                 [row['id'] for row in self.legacy(text)])

    def test_filters_cursor_tree_and_public_row_shape_are_preserved(self):
        for text in ('blue','.CR3','MAIN123','春夏'):
            for root,media in (('one','image'),('two','video'),('','')):
                extra=[];values=[]
                if root:extra.append('root=?');values.append(root)
                if media:extra.append('media_type=?');values.append(media)
                expected=self.legacy(text,' AND '.join(extra) or '1',values)
                result=self.catalog.items(text=text,root=root,media=media,limit=1)
                self.assertEqual(result['total'],len(expected))
                actual=[r['id'] for r in result['rows']]
                while result['has_more']:
                    result=self.catalog.items(text=text,root=root,media=media,limit=1,cursor=result['next_cursor'])
                    actual.extend(r['id'] for r in result['rows'])
                self.assertEqual(actual,[r['id'] for r in expected])
                counts={}
                for row in self.legacy(text,'media_type=?' if media else '1',[media] if media else []):
                    key=(row['root'],row['folder']);counts[key]=counts.get(key,0)+1
                tree=self.catalog.tree(text=text,media=media)
                self.assertEqual({(root['id'],folder['folder']):folder['count'] for root in tree['roots'] for folder in root['folders']},counts)
        result=self.catalog.items(text='春夏',folder='春夏新品')
        self.assertEqual([r['id'] for r in result['rows']],['unicode'])
        self.assertNotIn('search_pair',result['rows'][0])
        self.assertEqual(self.catalog.items(text='.CR3',sku='MAIN123')['total'],1)

    def test_plan_drives_fts_and_pair_candidates_before_online_sorted_catalog(self):
        with self.catalog.connect() as c:
            source,source_args,condition,args=self.catalog._search_query(c,'plain')
            plan='\n'.join(row[3] for row in c.execute('EXPLAIN QUERY PLAN SELECT capture_items.* FROM '+source+
                ' WHERE online=1 AND '+condition+' ORDER BY modified DESC,id LIMIT 60',source_args+args))
        self.assertIn('VIRTUAL TABLE INDEX',plan)
        self.assertIn('SEARCH matched USING INDEX catalog_pair (pair_key=',plan)
        self.assertIn('SEARCH a USING INDEX sqlite_autoindex_items_1 (id=',plan)
        self.assertNotIn('SEARCH a USING INDEX intelligence_catalog_order',plan)

    def test_broad_candidates_stay_indexed_without_truncating_pairs(self):
        with self.catalog.connect() as c:
            for number in range(600):
                for extension in ('.jpg','.CR3'):
                    self.add(c,f'broad-{number}{extension}','one',f'broad/{number}{extension}',f'broad-{number}','')
        with self.catalog.connect() as c:
            statements=[];c.set_trace_callback(statements.append)
            source,source_args,_,_=self.catalog._search_query(c,'broad/')
            self.assertIn('search_item_id',source)
            self.assertIn('reference_path_fts MATCH ?',source)
            self.assertFalse(any('LIMIT 513' in sql for sql in statements))
            self.assertFalse(any('SELECT DISTINCT pair_key' in sql for sql in statements))
        expected=self.legacy('broad/');result=self.catalog.items(text='broad/',limit=60)
        self.assertEqual(result['total'],600)
        self.assertEqual([r['id'] for r in result['rows']],[r['id'] for r in expected[:60]])

    def test_fts_overflow_is_only_a_plan_hint_for_stale_or_offline_matches(self):
        with self.catalog.connect() as c:
            for number in range(514):
                item=f'offline-gate-{number}'
                self.add(c,item,'one',f'offlinegate/{number}.jpg',item,'',online=0)
                c.execute("UPDATE catalog_metadata SET search_text='metadatagate' WHERE item=?",(item,))
                c.execute('INSERT INTO intelligence_fts(item,terms) VALUES(?,?)',(item,'visualgate'))
            self.add(c,'online-gate','one','offlinegate/live.jpg','online-gate','')
        for text,table in [('offlinegate','reference_path_fts'),('metadatagate','catalog_metadata_fts'),('visualgate','intelligence_fts')]:
            with self.subTest(table=table),self.catalog.connect() as c:
                statements=[];c.set_trace_callback(statements.append)
                source,_,_,_=self.catalog._search_query(c,text)
                self.assertIn('search_item_id',source)
                self.assertIn(table+' MATCH ?',source)
                self.assertFalse(any('SELECT DISTINCT pair_key' in sql for sql in statements))
            self.assertEqual([r['id'] for r in self.catalog.items(text=text)['rows']],[r['id'] for r in self.legacy(text)])

    def test_unavailable_fts_does_not_probe_unusable_indexes(self):
        self.catalog.fast_index._ready=False;self.catalog.metadata.fts=False;self.catalog.intelligence=None
        with self.catalog.connect() as c:
            statements=[];c.set_trace_callback(statements.append)
            self.catalog._search_query(c,'plain')
        self.assertFalse(any('SELECT 1 FROM '+table in sql for table in ('reference_path_fts','catalog_metadata_fts','intelligence_fts') for sql in statements))

    def test_short_search_empty_and_past_end_pages_keep_total_and_row_shape(self):
        for text in ('标签','xy','不存在的短词'):
            expected=self.legacy(text)
            for offset in (0,1,100000):
                with self.subTest(text=text,offset=offset):
                    result=self.catalog.items(text=text,limit=1,offset=offset)
                    self.assertEqual(result['total'],len(expected))
                    self.assertEqual([{k:v for k,v in row.items() if k!='preview'} for row in result['rows']],expected[offset:offset+1])
                    self.assertEqual(result['has_more'],len(expected)>offset+1)
                    if not result['rows']:self.assertEqual(result['next_cursor'],'')

    def test_broad_search_full_pagination_filters_and_readonly_connection(self):
        with self.catalog.connect() as c:
            for number in range(520):
                self.add(c,f'wide-{number}','one',f'wide/{number}.jpg',f'wide-{number}','WIDE',media='image')
            # 一部分来自同款 RAW 成员；重复时间依赖 id 稳定排序，手动代表不丢失。
            for number in range(0,520,2):
                self.add(c,f'wide-raw-{number}','one',f'wide/{number}.CR3',f'wide-{number}','WIDE',source='manual')
        from contextlib import contextmanager
        from unittest.mock import patch
        original=self.catalog.connect
        @contextmanager
        def readonly():
            with original() as c:
                c.execute('PRAGMA query_only=ON')
                yield c
        with patch.object(self.catalog,'connect',readonly):
            expected=self.legacy('wide/',"root=? AND media_type=? AND folder=?",['one','image','wide'])
            result=self.catalog.items(text='wide/',root='one',folder='wide',media='image',sku='WIDE',limit=240)
            collected=[]
            while True:
                self.assertEqual(result['total'],len(expected)-len(collected))
                collected.extend({k:v for k,v in row.items() if k!='preview'} for row in result['rows'])
                if not result['has_more']:break
                result=self.catalog.items(text='wide/',root='one',folder='wide',media='image',sku='WIDE',limit=240,cursor=result['next_cursor'])
            self.assertEqual(collected,expected)
            result=self.catalog.items(text='wide/',offset=1000)
            self.assertEqual(result['total'],520);self.assertEqual(result['rows'],[])

    def test_short_search_keeps_snapshot_across_concurrent_metadata_change(self):
        with self.catalog.connect() as c:
            source,source_args,condition,args=self.catalog._search_query(c,'标签')
            self.assertTrue(c.in_transaction)
            # 固定读取视图后另一个连接更新，旧读取不能混入新资料。
            c.execute('SELECT count(*) FROM items').fetchone()
            with self.catalog.connect() as writer:
                writer.execute("UPDATE catalog_metadata SET search_text='标签' WHERE item='video'")
            rows=c.execute('SELECT id FROM '+source+' WHERE online=1 AND '+condition,source_args+args).fetchall()
            self.assertEqual([r[0] for r in rows],['jpg'])
        self.assertEqual({r['id'] for r in self.catalog.items(text='标签')['rows']},{'jpg','video'})

    def test_candidate_and_filter_share_snapshot_when_other_writer_changes_match(self):
        with self.catalog.connect() as c:
            source,source_args,condition,args=self.catalog._search_query(c,'MAIN123')
            self.assertTrue(c.in_transaction)
            writer=sqlite3.connect(self.catalog.db)
            try:
                with writer:writer.execute("UPDATE items SET normalized='MAIN123' WHERE id='stale'")
            finally:writer.close()
            rows=c.execute('SELECT id FROM '+source+' WHERE online=1 AND '+condition,source_args+args).fetchall()
            self.assertEqual([r[0] for r in rows],['jpg'])
        self.assertEqual({r['id'] for r in self.catalog.items(text='MAIN123')['rows']},{'jpg','stale'})

    def test_combined_tree_keeps_global_matches_with_scoped_and_empty_pages(self):
        for text in ('','blue','.CR3','标签','春夏','%','no match'):
            for root,folder,media in (('','',''),('one','','image'),('two','春夏新品',''),('two','','video')):
                for offset in (0,100):
                    with self.subTest(text=text,root=root,offset=offset):
                        options=dict(text=text,root=root,folder=folder,media=media,offset=offset,limit=1)
                        expected=self.catalog.items(**options)
                        combined=self.catalog.items(**options,include_tree=True)
                        self.assertEqual(combined.pop('tree'),self.catalog.tree(text=text,media=media))
                        self.assertEqual(combined,expected)
        for text in ('.CR3','blue'):
            cursor=''
            while True:
                options=dict(text=text,limit=1,cursor=cursor,sku='')
                expected=self.catalog.items(**options)
                combined=self.catalog.items(**options,include_tree=True)
                self.assertEqual(combined.pop('tree'),self.catalog.tree(text=text))
                self.assertEqual(combined,expected)
                if not combined['has_more']:break
                cursor=combined['next_cursor']

    def test_combined_broad_pages_and_tree_remain_readonly_and_complete(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        with self.catalog.connect() as c:
            for number in range(530):
                self.add(c,f'shared-{number}','one' if number%2 else 'two',f'shared/{number}.jpg',f'shared-{number}','SHARED')
        original=self.catalog.connect
        @contextmanager
        def readonly():
            with original() as c:
                c.execute('PRAGMA query_only=ON');yield c
        with patch.object(self.catalog,'connect',readonly):
            expected_tree=self.catalog.tree(text='shared')
            result=self.catalog.items(text='shared',root='one',limit=240,include_tree=True,sku='SHARED')
            self.assertEqual(result.pop('tree'),expected_tree)
            self.assertEqual(result,self.catalog.items(text='shared',root='one',limit=240,sku='SHARED'))
            tail=self.catalog.items(text='shared',root='one',limit=240,cursor=result['next_cursor'],include_tree=True,sku='SHARED')
            self.assertEqual(tail['total'],25);self.assertEqual(len(tail['rows']),25)
            self.assertEqual(tail['tree'],expected_tree)

    def test_combined_rows_and_roots_share_snapshot_during_concurrent_write(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        expected=self.catalog.items(text='标签',include_tree=True)
        original=self.catalog.connect;changed=[]
        @contextmanager
        def traced():
            with original() as c:
                def trace(sql):
                    if sql.startswith('SELECT capture_items.* FROM (VALUES ') and not changed:
                        changed.append(True)
                        with original() as writer:
                            writer.execute("UPDATE items SET sku='NEW',normalized='NEW' WHERE id='jpg'")
                            writer.execute("UPDATE roots SET path='Z:/changed' WHERE id='one'")
                c.set_trace_callback(trace);yield c
        with patch.object(self.catalog,'connect',traced):
            self.assertEqual(self.catalog.items(text='标签',include_tree=True),expected)
        self.assertEqual(changed,[True])
        self.assertNotEqual(self.catalog.items(text='标签',include_tree=True),expected)


if __name__=='__main__':unittest.main()
