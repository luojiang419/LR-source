"""归档 OCR 可检索、目录互证/默认、版本失效与只读视觉指纹。"""
import hashlib
import json
import unittest
from unittest.mock import patch

from PIL import Image
import test_analysis_workflow as fixture
from catalog_ocr import save_ocr,backfill,verify_visual
from retrieval_store import query
from test_asset_intelligence import result


class CatalogOcrTests(unittest.TestCase):
    def setUp(self):
        fixture.AnalysisWorkflowTests.setUp(self)
        self.sku.set_sku_rules([{'kind':'prefix','value':'LC'}])
        self.folder=self.base/'LC12345';self.folder.mkdir()
        for number in range(2):Image.new('RGB',(160,120),('red','blue')[number]).save(self.folder/f'photo-{number}.jpg')
        self.ocr_root=self.catalog.add_root(str(self.folder))['root_id']
        with self.catalog.connect() as c:self.rows=[dict(r) for r in c.execute('SELECT * FROM items WHERE root=? ORDER BY id',(self.ocr_root,))]
        self.original={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.folder.iterdir()}

    def tearDown(self):
        self.assertEqual(self.original,{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.folder.iterdir()})
        fixture.AnalysisWorkflowTests.tearDown(self)

    def save(self,row,text='',codes=None):
        with self.catalog.connect() as c:save_ocr(c,row,'ocr-key','ocr-model',[{'seconds':0,'reading':{'text':text,'codes':codes or []}}],self.sku.sku_rules(),'a'*64)

    def group(self):
        with self.catalog.connect() as c:return dict(c.execute('SELECT * FROM intelligence_ocr_groups WHERE root=?',(self.ocr_root,)).fetchone())

    def items(self):
        with self.catalog.connect() as c:return [dict(r) for r in c.execute('SELECT * FROM items WHERE root=? ORDER BY id',(self.ocr_root,))]

    def test_no_label_defaults_to_unique_lc_folder_only_after_complete_coverage(self):
        self.save(self.rows[0],'普通衣服')
        self.assertEqual(self.group()['status'],'pending')
        self.assertFalse(any(r['source']=='folder_default' for r in self.items()))
        self.save(self.rows[1],'没有可读文字')
        self.assertEqual(self.group()['status'],'folder_default')
        self.assertTrue(all(r['sku']=='LC12345' and r['source']=='folder_default' for r in self.items()))
        with self.catalog.connect() as c:
            fingerprints=list(c.execute('SELECT * FROM intelligence_file_fingerprints WHERE item IN (?,?)',[r['id'] for r in self.rows]))
            self.assertEqual(len(fingerprints),2)
            self.assertTrue(all(r['content_sha']=='' and r['input_sha']=='a'*64 for r in fingerprints))

    def test_folder_and_ocr_match_using_existing_rules_not_model_truncated_code(self):
        self.save(self.rows[0],'款号 LC12345',['LC123'])
        self.save(self.rows[1],'产品展示')
        self.assertEqual((self.group()['status'],self.group()['sku']),('confirmed','LC12345'))
        self.assertTrue(all(r['source']=='ocr_folder' for r in self.items()))

    def test_conflicting_ocr_never_becomes_folder_default(self):
        self.save(self.rows[0],'LC12345');self.save(self.rows[1],'LC99999')
        self.assertEqual(self.group()['status'],'conflict')
        self.assertTrue(all(r['sku']=='' for r in self.items()))

    def test_later_conflict_retracts_default_and_manual_sku_is_preserved(self):
        self.save(self.rows[0]);self.save(self.rows[1])
        self.catalog.assign(self.rows[0]['id'],'MANUAL-900')
        self.save(self.rows[1],'LC99999')
        self.assertEqual(self.group()['status'],'conflict')
        items={r['id']:r for r in self.items()}
        self.assertEqual(items[self.rows[0]['id']]['sku'],'MANUAL-900')
        self.assertEqual(items[self.rows[1]['id']]['sku'],'')

    def test_ocr_text_search_without_any_vision_and_quick_search_survives_restart(self):
        self.save(self.rows[0],'COTTONSPECIAL 山川');self.save(self.rows[1])
        for word in ('COTTONSPECIAL','山川'):
            self.assertIn(self.rows[0]['id'],{r['id'] for r in query(self.catalog,text=word)['rows']})
        quick=self.intelligence.quick_index;quick.build()
        quick.build_memory()
        while quick.sync():pass
        for word in ('COTTONSPECIAL','山川','LC12345'):
            found=self.intelligence.search(text=word,quick=True)['rows']
            self.assertIn(self.rows[0]['id'],{r['id'] for r in found})
        quick.close()
        from quick_file_index import QuickFileIndex
        self.intelligence.quick_index=QuickFileIndex(self.catalog,folder=quick.path.parent)
        self.assertIn(self.rows[0]['id'],{r['id'] for r in self.intelligence.quick_index.query('COTTONSPECIAL')['rows']})

    def test_version_change_invalidates_group_and_stale_ocr_is_not_searchable(self):
        self.save(self.rows[0],'RAREOLDTEXT');self.save(self.rows[1])
        with self.catalog.connect() as c:c.execute("UPDATE items SET fingerprint='changed' WHERE id=?",(self.rows[0]['id'],))
        self.assertEqual(self.group()['status'],'pending')
        self.assertFalse(any(r['source']=='folder_default' for r in self.items()))
        self.assertEqual(query(self.catalog,text='RAREOLDTEXT')['rows'],[])
        with self.assertRaisesRegex(ValueError,'版本已变化'):self.save(self.rows[0],'OLD')

    def test_unchanged_rescan_keeps_default_and_new_member_requires_validation(self):
        self.save(self.rows[0]);self.save(self.rows[1]);self.catalog.scan(self.ocr_root)
        self.assertTrue(all(r['source']=='folder_default' for r in self.items()))
        new=self.folder/'new.jpg';Image.new('RGB',(160,120),'green').save(new)
        self.original[str(new)]=hashlib.sha256(new.read_bytes()).hexdigest()
        self.catalog.scan(self.ocr_root)
        self.assertEqual(self.group()['status'],'pending')
        self.assertFalse(any(r['source']=='folder_default' for r in self.items()))

    def test_visual_contradiction_marks_conflict_and_does_not_reappear_on_ocr_backfill(self):
        self.save(self.rows[0]);self.save(self.rows[1])
        value=result();value.update(visible_text='LC99999',sku_candidates=['LC99999'])
        from retrieval_store import save_result
        with self.catalog.connect() as c:
            save_result(c,self.rows[0]['id'],self.rows[0]['fingerprint'],'vision-key','vision',[{'seconds':0,'result':value}],[])
            verify_visual(c,self.rows[0],value,self.sku.sku_rules())
        self.assertEqual(self.group()['status'],'conflict')
        self.save(self.rows[0])
        self.assertEqual(self.group()['status'],'conflict')

    def test_old_ocr_results_backfill_without_another_model_request(self):
        with self.catalog.connect() as c:
            for row in self.rows:c.execute('INSERT INTO intelligence_ocr VALUES(?,?,?,?,?,?)',(row['id'],row['fingerprint'],'key','model',json.dumps([{'reading':{'text':'BACKFILLTEXT'}}]),1))
            self.assertEqual(backfill(c,self.sku.sku_rules()),2)
        self.assertEqual(self.group()['status'],'folder_default')
        self.assertIn(self.rows[0]['id'],{r['id'] for r in query(self.catalog,text='BACKFILLTEXT')['rows']})
        self.ocr.assert_not_called()

    def test_length_rules_can_block_folder_default_and_ambiguous_folders_need_review(self):
        self.sku.set_sku_rules([{'kind':'prefix','value':'LC'}],{'min_exclusive':9,'max_exclusive':None})
        self.save(self.rows[0]);self.save(self.rows[1])
        self.assertEqual(self.group()['status'],'review')
        self.assertFalse(any(r['source']=='folder_default' for r in self.items()))

    def test_quick_index_does_not_acknowledge_an_ocr_change_during_copy(self):
        self.save(self.rows[0],'FIRSTTEXT')
        quick=self.intelligence.quick_index;quick.build()
        quick.build_memory()
        original=quick._save
        def changed(c,rows):
            original(c,rows);self.save(self.rows[0],'SECONDTEXT')
        with patch.object(quick,'_save',side_effect=changed):self.assertTrue(quick.sync())
        with self.catalog.connect() as c:self.assertTrue(c.execute('SELECT 1 FROM quick_file_dirty WHERE item=?',(self.rows[0]['id'],)).fetchone())
        while quick.sync():pass
        self.assertIn(self.rows[0]['id'],{r['id'] for r in quick.query('SECONDTEXT')['rows']})
        self.assertEqual(quick.query('FIRSTTEXT')['rows'],[])

    def test_browser_archive_shows_ocr_validation_and_database_only_notice(self):
        from contextlib import ExitStack
        from playwright.sync_api import sync_playwright,expect
        import server
        import test_sku_input_confirm as http_fixture
        self.save(self.rows[0],'COTTONSPECIAL');self.save(self.rows[1])
        with http_fixture.InputConfirmTests.http(self) as url,ExitStack() as stack,sync_playwright() as pw:
            for name,value in dict(WORKFLOW=self.workflow,ANALYSIS=self.analysis,INTELLIGENCE=self.intelligence,CATALOG=self.catalog).items():stack.enter_context(patch.object(server,name,value))
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>window.FramekeepAssetViewer&&!FramekeepBoot.loading')
                item=self.items()[0];item['preview']='/api/catalog/preview/'+item['id']
                page.evaluate('(row)=>FramekeepAssetViewer.open("archive",[row],row.id)',item)
                expect(page.locator('#asset-viewer-fields')).to_contain_text('OCR 与款号核验')
                expect(page.locator('#asset-viewer-fields')).to_contain_text('目录内未识别到款号图')
                expect(page.locator('#asset-viewer-fields')).to_contain_text('数据库 SKU：LC12345')
                expect(page.locator('#asset-viewer-fields')).to_contain_text('SKU 元数据 · 等待写入')
                page.locator('#asset-viewer-fields .asset-viewer-ocr').scroll_into_view_if_needed()
                out=fixture.fixture.ROOT/'build/ocr-streaming';out.mkdir(exist_ok=True)
                page.screenshot(path=str(out/'ocr-folder-default-viewer.png'))
                self.assertFalse(errors,errors)
            finally:browser.close()

    def test_export_import_preserves_folder_default_fingerprint_links_and_searchable_text(self):
        from pathlib import Path
        import tempfile
        import test_ocr_metadata as metadata_fixture
        from sku_metadata_backup import export_metadata,import_metadata,_canonical
        self.save(self.rows[0],'MIGRATABLETEXT');self.save(self.rows[1])
        backup=export_metadata(self.sku)
        with tempfile.TemporaryDirectory() as folder:
            store=metadata_fixture.Store(Path(folder)/'data',metadata_fixture.ROOT)
            providers=metadata_fixture.Providers(store.data);archive=metadata_fixture.AssetArchive(store,autostart=False)
            sku=metadata_fixture.SkuService(store,providers,archive=archive,autostart=False)
            try:
                import_metadata(sku,_canonical(backup['backup']))
                imported=sku.intelligence.catalog
                with imported.connect() as c:
                    group=c.execute('SELECT status,sku FROM intelligence_ocr_groups WHERE root=?',(self.ocr_root,)).fetchone()
                    self.assertEqual(tuple(group),('folder_default','LC12345'))
                    self.assertEqual(c.execute('SELECT count(*) FROM intelligence_file_fingerprints').fetchone()[0],2)
                self.assertIn(self.rows[0]['id'],{r['id'] for r in query(imported,text='MIGRATABLETEXT')['rows']})
            finally:sku.close();archive.close()

    def test_folder_validation_and_text_update_do_not_scan_unrelated_root_rows(self):
        self.save(self.rows[0]);self.save(self.rows[1])
        with self.catalog.connect() as c:
            c.executemany('''INSERT INTO items(id,root,path,relative,folder,name,extension,size,modified,fingerprint,last_seen,pair_key)
                VALUES(?,?,?,?,'unrelated',?,'.jpg',1,1,'version',1,?)''',
                [(f'unrelated-{i}',self.ocr_root,str(self.folder/'unrelated'/f'{i}.jpg'),f'unrelated/{i}.jpg',f'{i}.jpg',f'pair-{i}') for i in range(4000)])
            c.executemany('INSERT INTO intelligence_fts(item,terms) VALUES(?,?)',[(f'unrelated-{i}','other text') for i in range(4000)])
        calls=0
        def bounded():
            nonlocal calls
            calls+=1;return calls>200
        config=self.sku.sku_rules()
        with self.catalog.connect() as c:
            c.set_progress_handler(bounded,100)
            try:
                save_ocr(c,self.rows[0],'ocr-key','ocr-model',[{'reading':{'text':'LC12345'}}],config)
                self.assertEqual(backfill(c,config),0)
            finally:c.set_progress_handler(None,0)
        self.assertEqual(self.group()['status'],'confirmed')

    def test_old_folder_triggers_upgrade_without_removing_saved_sku_assignments(self):
        self.save(self.rows[0]);self.save(self.rows[1])
        from catalog_ocr import install
        with self.catalog.connect() as c:
            for name in ('intelligence_ocr_version','intelligence_ocr_new_member'):
                sql=c.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()[0]
                c.execute('DROP TRIGGER '+name);c.execute(sql.replace(' AND online IN (0,1)',''))
            install(c)
            for name in ('intelligence_ocr_version','intelligence_ocr_new_member'):
                self.assertIn('online IN (0,1)',c.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()[0])
        self.assertTrue(all(row['source']=='folder_default' for row in self.items()))


if __name__=='__main__':unittest.main()
