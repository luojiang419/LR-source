"""按系统本地日记账，界面按本地时区显示，保留原始拍摄证据。"""
from contextlib import ExitStack
from datetime import datetime,timezone,timedelta
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from playwright.sync_api import expect,sync_playwright
import server
import test_ocr_metadata as fixture


class LocalTimeTests(unittest.TestCase):
    setUp=fixture.OcrMetadataTests.setUp
    tearDown=fixture.OcrMetadataTests.tearDown
    parse=fixture.OcrMetadataTests.parse
    choose=fixture.OcrMetadataTests.choose
    code='LC7878091'

    def test_record_and_automatic_day_follow_system_local_zone(self):
        zone=timezone(timedelta(hours=-7));case=self
        class LocalDateTime(datetime):
            @classmethod
            def fromtimestamp(cls,stamp,tz=None):
                case.assertIn(tz,(None,timezone.utc),'业务层不能固定为 UTC+8')
                return super().fromtimestamp(stamp,tz or zone)
            def astimezone(self,tz=None):return super().astimezone(tz or zone)
        epoch=datetime(2026,10,27,2,30,tzinfo=timezone.utc).timestamp()
        self.sku.clock=lambda:epoch
        with self.sku.connect() as c:c.execute('UPDATE projects SET enabled=0')
        with patch('sku_service.datetime',LocalDateTime):
            project=self.sku._automatic_project(self.sid,start_stream=False)
            self.assertEqual(project['metadata']['automatic_day'],'2026-10-26')
            job={'project':project['id'],'session_id':self.sid,'capture_id':self.ids[1],'scope':project['scope']}
            result=self.sku._record_unplanned(job,[{'codes':[self.code],'confidence':1}],{'status':'matched'})
            self.assertEqual(result['status'],'matched')
            row=next(r for r in self.sku.rows(project['id']) if r['sku']==self.code)
            self.assertEqual(json.loads(row['source_values'])['D'],'2026-10-26T19:30:00-07:00')
            self.sku.enqueue(self.sid,self.ids[1],trigger='auto')
            self.assertEqual(self.sku.project()['id'],project['id'])

    def test_browser_converts_offsets_without_shifting_naive_times_or_evidence(self):
        self.reading.update(text=self.code,codes=[self.code],ocr_candidates=[self.code])
        self.parse();self.choose(self.code)
        row=next(r for r in self.sku.state()['rows'] if r['sku']==self.code)
        self.sku.configure(self.pid,self.sid,row['sheet'])
        stamp='2026-10-26T11:36:43+08:00';completed='2026-10-26T17:19:23.04+08:00'
        with self.sku.connect() as c:
            c.execute("UPDATE sku_rows SET source_values=json_set(source_values,'$.D',?) WHERE id=?",(stamp,row['id']))
            c.execute('UPDATE jobs SET captured_at=? WHERE id=?',(completed,row['completion']['id']))
            self.sku._refresh_progress(c,self.pid)
        original=self.sku.state();target=next(r for r in original['rows'] if r['id']==row['id'])
        self.assertEqual(target['completed_at'],completed)
        values=dict(STORE=self.store,DATA=self.store.data,MODELS=self.providers,SKU=self.sku,ARCHIVE=self.archive,
                    CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,CANON=None,RAWLINK=None,FILESEARCH=None,
                    UPDATES=None,COMPONENTS=None,DRIVER=None)
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        out=fixture.ROOT/'build/sku-local-time';out.mkdir(parents=True,exist_ok=True)
        with ExitStack() as stack:
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        cases=[('Asia/Shanghai','2026-10-26 11:36:43','2026-10-26 17:19:23.04'),
                               ('America/Los_Angeles','2026-10-25 20:36:43','2026-10-26 02:19:23.04'),
                               ('UTC','2026-10-26 03:36:43','2026-10-26 09:19:23.04')]
                        for zone,record_text,shot_text in cases:
                            with self.subTest(zone=zone):
                                context=browser.new_context(timezone_id=zone,viewport={'width':1600,'height':1000},reduced_motion='reduce')
                                try:
                                    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                                    page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                                    page.locator('#sku-open').click();node=page.locator('[data-sku-row="'+row['id']+'"]')
                                    expect(node).to_be_visible()
                                    index=page.locator('#sku-rows th').all_text_contents().index('记录时间')
                                    expect(node.locator('td').nth(index)).to_have_text(record_text)
                                    expect(node.locator('.progress-time')).to_have_text(shot_text)
                                    node.locator('[data-completion]').click()
                                    expect(page.locator('#sku-image-title')).to_contain_text(shot_text)
                                    page.locator('#sku-image-close').click()
                                    page.screenshot(path=str(out/(zone.replace('/','-')+'.png')))
                                    # 不带时区的相机时间按本地墙上时间显示，不额外加减小时。
                                    payload=json.loads(json.dumps(original))
                                    item=next(r for r in payload['rows'] if r['id']==row['id'])
                                    item['completed_at']='2026-10-26T08:09:10.123456';item['source_values']['D']='待补录'
                                    page.route('**/api/sku',lambda route:route.fulfill(json=payload))
                                    page.reload(wait_until='domcontentloaded');page.locator('#sku-open').click()
                                    expect(node.locator('.progress-time')).to_have_text('2026-10-26 08:09:10.123456')
                                    expect(node.locator('td').nth(index)).to_have_text('待补录')
                                    self.assertFalse(errors,errors)
                                finally:context.close()
                    finally:browser.close()
            finally:http.shutdown();http.server_close()
        after=next(r for r in self.sku.state()['rows'] if r['id']==row['id'])
        self.assertEqual(after['completed_at'],completed);self.assertEqual(after['source_values']['D'],stamp)


if __name__=='__main__':unittest.main()
