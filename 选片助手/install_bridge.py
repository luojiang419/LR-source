"""安装本项目桥接到 Lightroom 用户 Modules；不修改插件注册表或原图。"""
import os
import shutil
from pathlib import Path
from datetime import datetime

def install(root):
    root=Path(root);source=root/'FramekeepBridge.lrplugin'
    if not source.is_dir():raise ValueError('发行包缺少 FramekeepBridge.lrplugin。')
    modules=Path(os.environ['APPDATA'])/'Adobe/Lightroom/Modules';modules.mkdir(parents=True,exist_ok=True)
    target=modules/'FramekeepBridge.lrplugin'
    if target.exists():
        changed=any(not (target/p.relative_to(source)).is_file() or (target/p.relative_to(source)).read_bytes()!=p.read_bytes() for p in source.rglob('*') if p.is_file())
        if changed:
            backup=root/'.framekeep/plugin-backups'/datetime.now().strftime('%Y%m%d-%H%M%S-%f');backup.parent.mkdir(parents=True,exist_ok=True);shutil.copytree(target,backup)
    shutil.copytree(source,target,dirs_exist_ok=True)
    return target

if __name__=='__main__':print(install(Path(__file__).resolve().parents[1]))
