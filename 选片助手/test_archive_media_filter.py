"""资产归档只列出媒体与 Adobe 素材，文档／伴随文件仍完整保留。"""
import unittest

import test_archive_visits as fixtures


class ArchiveMediaFilterTests(unittest.TestCase):
    setUp=fixtures.ArchiveVisitTests.setUp
    tearDown=fixtures.ArchiveVisitTests.tearDown

    def test_media_whitelist_keeps_photos_raw_video_psd_psb_and_folders(self):
        folder=self.photos/'A'
        visible=('image.PNG','raw.CR3','video.MOV','source.PSD','large.PsB','drawing.ai')
        hidden=('photo.xmp','notes.doc','notes.docx','sheet.xlsx','说明.txt','cache.json','archive.zip','unknown.bin','no-extension','document.pdf','layout.indd')
        originals={}
        for name in (*visible,*hidden):
            content=('保留内容：'+name).encode('utf8');(folder/name).write_bytes(content);originals[name]=content
        (folder/'只有文档的子目录').mkdir()
        result=self.visits.explorer.browse(self.rid,'A')
        self.assertEqual({r['name'] for r in result['rows']},{'photo.jpg','只有文档的子目录',*visible})
        self.assertEqual((result['files'],result['folders'],result['total']),(7,1,8))
        for name,content in originals.items():self.assertEqual((folder/name).read_bytes(),content)
        types={r['name']:r['media_type'] for r in result['rows']}
        self.assertEqual((types['raw.CR3'],types['video.MOV'],types['source.PSD'],types['large.PsB']),('image','video','design','design'))

    def test_document_only_folder_has_zero_media_count_without_deleting_files(self):
        folder=self.photos/'documents';folder.mkdir()
        for name in ('photo.xmp','资料.doc'): (folder/name).write_text('保留',encoding='utf8')
        result=self.visits.explorer.browse(self.rid,'documents')
        self.assertEqual((result['rows'],result['files'],result['total']),([],0,0))
        self.assertEqual(len(list(folder.iterdir())),2)

    def test_existing_index_and_pagination_search_exclude_non_visual_design_documents(self):
        folder=self.photos/'A'
        for name in ('image.psd','large.psb','document.pdf','layout.indd'):(folder/name).write_bytes(b'fixture')
        self.visits.touch(self.rid,'A');fixtures.ArchiveVisitTests.wait(self)
        unfiltered={r['name'] for r in self.catalog.items(root=self.rid,folder='A')['rows']}
        self.assertIn('document.pdf',unfiltered)  # 保留原索引及其它调用方兼容性。
        for sort in ('original','modified'):
            for quick in (False,True):
                with self.subTest(sort=sort,quick=quick):
                    first=self.catalog.items(root=self.rid,folder='A',sort=sort,quick=quick,visual_only=True,limit=1)
                    names=[r['name'] for r in first['rows']];cursor=first['next_cursor'];more=first['has_more']
                    while more:
                        page=self.catalog.items(root=self.rid,folder='A',sort=sort,quick=quick,visual_only=True,limit=1,cursor=cursor)
                        names.extend(r['name'] for r in page['rows']);cursor=page['next_cursor'];more=page['has_more']
                    self.assertEqual(set(names),{'photo.jpg','image.psd','large.psb'})
                    self.assertEqual(len(names),3)
                    for text in ('document','layout'):
                        result=self.catalog.items(root=self.rid,folder='A',sort=sort,text=text,visual_only=True,include_tree=True)
                        self.assertEqual(result['rows'],[])

    def test_startup_snapshot_does_not_restore_document_rows(self):
        rows=[{'id':'image','root':self.rid,'folder':'A','name':'photo.jpg','extension':'.jpg'},
              {'id':'pdf','root':self.rid,'folder':'A','name':'document.pdf','extension':'.pdf'}]
        from unittest.mock import patch
        startup=self.catalog.startup;key=startup.page_key(self.rid,'A')
        with patch.object(startup,'_read',side_effect=lambda name:{'rows':rows,'total':2} if name==key else None):
            result=startup.read(self.rid,'A')['items']
        self.assertEqual([r['id'] for r in result['rows']],['image'])
        self.assertIsNone(result['total'])


if __name__=='__main__':unittest.main()
