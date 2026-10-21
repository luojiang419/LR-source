"""从当前工作区冻结源码后启动隔离演示，不读取生产数据库和模型密钥。"""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

PROJECT = Path(__file__).resolve().parents[2]
OVERLAY = Path(__file__).resolve().parent
BASE = PROJECT / 'build/archive-v2'


def prepare():
    source = BASE / 'source'
    manifest = {}
    for directory in ('选片助手', '演示方案'):
        target = source / directory
        target.mkdir(parents=True, exist_ok=True)
        for path in (PROJECT / directory).iterdir():
            if path.is_file() and path.suffix in ('.py', '.js', '.css', '.html'):
                shutil.copy2(path, target / path.name)
                manifest[f'{directory}/{path.name}'] = hashlib.sha256(path.read_bytes()).hexdigest()
    for path in (PROJECT / '选片助手/mouse_native').rglob('*.py'):
        relative = path.relative_to(PROJECT)
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        manifest[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for path in (PROJECT / '更新系统').rglob('*.py'):
        relative = path.relative_to(PROJECT)
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        manifest[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    shutil.copy2(PROJECT / '更新系统/update-config.json', source / '更新系统/update-config.json')
    manifest['更新系统/update-config.json'] = hashlib.sha256((PROJECT / '更新系统/update-config.json').read_bytes()).hexdigest()
    runtime = source / '.runtime/local-inference'
    runtime.mkdir(parents=True, exist_ok=True)
    config = PROJECT / '.runtime/local-inference/local-models.json'
    if config.exists():
        shutil.copy2(config, runtime / config.name)
    assets = source / '演示方案/assets'
    assets.mkdir(parents=True, exist_ok=True)
    for path in (PROJECT / '演示方案/assets').iterdir():
        if path.is_file() and path.suffix in ('.js', '.svg', '.woff', '.woff2'):
            shutil.copy2(path, assets / path.name)
            manifest[f'演示方案/assets/{path.name}'] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ('archive_engine.py', 'serve_demo.py'):
        shutil.copy2(OVERLAY / name, source / '选片助手' / name)
    shutil.copy2(OVERLAY / 'workspace.js', source / '演示方案/asset-library.js')
    shutil.copy2(OVERLAY / 'workspace.css', source / '演示方案/archive-v2.css')
    index = source / '演示方案/index.html'
    index.write_text(index.read_text(encoding='utf8').replace('</head>', '<link rel="stylesheet" href="archive-v2.css"></head>'), encoding='utf8')
    samples = BASE / 'samples/2026秋冬/棚拍A'
    samples.mkdir(parents=True, exist_ok=True)
    inputs = [('001.jpg', 'local-model-validation/XSY_5194.jpg'), ('002.jpg', 'local-model-validation/XSY_5234.jpg'),
              ('003-实拍标签.jpg', 'ocr-validation/label.jpg'), ('004.jpg', 'local-model-validation/XSY_5294.jpg'),
              ('005.jpg', 'local-model-validation/XSY_5378.jpg'), ('006.jpg', 'local-model-validation/XSY_5458.jpg')]
    provenance = []
    for name, original in inputs:
        origin = PROJECT / 'build' / original
        if not origin.exists():
            continue
        target = samples / name
        if not target.exists():
            shutil.copy2(origin, target)
        provenance.append({'reference': str(origin), 'sample': str(target), 'source_sha256': hashlib.sha256(origin.read_bytes()).hexdigest()})
    # 仅用于展示第二个区间；不是实拍识别准确率样本。
    from PIL import Image, ImageDraw, ImageFont
    label = samples / '007-测试标签.jpg'
    if not label.exists():
        image = Image.new('RGB', (1200, 800), '#eeece5')
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 68)
        draw.text((95, 200), 'SKU: DEMO-AW2602', fill='#181818', font=font)
        draw.text((95, 360), 'TEST LABEL / END', fill='#545454', font=font)
        image.save(label, quality=95)
    manifest_path = BASE / 'source-manifest.json'
    manifest_path.write_text(json.dumps({'base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=PROJECT, text=True).strip(),
        'includes_working_changes': True, 'files': manifest, 'sample_provenance': provenance}, ensure_ascii=False, indent=2), encoding='utf8')
    return source


def main():
    BASE.mkdir(parents=True, exist_ok=True)
    port = 8786
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/archive-v2/state', timeout=2) as response:
            state = json.load(response)
        if state.get('demo') == 'archive-v2' and Path(state['data']).resolve() == (BASE / 'data').resolve():
            print(f'演示已运行：http://127.0.0.1:{port}/', flush=True)
            return
        raise RuntimeError('演示端口被其他服务使用')
    except OSError:
        pass
    source = prepare()
    env = os.environ.copy()
    env.update(FRAMEKEEP_DATA=str(BASE / 'data'), ARCHIVE_V2_BASE=str(BASE), ARCHIVE_V2_ORIGINAL=str(PROJECT))
    for key in ('FRAMEKEEP_CONFIG', 'FRAMEKEEP_APP_ROOT', 'FRAMEKEEP_DESKTOP_PID', 'FRAMEKEEP_DESKTOP_TOKEN'):
        env.pop(key, None)
    log = (BASE / 'server.log').open('ab')
    process = subprocess.Popen([sys.executable, '-X', 'utf8', str(source / '选片助手/serve_demo.py'), '--port', str(port), '--no-browser'],
        cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    log.close()
    (BASE / 'server.pid').write_text(str(process.pid), encoding='ascii')
    until = time.monotonic() + 25
    while time.monotonic() < until:
        if process.poll() is not None:
            raise RuntimeError('演示启动失败，请查看 build/archive-v2/server.log')
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/archive-v2/state', timeout=2) as response:
                if json.load(response).get('demo') == 'archive-v2':
                    break
        except OSError:
            time.sleep(.25)
    else:
        raise RuntimeError('演示未在规定时间内就绪，请查看 server.log')
    print(f'隔离演示已启动：http://127.0.0.1:{port}/；PID {process.pid}', flush=True)


if __name__ == '__main__':
    main()
