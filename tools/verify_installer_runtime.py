"""真实 EXE 验证：中文/空格路径、缓存修复及损坏归档的明确阻断。"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile


def verify(installer,bundle,out):
    installer=Path(installer).resolve();bundle=Path(bundle).resolve();out=Path(out).resolve();out.mkdir(parents=True,exist_ok=True)
    results=[]
    for name in ('ascii','中文 空格'):
        target=out/name
        process=subprocess.run([str(installer),'--verify-runtime',str(target)],capture_output=True,timeout=45)
        check=json.loads((target/'runtime-check.json').read_text('utf8'))
        assert process.returncode==0 and check['exitCode']==0 and check['planCreated'],check
        assert json.loads((target/'worker-integrity.json').read_text('utf8'))['runtime']=='onedir'
        dll=target/'_internal/python312.dll';before=hashlib.sha256(dll.read_bytes()).hexdigest();dll.write_bytes(b'corrupted cache')
        retry=subprocess.run([str(installer),'--verify-runtime',str(target)],capture_output=True,timeout=45)
        assert retry.returncode==0 and hashlib.sha256(dll.read_bytes()).hexdigest()==before
        results.append({'path':name,'native_exit':process.returncode,'worker_exit':check['exitCode'],'cache_repaired':True})
    binary=bytearray(installer.read_bytes());payload=bundle.read_bytes();offset=binary.find(payload)
    assert offset>=0 and binary.find(payload,offset+1)<0
    with zipfile.ZipFile(bundle) as package:
        entry=package.getinfo('_internal/python312.dll');position=entry.header_offset+30+len(entry.filename.encode('utf8'))+len(entry.extra)
    binary[offset+position+10]^=0x40
    broken=out/'damaged-installer.exe';broken.write_bytes(binary);target=out/'damaged-check'
    process=subprocess.run([str(broken),'--verify-runtime',str(target)],capture_output=True,timeout=45)
    error=json.loads((target/'runtime-check-error.json').read_text('utf8'))
    assert process.returncode!=0 and '完整性校验失败' in error['error']
    assert not (target/'update-worker.exe').exists()
    report={'checks':results,'corrupt_archive_blocked_before_worker':True,'installer_sha256':hashlib.sha256(installer.read_bytes()).hexdigest()}
    (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8');print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--installer',type=Path,required=True);parser.add_argument('--bundle',type=Path,required=True);parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();verify(args.installer,args.bundle,args.out)
