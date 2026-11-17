"""校验真实签名包并隔离执行文件替换事务；不启动或关闭用户桌面实例。"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'更新系统'))
import updater


def verify(snapshot,out,image):
    snapshot=Path(snapshot).resolve();out=Path(out).resolve();out.relative_to((ROOT/'build').resolve())
    out.mkdir(parents=True,exist_ok=False)
    session=out/'transaction';session.mkdir();installed=out/'installed';installed.mkdir()
    envelope=updater.read_json(snapshot/'envelope.json');manifest=updater.verify_envelope(envelope)
    updater.write_json(session/'envelope.json',envelope)
    package=snapshot/manifest['package']['name']
    try:os.link(package,session/'package.zip')
    except OSError:shutil.copyfile(package,session/'package.zip')
    sentinel=installed/'data/keep-photo.jpg';sentinel.parent.mkdir();shutil.copyfile(image,sentinel)
    sentinel_hash=hashlib.sha256(sentinel.read_bytes()).hexdigest()
    old_weight=installed/'data/components/models/ocr/old-user-model.gguf';old_weight.parent.mkdir(parents=True);old_weight.write_bytes(b'preserve-existing-model')
    settings={'currentVersion':'1.0.0','currentReleaseId':'20261104110836'}
    checked=updater.validate(session,settings)
    with (out/'transaction.log').open('w',encoding='utf8') as log,contextlib.redirect_stdout(log):
        updater.install(installed,session,checked)
    assert hashlib.sha256(sentinel.read_bytes()).hexdigest()==sentinel_hash
    assert old_weight.read_bytes()==b'preserve-existing-model'
    assert not any(r['path'].startswith('data/') for r in manifest['files'])
    components=json.loads((installed/'engine/_internal/component-manifest.json').read_text('utf8'))
    assert all(r['group']!='glm-ocr-local' for r in components['assets'])
    report={'releaseId':manifest['releaseId'],'signature_verified':True,'package_sha256':updater.sha256(package),
            'installed_files_verified':len(manifest['files']),'photo_preserved':True,'old_model_preserved':True,
            'glm_assets_absent':True,'native_window_tested':False,
            'native_window_note':'现用桌面实例保持运行；单实例应用不另起同标识窗口，本次校验冻结引擎真实业务接口。'}
    for mode in ('new','upgrade'):
        qa=out/('ocr-'+mode)
        subprocess.run([sys.executable,'-X','utf8',str(ROOT/'tools/verify_cpu_ocr_package.py'),
                        '--engine',str(installed/'engine/framekeep-engine.exe'),'--image',str(Path(image).resolve()),
                        '--out',str(qa),'--mode',mode,'--expected','LC7832773-P805'],check=True)
        report[mode]=json.loads((qa/'result.json').read_text('utf8'))
    updater.write_json(out/'result.json',report)
    print(json.dumps({k:v for k,v in report.items() if k not in ('new','upgrade')},ensure_ascii=False),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--snapshot',type=Path,required=True);parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--image',type=Path,required=True);args=parser.parse_args();verify(args.snapshot,args.out,args.image)
