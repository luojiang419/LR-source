"""可重建外观向量与独立 Chinese-CLIP 运行时；不导入受保护程序代码。"""
import base64
import io
import json
import importlib.util
import os
import sys
import math
from pathlib import Path
import queue
import subprocess
import threading
import time

from PIL import Image, ImageOps

APPEARANCE = 'appearance-v1:192'
CLIP = 'chinese-clip-large-336:768'
DEFAULT_MATERIAL = r'D:\Program Files\MaterialSearchWindows'


def unit(values):
    size = math.sqrt(sum(x*x for x in values))
    if not size or not all(math.isfinite(x) for x in values):
        raise ValueError('图像向量无效')
    return [x/size for x in values]


def appearance(payload):
    """8×8 区域 RGB 外观特征，仅表示布局/配色，不声称语义理解。"""
    with Image.open(io.BytesIO(payload)) as source:
        if source.width*source.height > 80_000_000:
            raise ValueError('参考图像像素过大')
        image = ImageOps.exif_transpose(source).convert('RGB').resize((8, 8))
        values = [value/255 for value in image.tobytes()]
    return unit(values)


def decode_upload(value):
    if not isinstance(value,str):raise ValueError('参考图片格式无效')
    if value.startswith('data:'):
        value = value.split(',', 1)[-1]
    try:
        payload = base64.b64decode(value, validate=True)
        with Image.open(io.BytesIO(payload)) as source:
            source.draft('RGB',(1600,1600));source.thumbnail((1600,1600))
            image = ImageOps.exif_transpose(source).convert('RGB')
            output = io.BytesIO();image.save(output, 'JPEG', quality=88)
            return output.getvalue()
    except (OSError, ValueError) as exc:
        raise ValueError('参考图片无法解码，请检查文件是否完整') from exc


class ClipRuntime:
    """长驻单进程、串行推理、有超时的行协议；模型只从本机加载。"""
    def __init__(self, root=DEFAULT_MATERIAL):
        self.root = Path(root);self.lock = threading.Lock();self.process = None
        self.lines = queue.Queue();self.error = '';self.log = None

    def available(self):
        hub = self.root/'huggingface/hub/models--OFA-Sys--chinese-clip-vit-large-patch14-336px/snapshots'
        return (self.root/'python.exe').is_file() and hub.is_dir() and any(hub.glob('*/config.json'))

    def _start(self):
        if self.process and self.process.poll() is None:
            return
        if not self.available():
            raise ValueError('未发现本机 Chinese-CLIP 模型，请设置 MaterialSearch 安装目录')
        self.lines = queue.Queue()
        python = sys.executable if not getattr(sys,'frozen',False) and all(importlib.util.find_spec(name) for name in ('torch','transformers')) else str(self.root/'python.exe')
        command = [python, '-u', str(Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'clip_worker.py'), str(self.root)]
        env=os.environ.copy();env.update(USE_TF='0',USE_FLAX='0',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false')
        self.process = subprocess.Popen(command, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding='utf8',
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        process = self.process;lines = self.lines
        def reader():
            for line in process.stdout:
                try:lines.put(json.loads(line))
                except ValueError:continue
            lines.put({'error': 'CLIP 运行时已退出'})
        threading.Thread(target=reader, daemon=True, name='clip-response').start()

    def encode(self, *, image=None, text=None):
        with self.lock:
            self._start()
            try:
                payload = {'image': base64.b64encode(image).decode()} if image else {'text': text}
                self.process.stdin.write(json.dumps(payload)+'\n');self.process.stdin.flush()
                deadline=time.monotonic()+180
                while True:
                    result=self.lines.get(timeout=max(.01,deadline-time.monotonic()))
                    if not result.get('stage'):break
                    self.error='CLIP: '+result['stage']
                if result.get('error'):
                    raise ValueError(str(result['error'])[:400])
                values = result['vector']
                if len(values) != 768:
                    raise ValueError('CLIP 向量维数不匹配')
                return unit(values)
            except (queue.Empty, OSError, KeyError, TypeError) as exc:
                self.close();raise ValueError('CLIP 推理失败或超时（'+self.error+'）') from exc

    def close(self):
        if self.process:
            process = self.process;self.process = None
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            for stream in (process.stdin, process.stdout):
                if stream:stream.close()
