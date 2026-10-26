"""真实 ExifTool 的常驻协议、元数据兼容与中断恢复。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
import io

from PIL import Image
import media_engine as media


ROOT=Path(__file__).resolve().parents[1]


class ExifToolSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:cls.exiftool=media.find_exiftool(ROOT)
        except RuntimeError as exc:raise unittest.SkipTest(str(exc))

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.photo=self.root/'中文 空格, 现场照片.JPG'
        Image.new('RGB',(320,200),(80,120,160)).save(self.photo)
        args=['-charset','filename=UTF8','-overwrite_original','-Make=Canon','-Model=Canon EOS R6m2','-SerialNumber=123456789','-DateTimeOriginal=2026:10:20 14:15:16','-SubSecTimeOriginal=123','-Orientation#=6','-Rating=4',str(self.photo)]
        subprocess.run([str(self.exiftool),'-@','-'],input=('\n'.join(args)+'\n').encode('utf8'),check=True,capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.session=media.ExifToolSession(self.exiftool)

    def tearDown(self):
        self.session.close();self.temp.cleanup()

    def read(self):
        return media.read_metadata([self.photo],self.exiftool,session=self.session)

    def slow_request(self,cancel=None):
        # ExifTool 的条件表达式在实际子进程内等待，覆盖读取过程中断。
        return self.session.execute(['-if','sleep(3);1','-json',str(self.photo)],cancel=cancel)

    def start_slow(self,cancel=None):
        errors=[];previous=self.session._sequence
        def run():
            try:self.slow_request(cancel)
            except BaseException as exc:errors.append(exc)
        thread=threading.Thread(target=run);thread.start()
        until=time.monotonic()+2
        while self.session._sequence==previous and time.monotonic()<until:time.sleep(.005)
        self.assertIsNotNone(self.session._process)
        self.assertGreater(self.session._sequence,previous)
        return thread,errors,self.session._process,self.session._reader

    def assert_stopped(self,process,reader):
        self.assertIsNotNone(process.poll());self.assertFalse(reader.is_alive())
        self.assertTrue(process.stdin.closed);self.assertTrue(process.stdout.closed)

    def test_real_metadata_matches_one_shot_and_reuses_process(self):
        expected=media.read_metadata([self.photo],self.exiftool)
        self.assertEqual(self.read(),expected);process=self.session._process
        self.assertEqual(self.read(),expected);self.assertIs(self.session._process,process)
        row=expected[0]
        self.assertEqual(row['Rating'],4);self.assertEqual(row['Orientation'],6)
        self.assertEqual(str(row['SerialNumber']),'123456789')
        self.assertEqual(str(row['SubSecTimeOriginal']),'123')
        self.assertEqual(row['Model'],'Canon EOS R6m2')

    def test_concurrent_requests_have_independent_numbered_responses(self):
        expected=self.read();process=self.session._process
        with ThreadPoolExecutor(max_workers=4) as workers:
            results=list(workers.map(lambda _:self.read(),range(8)))
        self.assertEqual(results,[expected]*8);self.assertIs(self.session._process,process)
        self.assertEqual(self.session._sequence,9)

    def test_binary_thumbnail_and_combined_orientation_preserve_jpeg_bytes(self):
        thumbnail=self.root/'缩略图.jpg';Image.new('RGB',(80,50),'teal').save(thumbnail)
        args=['-charset','filename=UTF8','-overwrite_original','-ThumbnailImage<='+str(thumbnail),str(self.photo)]
        subprocess.run([str(self.exiftool),'-@','-'],input=('\n'.join(args)+'\n').encode('utf8'),check=True,capture_output=True,
                       creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        payload=self.session.execute(['-charset','filename=UTF8','-b','-ThumbnailImage',str(self.photo)])
        self.assertEqual(payload,thumbnail.read_bytes())
        self.assertEqual(self.read()[0]['Orientation'],6)
        try:
            for _ in range(2):
                orientation,payload=media.raw_thumbnail_metadata(self.photo,self.exiftool)
                self.assertEqual(orientation,6);self.assertEqual(payload,thumbnail.read_bytes())
                with Image.open(io.BytesIO(payload)) as image:self.assertEqual(image.size,(80,50))
        finally:media.close_preview_sessions()

    def test_cancellation_stops_inflight_process_and_next_read_recovers(self):
        self.read();cancel=threading.Event()
        thread,errors,process,reader=self.start_slow(cancel)
        cancel.set();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertEqual(len(errors),1)
        self.assertIsInstance(errors[0],media.ScanCancelled);self.assert_stopped(process,reader)
        self.assertEqual(self.read()[0]['Rating'],4);self.assertIsNot(self.session._process,process)

    def test_timeout_stops_process_and_next_read_recovers(self):
        self.read();process=self.session._process;reader=self.session._reader
        self.session.timeout=.1
        with self.assertRaisesRegex(RuntimeError,'超时'):self.slow_request()
        self.assert_stopped(process,reader);self.session.timeout=180
        self.assertEqual(self.read()[0]['Rating'],4)

    def test_close_interrupts_reader_and_allows_reopen(self):
        self.read();thread,errors,process,reader=self.start_slow()
        self.session.close();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertEqual(len(errors),1)
        self.assertIsInstance(errors[0],RuntimeError);self.assert_stopped(process,reader)
        self.session.close();self.assertEqual(self.read()[0]['Rating'],4)

    def test_cancelled_request_does_not_start_a_process(self):
        cancel=threading.Event();cancel.set()
        with self.assertRaises(media.ScanCancelled):
            media.read_metadata([self.photo],self.exiftool,cancel=cancel,session=self.session)
        self.assertIsNone(self.session._process)
        self.assertEqual(self.read()[0]['Rating'],4)


if __name__=='__main__':unittest.main()
