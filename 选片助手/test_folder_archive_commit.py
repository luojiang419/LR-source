import base64
import json
from pathlib import Path
import tempfile
import unittest

from folder_archive_commit import prepare,recover


class CommitRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.stage=self.root/'stage';self.stage.mkdir();self.dest=self.root/'target';self.dest.mkdir()
        self.raw=self.stage/'photo.CR3';self.raw.write_bytes(b'raw-data')
        self.xmp=self.stage/'photo.xmp';self.xmp.write_bytes(b'<xmp>verified</xmp>')
        self.row={'destination':str(self.dest/'photo.CR3'),'xmp':str(self.dest/'photo.xmp')}
        self.log,self.entry=prepare(self.root/'log',self.row,self.raw,self.xmp)

    def test_interruption_between_raw_and_xmp_restores_verified_sidecar(self):
        self.raw.rename(self.row['destination'])
        receipt=recover(self.root/'log',self.dest)
        self.assertEqual(Path(self.row['xmp']).read_bytes(),self.xmp.read_bytes())
        self.assertEqual(receipt[self.row['destination']]['archived_at'],self.entry['archived_at'])

    def test_external_sidecar_change_is_not_overwritten(self):
        self.raw.rename(self.row['destination']);Path(self.row['xmp']).write_bytes(b'user-edit')
        with self.assertRaisesRegex(ValueError,'外部修改'):recover(self.root/'log',self.dest)
        self.assertEqual(Path(self.row['xmp']).read_bytes(),b'user-edit')

    def test_no_committed_picture_does_not_invent_success(self):
        self.assertEqual(recover(self.root/'log',self.dest),{})
        self.assertFalse(Path(self.row['destination']).exists())
        self.assertEqual(json.loads(self.log.read_text(encoding='utf8'))['state'],'not_committed')

    def test_video_full_filename_sidecar_is_recoverable_and_repeatable(self):
        video=self.dest/'clip.mp4';video.write_bytes(b'video-data')
        row={'destination':str(video),'xmp':str(video)+'.xmp'}
        prepare(self.root/'video-log',row,video,self.xmp)
        first=recover(self.root/'video-log',self.dest)
        second=recover(self.root/'video-log',self.dest)
        self.assertEqual(first,second)
        self.assertEqual(Path(row['xmp']).read_bytes(),self.xmp.read_bytes())
        self.assertEqual(video.read_bytes(),b'video-data')


if __name__=='__main__':unittest.main()
