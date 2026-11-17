"""真实相机字段映射的身份保护回归，不依赖网络或原片。"""
import unittest
from sku_folder_archive import camera_metadata, capture_key


class CaptureIdentityTests(unittest.TestCase):
    def setUp(self):
        self.jpg={'IFD0:Model':'Canon EOS R6m2','ExifIFD:SerialNumber':364022008657,
                  'ExifIFD:DateTimeOriginal':'2026:10:20 18:30:45','ExifIFD:SubSecTimeOriginal':8,
                  'Composite:SubSecDateTimeOriginal':'2026:10:20 18:30:45.08+08:00',
                  'Canon:ShutterCount':116348,'Canon:ImageUniqueID':'jpg-component-id'}

    def test_raw_track_shutter_count_matches_without_component_unique_id(self):
        raw={**self.jpg,'Track4:ShutterCount':116348,'Canon:ImageUniqueID':'raw-component-id'}
        del raw['Canon:ShutterCount']
        self.assertEqual(capture_key(camera_metadata(self.jpg)),capture_key(camera_metadata(raw)))

    def test_composite_time_preserves_leading_subsecond_zero(self):
        altered={**self.jpg,'Composite:SubSecDateTimeOriginal':'2026:10:20 18:30:45.8+08:00'}
        self.assertNotEqual(capture_key(camera_metadata(self.jpg)),capture_key(camera_metadata(altered)))

    def test_same_second_burst_with_different_shutter_counter_is_not_same_photo(self):
        altered={**self.jpg,'Canon:ShutterCount':116349}
        self.assertNotEqual(capture_key(camera_metadata(self.jpg)),capture_key(camera_metadata(altered)))

    def test_filename_and_file_size_do_not_enter_identity(self):
        altered={**self.jpg,'System:FileName':'renamed-original.CR3','System:FileSize':9999999}
        self.assertEqual(capture_key(camera_metadata(self.jpg)),capture_key(camera_metadata(altered)))

    def test_missing_camera_serial_blocks_auto_match(self):
        altered={k:v for k,v in self.jpg.items() if k!='ExifIFD:SerialNumber'}
        with self.assertRaises(ValueError):capture_key(camera_metadata(altered))

    def test_different_camera_does_not_match_same_time_and_number(self):
        altered={**self.jpg,'ExifIFD:SerialNumber':364022008658}
        self.assertNotEqual(capture_key(camera_metadata(self.jpg)),capture_key(camera_metadata(altered)))


if __name__=='__main__':unittest.main()
