import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from zipfile import ZipFile
from xml.etree import ElementTree as E

from PIL import Image
from catalog_store import Store
import media_engine as media
from model_providers import Providers
from sku_match import normalize,match
from sku_service import SkuService
from sku_workbook import inspect_workbook,export_workbook,read_image,NS,N

ROOT=Path(__file__).resolve().parents[1]
CODE='LC7877496-P905'


def fixture_book():
    output=io.BytesIO()
    with ZipFile(output,'w') as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>')
        z.writestr('xl/workbook.xml',f'<workbook xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="模特图" sheetId="1" r:id="rId1"/><sheet name="细节图" sheetId="2" r:id="rId2"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        for i in (1,2):
            memo='<c r="C1" t="inlineStr"><is><t>备注</t></is></c>' if i==1 else ''
            z.writestr(f'xl/worksheets/sheet{i}.xml',f'<worksheet xmlns="{NS}"><dimension ref="A1:C3"/><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>款号</t></is></c><c r="B1" t="inlineStr"><is><t>图片</t></is></c>{memo}</row><row r="2"><c r="A2" t="inlineStr"><is><t>{CODE}</t></is></c><c r="B2"><f>DISPIMG("native-id",1)</f></c></row><row r="3"><c r="A3" t="inlineStr"><is><t>LC7877496-P804</t></is></c></row></sheetData></worksheet>')
        z.writestr('xl/cellimages.xml','<cellImages xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><cellImage><xdr:pic><xdr:nvPicPr><xdr:cNvPr name="native-id"/></xdr:nvPicPr><xdr:blipFill><a:blip r:embed="image1"/></xdr:blipFill></xdr:pic></cellImage></cellImages>')
        z.writestr('xl/_rels/cellimages.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="image1" Target="media/image1.png"/></Relationships>')
        bitmap=io.BytesIO();Image.new('RGB',(40,60),'teal').save(bitmap,'PNG')
        z.writestr('xl/media/image1.png',bitmap.getvalue())
    return output.getvalue()


def reading(code=CODE,engine='vision',confidence=.99):
    return {'codes':[code],'text':code,'confidence':confidence,'is_label':True,'handwritten':True,'engine':engine,'model':engine,'provider_id':engine}


class MatchTests(unittest.TestCase):
    def rows(self):
        return [{'id':str(i),'sku':v,'normalized':normalize(v),'sheet':'模特图','excel_row':i+2} for i,v in enumerate([CODE,'LC7877496-P804'])]

    def test_exact_normalization_without_collapsing_ocr_confusions(self):
        self.assertEqual(normalize('ｌｃ７８７７４９６ － p９０５'),normalize(CODE))
        self.assertNotEqual(normalize('LC7877496-P9O5'),normalize(CODE))
        self.assertEqual(match([reading()],self.rows())['status'],'matched')

    def test_fuzzy_requires_two_distinct_engines_and_margin(self):
        code='LC7877496-P9O5'
        self.assertEqual(match([reading(code)],self.rows())['status'],'review')
        result=match([reading(code,'OCR'),reading(code,'vision')],self.rows())
        self.assertEqual(result['status'],'matched');self.assertEqual(result['row_ids'],['0'])
        self.assertEqual(match([reading(code,'same'),reading(code,'same')],self.rows())['status'],'review')
        near=self.rows()+[{'id':'3','sku':'LC7877496-P9Q5','normalized':normalize('LC7877496-P9Q5'),'sheet':'模特图','excel_row':4}]
        self.assertEqual(match([reading(code,'OCR'),reading(code,'vision')],near)['status'],'review')

    def test_duplicate_low_confidence_multiple_codes_and_disagreement_review(self):
        self.assertEqual(match([reading(confidence=.5)],self.rows())['status'],'review')
        duplicate=self.rows()+[dict(self.rows()[0],id='other',sheet='细节图')]
        self.assertEqual(match([reading()],duplicate)['status'],'review')
        multiple=reading();multiple['codes']=[CODE,'LC7877496-P804']
        self.assertEqual(match([reading(),multiple],self.rows())['status'],'review')
        self.assertEqual(match([reading(),reading('LC7877496-P804','ocr')],self.rows())['status'],'review')


class ProviderTests(unittest.TestCase):
    def test_cards_explicit_category_defaults_secret_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Providers(folder)
            for category in ['multimodal','ocr']:
                public=p.save({'name':category,'category':category,'protocol':'chat','url':'https://example.invalid/v1','model':'test-model','key':'private-test-key'})
                pid=public['providers'][-1]['id'];self.assertNotIn(category,public['selected']);p.select(pid)
            self.assertNotIn('private-test-key',Path(folder,'model-providers.json').read_text())
            self.assertNotIn('private-test-key',json.dumps(p.public()))
            reopened=Providers(folder);self.assertEqual(reopened.selected('ocr')['key'],'private-test-key')
            reopened.delete(reopened.selected('ocr')['id']);self.assertIsNone(reopened.selected('ocr'))
            self.assertIsNotNone(reopened.selected('multimodal'))


class WorkbookTests(unittest.TestCase):
    def test_wps_image_formula_resolves_without_formula_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.xlsx';source.write_bytes(fixture_book())
            sheets=inspect_workbook(source)
            self.assertEqual(sheets[0]['images'],[{'cell':'B2','media':'xl/media/image1.png'}])
            data,kind=read_image(source,sheets[0]['images'][0]['media'])
            self.assertEqual(kind,'image/png');self.assertEqual(Image.open(io.BytesIO(data)).size,(40,60))
            for invalid in ['../source.xlsx','xl/media/../../secret.png','https://example.com/a.png','xl/media/a.svg']:
                with self.assertRaises(ValueError):read_image(source,invalid)

    def test_delta_status_and_native_parts_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.xlsx';source.write_bytes(fixture_book());plans=inspect_workbook(source)
            self.assertEqual([p['status_letter'] for p in plans],['D','C'])
            out=Path(folder)/'out.xlsx';export_workbook(source,out,plans,{('模特图',2):'已拍摄'})
            with ZipFile(source) as a,ZipFile(out) as b:
                self.assertEqual(set(a.namelist()),set(b.namelist()))
                for name in a.namelist():
                    if not name.startswith('xl/worksheets/'):self.assertEqual(a.read(name),b.read(name),name)
                for name in ['xl/worksheets/sheet1.xml','xl/worksheets/sheet2.xml']:
                    before=E.fromstring(a.read(name));after=E.fromstring(b.read(name))
                    original={c.get('r'):E.tostring(c) for c in before.findall('m:sheetData/m:row/m:c',N)}
                    current={c.get('r'):E.tostring(c) for c in after.findall('m:sheetData/m:row/m:c',N)}
                    for ref,content in original.items():self.assertEqual(content,current[ref])
                self.assertIn('已拍摄',b.read('xl/worksheets/sheet1.xml').decode())
                self.assertNotIn('已拍摄',b.read('xl/worksheets/sheet2.xml').decode())
            again=inspect_workbook(out);self.assertEqual([p['status_letter'] for p in again],['D','C'])


class SkuWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.photos=self.base/'photos';self.photos.mkdir()
        self.store=Store(self.base/'data',ROOT);self.sid=self.store.ensure_live_session(self.photos)
        for i in range(2):Image.new('RGB',(100,80),(80+i*10,100,120)).save(self.photos/f'label{i}.JPG')
        self.store.append_live(self.sid,[media.capture_record(self.photos,p,None,{}) for p in sorted(self.photos.iterdir())]);self.store.activate(self.sid)
        self.cids=[r['id'] for r in self.store.records(self.sid)];self.clock=[1000.]
        self.providers=Providers(self.store.data)
        result=self.providers.save({'name':'test vision','category':'multimodal','protocol':'chat','url':'https://example.invalid','model':'test','key':''})
        self.providers.select(result['providers'][0]['id'])
        self.calls=[]
        def recognize(provider,image):self.calls.append(provider['id']);return reading()
        self.sku=SkuService(self.store,self.providers,recognizer=recognize,clock=lambda:self.clock[0],autostart=False)
        self.pid=self.sku.import_file('task.xlsx',fixture_book(),self.sid)['project']['id']
        self.sku.configure(self.pid,self.sid,'模特图')
        self.preview=patch('media_engine.preview_bytes',return_value=(self.photos/'label0.JPG').read_bytes());self.preview.start()
        self.exif=patch('media_engine.find_exiftool',return_value=Path('test'));self.exif.start()

    def tearDown(self):
        self.sku.close();self.preview.stop();self.exif.stop();self.temp.cleanup()

    def rate(self,value,cid=None):
        cid=cid or self.cids[0];session=self.store.session(self.sid);before=copy.deepcopy(session['state']);state=copy.deepcopy(before);state.setdefault('overrides',{})[cid]=value
        self.store.save(self.sid,session['revision'],state);self.sku.on_save(self.sid,before,state)

    def tick(self):self.clock[0]+=2;self.sku.tick()

    def statuses(self):return {(r['sheet'],r['excel_row']):r['status'] for r in self.sku.rows(self.pid)}

    def test_images_are_scoped_to_known_row_and_migrate_old_metadata(self):
        project=self.sku.project(self.pid)
        for sheet in project['metadata']['sheets']:sheet.pop('images')
        with self.sku.connect() as c:c.execute('UPDATE projects SET metadata=? WHERE id=?',(json.dumps(project['metadata']),self.pid))
        state=self.sku.state();row=next(r for r in state['rows'] if r['excel_row']==2)
        self.assertEqual(len(row['images']),1)
        data,kind=self.sku.image(row['id']);self.assertEqual(kind,'image/png');Image.open(io.BytesIO(data)).verify()
        for index in [-1,1]:
            with self.assertRaises(ValueError):self.sku.image(row['id'],index)
        with self.assertRaises(ValueError):self.sku.image('../../source.xlsx')
        self.assertTrue(all('images' in s for s in self.sku.project(self.pid)['metadata']['sheets']))

    def test_scope_frozen_and_other_page_untouched(self):
        self.rate(2);self.sku.configure(self.pid,self.sid,'细节图');self.tick()
        self.assertEqual(self.statuses()[('模特图',2)],'已拍摄');self.assertEqual(self.statuses()[('细节图',2)],'未拍摄')
        self.assertEqual(self.sku.state()['jobs'][0]['scope'],'模特图')
        self.assertEqual(len(self.calls),1)
        self.rate(2);self.tick();self.assertEqual(len(self.calls),1)

    def test_delayed_click_marker_overrides_later_scope_selection(self):
        self.sku.configure(self.pid,self.sid,'细节图')
        session=self.store.session(self.sid);state=copy.deepcopy(session['state']);state['overrides']={self.cids[0]:2}
        state['skuMarkers']={self.cids[0]:{'project_id':self.pid,'scope':'模特图'}}
        self.store.save(self.sid,session['revision'],state);self.sku.on_save(self.sid,session['state'],state);self.tick()
        self.assertEqual(self.sku.state()['jobs'][0]['scope'],'模特图')
        self.assertEqual(self.statuses()[('细节图',2)],'未拍摄')

    def test_two_selected_models_can_auto_resolve_one_ocr_confusion(self):
        public=self.providers.save({'name':'OCR','category':'ocr','protocol':'ocr_json','url':'http://127.0.0.1:1/ocr','model':'ocr-model','key':''})
        self.providers.select(public['providers'][-1]['id'])
        self.sku.recognizer=lambda provider,image:reading('LC7877496-P9O5',provider['model'])
        self.rate(2);self.tick();self.assertEqual(self.sku.state()['jobs'][0]['status'],'matched')
        self.assertEqual(len(self.sku.state()['jobs'][0]['result']['readings']),2)

    def test_three_star_pass_through_and_inflight_change_do_not_count(self):
        self.rate(2);self.rate(3);self.tick();self.assertEqual(self.calls,[])
        self.rate(2)
        def changed(provider,image):self.rate(3);return reading()
        self.sku.recognizer=changed;self.tick()
        self.assertTrue(all(v=='未拍摄' for v in self.statuses().values()))
        self.assertEqual(self.sku.state()['jobs'][0]['status'],'cancelled')

    def test_cancel_removes_only_own_contribution(self):
        self.rate(2);self.tick();self.rate(2,self.cids[1]);self.tick()
        self.rate(0);self.tick();self.assertEqual(self.statuses()[('模特图',2)],'已拍摄')
        self.rate(0,self.cids[1]);self.tick();self.assertEqual(self.statuses()[('模特图',2)],'未拍摄')

    def test_manual_fuzzy_review_confined_to_job_sheet(self):
        self.sku.recognizer=lambda p,b:reading('LC7877496-P9O5',confidence=.5)
        self.rate(2);self.tick();job=self.sku.state()['jobs'][0];self.assertEqual(job['status'],'review')
        other=next(r for r in self.sku.rows(self.pid) if r['sheet']=='细节图')
        with self.assertRaisesRegex(ValueError,'所属工作表'):self.sku.review(job['id'],'confirm',other['id'])
        own=next(r for r in self.sku.rows(self.pid) if r['sheet']=='模特图')
        self.sku.review(job['id'],'confirm',own['id']);self.assertEqual(self.statuses()[('模特图',2)],'已拍摄')
        self.sku.review(job['id'],'ignore');self.assertEqual(self.statuses()[('模特图',2)],'未拍摄')

    def test_wait_for_model_then_resume_and_restart(self):
        with self.providers.lock:self.providers.data['selected']={}
        self.rate(2);self.tick();self.assertEqual(self.sku.state()['jobs'][0]['status'],'waiting_model')
        self.providers.select(self.providers.public()['providers'][0]['id']);self.tick()
        self.assertEqual(self.sku.state()['jobs'][0]['status'],'matched')
        reopened=SkuService(self.store,self.providers,autostart=False)
        try:self.assertEqual(reopened.rows(self.pid)[0]['status'],'已拍摄')
        finally:reopened.close()

    def test_export_failure_does_not_erase_recognition(self):
        self.rate(2)
        with patch('sku_service.export_workbook',side_effect=PermissionError('locked')):self.tick()
        self.assertEqual(self.sku.state()['jobs'][0]['status'],'matched')
        self.assertTrue(self.sku.project()['export_error']);self.assertEqual(self.statuses()[('模特图',2)],'已拍摄')
        self.sku.export(self.pid);self.assertFalse(self.sku.project()['export_error'])

    def test_missing_scope_uses_first_sheet_without_blocking_ocr(self):
        self.sku.import_file('next.xlsx',fixture_book(),self.sid);self.rate(2);self.tick()
        self.assertEqual(len(self.calls),1);self.assertEqual(self.sku.state()['jobs'][0]['status'],'matched')
        with self.assertRaisesRegex(ValueError,'工作表'):self.sku.configure(self.sku.project()['id'],self.sid,'')


if __name__=='__main__':unittest.main()
