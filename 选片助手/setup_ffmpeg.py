"""部署固定版本的 FFmpeg/ffprobe 到项目运行时，不修改系统 PATH。"""
import hashlib
from pathlib import Path
import shutil
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = '9.0.2'
URL = f'https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-{VERSION}-essentials_build.zip'
SHA256 = '60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba'
RUNTIME_SHA256 = {
    'ffmpeg.exe': '3256173f3f8bffd7df12227c68adf68025edb1832273a9530688a7bb1ed8edec',
    'ffprobe.exe': 'f0d36ecbbdd3bcfac3efa078c96c7271c2e68b3810595552ac3b7f17e9a65c52',
    'LICENSE': '8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903',
}


def verify_runtime(target):
    import json
    target = Path(target)
    try:
        metadata = json.loads((target / 'source.json').read_text(encoding='utf8'))
        if metadata['version'] != VERSION or metadata['archive_sha256'] != SHA256: return False
        for name, expected in RUNTIME_SHA256.items():
            if metadata['files'].get(name) != expected: return False
            with (target / name).open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != expected: return False
        return True
    except (OSError, ValueError, KeyError, TypeError): return False


def install():
    target = ROOT / '.runtime/ffmpeg'
    if verify_runtime(target):
        print('FFmpeg / ffprobe ' + VERSION + ' 已部署，SHA-256 校验通过')
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ffmpeg-', dir=target.parent) as work:
        package = Path(work) / 'ffmpeg.zip'
        with urllib.request.urlopen(URL, timeout=60) as source, package.open('wb') as output:
            shutil.copyfileobj(source, output, 1024 * 1024)
        with package.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != SHA256:
                raise ValueError('FFmpeg 下载包校验失败，已保留现有运行时')
        stage = Path(work) / 'runtime'; stage.mkdir()
        with zipfile.ZipFile(package) as archive:
            for name in ('ffmpeg.exe', 'ffprobe.exe', 'LICENSE'):
                matches = [entry for entry in archive.infolist() if Path(entry.filename).name == name]
                if len(matches) != 1: raise ValueError('FFmpeg 包缺少唯一组件：' + name)
                with archive.open(matches[0]) as source, (stage / name).open('wb') as output:
                    shutil.copyfileobj(source, output)
        manifest = {'version': VERSION, 'url': URL, 'archive_sha256': SHA256, 'files': {}}
        for file in stage.iterdir():
            with file.open('rb') as stream: manifest['files'][file.name] = hashlib.file_digest(stream, 'sha256').hexdigest()
        import json
        (stage / 'source.json').write_text(json.dumps(manifest, indent=2), encoding='utf8')
        target.mkdir(exist_ok=True)
        for file in stage.iterdir(): shutil.copy2(file, target / file.name)
    print(target)
    return target


if __name__ == '__main__': install()
