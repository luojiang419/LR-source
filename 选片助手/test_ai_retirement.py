"""覆盖移除功能时的历史评级迁移及保留的助手配置。全程不请求模型。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import assistant_config
from catalog_store import Store,stable_json,now

ROOT=Path(__file__).resolve().parents[1]


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.store=Store(self.base,ROOT)

    def tearDown(self):self.temp.cleanup()

    def seed(self,sid='s',basis='visual-contact-sheet',reviewed=False):
        records=[{'id':f'p{i}','raw':f'p{i}.CR3','jpg':f'p{i}.JPG','fingerprint':str(i),'missingTime':False,
                  'time':f'2026-10-19T10:00:0{i}','cameraKey':'one','existingRating':4 if i==3 else 0} for i in range(4)]
        groups=[{'ids':['p0','p1'],'keep_ids':['p0'],'needs_review':True,'confidence':.5},
                {'ids':['p2','p3'],'keep_ids':['p3'],'needs_review':False,'confidence':.9}]
        state={'analysisMode':'ai','activeAiRunId':sid+'-run','keep':1,'threshold':4,'filter':'review','scope':1,
               'overrides':{'p1':5,'p2':-1},'rejectedPrevious':{'p2':1},'selectedId':'p1'}
        with self.store.connect() as c:
            c.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?)',(sid,str(self.base),sid,'snap',7,stable_json(state),now()))
            c.executemany('INSERT INTO captures VALUES (?,?,?,?)',[(sid,r['id'],stable_json(r),i) for i,r in enumerate(records)])
            c.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)',(sid+'-run',sid,'snap',1,'completed','completed',now(),stable_json({'result':{'basis':basis,'groups':groups}})))
            c.execute("INSERT OR REPLACE INTO settings VALUES ('active',?)",(sid,))
            if reviewed:c.execute('INSERT INTO reviews VALUES (?,?)',(sid+'-run',0))
        return state

    def test_visual_ratings_exclusions_and_history_survive_once_only(self):
        self.seed();self.seed('other')
        with self.store.connect() as c:before=c.execute('SELECT * FROM runs ORDER BY id').fetchall()
        reopened=Store(self.base,ROOT)
        for sid in ['s','other']:
            state=reopened.session(sid)['state']
            self.assertEqual(state['overrides'],{'p0':1,'p1':5,'p2':-1,'p3':4})
            self.assertEqual(state['rejectedPrevious'],{'p2':1});self.assertEqual(state['filter'],'all');self.assertEqual(state['scope'],'all')
            self.assertNotIn('activeAiRunId',state);self.assertNotIn('analysisMode',state)
            manifest=reopened.manifest(False,sid)
            self.assertEqual([(r['rating'],r['excluded']) for r in manifest['captures']],[(1,False),(5,False),(0,True),(4,False)])
            self.assertEqual({p['id'] for p in reopened.manifest(True,sid)['captures']},{'p0','p1','p3'})
            self.assertEqual(reopened.session(sid)['revision'],8)
        again=Store(self.base,ROOT)
        self.assertEqual(again.session('s')['revision'],8);self.assertEqual(again.active_id(),'other')
        with again.connect() as c:self.assertEqual([tuple(r) for r in before],[tuple(r) for r in c.execute('SELECT * FROM runs ORDER BY id')])

    def test_non_selected_photos_remain_zero_despite_time_rule(self):
        self.seed()
        with self.store.connect() as c:
            state=self.store.session()['state'];state['overrides']={}
            c.execute('UPDATE sessions SET state=?',(stable_json(state),))
        reopened=Store(self.base,ROOT)
        self.assertEqual(reopened.session()['state']['overrides'],{'p0':1,'p1':0,'p2':0,'p3':4})

    def test_legacy_time_pending_semantics_and_reviewed_groups(self):
        self.seed(basis='time');self.seed('reviewed',basis='time',reviewed=True)
        for sid in ['s','reviewed']:
            with self.store.connect() as c:
                state=self.store.session(sid)['state'];state['overrides']={}
                c.execute('UPDATE sessions SET state=? WHERE id=?',(stable_json(state),sid))
        reopened=Store(self.base,ROOT)
        self.assertEqual(reopened.session('s')['state']['overrides']['p1'],1)
        self.assertEqual(reopened.session('reviewed')['state']['overrides']['p1'],0)

    def test_invalid_history_rolls_back_all_sessions(self):
        self.seed();self.seed('invalid')
        with self.store.connect() as c:c.execute("UPDATE runs SET snapshot='wrong' WHERE session='invalid'")
        original=self.store.session('s')
        with self.assertRaisesRegex(ValueError,'无法迁移'):Store(self.base,ROOT)
        self.assertEqual(self.store.session('s'),original)

    def test_missing_or_duplicate_coverage_is_rejected(self):
        self.seed()
        with self.store.connect() as c:
            result=json.loads(c.execute('SELECT data FROM runs').fetchone()[0]);result['result']['groups'][1]['ids']=['p2','p2']
            c.execute('UPDATE runs SET data=?',(stable_json(result),))
        with self.assertRaisesRegex(ValueError,'未完整覆盖'):Store(self.base,ROOT)
        self.assertEqual(self.store.session()['revision'],7)

    def test_time_only_cleanup_keeps_decisions_and_settings(self):
        state=self.seed();state.update(analysisMode='time',activeAiRunId=None,filter='picked',scope=0)
        with self.store.connect() as c:c.execute('UPDATE sessions SET state=?',(stable_json(state),))
        expected=copy.deepcopy(state);expected.pop('analysisMode');expected.pop('activeAiRunId')
        self.assertEqual(Store(self.base,ROOT).session()['state'],expected)


class AssistantConfigTests(unittest.TestCase):
    def test_config_path_override_bom_and_sanitized_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'配置.md';path.write_text('https://example.invalid/v1\nsk-test-secret\ngpt-test',encoding='utf-8-sig')
            with patch.dict('os.environ',{'FRAMEKEEP_CONFIG':str(path)}):
                self.assertEqual(assistant_config.load_config()['url'],'https://example.invalid/v1/responses')
                info=assistant_config.public_config();self.assertTrue(info['configured']);self.assertEqual(info['path'],str(path))
                self.assertNotIn('sk-test-secret',json.dumps(info))
            path.write_text('http://remote.example:8000/custom/v2\nsk-test-secret\ngpt-test',encoding='utf8')
            self.assertEqual(assistant_config.load_config(path)['url'],'http://remote.example:8000/custom/v2/responses')

    def test_invalid_endpoint_and_missing_config_have_no_secret_in_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'配置.md'
            for endpoint in ['ftp://example.invalid','https://user:password@example.invalid','https://example.invalid?key=secret']:
                path.write_text(endpoint+'\nsk-test-secret\ngpt-test',encoding='utf8')
                with patch.dict('os.environ',{'FRAMEKEEP_CONFIG':str(path)}):
                    info=assistant_config.public_config();self.assertFalse(info['configured']);self.assertNotIn('sk-test-secret',json.dumps(info))
            with patch.dict('os.environ',{'FRAMEKEEP_CONFIG':str(Path(folder)/'absent.md')}):self.assertFalse(assistant_config.public_config()['configured'])


if __name__=='__main__':unittest.main()
