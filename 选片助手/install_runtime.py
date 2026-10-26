"""从锁定版本的公开分发包取得 ExifTool，核对 npm SHA-512 完整性。"""
import base64
import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path

def main():
    version='13.59.3'
    with urllib.request.urlopen('https://registry.npmjs.org/exiftool-vendored.exe/'+version,timeout=30) as r:metadata=json.load(r)
    with urllib.request.urlopen(metadata['dist']['tarball'],timeout=90) as r:payload=r.read()
    expected=metadata['dist']['integrity'];actual='sha512-'+base64.b64encode(hashlib.sha512(payload).digest()).decode()
    if actual!=expected:raise RuntimeError('ExifTool 分发包完整性校验失败。')
    target=Path(__file__).resolve().parents[1]/'.runtime/exiftool';target.mkdir(parents=True,exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload),mode='r:gz') as archive:archive.extractall(target,filter='data')
    print('ExifTool 13.59 运行时已安装，SHA-512 校验通过。')
    from setup_ffmpeg import install
    install()
    from setup_gpu import install as install_gpu
    from setup_gpu_video import install as install_gpu_video
    install_gpu();install_gpu_video()
if __name__=='__main__':main()
