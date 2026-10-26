"""准备兼容 570+ NVIDIA 驱动的 FFmpeg 8.1.1，不改变系统 FFmpeg。"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parents[1]
FILES={
    'ffmpeg.exe':'228d7a8556258de907fdb55f36850078ebc7680b84ec30d84ea02e99bec1d1eb',
    'ffprobe.exe':'0fde260f5abd35c9cafd96f594cc76365a780c1b73a90e35b6a3409ea1db1bf0',
    'LICENSE':'8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903',
    'README.txt':'af90ccd9d705c74cadb9c36918664353de0533eade19f7ccd5cbe6081c262001',
}
PACKAGE_SHA='1d96324a4863475e4f3732f2f9586b6a45b1fdd93b5a1eefcc2c6030a1299dfa'
PACKAGE_URL='https://community.chocolatey.org/api/v2/package/ffmpeg/8.1.1'


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def verify_runtime(folder):
    try:
        folder=Path(folder);manifest=json.loads((folder/'source.json').read_text(encoding='utf8'))
        return manifest['version']=='8.1.1' and manifest['files']==FILES and all(digest(folder/name)==sha for name,sha in FILES.items())
    except (OSError,ValueError,KeyError,TypeError):return False


def install(source=None):
    target=ROOT/'.runtime/ffmpeg-compat'
    if verify_runtime(target):print('兼容 FFmpeg 8.1.1 已通过 SHA-256 校验');return target
    def originals(folder):return {name:folder/('bin/'+name if name.endswith('.exe') else name) for name in FILES}
    def valid(files):return all(path.is_file() and digest(path)==FILES[name] for name,path in files.items())
    folder=Path(source) if source else Path('C:/ProgramData/chocolatey/lib/ffmpeg/tools/ffmpeg')
    files=originals(folder)
    if not valid(files):
        if source is not None:raise ValueError('FFmpeg 源目录文件未匹配锁定哈希')
        download=ROOT/'.runtime/gpu-downloads/ffmpeg-8.1.1.nupkg';download.parent.mkdir(parents=True,exist_ok=True)
        local=Path('C:/ProgramData/chocolatey/lib/ffmpeg/ffmpeg.nupkg')
        if not download.is_file() or digest(download)!=PACKAGE_SHA:
            if local.is_file() and digest(local)==PACKAGE_SHA:shutil.copy2(local,download)
            else:
                with urllib.request.urlopen(PACKAGE_URL,timeout=90) as incoming,download.open('wb') as output:shutil.copyfileobj(incoming,output,1024*1024)
        if digest(download)!=PACKAGE_SHA:raise ValueError('兼容 FFmpeg 分发包 SHA-256 不匹配')
        with tempfile.TemporaryDirectory(prefix='ffmpeg-compat-',dir=target.parent) as work:
            work=Path(work);archive=work/'essentials.7z'
            with zipfile.ZipFile(download) as package,package.open('tools/ffmpeg-release-essentials.7z') as incoming,archive.open('wb') as output:shutil.copyfileobj(incoming,output)
            subprocess.run(['tar','-xf',str(archive),'-C',str(work),'--include','*/bin/ffmpeg.exe','--include','*/bin/ffprobe.exe','--include','*/LICENSE','--include','*/README.txt'],check=True,
                           creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            candidates=list(work.glob('*/bin/ffmpeg.exe'))
            if len(candidates)!=1:raise ValueError('兼容 FFmpeg 分发目录不明确')
            return install(candidates[0].parent.parent)
    target.mkdir(parents=True,exist_ok=True)
    for name,path in files.items():shutil.copy2(path,target/name)
    manifest={'version':'8.1.1','source':'Gyan essentials / Chocolatey ffmpeg 8.1.1',
              'source_code':'https://github.com/FFmpeg/FFmpeg/commit/239f2c733d',
              'distribution':PACKAGE_URL,'distribution_sha256':PACKAGE_SHA,'files':FILES}
    (target/'source.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
    assert verify_runtime(target);print(target);return target


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source',type=Path)
    install(parser.parse_args().source)
