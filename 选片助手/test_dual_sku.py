from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from asset_archive import AssetArchive
from catalog_store import Store
import media_engine as media
from metadata_writer import sha,raw_integrity
from model_providers import Providers

ROOT=Path(__file__).resolve().parents[1]
RAW=ROOT/'build/dual-sku/input/pair.CR3'
JPG=ROOT/'build/dual-sku/input/pair.JPG'


@unittest.skipUnless(RAW.is_file() and JPG.is_file(),'需要 build/dual-sku/input 下真实 CR3/JPG 隔离样本')
class DualSkuTests(unittest.TestCase):
    def test_real_pair_both_embedded_and_partial_failure_is_visible(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir();raw=photos/'pair.CR3';jpg=photos/'pair.JPG'
            before_sources=[sha(RAW),sha(JPG)];shutil.copy2(RAW,raw);shutil.copy2(JPG,jpg)
            store=Store(base/'data',ROOT);archive=AssetArchive(store,autostart=False)
            archive.writer.run('-overwrite_original','-XMP-xmp:Rating=4',raw,jpg)
            raw_before=raw_integrity(raw)
            with Image.open(jpg) as image:jpg_before=image.convert('RGB').tobytes()
            sid=store.ensure_live_session(photos);meta=media.read_metadata([raw],media.find_exiftool(ROOT))[0];record=media.capture_record(photos,raw,jpg,meta);store.append_live(sid,[record]);store.activate(sid)
            archive.thread.start();http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            patches=[patch.object(server,k,v) for k,v in [('STORE',store),('DATA',store.data),('ARCHIVE',archive),('MODELS',Providers(store.data)),('SKU',None),('ANALYSIS',None),('RAWLINK',None),('FILESEARCH',None),('WATCH',None),('MOUSE',None)]]
            for item in patches:item.start()
            threading.Thread(target=http.serve_forever,daemon=True).start();out=ROOT/'build/dual-sku';errors=[]
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1680,'height':1050});page.on('pageerror',lambda e:errors.append(str(e)));page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                        page.wait_for_function('()=>window.FramekeepAssets&&window.FramekeepLayout');page.locator('#photo-grid [data-focus]').first.click()
                        self.assertEqual(page.locator('#asset-mode option').count(),1);self.assertEqual(page.locator('#asset-mode').input_value(),'embedded')
                        # 旧客户端的伴随参数也不能使新的备案降级为 sidecar。
                        preview=page.evaluate('(cid)=>FramekeepIO.api("/api/metadata/preview",{capture_ids:[cid],sku:"DUAL-TEST-20261020",scope:"细节图",mode:"sidecar"})',record['id'])
                        self.assertEqual(preview['mode'],'embedded');self.assertEqual([f['capability'] for f in preview['targets'][0]['files']],['embedded','embedded'])
                        page.locator('#asset-sku').fill('DUAL-TEST-20261020');page.locator('#current-shooting-tab').click();page.locator('#shooting-manual-type').fill('细节图');page.locator('#photo-settings-tab').click();page.locator('#asset-preview').click();page.wait_for_selector('#asset-preview-dialog[open]')
                        self.assertIn('pair.CR3',page.locator('#asset-preview-content').inner_text());self.assertIn('pair.JPG',page.locator('#asset-preview-content').inner_text());page.screenshot(path=str(out/'pair-preview.png'))
                        page.locator('#asset-apply').click();page.wait_for_function('()=>document.getElementById("asset-message").textContent.includes("RAW 1 / JPG 1")',timeout=120000)
                        a=archive.writer.read(raw);b=archive.writer.read(jpg)
                        for value in (a,b):self.assertEqual(value['XMP-fk:SKU'],'DUAL-TEST-20261020');self.assertEqual(value['XMP-fk:ShootType'],'细节图');self.assertEqual(value['XMP-xmp:Rating'],4)
                        self.assertEqual(a['XMP-fk:AssetId'],b['XMP-fk:AssetId']);self.assertNotEqual(a['XMP-fk:FileId'],b['XMP-fk:FileId']);self.assertFalse(raw.with_suffix('.xmp').exists())
                        self.assertEqual(raw_integrity(raw),raw_before)
                        with Image.open(jpg) as image:self.assertEqual(image.convert('RGB').tobytes(),jpg_before)
                        page.locator('.asset-panel').screenshot(path=str(out/'pair-applied.png'))
                        # RAW 写入失败时，界面必须明确只有 JPG 成功。
                        writer=archive.writer.write
                        def fail_raw(path,*args,**kwargs):
                            if Path(path).suffix.lower()=='.cr3':raise ValueError('测试注入 RAW 写入失败')
                            return writer(path,*args,**kwargs)
                        with patch.object(archive.writer,'write',side_effect=fail_raw):
                            page.locator('#asset-sku').fill('DUAL-RETRY-20261020');page.locator('#app-settings-open').click();page.locator('#asset-overwrite').check();page.locator('#app-settings-close').click();page.locator('#asset-preview').click();page.wait_for_selector('#asset-preview-dialog[open]');page.locator('#asset-apply').click()
                            page.wait_for_function('()=>document.getElementById("asset-message").textContent.includes("部分文件未写入")',timeout=120000)
                            message=page.locator('#asset-message').inner_text();self.assertIn('RAW 0 / JPG 1',message);self.assertIn('pair.CR3',message);self.assertIn('测试注入',message);page.locator('.asset-panel').screenshot(path=str(out/'pair-partial.png'))
                        with archive.connect() as c:op=c.execute("SELECT operation FROM metadata_jobs WHERE status='failed'").fetchone()[0]
                        archive.retry(op)
                        for _ in range(2):archive.process_one()
                        self.assertTrue(all(j['status']=='applied' and j['storage']=='embedded' for j in archive.operation(op)['jobs']))
                        self.assertEqual(archive.writer.read(raw)['XMP-fk:SKU'],'DUAL-RETRY-20261020');self.assertEqual(archive.writer.read(jpg)['XMP-fk:SKU'],'DUAL-RETRY-20261020');self.assertEqual(raw_integrity(raw),raw_before)
                        self.assertFalse(errors,errors);self.assertEqual(before_sources,[sha(RAW),sha(JPG)])
                        (out/'result.json').write_text(json.dumps({'passed':True,'source':'real Canon R6 II CR3 and paired JPG copies','sku':'DUAL-TEST-20261020','embedded':['RAW','JPG'],'rating_preserved':4,'raw_integrity_preserved':True,'jpg_pixels_preserved':True,'sidecar_created':False,'partial_failure_visible':message,'retry_passed':True,'errors':errors},ensure_ascii=False,indent=2),encoding='utf8')
                    finally:browser.close()
            finally:
                archive.close();http.shutdown();http.server_close()
                for item in reversed(patches):item.stop()


if __name__=='__main__':unittest.main()
