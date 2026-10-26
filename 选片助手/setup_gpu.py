"""准备锁定的 NVIDIA 图像运行库；只解包 DLL 和许可证，不修改系统环境。"""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parents[1]
PACKAGES=(
    ('nvidia_nvjpeg_cu12-12.4.0.76-py3-none-win_amd64.whl','21923726db667bd53050d0de88320983ff423322b7f376057dd943e487c40abc',
     'https://files.pythonhosted.org/packages/bc/28/e05bb8e6cdb98e79c6822f8bbd7154a26d8102412b3a0bfd5e4c7c52db8c/nvidia_nvjpeg_cu12-12.4.0.76-py3-none-win_amd64.whl'),
    ('nvidia_cuda_nvrtc_cu12-12.9.86-py3-none-win_amd64.whl','72972ebdcf504d69462d3bcd67e7b81edd25d0fb85a2c46d3ea3517666636349',
     'https://files.pythonhosted.org/packages/52/de/823919be3b9d0ccbf1f784035423c5f18f4267fb0123558d58b813c6ec86/nvidia_cuda_nvrtc_cu12-12.9.86-py3-none-win_amd64.whl'),
    ('nvidia_cuda_runtime_cu12-12.9.79-py3-none-win_amd64.whl','8e018af8fa02363876860388bd10ccb89eb9ab8fb0aa749aaf58430a9f7c4891',
     'https://files.pythonhosted.org/packages/59/df/e7c3a360be4f7b93cee39271b792669baeb3846c58a4df6dfcf187a7ffab/nvidia_cuda_runtime_cu12-12.9.79-py3-none-win_amd64.whl'),
)
FILES={
    'nvjpeg64_12.dll':'26559f5319f6b947912daded08680ab248d5af7867529402af050b38b53193ff',
    'nvrtc-builtins64_129.dll':'c405226b0b394ed8bdc593bb1419b94a989f93248ed2873e8f2e0e27c1688ca1',
    'nvrtc64_120_0.dll':'0cc63349fedffeec1660b30955e8028b6e4e4c26cf1e41c56dc4569fd5eca82c',
    'cudart64_12.dll':'760c38928bbe5759f7b31ed6692599eb7ec83cedd5702e84c2b72028a89837e1',
    **{'licenses/'+name+'-License.txt':'ad6f5853fba0ca0d159d0f58d49ae49830c2f8c93f7a92648b9ce90adb4c6ccd' for name in ('nvidia_nvjpeg_cu12','nvidia_cuda_nvrtc_cu12','nvidia_cuda_runtime_cu12')},
}


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def verify_runtime(target):
    try:
        target=Path(target);manifest=json.loads((target/'source.json').read_text(encoding='utf8'))
        if manifest['packages']!=[{'name':name,'sha256':sha} for name,sha,_ in PACKAGES]:return False
        files=manifest['files']
        if files!=FILES:return False
        if not {'nvjpeg64_12.dll','cudart64_12.dll','nvrtc64_120_0.dll'}<=set(files):return False
        if not any(name.startswith('nvrtc-builtins') for name in files):return False
        if len([name for name in files if name.startswith('licenses/')])<3:return False
        for name,sha in files.items():
            relative=Path(name)
            if relative.is_absolute() or '..' in relative.parts:return False
            if digest(target/relative)!=sha:return False
        return True
    except (OSError,ValueError,KeyError,TypeError):return False


def install():
    target=ROOT/'.runtime/gpu';downloads=ROOT/'.runtime/gpu-downloads'
    if verify_runtime(target):print('CUDA / nvJPEG 图像运行库已通过 SHA-256 校验');return target
    downloads.mkdir(parents=True,exist_ok=True);target.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='gpu-stage-',dir=target.parent) as temporary:
        stage=Path(temporary);files={}
        for name,sha,url in PACKAGES:
            package=downloads/name
            if not package.is_file() or digest(package)!=sha:
                with urllib.request.urlopen(url,timeout=60) as source,package.open('wb') as output:shutil.copyfileobj(source,output,1024*1024)
            if digest(package)!=sha:raise ValueError('GPU 组件下载校验失败：'+name)
            licenses=0
            with zipfile.ZipFile(package) as archive:
                for entry in archive.infolist():
                    path=Path(entry.filename)
                    if path.suffix.lower()=='.dll' and path.name in FILES:relative=Path(path.name)
                    elif not entry.is_dir() and ('license' in path.name.lower() or path.name.lower()=='copying'):
                        relative=Path('licenses')/(name.split('-')[0]+'-'+path.name);licenses+=1
                    else:continue
                    destination=stage/relative;destination.parent.mkdir(parents=True,exist_ok=True)
                    with archive.open(entry) as source,destination.open('wb') as output:shutil.copyfileobj(source,output)
                    files[relative.as_posix()]=digest(destination)
            if not licenses:raise ValueError('GPU 组件缺少许可证：'+name)
        manifest={'packages':[{'name':name,'sha256':sha} for name,sha,_ in PACKAGES],'files':files}
        (stage/'source.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
        if not verify_runtime(stage):raise ValueError('GPU 运行库不完整')
        shutil.copytree(stage,target,dirs_exist_ok=True)
        (target/'nvrtc64_120_0.alt.dll').unlink(missing_ok=True)
    print(target);return target


if __name__=='__main__':install()
