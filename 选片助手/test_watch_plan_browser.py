"""监控窗口导入真实表格、切换目录后绑定计划及失败恢复。"""
from http.server import ThreadingHTTPServer
from pathlib import Path
import io
import tempfile
import threading
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as E
from zipfile import ZipFile

from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import Store
from folder_watch import FolderWatch
from model_providers import Providers
from sku_service import SkuService
import media_engine as media
from test_sku_workflow import fixture_book,CODE

ROOT=Path(__file__).resolve().parents[1]


class WatchPlanBrowserTests(unittest.TestCase):
    def test_import_read_counts_bind_new_folder_and_keep_plan_after_failed_import(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);old=base/'原照片目录';old.mkdir();photos=base/'本次拍摄';photos.mkdir()
            store=Store(base/'data',ROOT);sid=store.ensure_live_session(old);store.activate(sid)
            path=old/'old.JPG';Image.new('RGB',(400,300),'teal').save(path);store.append_live(sid,[media.capture_record(old,path,None,{})])
            providers=Providers(store.data);sku=SkuService(store,providers,autostart=False)
            watch=FolderWatch(store,autostart=False);watch.STABLE_SECONDS=.15;watch.INTERVAL=.08
            patches=[patch.object(server,k,v) for k,v in [('STORE',store),('DATA',store.data),('MODELS',providers),('SKU',sku),('WATCH',watch),('MOUSE',None),('ARCHIVE',None),('ANALYSIS',None),('RAWLINK',None),('FILESEARCH',None)]]
            for p in patches:p.start()
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start();watch.thread.start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1100,'height':1000},reduced_motion='reduce');errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading');page.locator('#folder-watch').click()
                        self.assertTrue(page.locator('#watch-plan-import').is_visible());self.assertTrue(page.locator('#watch-plan-scope').is_disabled())
                        # 使用真实文件选择入口触发导入，不伪造导入结果。
                        with page.expect_file_chooser() as chooser:page.locator('#watch-plan-import').click()
                        chooser.value.set_files({'name':'本次拍摄计划.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':fixture_book()})
                        page.wait_for_function('()=>document.getElementById("watch-plan-message").textContent.startsWith("已自动读取")')
                        self.assertTrue(page.locator('#watch-dialog').evaluate('(el)=>el.open'))
                        self.assertEqual(page.locator('#watch-plan-name').inner_text(),'本次拍摄计划.xlsx')
                        self.assertIn('2 个工作表 · 4 项款号',page.locator('#watch-plan-summary').inner_text())
                        self.assertEqual(page.locator('#watch-plan-scope option').all_text_contents(),['请选择本次拍摄类型','模特图','细节图'])
                        self.assertEqual(page.locator('#watch-plan-file').input_value(),'')
                        self.assertEqual(len(sku.state()['rows']),4);self.assertIn(CODE,[r['sku'] for r in sku.state()['rows']]);pid=sku.project()['id']
                        def transfer(files):
                            return page.evaluate_handle('''files=>{const data=new DataTransfer();for(const file of files)data.items.add(new File([new Uint8Array(file.bytes)],file.name));return data}''',files)
                        zone=page.locator('#watch-plan-dropzone')
                        invalid=transfer([{'name':'错误格式.txt','bytes':list(b'not xlsx')}])
                        zone.dispatch_event('dragenter',{'dataTransfer':invalid});self.assertIn('drag-over',zone.get_attribute('class'))
                        page.locator('.watch-plan-heading').dispatch_event('dragenter',{'dataTransfer':invalid})
                        page.locator('.watch-plan-heading').dispatch_event('dragleave',{'dataTransfer':invalid});self.assertIn('drag-over',zone.get_attribute('class'))
                        zone.dispatch_event('dragleave',{'dataTransfer':invalid});self.assertNotIn('drag-over',zone.get_attribute('class'))
                        zone.dispatch_event('drop',{'dataTransfer':invalid})
                        page.wait_for_function('()=>document.getElementById("watch-plan-message").textContent.includes("请选择包含款号的 .xlsx")')
                        self.assertEqual(sku.project()['id'],pid)
                        multiple=transfer([{'name':'一.xlsx','bytes':[1]},{'name':'二.xlsx','bytes':[2]}])
                        zone.dispatch_event('drop',{'dataTransfer':multiple});self.assertIn('每次请只拖入一个',page.locator('#watch-plan-message').inner_text());self.assertEqual(sku.project()['id'],pid)
                        # 区域外落下文件仅阻止默认导航，不能创建任务；文字拖放也不被当成计划。
                        self.assertTrue(page.locator('#watch-path').evaluate('(el,data)=>!el.dispatchEvent(new DragEvent("drop",{bubbles:true,cancelable:true,dataTransfer:data}))',invalid))
                        self.assertEqual(sku.project()['id'],pid)
                        text=page.evaluate_handle('()=>{const d=new DataTransfer();d.setData("text/plain","文字");return d}')
                        zone.dispatch_event('drop',{'dataTransfer':text});self.assertEqual(sku.project()['id'],pid)
                        self.assertEqual(page.locator('#watch-plan-scope').input_value(),'模特图')
                        page.locator('#watch-plan-scope').select_option('');page.locator('#watch-path').fill(str(photos));page.locator('#watch-start').click()
                        page.wait_for_function('()=>document.getElementById("watch-plan-message").textContent.includes("请选择本次拍摄类型")')
                        self.assertFalse(watch.state()['enabled']);self.assertFalse(page.locator('#watch-start').is_disabled())
                        page.locator('#watch-plan-scope').select_option('细节图')
                        # 损坏文件不能替换原任务或清掉已选工作表，按钮应恢复可用。
                        page.locator('#watch-plan-file').set_input_files({'name':'损坏计划.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':b'broken xlsx'})
                        page.wait_for_function('()=>document.getElementById("watch-plan-message").textContent.startsWith("导入失败")')
                        self.assertEqual(sku.project()['id'],pid);self.assertEqual(page.locator('#watch-plan-scope').input_value(),'细节图');self.assertFalse(page.locator('#watch-start').is_disabled())
                        page.locator('#watch-plan-file').set_input_files({'name':'拍摄计划.txt','mimeType':'text/plain','buffer':b'not a workbook'})
                        page.wait_for_function('()=>document.getElementById("watch-plan-message").textContent.includes("请选择包含款号的 .xlsx")')
                        self.assertEqual(sku.project()['id'],pid);self.assertEqual(page.locator('#watch-plan-file').input_value(),'')
                        page.locator('#watch-start').click()
                        page.wait_for_function('()=>window.FramekeepSku.context()?.scope==="细节图"&&window.FramekeepSku.context().project_id===window.FramekeepSku.shootingTypes().project_id')
                        target=watch.state()['session_id'];self.assertNotEqual(target,sid)
                        self.assertEqual(sku.project()['session_id'],target);self.assertEqual(sku.project()['scope'],'细节图')
                        self.assertEqual(page.evaluate('()=>FRAMEKEEP_SESSION.id'),target);self.assertFalse(page.locator('#watch-dialog').evaluate('(el)=>el.open'))
                        page.locator('#sku-open').click();page.wait_for_function('()=>document.getElementById("progress-total")?.textContent==="2"')
                        self.assertIn(CODE,page.locator('#sku-rows').inner_text());self.assertEqual(page.evaluate('FramekeepSku.currentType()'),'细节图')
                        page.locator('#selection-tab').click();page.reload(wait_until='domcontentloaded');page.locator('#folder-watch').click()
                        page.wait_for_function('()=>document.getElementById("watch-plan-scope").value==="细节图"')
                        self.assertEqual(page.locator('#watch-plan-name').inner_text(),'本次拍摄计划.xlsx')
                        out=ROOT/'build/watch-plan-acceptance';out.mkdir(exist_ok=True)
                        for width in [1100,400]:
                            page.set_viewport_size({'width':width,'height':1000})
                            self.assertTrue(page.locator('#watch-dialog').evaluate('(el)=>{const r=el.getBoundingClientRect();return el.scrollWidth<=el.clientWidth&&r.left>=0&&r.right<=innerWidth}'))
                            page.locator('#watch-dialog').screenshot(path=str(out/f'watch-plan-{width}.png'))
                        single=io.BytesIO()
                        with ZipFile(io.BytesIO(fixture_book())) as original,ZipFile(single,'w') as modified:
                            for name in original.namelist():
                                content=original.read(name)
                                if name=='xl/workbook.xml':
                                    book=E.fromstring(content);sheets=next(el for el in book if el.tag.endswith('}sheets'));sheets.remove(list(sheets)[1]);content=E.tostring(book)
                                modified.writestr(name,content)
                        data=transfer([{'name':'单页拖拽拍摄计划.xlsx','bytes':list(single.getvalue())}]);before=len(sku.state()['projects'])
                        zone.dispatch_event('dragenter',{'dataTransfer':data});self.assertIn('drag-over',zone.get_attribute('class'))
                        zone.screenshot(path=str(out/'drag-highlight.png'))
                        zone.evaluate('(el,data)=>{for(let i=0;i<2;i++)el.dispatchEvent(new DragEvent("drop",{bubbles:true,cancelable:true,dataTransfer:data}))}',data)
                        page.wait_for_function('()=>document.getElementById("watch-plan-summary").textContent.includes("1 个工作表 · 2 项款号")')
                        self.assertEqual(len(sku.state()['projects']),before+1);self.assertEqual(page.locator('#watch-plan-name').inner_text(),'单页拖拽拍摄计划.xlsx')
                        self.assertNotIn('drag-over',zone.get_attribute('class'));self.assertIsNone(zone.get_attribute('aria-busy'))
                        self.assertEqual(page.locator('#watch-plan-scope').input_value(),'模特图')
                        page.locator('#watch-start').click();page.wait_for_function('()=>window.FramekeepSku.context()?.scope==="模特图"')
                        self.assertEqual(sku.project()['session_id'],target);self.assertNotEqual(sku.project()['id'],pid)
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:
                watch.close();sku.close();http.shutdown();http.server_close()
                for p in reversed(patches):p.stop()


if __name__=='__main__':unittest.main()
