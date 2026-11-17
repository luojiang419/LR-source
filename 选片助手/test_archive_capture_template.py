import io
import unittest
from PIL import Image
from openpyxl import load_workbook
from archive_capture_template import workbook


class ArchiveTemplateTests(unittest.TestCase):
    def test_types_layout_images_times_and_summary_follow_progress_template(self):
        image=io.BytesIO();Image.new('RGB',(320,200),'teal').save(image,'JPEG');image=image.getvalue()
        entries=[]
        for index,kind in enumerate(['细节图','上身图','细节图']):
            entries.append(dict(id=str(index),sku='SKU001' if index<2 else '',shoot_type=kind,source='label.jpg',folder='2026-11-10',
                                added='',completed='2026-11-10T08:12:00.25+08:00' if index<2 else '',status='已拍摄' if index<2 else '未拍摄',picture=image,label=image if index<2 else None))
        book=load_workbook(io.BytesIO(workbook(entries,[['文件'],['photo.jpg']],[['项目','值']])))
        self.assertEqual(book.sheetnames,['细节图','上身图','文件明细','范围与版本','全部款号'])
        expected=['序号','款号','款号图片','标签图片','来源文件','现场文件夹','入库时间','拍摄完成时间','拍摄状态']
        for kind in ['细节图','上身图']:
            table=book[kind];self.assertEqual([c.value for c in table[1]],expected)
            self.assertEqual(table['A2'].value,1);self.assertEqual(table['B2'].value,'SKU001')
            self.assertEqual(table['H2'].value,'2026-11-10 08:12:00.25');self.assertIsNone(table['G2'].value)
            for column in (3,4):
                width=next(d.width for d in table.column_dimensions.values() if d.min<=column<=d.max)
                self.assertEqual(width,19)
            self.assertEqual(table.row_dimensions[2].height,76)
            self.assertEqual({image.anchor._from.col for image in table._images},{2,3})
        self.assertEqual(book['细节图']['A3'].value,2)
        self.assertEqual(book['细节图']['I3'].value,'未拍摄');self.assertFalse(book['细节图']['H3'].value)
        self.assertEqual(book['全部款号'].max_row,2)
        book.close()

    def test_text_is_not_exported_as_formulas(self):
        entry=dict(id='one',sku='=1+2',shoot_type='现场记录',source='=1+3',folder='=1+4',added='',completed='',status='已拍摄',picture=None,label=None)
        book=load_workbook(io.BytesIO(workbook([entry],[['文件'],['=1+5']],[['项目','值']])))
        for cell in [book['现场记录']['B2'],book['现场记录']['E2'],book['现场记录']['F2'],book['文件明细']['A2']]:self.assertEqual(cell.data_type,'s')
        book.close()
