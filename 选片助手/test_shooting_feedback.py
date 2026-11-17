import unittest
from unittest.mock import patch
import test_ocr_metadata as fixture
from shooting_feedback import feedback


class ShootingFeedbackTests(unittest.TestCase):
    setUp=fixture.OcrMetadataTests.setUp
    tearDown=fixture.OcrMetadataTests.tearDown
    parse=fixture.OcrMetadataTests.parse
    choose=fixture.OcrMetadataTests.choose

    def test_pending_does_not_require_ocr_or_full_catalog(self):
        session=self.store.session(self.sid)
        state={**session['state'],'overrides':{self.ids[1]:2}}
        self.store.save(self.sid,session['revision'],state)
        original=self.store.records
        def bounded(*args,**kwargs):
            self.assertIsNotNone(kwargs.get('ids'),'反馈只能按标签ID查索引')
            return original(*args,**kwargs)
        with patch.object(self.store,'records',side_effect=bounded):
            state=feedback(self.sku,self.sid)
        self.assertEqual((state['total'],state['pending'],state['confirmed']),(1,1,0))

    def test_confirmed_and_withdrawn_are_not_counted_twice(self):
        self.parse();self.choose()
        state=feedback(self.sku,self.sid)
        self.assertEqual(state['confirmed'],1)
        total=state['total']
        session=self.store.session(self.sid)
        self.store.save(self.sid,session['revision'],{**session['state'],'overrides':{**session['state']['overrides'],self.ids[1]:0}})
        self.assertEqual(feedback(self.sku,self.sid)['total'],total-1)

    def test_two_star_details_do_not_expand_all_intervals(self):
        self.parse()
        with patch('sku_library.interval_rows',side_effect=AssertionError('二星不应构造全目录区间')):
            details=self.sku.ocr_details(self.sid,self.ids[1])
        self.assertEqual(details['target_capture_id'],self.ids[1])

if __name__=='__main__':unittest.main()
