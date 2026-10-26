"""使用真实签名包和 EXE 在隔离目录安装；首次发布前不依赖公网 latest。"""
from pathlib import Path
import contextlib
import json
import shutil
import sys
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'更新系统'))
import updater as u


def main():
    snapshot=Path(sys.argv[1]).resolve()
    base=ROOT/'build/update-smoke'/uuid.uuid4().hex[:8];base.mkdir(parents=True)
    session=base/'local-install';session.mkdir(exist_ok=False)
    install=base/'安装目录'
    settings={'mode':'install','session':str(session),'root':str(install),'parentPid':0,
              'port':18792,'data':str(install/'.framekeep'),'integration':False}
    u.write_json(session/'session.json',settings)
    envelope=u.read_json(snapshot/'envelope.json');manifest=u.verify_envelope(envelope)
    shutil.copy2(snapshot/manifest['package']['name'],session/'package.zip')
    u.fetch_envelope=lambda:(envelope,manifest)
    with (session/'worker.log').open('w',encoding='utf8') as log,contextlib.redirect_stdout(log):u.run(session)
    def get(path):
        with urllib.request.urlopen('http://127.0.0.1:18792'+path,timeout=5) as response:return json.load(response)
    result={'transaction':u.read_json(session/'status.json'),'health':get('/api/health'),
        'updateHealth':get('/api/update/health'),'update':get('/api/update'),
        'desktopHealth':u.read_json(session/'healthy.json'),'installed':str(install)}
    assert result['transaction']['status']=='complete'
    assert result['health']['application']=='framekeep' and not result['updateHealth']['busy']
    assert result['update']['currentReleaseId']==manifest['releaseId']
    u.write_json(base/'local-install-result.json',result)
    u.write_json(ROOT/'build/update-smoke/latest.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
