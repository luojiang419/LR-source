"""增量同步与真实 HTTP 回执边界；不连接用户手机。"""
from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit,parse_qs
from inventory_service import InventoryService
from test_inventory import record

class FakePhone(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def answer(self,data,status=200):
        content=json.dumps(data).encode();self.send_response(status);self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
    def do_GET(self):
        phone=self.server;url=urlsplit(self.path);params=parse_qs(url.query)
        if self.headers.get('Authorization')!='Bearer '+'a'*32:return self.answer({},401)
        if url.path=='/v1/health':return self.answer({'protocol':1,'capabilities':['delta'],'device':phone.device,'summary':{'analyzing':1,'review':2}})
        if url.path=='/v1/changes':
            after=int(params['after'][0]);rows=[{k:v for k,v in row.items() if k not in ('product','label')} for seq,row in phone.rows if seq>after];latest=max([seq for seq,_ in phone.rows],default=after)
            return self.answer({'records':rows,'next':latest,'done':True})
        if url.path=='/v1/record':
            id=params['id'][0];phone.fetches+=1;row=next((r for _,r in phone.rows if r['id']==id),None)
            return self.answer(row) if row else self.answer({},404)
        self.answer({},404)
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length','0')))
        if self.server.fail_ack:self.server.fail_ack=False;return self.answer({},503)
        self.server.acks+=1;self.answer({'ok':True})

class InventoryUsbTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.owner=InventoryService(Path(self.tmp.name),autostart=False);self.link=self.owner.usb_link
        self.phone=ThreadingHTTPServer(('127.0.0.1',0),FakePhone);row=record();self.phone.device=row['device'];self.phone.rows=[(1,row)];self.phone.fetches=0;self.phone.acks=0;self.phone.fail_ack=False
        threading.Thread(target=self.phone.serve_forever,daemon=True).start();self.link.session={'url':f'http://127.0.0.1:{self.phone.server_port}','token':'a'*32,'device':row['device'],'name':'小米 9'};self.link.active=True;self.owner.remember_device(row['device'],'小米 9')
    def tearDown(self):self.link.session=None;self.owner.close();self.phone.shutdown();self.phone.server_close();self.tmp.cleanup()
    def test_delta_import_ack_and_no_repeat_images(self):
        self.link.sync();self.assertEqual(self.owner.state()['summary']['total'],1);self.assertEqual(self.phone.acks,1);self.assertEqual(self.phone.fetches,1);self.link.sync();self.assertEqual(self.phone.fetches,1);self.assertEqual(self.owner.records({})['rows'][0]['device_name'],'小米 9');self.assertEqual(self.link.state()['phone']['review'],2)
    def test_failed_ack_replays_without_double_count(self):
        self.phone.fail_ack=True
        with self.assertRaises(Exception):self.link.sync()
        self.assertEqual(self.owner.state()['summary']['total'],1)
        with closing(self.owner.connect()) as db:self.assertEqual(db.execute('SELECT cursor FROM devices').fetchone()[0],0)
        self.link.sync();self.assertEqual(self.phone.fetches,1);self.assertEqual(self.owner.state()['summary']['total'],1);self.assertEqual(self.phone.acks,1)
    def test_late_ocr_and_restart_resume_cursor(self):
        self.link.sync();second=record();second['device']=self.phone.device;second['at']='2020-01-01T10:00:00+08:00';self.phone.rows.append((2,second));self.link.sync();self.assertEqual(self.owner.state()['summary']['total'],2)
        other=InventoryService(Path(self.tmp.name),autostart=False)
        try:
            with closing(other.connect()) as db:self.assertEqual(db.execute('SELECT cursor FROM devices').fetchone()[0],2)
        finally:other.close()
    def test_missing_local_image_repairs_on_rescan(self):
        self.link.sync();row=self.phone.rows[0][1];(self.owner.images/(row['id']+'-label.jpg')).unlink();self.link.rescan=True;self.link.sync();self.assertEqual(self.phone.fetches,2);self.assertTrue((self.owner.images/(row['id']+'-label.jpg')).is_file());self.assertEqual(self.owner.state()['summary']['total'],1)
    def test_connect_targets_explicit_device_and_hides_usb_token(self):
        self.link.session=None;self.link.adb='fake-adb';commands=[]
        def run(args):
            commands.append(args)
            if 'content' in args:return 'Result: Bundle[{port=8793, token='+'a'*32+'}]'
            if 'forward' in args:return str(self.phone.server_port)
            return 'Status: ok'
        self.link.run=run;self.link.connect({'id':'bea02aff','name':'小米 9'});self.assertTrue(all(args[:2]==['-s','bea02aff'] for args in commands));self.assertNotIn('a'*32,json.dumps(self.link.state()));self.assertIn('content',commands[1]);self.assertEqual(self.link.session['device'],self.phone.device)
    def test_invalid_cursor_does_not_change_committed_cursor(self):
        self.link.sync();original=self.link.request
        self.link.request=lambda path,data=None:{'records':[],'next':0,'done':True} if path.startswith('/v1/changes') else original(path,data)
        with self.assertRaisesRegex(ValueError,'游标'):self.link.sync()
        with closing(self.owner.connect()) as db:self.assertEqual(db.execute('SELECT cursor FROM devices').fetchone()[0],1)

class InventoryDateTests(unittest.TestCase):
    def test_exact_local_day_range_and_inverted_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            service=InventoryService(Path(tmp),autostart=False)
            try:
                for value in [datetime(2026,11,6,0,0),datetime(2026,11,6,23,59,59,999999),datetime(2026,11,7,0,0)]:
                    row=record();row['at']=value.astimezone().isoformat();service.receive(row)
                self.assertEqual(service.records({'start':'2026-11-06','end':'2026-11-06'})['total'],2)
                with self.assertRaisesRegex(ValueError,'早于'):service.records({'start':'2026-11-07','end':'2026-11-06'})
                with self.assertRaisesRegex(ValueError,'同时'):service.records({'start':'2026-11-06'})
            finally:service.close()

if __name__=='__main__':unittest.main()
