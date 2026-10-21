"""末帧独立更新事务，改编自 VoxCPM 成熟更新器。"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
import zipfile
import urllib.request
from contextlib import contextmanager

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import installation

TRUST = json.loads((Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / '更新系统/update-config.json' if getattr(sys, 'frozen', False) else Path(__file__).parent / 'update-config.json').read_text('utf-8'))


def write_json(file: Path, value):
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(file.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(30):
        try:os.replace(temporary,file);return
        except PermissionError:
            if attempt==29:raise
            time.sleep(.02)


def read_json(file):
    return json.loads(Path(file).read_text("utf-8-sig"))


def sha256(file):
    with Path(file).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def emit(message, progress=0):
    print(json.dumps({"message": message, "progress": progress}, ensure_ascii=True), flush=True)


def safe_path(relative: str) -> str:
    return installation.safe_relative(relative)


def destination(root: Path, relative: str):
    target = root / safe_path(relative)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("更新路径超出软件目录")
    cursor = target
    while cursor != root:
        if cursor.is_symlink() or cursor.is_junction():
            raise ValueError("更新目标不能是符号链接或目录联接")
        cursor = cursor.parent
    if target.exists() and not target.is_file():
        raise ValueError("更新文件目标已被目录占用")
    return target


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("版本号格式无效")
    return tuple(map(int, value.split(".")))


def validate(session: Path, settings):
    envelope = read_json(session / "envelope.json")
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(TRUST["publicKey"])).verify(
        bytes.fromhex(envelope["signature"]), envelope["payload"].encode("utf-8"))
    manifest = json.loads(envelope["payload"])
    if (manifest.get("protocol"), manifest.get("product"), manifest.get("platform")) != (
        1, "framekeep", "win32-x64"
    ):
        raise ValueError("更新包不兼容")
    release_id = manifest.get("releaseId", "00000000000000")
    if not re.fullmatch(r"\d{14}", release_id):
        raise ValueError("发布序号无效")
    reference_version = settings["currentVersion"]
    if reference_version is None and settings.get("repair") is True and settings.get("repairIdentityUnknown") is True:
        reference_version = settings["baselineVersion"]
    same_repair = settings.get("repair") is True and settings["currentVersion"] is not None and version(manifest["version"]) == version(reference_version) and release_id == settings.get("currentReleaseId", "00000000000000")
    if version(manifest["version"]) < version(reference_version) or (
        version(manifest["version"]) == version(reference_version) and
        release_id <= settings.get("currentReleaseId", "00000000000000") and not same_repair
    ):
        raise ValueError("拒绝安装相同或更旧的版本")
    if version(reference_version) < version(manifest["minVersion"]):
        raise ValueError("需要先升级完整便携基线")
    package = session / "package.zip"
    meta = manifest["package"]
    if package.stat().st_size != meta["size"] or sha256(package) != meta["sha256"]:
        raise ValueError("更新包完整性校验失败")
    records = manifest["files"]
    if not records or len(records) > 20000:
        raise ValueError("更新文件数量无效")
    names = set()
    total = 0
    for record in records:
        relative = safe_path(record["path"])
        if relative.lower() in names:
            raise ValueError("更新包含重复路径")
        names.add(relative.lower())
        if not isinstance(record["size"], int) or record["size"] < 0 or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"]):
            raise ValueError("更新文件元数据无效")
        total += record["size"]
    if total > 2 * 1024 ** 3:
        raise ValueError("更新解压总量超过限制")
    stage = session / "stage"
    stage.mkdir(exist_ok=True)
    with zipfile.ZipFile(package) as archive:
        entries = archive.infolist()
        if len(entries) != len(records) or {e.filename for e in entries} != {r["path"] for r in records}:
            raise ValueError("压缩包与签名文件清单不一致")
        by_name = {e.filename: e for e in entries}
        for record in records:
            entry = by_name[record["path"]]
            if stat.S_ISLNK(entry.external_attr >> 16) or entry.file_size != record["size"]:
                raise ValueError("更新包含链接或文件大小异常")
            target = destination(stage, record["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            if sha256(target) != record["sha256"]:
                raise ValueError("解压文件哈希校验失败")
    config = read_json(stage / 'update-config.json')
    if config['version'] != manifest['version'] or config['releaseId'] != release_id:
        raise ValueError('应用身份与签名清单不一致')
    required = {'末帧选片.exe', 'engine/framekeep-engine.exe', 'update/末帧更新器.exe', 'update/update-worker.exe', 'update-config.json'}
    if not required.issubset(names):
        raise ValueError('更新包缺少必要组件')
    return manifest


def alive(pid):
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False


def wait_exit(pid, timeout=120, cancel=None):
    deadline = time.monotonic() + timeout
    while alive(pid):
        if cancel and cancel.exists():
            raise RuntimeError("主程序已取消本次安装")
        if time.monotonic() >= deadline:
            raise TimeoutError("软件尚未退出，已取消安装，请关闭软件后重新尝试")
        time.sleep(0.2)


def rollback(root: Path, session: Path):
    journal_path = session / "journal.json"
    if not journal_path.exists():
        return
    journal = read_json(journal_path)
    for change in reversed(journal["changes"]):
        target = destination(root, change["path"])
        if change["existed"]:
            backup = session / "backup" / change["path"]
            if not backup.is_file():
                raise RuntimeError(f"回滚备份缺失：{change['path']}")
            temporary = target.with_name(target.name + ".framekeep-rollback")
            shutil.copy2(backup, temporary)
            os.replace(temporary, target)
        else:
            target.unlink(missing_ok=True)
    installation.rollback_registration(root,session)
    marker_before=session/'installation-marker.json'
    if marker_before.exists():
        before=read_json(marker_before)
        if not installation.same_path(before['root'],root):raise ValueError('安装资料目录备份无效')
        marker=root/installation.MARKER
        if before['existed']:marker.write_bytes((session/'installation-marker.before').read_bytes())
        else:marker.unlink(missing_ok=True)
    journal["status"] = "rolled-back"
    write_json(journal_path, journal)


def install(root: Path, session: Path, manifest, fail_after=None):
    journal = {"status": "installing", "version": manifest["version"], "changes": []}
    for record in manifest["files"]:
        destination(root, record["path"])
    if shutil.disk_usage(root).free < sum(r["size"] for r in manifest["files"]) * 2 + 20 * 1024 ** 2:
        raise OSError("软件所在磁盘空间不足")
    try:
        for index, record in enumerate(manifest["files"]):
            relative = record["path"]
            target = destination(root, relative)
            existed = target.is_file()
            if existed:
                backup = session / "backup" / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
            target.parent.mkdir(parents=True, exist_ok=True)
            journal["changes"].append({"path": relative, "existed": existed})
            write_json(session / "journal.json", journal)
            temporary = target.with_name(target.name + ".framekeep-update")
            shutil.copy2(session / "stage" / relative, temporary)
            os.replace(temporary, target)
            if target.stat().st_size != record["size"] or sha256(target) != record["sha256"]:
                raise OSError(f"安装后文件校验失败：{relative}")
            emit(f"正在安装 {index + 1}/{len(manifest['files'])}", 30 + int((index + 1) * 55 / len(manifest["files"])))
            if fail_after is not None and index + 1 >= fail_after:
                raise OSError("测试注入写入失败")
        journal["status"] = "awaiting-health"
        write_json(session / "journal.json", journal)
    except BaseException:
        rollback(root, session)
        raise


def preparation_deadline(session, manifest, pid, deadline, limit):
    """只有本次新版进程的近期准备心跳能延长健康检查，有24小时上限。"""
    preparing = session / 'healthy.json.preparing'
    try:
        if preparing.exists() and time.time() - preparing.stat().st_mtime < 45:
            state = read_json(preparing)
            if (state.get('version') == manifest['version'] and state.get('releaseId') == manifest.get('releaseId', '00000000000000')
                    and state.get('pid') == pid and state.get('status') not in ('error', 'cancelled')):
                return min(limit, time.monotonic() + 210)
    except (OSError, ValueError):
        pass
    return deadline


def restart(root, session, health=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith('FRAMEKEEP_UPDATE_')}
    settings = read_json(session / 'session.json')
    env['FRAMEKEEP_PORT'] = str(settings.get('port', 8769))
    if settings.get('data'): env['FRAMEKEEP_DATA'] = settings['data']
    if health: env['FRAMEKEEP_UPDATE_HEALTH_FILE'] = str(session / 'healthy.json')
    return subprocess.Popen([str(root / '末帧选片.exe')], cwd=root, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def verify_envelope(envelope):
    payload = envelope['payload']
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(TRUST['publicKey'])).verify(
        bytes.fromhex(envelope['signature']), payload.encode('utf-8'))
    manifest = json.loads(payload)
    if (manifest.get('protocol'), manifest.get('product'), manifest.get('platform')) != (1, 'framekeep', 'win32-x64'):
        raise ValueError('更新清单不兼容')
    version(manifest['version']); version(manifest['minVersion'])
    if not re.fullmatch(r'\d{14}', manifest['releaseId']): raise ValueError('发布序号无效')
    package = manifest['package']
    if not isinstance(package['size'], int) or not 0 < package['size'] < 2 * 1024**3:
        raise ValueError('安装包大小无效')
    if not re.fullmatch(r'[0-9a-f]{64}', package['sha256']): raise ValueError('安装包校验值无效')
    if not package['url'].startswith('https://modelscope.cn/api/v1/models/' + TRUST['repository'] + '/repo?'):
        raise ValueError('更新包不属于当前更新源')
    return manifest


def fetch_envelope():
    request = urllib.request.Request(TRUST['manifestUrl'], headers={'Cache-Control': 'no-cache', 'Range': 'bytes=0-'})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(16 * 1024**2 + 1)
    if len(raw) > 16 * 1024**2: raise ValueError('清单超过大小限制')
    envelope = json.loads(raw)
    return envelope, verify_envelope(envelope)


def download(meta, target):
    """沿用 VoxCPM 的分片保留、Range 对齐及完整哈希检查。"""
    target = Path(target); part = target.with_suffix('.part')
    if target.is_file() and target.stat().st_size == meta['size'] and sha256(target) == meta['sha256']:
        return
    last = None
    for attempt in range(3):
        try:
            offset = part.stat().st_size if part.exists() else 0
            if offset == meta['size'] and sha256(part) == meta['sha256']:
                os.replace(part, target); return
            if offset >= meta['size']: part.unlink(); offset = 0
            request = urllib.request.Request(meta['url'], headers={'Range': f'bytes={offset}-', 'User-Agent': 'Framekeep-Updater/1'})
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status == 206:
                    if response.headers.get('Content-Range') != f"bytes {offset}-{meta['size']-1}/{meta['size']}":
                        raise ValueError('下载续传范围异常')
                elif response.status == 200: offset = 0
                else: raise OSError('下载 HTTP 状态异常')
                done = offset; last_emit = time.monotonic()
                with part.open('ab' if offset else 'wb') as output:
                    while block := response.read(256 * 1024):
                        done += len(block)
                        if done > meta['size']: raise ValueError('下载超出预期大小')
                        output.write(block)
                        if time.monotonic() - last_emit > .3:
                            emit(f"正在下载 {done/1024**2:.1f} / {meta['size']/1024**2:.1f} MB", int(done*18/meta['size']))
                            last_emit = time.monotonic()
                    output.flush(); os.fsync(output.fileno())
            if part.stat().st_size != meta['size'] or sha256(part) != meta['sha256']:
                if part.stat().st_size >= meta['size']: part.unlink()
                raise ValueError('下载校验失败，保留有效分片后重试')
            os.replace(part, target); return
        except (OSError, ValueError) as error: last = error
    raise RuntimeError(f'下载失败：{last}')


@contextmanager
def update_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    lock = (root / '.framekeep-update.lock').open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
            try: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError: raise RuntimeError('此目录已有安装或更新进行中')
        yield
    finally:
        lock.close()


def check_root(root, install_mode):
    if root.is_symlink() or root.is_junction(): raise ValueError('安装目录不能是链接')
    for parent in root.parents:
        if parent.is_symlink() or parent.is_junction(): raise ValueError('安装目录父路径不能是链接')
    if len(root.parts) < 3: raise ValueError('请选择独立的软件子目录')
    if root.exists() and not root.is_dir(): raise ValueError('安装目录已被文件占用')
    known = (root / '末帧选片.exe').is_file()
    # 安装允许非空目录；只覆盖签名清单内文件，由安装事务备份并在失败时恢复。
    if not install_mode and not known: raise ValueError('原软件目录无效')


def recover_pending(root):
    pending = root / '.update-state/pending.json'
    if not pending.exists(): return
    state = read_json(pending); previous = Path(state['session'])
    if state.get('pid') and alive(state['pid']): raise RuntimeError('上次更新仍在执行')
    if Path(read_json(previous / 'session.json')['root']).resolve() != root:
        raise ValueError('恢复会话与安装目录不匹配')
    journal = read_json(previous / 'journal.json') if (previous / 'journal.json').exists() else {}
    if journal.get('status')=='awaiting-health' and (root/installation.MARKER).is_file() and (previous/'healthy.json').is_file():
        state=read_json(root/installation.MARKER);health=read_json(previous/'healthy.json')
        manifest=verify_envelope(read_json(previous/'envelope.json'))
        if state.get('status')=='installed' and state.get('releaseId')==health.get('releaseId')==manifest['releaseId'] and all(destination(root,r['path']).is_file() and sha256(destination(root,r['path']))==r['sha256'] for r in manifest['files']):
            journal['status']='complete';write_json(previous/'journal.json',journal)
    if journal.get('status') not in ('complete', 'rolled-back'):
        emit('正在恢复上次中断的更新…', 0); rollback(root, previous)
    pending.unlink()


def register_install(root, manifest,session=None):
    installation.complete_registration(root,manifest,session=session)


def check_storage(root,session,manifest):
    size=sum(r['size'] for r in manifest['files']);margin=64*1024**2
    backups=sum((root/r['path']).stat().st_size for r in manifest['files'] if (root/r['path']).is_file())
    target=size+max(r['size'] for r in manifest['files'])+margin
    staging=manifest['package']['size']+size+backups+margin
    if root.stat().st_dev==session.stat().st_dev:
        if shutil.disk_usage(root).free<target+staging:raise OSError('安装及临时目录所在分区空间不足，请选择其他分区或释放空间')
    elif shutil.disk_usage(root).free<target or shutil.disk_usage(session).free<staging:
        raise OSError('安装分区或用户临时分区空间不足，请释放空间后重试')


def run_uninstall(session,settings):
    root=Path(os.path.abspath(settings['root']))
    emit('正在检查安装记录和运行状态…',10)
    with update_lock(root):
        result=installation.uninstall(root)
        write_json(session/'status.json',result)
    (root/'.framekeep-update.lock').unlink(missing_ok=True)
    for directory in (root/'.update-state',root):
        try:directory.rmdir()
        except OSError:pass
    emit('已卸载应用；照片、评级、设置及模型资料已保留。',100)


def prepare_components(root, data, session, settings):
    if not (root/'update/component-manifest.json').is_file():raise ValueError('此软件包不支持自动准备模型，请使用新版完整安装包')
    report=session/'components-status.json';report.unlink(missing_ok=True)
    command=[str(root/'engine/framekeep-engine.exe'),'--prepare-components',str(data),str(settings.get('componentSeed','')),str(report)]
    environment={**os.environ,'FRAMEKEEP_PREPARE_DRIVER':'1' if settings.get('installDriver',True) else '0'}
    child=subprocess.Popen(command,env=environment,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    last=None
    while child.poll() is None:
        if report.is_file():
            try:state=read_json(report)
            except (OSError,ValueError):state={}
            if state!=last:
                done=state.get('done',0);total=state.get('total',0)
                message=state.get('message','正在准备内置模型及依赖')
                if total:message+=f' · {done/1024**2:.1f} / {total/1024**2:.1f} MB'
                emit(message,85);last=state
        time.sleep(.25)
    state=read_json(report) if report.is_file() else {}
    if child.returncode or state.get('status')!='ready':raise RuntimeError(state.get('message','组件准备进程未完成，请查看日志后重试'))
    return state


def run(session):
    settings = read_json(session / 'session.json')
    if settings.get('mode')=='uninstall':return run_uninstall(session,settings)
    root = Path(os.path.abspath(settings['root']))
    install_mode = settings.get('mode') == 'install'
    check_root(root, install_mode)
    with update_lock(root):
        recover_pending(root)
        current = read_json(root/'update-config.json') if (root/'update-config.json').exists() else {'version':'1.0.0', 'releaseId':'00000000000000'}
        settings.update(currentVersion=current['version'], currentReleaseId=current['releaseId'], repair=install_mode,
                        session=str(session), root=str(root))
        write_json(session/'session.json', settings)
        emit('正在检查魔搭签名更新清单…', 0)
        if settings.get('localEnvelope'):
            envelope=read_json(Path(settings['localEnvelope']));manifest=verify_envelope(envelope)
        else:envelope, manifest = fetch_envelope()
        if version(manifest['version']) < version(current['version']) or (
            version(manifest['version']) == version(current['version']) and manifest['releaseId'] < current['releaseId']):
            raise ValueError('拒绝降级已安装的新版')
        if not install_mode and (manifest['version'],manifest['releaseId']) == (current['version'],current['releaseId']):
            raise ValueError('当前已是最新版本')
        if install_mode and os.name == 'nt':
            # 不强制结束用户程序；安装/修复前要求本目录进程已正常关闭。
            script = "$p=Get-CimInstance Win32_Process | Where-Object {$_.ExecutablePath -and ($_.ExecutablePath -eq $env:FRAMEKEEP_EXE -or $_.ExecutablePath -eq $env:FRAMEKEEP_ENGINE)};if($p){exit 2}"
            result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
                env={**os.environ,'FRAMEKEEP_EXE':str(root/'末帧选片.exe'),'FRAMEKEEP_ENGINE':str(root/'engine/framekeep-engine.exe')},
                capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode: raise RuntimeError('请先保存任务并关闭此安装目录的末帧选片，再重试安装或修复')
        if install_mode:installation.require_port_free(settings.get('port',8769))
        write_json(session/'envelope.json', envelope)
        if not installation.writable_directory(root):raise OSError('安装目录不可写，请选择无需管理员权限的目录')
        check_storage(root,session,manifest)
        backend=installation.integration_backend()
        if install_mode and settings.get('integration',True) and backend:
            previous_root=backend.preferences().get('InstallLocation','')
            if previous_root and not installation.same_path(previous_root,root) and installation.running_application(previous_root):raise RuntimeError('请先保存并关闭旧安装位置的末帧选片，再安装到新目录')
        data=installation.data_directory(root,settings.get('data'),backend)
        if not installation.writable_directory(data):raise OSError('资料目录不可写，未创建空数据库，请调整原资料目录权限')
        settings['data']=str(data);write_json(session/'session.json',settings)
        if settings.get('localPackage'):
            source=Path(settings['localPackage'])
            if source.stat().st_size!=manifest['package']['size'] or sha256(source)!=manifest['package']['sha256']:raise ValueError('离线软件包校验失败，请重新完整解压安装包')
            if not (session/'package.zip').exists() or sha256(session/'package.zip')!=manifest['package']['sha256']:shutil.copy2(source,session/'package.zip')
        else:download(manifest['package'], session/'package.zip')
        emit('正在验证签名及全部文件…', 20)
        manifest = validate(session, settings)
        parent = settings.get('parentPid', 0)
        if parent:
            write_json(session/'ready.json', {'session': str(session), 'pid': os.getpid(), 'releaseId': manifest['releaseId']})
            emit('已校验完成，等待工作台保存并正常退出…', 25)
            wait_exit(parent, cancel=session/'cancel.json')
            if settings.get('enginePid'): wait_exit(settings['enginePid'], 30)
        pending = root/'.update-state/pending.json'
        write_json(pending, {'session': str(session), 'pid': os.getpid()})
        child = None
        marker=root/installation.MARKER;old_marker=marker.read_bytes() if marker.exists() else None
        write_json(session/'installation-marker.json',{'root':str(root),'existed':old_marker is not None})
        if old_marker is not None:(session/'installation-marker.before').write_bytes(old_marker)
        try:
            install(root, session, manifest)
            integrated=settings.get('integration',install_mode or installation.registered_here(root))
            state=installation.prepare_marker(root,data,manifest,integrated)
            components=prepare_components(root,data,session,settings) if settings.get('prepareComponents') else None
            emit('正在启动新版并验证工作台和引擎…', 90)
            (session/'healthy.json').unlink(missing_ok=True)
            child = restart(root, session)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if child.poll() is not None: raise RuntimeError('新版程序提前退出')
                if (session/'healthy.json').exists():
                    health = read_json(session/'healthy.json')
                    if (health.get('pid'),health.get('version'),health.get('releaseId')) == (child.pid, manifest['version'], manifest['releaseId']): break
                time.sleep(.25)
            else: raise TimeoutError('新版界面与引擎健康检查超时')
            register_install(root,state,session)
            journal = read_json(session/'journal.json'); journal['status'] = 'complete'; write_json(session/'journal.json',journal)
            write_json(session/'status.json', {'status':'complete','version':manifest['version'],'releaseId':manifest['releaseId'],'pid':child.pid,'components':components})
            emit('安装更新完成，工作台与本地引擎均已就绪。',100)
        except BaseException as error:
            if child and child.poll() is None:
                subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
                wait_exit(child.pid, 20)
            rollback(root,session)
            write_json(session/'status.json',{'status':'rolled-back','message':str(error)})
            if not install_mode: restart(root,session,False)
            raise
        finally:
            if (session/'journal.json').exists() and read_json(session/'journal.json')['status'] in ('complete','rolled-back'):
                pending.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('session', type=Path,nargs='?')
    parser.add_argument('--recover', action='store_true')
    parser.add_argument('--install-plan',type=Path)
    args = parser.parse_args()
    try:
        if args.install_plan:
            write_json(args.install_plan,installation.install_plan())
        elif args.session is None:raise ValueError('缺少安装会话')
        elif args.recover:
            with update_lock(args.session): recover_pending(args.session.resolve(strict=True))
        else: run(args.session.resolve(strict=True))
    except BaseException as error:
        if args.session:
            status = args.session/'status.json'
            if not status.exists(): write_json(status,{'status':'error','message':str(error)})
        emit(f'未完成：{error}。下载分片、日志和备份均已保留。')
        sys.exit(1)
