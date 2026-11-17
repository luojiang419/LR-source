"""真实冻结 worker 的卡死进程、文件锁、签名事务和启动失败回滚验收（隔离目录）。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'更新系统'))
sys.path.insert(0,str(ROOT/'选片助手'))
from update_client import copy_worker
import updater as u
import process_control as pc
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat


def compile_host(work,name,source):
    file=work/(name+'.cs');file.write_text(source,encoding='utf8');output=file.with_suffix('.exe')
    subprocess.run(['C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe','/nologo','/reference:System.Web.Extensions.dll','/out:'+str(output),str(file)],check=True,capture_output=True)
    return output


def wait_file(path,timeout=40):
    until=time.monotonic()+timeout
    while not path.exists():
        if time.monotonic()>=until:raise TimeoutError(str(path))
        time.sleep(.05)


def stop(pid):
    try:
        process=pc.Process(pid)
        try:process.terminate()
        finally:process.close()
    except OSError:pass


def run_case(work,runtime,host,healthy,fail=False):
    work.mkdir();root=work/'安装目录';root.mkdir();session=work/'session';session.mkdir();data=root/'data';data.mkdir()
    photo=data/'photo.jpg';photo.write_bytes(b'user original photo');(data/'settings.json').write_text('{"keep":true}')
    (root/'engine').mkdir();desktop=root/'SHIYING 数字资产.exe';engine=root/'engine/framekeep-engine.exe'
    shutil.copy2(host,desktop);shutil.copy2(host,engine);helper=work/'metadata-helper.exe';shutil.copy2(host,helper)
    library=root/'engine/library.dll';library.write_bytes(b'previous program library')
    adb=root/'engine/_internal/runtime/adb/adb.exe';adb.parent.mkdir(parents=True);shutil.copy2(host,adb)
    sdk=work/'sdk/adb.exe';sdk.parent.mkdir();shutil.copy2(host,sdk)
    u.write_json(root/'update-config.json',{'version':'1.0.1','releaseId':'20261101000000'})
    worker=work/'runtime';worker.mkdir();copy_worker(runtime/'update-worker.exe',worker)
    key=Ed25519PrivateKey.generate();trust={**u.TRUST,'publicKey':key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()}
    u.write_json(worker/'_internal/更新系统/update-config.json',trust)
    package=work/'payload.zip';release='20261114062655'
    files={'SHIYING 数字资产.exe':healthy.read_bytes(),'engine/framekeep-engine.exe':b'new engine',
           'update/SHIYING 更新器.exe':b'new native','update/update-worker.exe':b'new worker',
           'engine/library.dll':b'new program library',
           'engine/_internal/runtime/adb/adb.exe':host.read_bytes()+b'new adb version',
           'update-config.json':json.dumps({'version':'1.0.1','releaseId':release}).encode()}
    records=[]
    with zipfile.ZipFile(package,'w') as bundle:
        for name,value in files.items():
            bundle.writestr(name,value);records.append({'path':name,'size':len(value),'sha256':hashlib.sha256(value).hexdigest()})
    manifest={'protocol':1,'product':'framekeep','platform':'win32-x64','version':'1.0.1','releaseId':release,
              'minVersion':'1.0.0','package':{'size':package.stat().st_size,'sha256':u.sha256(package),
              'url':'https://modelscope.cn/api/v1/models/'+u.TRUST['repository']+'/repo?fixture=1'},'files':records}
    payload=json.dumps(manifest);envelope=work/'envelope.json'
    u.write_json(envelope,{'payload':payload,'signature':key.sign(payload.encode()).hex()})
    settings={'mode':'update','root':str(root),'session':str(session),'data':str(data),'port':19835,
              'integration':False,'localEnvelope':str(envelope),'localPackage':str(package)}
    app=None;old_engine=None;adb_server=None;sdk_server=None
    try:
        adb_server=subprocess.Popen([str(adb),str(work/'adb.lock'),str(work/'adb.ready')],creationflags=subprocess.CREATE_NO_WINDOW)
        sdk_server=subprocess.Popen([str(sdk),str(work/'sdk.lock'),str(work/'sdk.ready')],creationflags=subprocess.CREATE_NO_WINDOW)
        wait_file(work/'adb.ready');wait_file(work/'sdk.ready')
        args=subprocess.list2cmdline([str(work/'child.lock'),str(work/'child.ready')])
        old_engine=subprocess.Popen([str(engine),str(work/'engine.lock'),str(work/'engine.ready'),str(helper),args],creationflags=subprocess.CREATE_NO_WINDOW)
        wait_file(work/'engine.ready');wait_file(work/'child.ready');settings['enginePid']=old_engine.pid
        # 旧桌面派生真实冻结 worker；worker 必须在祖先进程被结束后继续运行。
        app=subprocess.Popen([str(desktop),str(library),str(work/'app.ready'),str(worker/'update-worker.exe'),subprocess.list2cmdline([str(session)])],creationflags=subprocess.CREATE_NO_WINDOW)
        settings['parentPid']=app.pid;u.write_json(session/'session.json',settings)
        wait_file(work/'app.ready');started=time.monotonic();wait_file(session/'status.json')
        status=u.read_json(session/'status.json');elapsed=time.monotonic()-started
        assert status['status']==('rolled-back' if fail else 'complete'),status
        assert photo.read_bytes()==b'user original photo'
        assert (data/'settings.json').read_text()=='{"keep":true}'
        assert not (root/'.update-state/pending.json').exists()
        assert (session/'forced-exit.json').exists()
        assert not u.alive(old_engine.pid) and not u.alive(int((work/'child.ready').read_text()))
        assert not u.alive(adb_server.pid) and sdk_server.poll() is None
        assert adb.read_bytes()==(host.read_bytes() if fail else files['engine/_internal/runtime/adb/adb.exe'])
        if fail:
            assert desktop.read_bytes()==host.read_bytes()
            assert library.read_bytes()==b'previous program library'
        else:
            assert desktop.read_bytes()==healthy.read_bytes()
            assert library.read_bytes()==b'new program library'
            stop(status['pid'])
        result={'status':status['status'],'elapsedSeconds':round(elapsed,3),'forceExit':True,
                'programFilesVerified':True,'userDataPreserved':True,'workerSurvivedParentExit':True,
                'detachedBundledAdbReleased':True,'externalAdbPreserved':True,'adbReplacementOrRollbackVerified':True}
        u.write_json(work/'result.json',result);return result
    finally:
        for process in (app,old_engine,adb_server,sdk_server):
            if process:
                if process.poll() is None:stop(process.pid)
                process.wait(timeout=5)
        if (work/'child.ready').exists():stop(int((work/'child.ready').read_text()))
        # 仅回收当前隔离目录的测试应用，不关闭其它应用。
        with pc.ApplicationProcesses(root) as processes:processes.terminate()


def main(output,runtime):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False);runtime=Path(runtime).resolve()
    host=compile_host(output,'host','''using System;using System.IO;using System.Diagnostics;using System.Threading;
class Host {static void Main(string[] a) {if(a.Length==0)return;
using(var file=new FileStream(a[0],FileMode.OpenOrCreate,FileAccess.ReadWrite,FileShare.None)) {
File.WriteAllText(a[1],Process.GetCurrentProcess().Id.ToString());
if(a.Length>2){if(a[2].EndsWith("update-worker.exe"))while(!File.Exists(Path.Combine(a[3].Trim('"'),"session.json")))Thread.Sleep(20);
Process.Start(new ProcessStartInfo(a[2],a[3]){UseShellExecute=false,CreateNoWindow=true});}
Thread.Sleep(Timeout.Infinite);}}}''')
    healthy=compile_host(output,'healthy','''using System;using System.IO;using System.Diagnostics;using System.Threading;using System.Web.Script.Serialization;
class Healthy {static void Main(){string file=Environment.GetEnvironmentVariable("FRAMEKEEP_UPDATE_HEALTH_FILE");
if(file!=null)File.WriteAllText(file,new JavaScriptSerializer().Serialize(new {pid=Process.GetCurrentProcess().Id,version="1.0.1",releaseId="20261114062655"}));
Thread.Sleep(Timeout.Infinite);}}''')
    failed=compile_host(output,'failed','class Failed {static int Main(){return 7;}}')
    result={'success':run_case(output/'success',runtime,host,healthy),'rollback':run_case(output/'rollback',runtime,host,failed,True)}
    u.write_json(output/'result.json',result);print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--runtime',type=Path,default=ROOT/'build/update-tools');args=parser.parse_args()
    main(args.output,args.runtime)
