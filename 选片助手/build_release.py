"""构建 Tauri 桌面程序及本机选片引擎，不包含用户数据和密钥。"""
from pathlib import Path
import argparse
import os
import shutil
import subprocess
import sys
import hashlib
import json

ROOT=Path(__file__).resolve().parents[1]
TARGET=ROOT/'build/tauri-target'
WORKBENCH_FILES=('asset-loading.js','preview-histogram.js','preview-histogram.css','session-paging.js','split-capture.js','split-capture.css','ocr-feedback.js','ocr-feedback.css','selection-folders.js','selection-folders.css','asset-workspace.css','workbench.css','classic-themes.css','view-menu.css','toolbar-layout.css','workbench-ui.js','sidebar-resize.js','update.js','component-ui.js','preview-empty.js','preview-empty.css','sku-media-pool.js','sku-media-pool.css')
CAMERA_ASSETS=('camera-iris.svg','camera-transfer.svg','camera-framing.svg','camera-iris-still.svg','camera-transfer-still.svg')

def build_engine():
    web=ROOT/'build/release-web/演示方案'
    if web.exists():
        web.resolve().relative_to(ROOT.resolve())
        shutil.rmtree(web)
    web.mkdir(parents=True,exist_ok=True)
    for name in ('index.html','style.css','theme.css','theme.js','motion.css','app.js','image-viewer.js','formal.js','task-status.js','assistant-settings.js','desktop.js','mouse-remote.js','folder-watch.js','sku-reconciliation.js','asset-metadata.js','asset-viewer.js','asset-library.js','asset-analysis.js','raw-reconcile.js','workbench-layout.js'):shutil.copy2(ROOT/'演示方案'/name,web/name)
    for name in WORKBENCH_FILES:shutil.copy2(ROOT/'演示方案'/name,web/name)
    (web/'assets').mkdir(exist_ok=True);(web/'assets/demo-data.js').write_text('window.DEMO_PHOTOS=[];',encoding='utf8')
    for name in CAMERA_ASSETS:shutil.copy2(ROOT/'演示方案/assets'/name,web/'assets'/name)
    command=[sys.executable,'-m','PyInstaller','--noconfirm','--log-level','WARN','--distpath',str(ROOT/'build/engine-dist'),'--onedir','--windowed','--name','framekeep-engine','--paths',str(ROOT/'选片助手'),'--paths',str(ROOT/'更新系统'),'--add-data',str(ROOT/'更新系统/update-config.json')+';更新系统','--collect-binaries','rawpy','--copy-metadata','rawpy','--copy-metadata','numpy','--copy-metadata','Pillow','--collect-data','jsonschema_specifications','--add-data',str(web)+';演示方案']
    for module in ('tkinter','rawpy.enhance','torch','scipy','skimage','matplotlib','pytest','IPython','PyQt5','PySide6','PyQt6','webview'):
        command+=['--exclude-module',module]
    command+=['--add-data',str(ROOT/'选片助手/component-manifest.json')+';.',
              '--add-data',str(ROOT/'选片助手/canon_probe.ps1')+';.',
              '--add-data',str(ROOT/'.runtime/component-seed')+';component-seed']
    from setup_ffmpeg import verify_runtime
    runtime=ROOT/'.runtime/ffmpeg'
    if not all((runtime/name).is_file() for name in ('ffmpeg.exe','ffprobe.exe','LICENSE','source.json')):raise RuntimeError('请先运行 python 选片助手/setup_ffmpeg.py 部署视频组件')
    if not verify_runtime(runtime):raise RuntimeError('FFmpeg 运行时校验失败，请重新运行 setup_ffmpeg.py')
    command+=['--add-data',str(runtime)+';runtime/ffmpeg']
    from setup_gpu import verify_runtime as verify_gpu
    from setup_gpu_video import verify_runtime as verify_gpu_video
    for folder,verify in (('gpu',verify_gpu),('ffmpeg-compat',verify_gpu_video)):
        dependency=ROOT/'.runtime'/folder
        if not verify(dependency):raise RuntimeError('GPU 组件未就绪，请运行 setup_gpu.py 和 setup_gpu_video.py：'+folder)
        command+=['--add-data',str(dependency)+';runtime/'+folder]
    command.append(str(ROOT/'选片助手/server.py'));subprocess.run(command,cwd=ROOT,check=True)
    return ROOT/'build/engine-dist/framekeep-engine'

def build_desktop():
    env=os.environ.copy();env['CARGO_TARGET_DIR']=str(TARGET)
    subprocess.run(['cargo','build','--release','--locked','--manifest-path',str(ROOT/'桌面程序/Cargo.toml')],env=env,cwd=ROOT,check=True)
    return TARGET/'release/framekeep-desktop.exe'

def stage_release(engine,desktop):
    release=ROOT/'build/tauri-release/末帧选片';release.mkdir(parents=True,exist_ok=True)
    shutil.copy2(desktop,release/'末帧选片.exe');shutil.copytree(engine,release/'engine',dirs_exist_ok=True)
    (release/'engine/_internal/演示方案/ai-panel.js').unlink(missing_ok=True)
    shutil.copytree(ROOT/'FramekeepBridge.lrplugin',release/'FramekeepBridge.lrplugin',dirs_exist_ok=True)
    shutil.copytree(ROOT/'.runtime/exiftool',release/'runtime/exiftool',dirs_exist_ok=True)
    if (ROOT/'.runtime/everything/Everything64.dll').is_file():
        if not (ROOT/'.runtime/everything/LICENSE-SDK.txt').is_file():raise RuntimeError('Everything SDK 缺少许可证，请重新执行 setup_everything.py')
        shutil.copytree(ROOT/'.runtime/everything',release/'runtime/everything',dirs_exist_ok=True)
    shutil.copy2(ROOT/'README.md',release/'完整使用说明.md')
    shutil.copy2(ROOT/'更新系统/update-config.json',release/'update-config.json')
    shutil.copytree(ROOT/'build/update-tools',release/'update',dirs_exist_ok=True)
    shutil.copy2(ROOT/'选片助手/component-manifest.json',release/'update/component-manifest.json')
    (release/'使用说明.txt').write_text('末帧选片 1.0.0 · Tauri 桌面版\n\n双击末帧选片.exe打开独立Windows窗口，不需要浏览器或Python。请保留整个文件夹。\n使用系统WebView2运行时；本机已安装的Edge WebView2可直接使用。\n照片索引、评级和设置保存在.framekeep。项目根启动器沿用原项目数据库。\n窗口关闭前保存评级，并结束自己启动的本机选片引擎。重复启动会聚焦已有窗口。\nAI助手配置和LR桥接使用方式见完整使用说明，预览只读；标星自动入库并同步 XMP，JPG 同时更新内嵌评级；资产备案按所选策略写入元数据。\n\n许可证：Tauri/Rust生态 (MIT/Apache等)、Python (PSF)、Pillow (HPND)、NumPy (BSD)、rawpy (MIT)/LibRaw (LGPL/CDDL)、ExifTool (Artistic/GPL)。运行时许可证随相应目录及包元数据保留。\n',encoding='utf8')
    instructions=release/'使用说明.txt'
    instructions.write_text(instructions.read_text(encoding='utf8').replace('照片索引、评级和设置保存在.framekeep。项目根启动器沿用原项目数据库。','正式安装提供桌面、开始菜单和 Windows 应用卸载入口。新安装和便携版资料、模型、缓存统一保存在安装目录 data；升级核验迁移旧资料，卸载保留 data 和照片。项目根启动器的显式开发资料路径兼容。'),encoding='utf8')
    config=json.loads((release/'update-config.json').read_text(encoding='utf8'))
    layout=release/'update/install-layout.json'
    files=[{'path':p.relative_to(release).as_posix(),'size':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(release.rglob('*')) if p.is_file() and p!=layout]
    layout.write_text(json.dumps({'product':'framekeep','version':config['version'],'releaseId':config['releaseId'],'files':files},ensure_ascii=False,indent=2),encoding='utf8')
    validate_release(release)
    return release

def validate_release(release):
    web=release/'engine/_internal/演示方案'
    from setup_gpu import verify_runtime as verify_gpu
    from setup_gpu_video import verify_runtime as verify_gpu_video
    if not verify_gpu(release/'engine/_internal/runtime/gpu'):raise RuntimeError('发行包 GPU 图像组件缺失或校验失败')
    if not verify_gpu_video(release/'engine/_internal/runtime/ffmpeg-compat'):raise RuntimeError('发行包 GPU 视频组件缺失或校验失败')
    manifest=json.loads((ROOT/'选片助手/component-manifest.json').read_text(encoding='utf8'))
    for record in manifest['assets']:
        if not record.get('bundled'):continue
        seed=release/'engine/_internal/component-seed'/record['path']
        if not seed.is_file() or seed.stat().st_size!=record['size'] or hashlib.sha256(seed.read_bytes()).hexdigest()!=record['sha256']:
            raise RuntimeError('发行包缺少经过校验的轻量安装组件，请先准备 .runtime/component-seed：'+record['path'])
    if not all((release/name).is_file() for name in ('update/卸载末帧选片.exe','update/install-layout.json')):raise RuntimeError('发行包缺少卸载程序或安装清单。')
    if (web/'ai-panel.js').exists():raise RuntimeError('发行包仍包含 AI 选片入口。')
    if not (web/'assistant-settings.js').is_file():raise RuntimeError('发行包缺少 AI 助手配置。')
    if not (web/'image-viewer.js').is_file():raise RuntimeError('发行包缺少图片缩放和平移模块。')
    if not (web/'task-status.js').is_file():raise RuntimeError('发行包缺少导航任务状态模块。')
    if not (web/'mouse-remote.js').is_file():raise RuntimeError('发行包缺少无线鼠标遥控模块。')
    if not (web/'folder-watch.js').is_file():raise RuntimeError('发行包缺少现场目录监控模块。')
    if not all((release/'engine/_internal/runtime/ffmpeg'/name).is_file() for name in ('ffmpeg.exe','ffprobe.exe','LICENSE','source.json')):raise RuntimeError('发行包缺少 FFmpeg 视频解码组件')
    if not (web/'asset-viewer.js').is_file():raise RuntimeError('发行包缺少归档与 SKU 全屏图片浏览模块。')
    if not (web/'motion.css').is_file():raise RuntimeError('发行包缺少统一贝塞尔动效样式。')
    if not all((web/name).is_file() for name in ('theme.css','theme.js')):raise RuntimeError('发行包缺少三主题样式或切换模块。')
    if not all((web/name).is_file() for name in WORKBENCH_FILES):raise RuntimeError('发行包缺少四主题布局或视图菜单资源。')
    if not all((web/'assets'/name).is_file() for name in CAMERA_ASSETS):raise RuntimeError('发行包缺少联机拍摄 SVG 动画。')
    if not (web/'workbench-layout.js').is_file():raise RuntimeError('发行包缺少折叠布局与实时缩略图模块。')
    if not (web/'sku-reconciliation.js').is_file():raise RuntimeError('发行包缺少 SKU 对账模块。')
    if not all((web/name).is_file() for name in ['asset-metadata.js','asset-library.js','asset-analysis.js','raw-reconcile.js']):raise RuntimeError('发行包缺少资产备案/本地视觉模块。')
    if list(web.rglob('*.jpg')) or list(web.rglob('*.png')):raise RuntimeError('发行包包含示例图片。')
    for path in release.rglob('*'):
        if path.name in ('.framekeep','api key.md') or path.suffix in ('.sqlite3','.db'):raise RuntimeError('发行包包含用户数据或配置：'+str(path))

def deploy_release(release):
    destination=ROOT/'dist/末帧选片';destination.mkdir(parents=True,exist_ok=True)
    # 若旧程序仍占用主程序则立即失败，不修改其余运行文件。
    shutil.copy2(release/'末帧选片.exe',destination/'末帧选片.exe')
    shutil.copytree(release,destination,dirs_exist_ok=True)
    (destination/'engine/_internal/演示方案/ai-panel.js').unlink(missing_ok=True)
    legacy=destination/'_internal'
    if legacy.is_dir() and (legacy/'python312.dll').is_file():
        assert legacy.resolve().parent==destination.resolve()
        shutil.rmtree(legacy) # 仅清理旧版可再生运行库，保留.framekeep。
    return destination

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--stage-only',action='store_true');parser.add_argument('--engine-only',action='store_true');args=parser.parse_args()
    engine=build_engine()
    if args.engine_only:print(engine);return
    release=stage_release(engine,build_desktop())
    print(release if args.stage_only else deploy_release(release))

if __name__=='__main__':main()
