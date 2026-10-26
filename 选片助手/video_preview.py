"""FFmpeg 只读视频封面；帧号从 1 计数，与归档预览缓存共享容量。"""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

from PIL import Image
from file_safety import atomic_write

DEFAULT_FRAME = 3
MAX_FRAME = 1000
from preview_cache import WARM_WORKERS
_VIDEO_SLOTS = threading.BoundedSemaphore(WARM_WORKERS+2)


def validate_frame(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError('视频封面帧序号必须为 1–1000 的整数')
    try: number = int(value)
    except (ValueError, TypeError): raise ValueError('视频封面帧序号必须为 1–1000 的整数') from None
    if not 1 <= number <= MAX_FRAME: raise ValueError('视频封面帧序号必须为 1–1000 的整数')
    return number


def find_tool(name, app_root=None):
    root = Path(app_root) if app_root else Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
    resource = Path(getattr(sys, '_MEIPASS', root))
    filename = name + ('.exe' if os.name == 'nt' else '')
    for folder in (root / 'runtime/ffmpeg', root.parent / 'runtime/ffmpeg', root / '.runtime/ffmpeg', resource / 'runtime/ffmpeg'):
        if (folder / filename).is_file(): return str(folder / filename)
    return shutil.which(name)


class VideoPreview:
    def __init__(self, data, app_root=None):
        self.path = Path(data) / 'video-preview.json'
        self.app_root = app_root
        self.lock = threading.RLock()

    @property
    def frame(self):
        try: return validate_frame(json.loads(self.path.read_text(encoding='utf8'))['frame_number'])
        except (OSError, ValueError, KeyError, TypeError): return DEFAULT_FRAME

    def state(self):
        ffmpeg, ffprobe = find_tool('ffmpeg', self.app_root), find_tool('ffprobe', self.app_root)
        from gpu_video import state
        return {'frame_number': self.frame, 'max_frame': MAX_FRAME, 'ready': bool(ffmpeg),'gpu':state(),
                'ffmpeg': ffmpeg or '', 'ffprobe': ffprobe or '',
                'message': '视频解码组件已就绪' if ffmpeg else '未找到 FFmpeg，请部署视频解码组件'}

    def configure(self, value):
        frame = validate_frame(value)
        with self.lock:
            atomic_write(self.path, json.dumps({'frame_number': frame}).encode('utf8'))
        return self.state()


def _run(command, timeout=30):
    try:
        return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired: raise RuntimeError('视频封面读取超时，可切换素材或选择更靠前的帧') from None


def extract_frame(source, size, frame, app_root=None):
    frame = validate_frame(frame)
    ffmpeg = find_tool('ffmpeg', app_root)
    if not ffmpeg: raise RuntimeError('未找到 FFmpeg，请先部署视频解码组件')
    edge = max(160, min(2048, int(size)))

    def extract(number):
        from gpu_video import extract_frame as gpu_extract
        payload=gpu_extract(source,edge,number,ffmpeg,find_tool('ffprobe',app_root))
        if payload:return payload
        filters = f"select=eq(n\\,{number-1}),scale=w='min({edge},iw)':h='min({edge},ih)':force_original_aspect_ratio=decrease"
        result = _run([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1',
                       '-probesize', '1048576', '-analyzeduration', '1000000', '-fpsprobesize', '0',
                       '-i', str(source), '-map', '0:v:0', '-an', '-sn', '-dn', '-vf', filters,
                       '-filter_threads', '1', '-frames:v', '1', '-fps_mode', 'vfr', '-f', 'image2pipe', '-threads', '1', '-c:v', 'png', 'pipe:1'])
        if result.returncode:
            detail = result.stderr.decode('utf8', errors='replace').strip()[-400:]
            raise RuntimeError('视频无法解码，请检查素材是否完整或编码是否受支持：' + detail)
        return result.stdout

    with _VIDEO_SLOTS:
        payload = extract(frame)
        if not payload:
            # 仅在视频不足所选帧数时计数，正常提取不扫描整段视频。
            ffprobe = find_tool('ffprobe', app_root)
            if ffprobe:
                result = _run([ffprobe, '-v', 'error', '-select_streams', 'v:0', '-count_frames',
                               '-show_entries', 'stream=nb_read_frames', '-of', 'json', str(source)])
                try: count = int(json.loads(result.stdout)['streams'][0]['nb_read_frames'])
                except (ValueError, KeyError, IndexError, TypeError): count = 0
                if 0 < count < frame: payload = extract(count)
            if not payload: raise RuntimeError('视频中没有可用画面，或不足所选封面帧数')
        with Image.open(io.BytesIO(payload)) as image:
            return image.convert('RGB')
