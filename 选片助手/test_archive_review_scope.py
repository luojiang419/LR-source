"""本次选用成员、兄弟目录复核和父子数据集权限的归档回归。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest
import uuid

from archive_review_scope import validate_archive_review_scope
import test_archive_capture_service as fixtures


class ReviewScopeTests(unittest.TestCase):
    def setUp(self):
        self.root=Path('review-fixture').absolute()
        self.selected=self.root/'10-30/one.JPG'
        self.rows=[{'relative_path':'10-30/one.JPG','asset_id':'one','group_id':'selected'},
                   {'relative_path':'11-04/other.JPG','asset_id':'other','group_id':'unrelated'}]
        self.groups=[{'id':'unrelated','status':'review','members':['other']}]
        self.dataset=SimpleNamespace(root=self.root,groups=lambda:self.groups,records_snapshot=lambda:self.rows)
        self.capture=SimpleNamespace(datasets={'parent':self.dataset},dataset_for_path=lambda p:self.dataset)
        self.plan={'items':[{'source':str(self.selected),'reference':str(self.selected)}]}

    def test_unrelated_date_review_does_not_block(self):
        validate_archive_review_scope(self.capture,self.plan)

    def test_selected_member_review_still_blocks(self):
        self.groups[0]['members'].append('one')
        with self.assertRaisesRegex(ValueError,'本次选用来源存在待复核分组'):
            validate_archive_review_scope(self.capture,self.plan)

    def test_selected_label_without_member_entry_still_blocks(self):
        self.groups[0]['label']='one'
        with self.assertRaisesRegex(ValueError,'待复核分组'):
            validate_archive_review_scope(self.capture,self.plan)

    def test_selected_record_group_link_still_blocks(self):
        self.rows[0]['inherited_group_id']='unrelated'
        with self.assertRaisesRegex(ValueError,'待复核分组'):
            validate_archive_review_scope(self.capture,self.plan)

    def test_parent_shadow_record_does_not_override_independent_child(self):
        self.groups[0]['members'].append('one')
        child=SimpleNamespace(root=self.selected.parent,groups=lambda:[],records_snapshot=lambda:[])
        self.capture.datasets['child']=child
        self.capture.dataset_for_path=lambda p:child
        validate_archive_review_scope(self.capture,self.plan)

    def test_final_source_review_is_checked_even_when_reference_is_clean(self):
        self.plan['items'][0]['reference']=str(self.root/'10-30/small.JPG')
        self.groups[0]['members'].append('one')
        with self.assertRaisesRegex(ValueError,'待复核分组'):
            validate_archive_review_scope(self.capture,self.plan)

    def test_excluded_files_are_not_selected(self):
        self.plan['excluded_records']=[{'source':str(self.root/'11-04/other.JPG')}]
        validate_archive_review_scope(self.capture,self.plan)


class ReviewExecutionTests(unittest.TestCase):
    setUp=fixtures.ArchiveCaptureTests.setUp
    tearDown=fixtures.ArchiveCaptureTests.tearDown
    client=fixtures.ArchiveCaptureTests.client
    item=fixtures.ArchiveCaptureTests.item
    drain=fixtures.ArchiveCaptureTests.drain

    def plan(self):
        self.api.confirm(self.item(1),'LC123456',whole_group=False);self.drain()
        record=self.dataset.record(path='IMG_0001.JPG');path=str(self.photos/'IMG_0001.JPG')
        return {'source':str(self.photos),'reference_root':str(self.photos),'target':str(self.base/'archive-target'),
                'items':[{'source':path,'reference':path,'sku':'LC123456','scope':'detail','rating':0,
                          'parent_sku':'LC12','metadata_fingerprint':record.get('capture_identity') or 'test-capture'}]}

    def review(self,n):
        member=self.dataset.record(path=f'IMG_{n:04d}.JPG')['asset_id']
        self.dataset.change(groups=[{'id':uuid.uuid4().hex,'status':'review','members':[member],'reason':'测试待复核'}],kind='test-review')

    def test_execution_with_unrelated_review_prepares_and_releases_target(self):
        plan=self.plan();self.review(3)
        with self.api.archive_execution(plan) as destination:
            self.assertTrue(destination.meta('archive_freeze'))
            self.assertEqual(plan['items'][0]['portable_fields']['SKU'],'LC123456')
        self.assertFalse(destination.meta('archive_freeze'))
        self.assertFalse(list((self.base/'archive-target').glob('*.JPG')))

    def test_execution_blocks_selected_review_before_any_target_creation(self):
        plan=self.plan();self.review(1)
        with self.assertRaisesRegex(ValueError,'本次选用来源存在待复核分组'):
            with self.api.archive_execution(plan):self.fail('不应进入复制阶段')
        self.assertFalse((self.base/'archive-target').exists())


if __name__=='__main__':unittest.main()
