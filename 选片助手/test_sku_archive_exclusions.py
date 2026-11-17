"""未联机入库文件排除：区分无记录、身份缺失和归属冲突。"""
from contextlib import closing
from pathlib import Path
import io,json,sqlite3,tempfile,unittest
from unittest.mock import patch
from PIL import Image
from openpyxl import load_workbook
from metadata_writer import MetadataWriter,sha
from sku_folder_archive import SkuFolderArchive,camera_metadata,capture_key
from folder_archive_report import build_report

ROOT=Path(__file__).resolve().parents[1]
class ExclusionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.small=self.root/'small';self.final=self.root/'final';self.target=self.root/'target'
        for p in (self.small,self.final,self.target):p.mkdir()
        self.writer=MetadataWriter(ROOT,self.root);self.addCleanup(self.writer.close)
        self.db=self.root/'sources.sqlite3'
        base={'IFD0:Model':'Canon EOS R6m2','ExifIFD:SerialNumber':'123456','ExifIFD:DateTimeOriginal':'2026:11:04 11:36:02','Composite:SubSecDateTimeOriginal':'2026:11:04 11:36:02.57','Canon:ShutterCount':1234}
        self.kept=base;self.unknown={**base,'Canon:ShutterCount':1235}
        Image.new('RGB',(30,40),'red').save(self.small/'kept.JPG')
        (self.final/'kept.JPG').write_bytes((self.small/'kept.JPG').read_bytes())
        Image.new('RGB',(30,40),'blue').save(self.final/'unknown.JPG');(self.final/'unknown.CR3').write_bytes(b'untouched raw')
        with closing(sqlite3.connect(self.db)) as c,c:
            c.execute('CREATE TABLE captures(id TEXT,source TEXT,fingerprint TEXT,sku TEXT,scope TEXT,rating INTEGER,metadata TEXT,asset_id TEXT)')
            c.execute('INSERT INTO captures VALUES (?,?,?,?,?,?,?,?)',('one',str(self.small/'kept.JPG'),capture_key(camera_metadata(base)),'LC7877847','细节图',0,json.dumps(camera_metadata(base)),''))
        real=self.writer.read
        def read(path,full=False):
            if isinstance(path,bytes):return {**real(path,full=full),**self.kept}
            value=self.unknown if Path(path).stem.startswith('unknown') else self.kept
            return {**real(path,full=full),**value} if Path(path).suffix.lower()!='.cr3' else dict(value)
        self.read_patch=patch.object(self.writer,'read',side_effect=read);self.read_patch.start();self.addCleanup(self.read_patch.stop)
        batch=patch('sku_folder_archive.capture_metadata_batches',side_effect=lambda writer,files,**options:((p,writer.read(p,full=True)) for p in files));batch.start();self.addCleanup(batch.stop)
        self.core=SkuFolderArchive(self.writer,self.db)

    def test_pair_exclusion_counts_and_audit_workbook(self):
        before={str(p):sha(p) for p in self.final.iterdir()}
        plan=self.core.plan(self.small,self.final,self.target,preprocess=False)
        self.assertEqual(plan['final_inventory_count'],3);self.assertEqual(plan['final_expected'],1)
        self.assertEqual(plan['expected'],1);self.assertEqual(plan['excluded_count'],2)
        self.assertEqual(len({r['metadata_fingerprint'] for r in plan['excluded_records']}),1)
        result=self.core.execute(plan)
        self.assertEqual(result['verified'],1);self.assertEqual(result['excluded'],2)
        self.assertEqual(before,{str(p):sha(p) for p in self.final.iterdir()})
        self.assertFalse(list(self.target.rglob('unknown.*')))
        content,_=build_report(result);book=load_workbook(io.BytesIO(content));sheet=book['未入库排除记录']
        self.assertEqual(sheet.max_row,7)
        for row in (6,7):
            self.assertEqual(sheet.cell(row,8).value,'未入库，按规则排除')
            for col in (1,2,3,5,7,9):self.assertFalse(sheet.cell(row,col).value)
            self.assertIn('unknown.',sheet.cell(row,6).value)

    def test_disabled_rule_still_blocks_unknown(self):
        with self.assertRaisesRegex(ValueError,'未唯一匹配'):self.core.plan(self.small,self.final,self.target,exclude_unregistered=False)

    def test_incomplete_identity_is_not_excluded(self):
        self.unknown.pop('ExifIFD:SerialNumber')
        with self.assertRaisesRegex(ValueError,'缺少可确定快门身份'):self.core.plan(self.small,self.final,self.target)

    def test_conflicting_sku_is_not_excluded(self):
        with closing(sqlite3.connect(self.db)) as c,c:
            c.execute("INSERT INTO captures SELECT 'two',source,fingerprint,'LC7979103',scope,rating,metadata,asset_id FROM captures")
        with self.assertRaisesRegex(ValueError,'未唯一匹配'):self.core.plan(self.small,self.final,self.target)

    def test_existing_embedded_sku_is_not_silently_excluded(self):
        self.unknown['XMP-fk:SKU']='LC7979103'
        with self.assertRaisesRegex(ValueError,'未唯一匹配'):self.core.plan(self.small,self.final,self.target)

    def test_preprocessing_leaves_excluded_files_in_place(self):
        before={str(p):sha(p) for p in self.final.glob('unknown.*')}
        plan=self.core.plan(self.small,self.final,self.target)
        self.core.execute(plan)
        self.assertEqual(before,{str(p):sha(p) for p in self.final.glob('unknown.*')})

if __name__=='__main__':unittest.main()
