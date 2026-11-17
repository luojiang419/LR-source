"""Web 快捷搜索的实际检索、动效、输入竞争及汇总标签证据。"""
import json
from pathlib import Path
import unittest

from PIL import Image
from playwright.sync_api import sync_playwright,expect

import test_lan_workspace as lan_fixture
from test_asset_retrieval_browser import ROOT


class LanQuickTests(unittest.TestCase):
    setUp=lan_fixture.LanWorkspaceTests.setUp
    tearDown=lan_fixture.LanWorkspaceTests.tearDown
    call=lan_fixture.LanWorkspaceTests.call
    assert_access=lan_fixture.LanWorkspaceTests.assert_access

    def enter(self,page):
        page.goto(self.lan_url,wait_until='domcontentloaded')
        page.wait_for_selector('#workspace:not([hidden])')

    def test_quick_layout_background_keyboard_and_responsive_themes(self):
        out=ROOT/'build/lan-web-quick';out.mkdir(parents=True,exist_ok=True)
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                self.enter(page);self.assertEqual(page.locator('#tab-archive,#panel-archive').count(),0)
                self.assertEqual(page.locator('.lan-tabs button').all_text_contents(),['拍摄进度','资产检索','快捷搜索'])
                page.locator('#tab-quick').click();page.wait_for_function('()=>document.activeElement.id==="quick-query"')
                geometry=page.evaluate('''()=>{const a=document.getElementById('panel-quick').getBoundingClientRect(),b=document.getElementById('quick-form').getBoundingClientRect();return Math.abs(a.y+a.height/2-b.y-b.height/2)}''')
                self.assertLess(geometry,2);page.screenshot(path=str(out/'quick-empty.png'))
                page.locator('#quick-query').fill('LC1234');expect(page.locator('#quick-grid .lan-quick-card')).to_have_count(2)
                page.wait_for_function('()=>document.querySelector(".lan-quick-background.is-active img")?.naturalWidth>0')
                page.wait_for_timeout(700)
                self.assertEqual(page.locator('#quick-query-text').inner_text(),'LC1234')
                geometry=page.evaluate('''()=>{const a=document.getElementById('panel-quick').getBoundingClientRect(),b=document.getElementById('quick-form').getBoundingClientRect();return {moved:b.y>a.y+a.height*.6,curve:getComputedStyle(document.getElementById('quick-form')).transitionTimingFunction,background:document.querySelector('.lan-quick-background.is-active').dataset.item}}''')
                self.assertTrue(geometry['moved']);self.assertIn('cubic-bezier',geometry['curve'])
                self.assert_access()
                matches={row['id'] for row in self.call('/api/search?q=LC1234&quick=1')[1]['rows']}
                self.assertIn(geometry['background'],matches)
                page.locator('#quick-query').press('ArrowDown');page.locator('#quick-query').press('Enter')
                page.wait_for_selector('#asset-detail[open]');page.keyboard.press('Escape')
                page.screenshot(path=str(out/'quick-results.png'))
                for theme in ('dark','light','pure-white','hardcore'):
                    page.evaluate('(value)=>FramekeepTheme.set(value)',theme)
                    for width,height in ((1680,1050),(1024,768),(760,900),(390,844),(340,740),(700,480)):
                        page.set_viewport_size({'width':width,'height':height})
                        self.assertFalse(page.evaluate('()=>document.documentElement.scrollWidth>innerWidth'),(theme,width))
                        self.assertTrue(page.locator('#quick-query').is_visible())
                        self.assertGreater(page.locator('#quick-results').bounding_box()['height'],50)
                        if width in (1680,390):page.screenshot(path=str(out/f'quick-{theme}-{width}.png'))
                page.emulate_media(reduced_motion='reduce')
                self.assertTrue(page.locator('#quick-form').evaluate('e=>getComputedStyle(e).transitionDuration.split(",").every(v=>parseFloat(v)===0)'))
                page.locator('#quick-clear').click();self.assertFalse(page.locator('#panel-quick').evaluate('e=>e.classList.contains("has-query")'))
                self.assertTrue(page.locator('#quick-heading').is_hidden())
                self.assertFalse(errors,errors)
                (out/'visual-result.json').write_text(json.dumps({'passed':True,'background_from_results':True,'themes':4,'page_errors':errors}),encoding='utf8')
            finally:browser.close()

    def test_composition_stale_response_pagination_empty_and_tab_restore(self):
        folder=Path(self.rows[0]['path']).parent
        for i in range(64):Image.new('RGB',(24,32),'teal').save(folder/f'LC1234-quick-{i}.jpg')
        self.catalog.scan(self.rows[0]['root'])
        while self.service.quick_index.sync():pass
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':900},reduced_motion='reduce');self.enter(page);page.locator('#tab-quick').click()
                requests=[];page.on('request',lambda r:requests.append(r.url) if '/api/search?' in r.url else None)
                query=page.locator('#quick-query')
                query.evaluate('''e=>{e.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true}));e.value='未选定';e.dispatchEvent(new InputEvent('input',{bubbles:true,isComposing:true}))}''')
                page.wait_for_timeout(180);self.assertEqual(requests,[])
                query.evaluate('''e=>{e.value='LC1234';e.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true}))}''')
                expect(page.locator('#quick-grid .lan-quick-card')).to_have_count(60)
                page.locator('#quick-results').evaluate('e=>e.scrollTop=e.scrollHeight')
                page.wait_for_function('()=>document.querySelectorAll("#quick-grid .lan-quick-card").length>60')
                pending=[]
                page.route('**/api/search?**',lambda route:pending.append(route) if 'q=slow' in route.request.url else route.continue_())
                query.fill('slow');page.wait_for_timeout(150);self.assertTrue(pending)
                query.fill('LC5678');expect(page.locator('#quick-grid .lan-quick-card')).to_have_count(1)
                for route in pending:
                    try:route.fulfill(json={'rows':[],'has_more':False,'next_cursor':''})
                    except Exception:pass
                self.assertEqual(page.locator('#quick-query-text').inner_text(),'LC5678')
                self.assertIn('蓝色',page.locator('#quick-grid').inner_text())
                query.fill('NO-MATCH-QUICK');page.wait_for_selector('.lan-quick-empty')
                self.assertEqual(page.locator('.lan-quick-background.is-active').count(),0)
                query.fill('LC5678');expect(page.locator('#quick-grid .lan-quick-card')).to_have_count(1)
                page.locator('#tab-progress').click();page.locator('#tab-quick').click()
                self.assertEqual(query.input_value(),'LC5678');expect(page.locator('#quick-grid .lan-quick-card')).to_have_count(1)
                query.press('Escape');self.assertEqual(query.input_value(),'')
            finally:browser.close()

    def test_all_common_and_sheet_show_matching_label_and_capture_time(self):
        import media_engine as media
        label=next(Path(row['path']) for row in self.rows if '蓝色' in row['name'])
        sid=self.store.ensure_live_session(label.parent);record=media.capture_record(label.parent,label,None,{})
        self.store.append_live(sid,[record])
        with self.sku.connect() as c:
            c.execute('''INSERT INTO jobs(id,project,session_id,capture_id,scope,status,captured_at,result,targets)
                VALUES('web-label','plan',?,?,'细节图','confirmed','2026-11-03T18:30:00+08:00','{}','[]')''',(sid,record['id']))
            c.execute("UPDATE sku_rows SET completion_job='web-label',completed_at='2026-11-03T18:30:00+08:00' WHERE id='row4'")
        self.assert_access()
        for view in ('all','common','sheet&sheet=细节图'):
            status,data,_=self.call('/api/progress?project=plan&view='+view)
            self.assertEqual(status,200)
            row=next(row for row in data['rows'] if row['sku']=='LC5678')
            self.assertEqual(row['id'],'row4');self.assertTrue(row['label_preview'])
            self.assertEqual(row['completed_at'],'2026-11-03T18:30:00+08:00')
            self.assertEqual(self.call(row['label_preview']+'&size=2048')[0],200)
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();self.enter(page)
                for view,sheet in (('all',''),('common',''),('sheet','细节图')):
                    selector=f'#progress-sheets [data-view="{view}"]'+(f'[data-sheet="{sheet}"]' if sheet else '')
                    page.locator(selector).click();page.wait_for_function('()=>document.getElementById("progress-head").textContent.includes("标签图片")')
                    self.assertIn('拍摄完成时间',page.locator('#progress-head').inner_text())
                    row=page.locator('#progress-rows [data-progress-row="row4"]');expect(row).to_contain_text('2026-11-03 18:30:00')
                    row.locator('[data-image-title$="标签图片"]').click()
                    page.wait_for_function('()=>document.getElementById("progress-image-content").naturalWidth>0')
                    self.assertIn('size=2048',page.locator('#progress-image-content').get_attribute('src'));page.keyboard.press('Escape')
            finally:browser.close()

    def test_imported_label_preview_does_not_require_original_photo_session(self):
        from sku_workbook import inspect_workbook,export_workbook
        from sku_workbook_pictures import add_pictures
        from sku_workbook_presentation import finalize
        import test_sku_workflow as workbook
        source=self.base/'source.xlsx';source.write_bytes(workbook.fixture_book());plans=inspect_workbook(source)
        output=self.base/'embedded-label.xlsx';picture=Path(self.rows[0]['path']).read_bytes()
        images={('模特图',2):picture};stamp='2026-11-03T18:30:00+08:00'
        export_workbook(source,output,plans,{('模特图',2):'已拍摄'},{('模特图',2):stamp},capture_images=images)
        labels=[dict(plan,status_col=plan['image_col'],image_col=None) for plan in inspect_workbook(output)]
        add_pictures(output,labels,images,picture_title='标签图片',picture_kind='Label')
        finalize(output,plans,{},[],images)
        project=self.sku.import_file('embedded.xlsx',output.read_bytes())['project']['id'];self.assert_access()
        for view in ('all','sheet&sheet=模特图'):
            _,data,_=self.call('/api/progress?project='+project+'&view='+view)
            row=next(r for r in data['rows'] if r['sku']==workbook.CODE)
            self.assertTrue(row['label_preview']);self.assertEqual(row['completed_at'],stamp)
            status,payload,headers=self.call(row['label_preview']+'&size=2048')
            self.assertEqual(status,200);self.assertEqual(headers['Content-Type'],'image/jpeg')
            self.assertGreater(len(payload),100)
