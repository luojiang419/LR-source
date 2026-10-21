"""从最新页面实时生成候选布局；只使用隔离样片和独立数据目录。"""
from pathlib import Path
from urllib.parse import urlsplit, unquote
import os
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
PREVIEW = ROOT / 'build' / 'hardcore-workbench'
SAMPLES = PREVIEW / 'samples'
SAMPLES.mkdir(parents=True, exist_ok=True)
for photo in (ROOT / '演示方案' / 'assets').glob('*.jpg'):
    target = SAMPLES / photo.name
    if not target.exists():
        shutil.copy2(photo, target)
os.environ['FRAMEKEEP_DATA'] = str(PREVIEW / 'data')
sys.path.insert(0, str(ROOT / '选片助手'))
import server


class PreviewHandler(server.Handler):
    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if path in ('/硬核工作台/', '/硬核工作台/index.html'):
            if not self.trusted():
                return self.reply({'error': '本机 Host 不匹配'}, 403)
            # 已确认方案接入正式页面，不再注入旧预览脚本或重复移动组件。
            html = (server.WEB / 'index.html').read_text(encoding='utf-8')
            html = html.replace('<head>', '<head><base href="/">', 1)
            return self.reply(html.encode('utf-8'), kind='text/html; charset=utf-8')
        return super().do_GET()

    def do_POST(self):
        # 评级仅保存到隔离库；设备、模型、LR、导入和元数据写入不在布局演示执行。
        if urlsplit(self.path).path not in ('/api/session', '/api/export'):
            return self.reply({'error': '布局演示：此操作请在正式工作台执行。'}, 400)
        return super().do_POST()


if __name__ == '__main__':
    server.Handler = PreviewHandler
    if '--port' not in sys.argv:
        sys.argv.extend(['--port', '8795'])
    if '--folder' not in sys.argv and not (PREVIEW / 'data' / 'framekeep.sqlite3').exists():
        sys.argv.extend(['--folder', str(SAMPLES)])
    server.main()
