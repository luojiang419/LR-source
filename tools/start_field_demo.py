"""使用已安装桌面外壳启动隔离源码与指定测试资料，生产data不参与。"""
from pathlib import Path
import json,os,socket,subprocess,sys,time,urllib.request
root=Path(__file__).resolve().parents[1];data=Path(sys.argv[1]).resolve()
assert data.is_relative_to(root/'build/field-trial') and (data/'framekeep.sqlite3').exists()
assert (data.parent/'fixture-ready.json').exists(),'等待素材准备完成再启动演练'
with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
env={**os.environ,'FRAMEKEEP_DATA':str(data),'FRAMEKEEP_APP_ROOT':str(root),'FRAMEKEEP_PORT':str(port),
     'FRAMEKEEP_ENGINE':sys.executable,'FRAMEKEEP_ENGINE_SCRIPT':str(root/'tools/field_engine.py'),
     'FRAMEKEEP_EXIFTOOL':'D:/Program Files/Framekeep/runtime/exiftool/package/bin/exiftool.exe',
     'FRAMEKEEP_CANON_REGISTRATION':'0'}
env['FRAMEKEEP_TRACE_PIPELINE']='1'
if '--frozen' in sys.argv:
    engine=(Path(sys.argv[sys.argv.index('--engine')+1]).resolve() if '--engine' in sys.argv else root/'build/engine-dist/framekeep-engine/framekeep-engine.exe')
    assert engine.is_relative_to(root/'build')
    assert engine.is_file(),'请先构建隔离发行引擎'
    env['FRAMEKEEP_ENGINE']=str(engine);env.pop('FRAMEKEEP_ENGINE_SCRIPT',None)
started=time.time();proc=subprocess.Popen(['D:/Program Files/Framekeep/SHIYING 数字资产.exe'],env=env,cwd=root,creationflags=subprocess.CREATE_NO_WINDOW)
result={'pid':proc.pid,'url':f'http://127.0.0.1:{port}','started':started,'data':str(data)}
result.update(engine=env['FRAMEKEEP_ENGINE'],frozen='--frozen' in sys.argv)
import hashlib
result['source_sha256']={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for folder in ('选片助手','演示方案') for p in (root/folder).glob('*') if p.is_file() and p.suffix in ('.py','.js','.pl','.css') and not p.name.startswith('test_')}
(data.parent/'desktop-launch.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf8')
print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'},ensure_ascii=False))
