"""验证真实 NVDEC/NVENC 路径与发送首字节前的自动回退。"""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import urlopen

import gpu_video as gpu
from test_video_preview import make_clip
import test_video_playback as playback_fixtures
from video_preview import find_tool


class GpuVideoTests(unittest.TestCase):
    def require_gpu(self):
        if not gpu.capabilities(find_tool('ffmpeg'))['ready']:self.skipTest('需要可实际编码的 CUDA/NVENC')

    def test_real_standard_and_professional_posters_use_gpu_and_keep_sources(self):
        self.require_gpu()
        with tempfile.TemporaryDirectory() as temporary:
            before=gpu.state()['gpu_previews']
            for codec in ('libx264','libx265','prores_ks','dnxhd'):
                source=Path(temporary)/(codec+'.mov');make_clip(source,codec);digest=hashlib.sha256(source.read_bytes()).hexdigest()
                payload=gpu.extract_frame(source,160,3,find_tool('ffmpeg'),find_tool('ffprobe'))
                self.assertTrue(payload,codec);self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),digest)
            self.assertEqual(gpu.state()['gpu_previews']-before,4)

    def test_real_playback_selects_hardware_decode_and_professional_upload(self):
        self.require_gpu();fixture=playback_fixtures.VideoPlaybackTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        for codec,expected in [('libx264','cuda-decode-scale-nvenc'),('prores_ks','cuda-scale-nvenc')]:
            source=fixture.photos/(codec+'.mov');make_clip(source,codec);row=fixture.indexed(source);route,session=fixture.stream_url(row)
            with urlopen(fixture.url+route,timeout=20) as response:payload=response.read()
            info=fixture.probe(payload);self.assertEqual(info['streams'][0]['codec_name'],'h264')
            fixture.wait(lambda:fixture.catalog.playback.active_count()==0)
            self.assertEqual(fixture.catalog.playback.state(session)['backend'],expected)

    def test_failed_gpu_initialization_retries_cpu_before_any_response_bytes(self):
        fixture=playback_fixtures.VideoPlaybackTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        source=fixture.photos/'fallback.mov';make_clip(source,'libx264');row=fixture.indexed(source)
        with patch('gpu_video.enabled',return_value=False):software=gpu.playback_candidates(source,0,find_tool('ffmpeg'),find_tool('ffprobe'))[-1]
        tool,inputs,filters,encoder,_=software
        bad=(tool,inputs,['-vf','framekeep_missing_gpu_filter'],encoder,'cuda-failure')
        with patch('gpu_video.playback_candidates',return_value=[bad,software]):
            route,session=fixture.stream_url(row)
            with urlopen(fixture.url+route,timeout=20) as response:payload=response.read()
        info=fixture.probe(payload);self.assertEqual(info['streams'][0]['codec_name'],'h264')
        fixture.wait(lambda:fixture.catalog.playback.active_count()==0)
        state=fixture.catalog.playback.state(session);self.assertEqual(state['backend'],'cpu');self.assertEqual(state['fallbacks'],1)

    def test_cpu_mode_does_not_probe_or_reserve_gpu(self):
        with patch('gpu_video.enabled',return_value=False),patch('gpu_video.run',side_effect=AssertionError('关闭 GPU 不应探测驱动')):
            candidates=gpu.playback_candidates('not-read.mov',0,'ffmpeg','ffprobe')
            self.assertEqual(len(candidates),1);self.assertEqual(candidates[0][-1],'cpu')


if __name__=='__main__':unittest.main()
