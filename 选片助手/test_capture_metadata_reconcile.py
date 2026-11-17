import json
import unittest
import uuid

from capture_metadata_reconcile import parse_metadata,reconcile,merge_fields


class MetadataReconcileTests(unittest.TestCase):
    def setUp(self):
        self.file,self.asset,self.group,self.scope,self.source,self.old=[uuid.uuid4().hex for _ in range(6)]
        self.current={'id':self.file,'file_id':self.file,'asset_id':self.asset,'media_type':'image','sku':'00123-A',
                      'base_sku':'00123','group_id':self.group,'group_revision':self.old,'catalog_scope':self.scope,
                      'decision_id':self.old,'decision_source_id':self.source,'parents':[],'rating':0,'role':'product',
                      'assignment_state':'confirmed','write_state':'verified'}

    def metadata(self,**overrides):
        values={'FileId':self.file,'AssetId':self.asset,'SKU':'00123-A','BaseSKU':'00123','SegmentId':self.group,
                'GroupRevision':self.old,'CatalogScopeId':self.scope,'DecisionId':self.old,'DecisionSourceId':self.source,
                'DecisionParents':'[]','MetadataSchemaVersion':'1','AssignmentState':'confirmed','PhotoRole':'product',
                'GroupProductCount':'1','GroupLabelCount':'0','GroupFileCount':'1','GroupMembersDigest':'members','GroupFilesDigest':'files'}
        values.update(overrides);full={'XMP-fk:'+k:v for k,v in values.items()};full['XMP-xmp:Rating']=0
        return full

    def test_complete_xmp_keeps_literal_sku_and_parent_chain(self):
        metadata=parse_metadata(self.metadata(DecisionParents=json.dumps([uuid.uuid4().hex])))
        self.assertEqual(metadata['kind'],'complete');self.assertEqual(metadata['patch']['sku'],'00123-A')
        self.assertEqual(reconcile(self.current,metadata)['relation'],'same')

    def test_provable_newer_xmp_updates_old_projection(self):
        new=uuid.uuid4().hex
        metadata=parse_metadata(self.metadata(SKU='00123-B',DecisionId=new,DecisionParents=json.dumps([self.old]),GroupRevision=new))
        result=reconcile(self.current,metadata,dataset_id=self.source)
        self.assertEqual(result['relation'],'file_newer');self.assertEqual(result['record']['sku'],'00123-B')

    def test_older_file_never_rolls_back_new_confirmation(self):
        current={**self.current,'decision_id':uuid.uuid4().hex,'parents':[self.old],'sku':'NEW-SKU'}
        result=reconcile(current,parse_metadata(self.metadata()))
        self.assertEqual(result['relation'],'archive_newer');self.assertEqual(result['record']['sku'],'NEW-SKU')

    def test_same_revision_different_sku_requires_source_choice(self):
        result=reconcile(self.current,parse_metadata(self.metadata(SKU='OTHER')))
        self.assertEqual(result['relation'],'conflict');self.assertIn('sku',result['fields'])

    def test_fork_or_foreign_dataset_does_not_win_by_revision_number(self):
        for parents,source in (([],self.source),([self.old],uuid.uuid4().hex)):
            metadata=parse_metadata(self.metadata(DecisionId=uuid.uuid4().hex,DecisionParents=json.dumps(parents),DecisionSourceId=source))
            self.assertEqual(reconcile(self.current,metadata,dataset_id=self.source)['relation'],'conflict')

    def test_rating_only_cannot_erase_sku(self):
        metadata=parse_metadata({'XMP-xmp:Rating':2});result=reconcile(self.current,metadata)
        self.assertEqual(result['relation'],'conflict')
        self.assertEqual(merge_fields(self.current,metadata['patch'])['sku'],'00123-A')

    def test_missing_sku_is_not_a_clear(self):
        self.assertEqual(merge_fields(self.current,{'sku':'','assignment_state':'unassigned'})['sku'],'00123-A')
        current={**self.current,'decision_id':''}
        result=reconcile(current,parse_metadata({'XMP-xmp:Rating':1}))
        self.assertEqual(result['record']['sku'],'00123-A');self.assertFalse(result['record']['boundary'])

    def test_clear_requires_explicit_state_and_valid_new_revision(self):
        new=uuid.uuid4().hex
        metadata=parse_metadata(self.metadata(SKU='',AssignmentState='cleared',DecisionId=new,DecisionParents=json.dumps([self.old])))
        self.assertEqual(reconcile(self.current,metadata,dataset_id=self.source)['record']['sku'],'')

    def test_two_stars_always_mean_boundary_even_on_product_photo(self):
        full=self.metadata();full['XMP-xmp:Rating']=2
        current={**self.current,'decision_id':''}
        record=reconcile(current,parse_metadata(full))['record']
        self.assertTrue(record['boundary']);self.assertEqual(record['role'],'sku_label')

    def test_no_or_one_star_never_creates_boundary_but_sku_is_restored(self):
        for rating in (None,0,1,3,4,5):
            full=self.metadata(PhotoRole='sku_label')
            if rating is None:full.pop('XMP-xmp:Rating')
            else:full['XMP-xmp:Rating']=rating
            record=reconcile({**self.current,'decision_id':'','rating':0},parse_metadata(full))['record']
            self.assertFalse(record['boundary']);self.assertEqual(record['sku'],'00123-A')

    def test_identity_scope_and_unknown_schema_are_not_silently_adopted(self):
        self.assertEqual(reconcile(self.current,parse_metadata(self.metadata(FileId=uuid.uuid4().hex)))['relation'],'identity')
        result=reconcile(self.current,parse_metadata(self.metadata(CatalogScopeId=uuid.uuid4().hex)))
        self.assertEqual(result['relation'],'conflict');self.assertFalse(result['can_use_file'])
        self.assertFalse(reconcile(self.current,parse_metadata(self.metadata(MetadataSchemaVersion='99')))['can_use_file'])


if __name__=='__main__':unittest.main()
