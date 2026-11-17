"""验证双图事务、重传回执、筛选导出与手机通信边界。"""
import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request,urlopen
import uuid
from zipfile import ZipFile
from PIL import Image
from inventory_service import InventoryService,InventoryHandler
from http.server import ThreadingHTTPServer

def record(mode='in',sku='LC7877529-P105'):
    b=io.BytesIO();Image.new('RGB',(300,200),(50,70,90)).save(b,'JPEG');image=b.getvalue()
    return dict(id=str(uuid.uuid4()),device=str(uuid.uuid4()),mode=mode,sku=sku,at='2026-11-05T15:00:00+08:00',raw=sku,source='offline',product_sha=hashlib.sha256(image).hexdigest(),label_sha=hashlib.sha256(image).hexdigest(),product=base64.b64encode(image).decode(),label=base64.b64encode(image).decode())

class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=InventoryService(Path(self.tmp.name))
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def test_restart_idempotence_and_conflict(self):
        r=record();a=self.s.receive(r);self.assertFalse(a['duplicate']);self.assertTrue(self.s.receive(r)['duplicate']);self.assertEqual(self.s.state()['summary']['in'],1)
        other=InventoryService(Path(self.tmp.name));self.assertTrue(other.receive(r)['duplicate']);other.close()
        wrong={**r,'sku':'LCOTHER12'}
        with self.assertRaisesRegex(ValueError,'冲突'):self.s.receive(wrong)
        self.assertEqual(self.s.records({})['rows'][0]['sku'],r['sku'])
    def test_corrupt_second_image_never_commits_partial_record(self):
        r=record();r['label_sha']='0'*64
        with self.assertRaisesRegex(ValueError,'校验'):self.s.receive(r)
        self.assertEqual(self.s.state()['summary']['total'],0);self.assertEqual(list(self.s.images.iterdir()),[])
    def test_missing_photo_and_bad_identity_and_timestamp(self):
        for field,value in [('product',''),('id','../../file'),('at','2026-11-05T15:00:00'),('mode','bad'),('sku','=DDE123')]:
            r=record();r[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):self.s.receive(r)
    def test_search_counts_and_xlsx_has_embedded_double_images(self):
        self.s.receive(record());self.s.receive(record('out','LC12345'));self.s.receive(record('in','AB98765'))
        self.assertEqual(self.s.state()['summary']['net'],1);self.assertEqual(self.s.records({'mode':'out'})['total'],1)
        self.assertEqual(self.s.records({'query':'lc'})['total'],2)
        content=self.s.export({'query':'LC'});
        with ZipFile(io.BytesIO(content)) as z:
            self.assertEqual(len([n for n in z.namelist() if n.startswith('xl/media/')]),4)
            self.assertNotIn('TargetMode="External"',''.join(z.read(n).decode() for n in z.namelist() if n.endswith('.rels')))
        from openpyxl import load_workbook
        w=load_workbook(io.BytesIO(content));sheet=w.active;self.assertEqual(sheet.max_row,3);self.assertEqual(len(sheet._images),4);self.assertIn(sheet['B2'].value,['LC12345','LC7877529-P105'])
    def test_network_auth_origin_and_retry(self):
        http=ThreadingHTTPServer(('127.0.0.1',0),InventoryHandler);http.inventory=self.s
        threading.Thread(target=http.serve_forever,daemon=True).start();url=f'http://127.0.0.1:{http.server_port}'
        try:
            with self.assertRaises(HTTPError) as bad:urlopen(url+'/v1/health')
            self.assertEqual(bad.exception.code,401)
            h={'Authorization':'Bearer '+self.s.config['token'],'Content-Type':'application/json'}
            req=Request(url+'/v1/health',headers={**h,'Origin':'https://external.invalid'})
            with self.assertRaises(HTTPError):urlopen(req)
            r=record();req=Request(url+'/v1/records',data=json.dumps(r).encode(),headers=h)
            for duplicate in [False,True]:
                with urlopen(req) as response:d=json.load(response)
                self.assertEqual(d['duplicate'],duplicate);self.assertEqual(d['product_sha'],r['product_sha'])
        finally:http.shutdown();http.server_close()
    def test_config_only_port_when_enabled(self):
        self.assertFalse(self.s.state()['enabled'])
        with self.assertRaises(ValueError):self.s.configure({'enabled':True,'port':80})
        self.s.configure({'theme':'light'});self.assertEqual(self.s.state()['theme'],'light')
    def test_duplicate_repair_missing_picture(self):
        r=record();self.s.receive(r);p=self.s.images/f'{r["id"]}-product.jpg';p.unlink();self.s.receive(r);self.assertTrue(p.is_file())
    def test_workspace_identity_survives_restart_and_cross_instance_duplicate(self):
        other=InventoryService(Path(self.tmp.name));self.assertEqual(self.s.config['workspace'],other.config['workspace'])
        r=record()
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda s:s.receive(r),[self.s,other]))
        self.assertEqual(sum(not x['duplicate'] for x in results),1);self.assertEqual(len(list(self.s.images.iterdir())),2);other.close()

if __name__=='__main__':unittest.main()
