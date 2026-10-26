"""独立接收器验收入口：不写照片，限时采集后恢复普通鼠标。"""
import argparse
from collections import Counter
import json
from pathlib import Path
import threading
import time

from mouse_native.interception import Interception
from mouse_native.lease import ReceiverLease, check_heartrelay
from mouse_native.mouse_transport import Win32MouseTransport
from mouse_native.profiles import PROFILES
from mouse_native.recovery import RecoveryHotkey


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=[p.id for p in PROFILES], default='logitech-c534')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--capture', action='store_true', help='限时接管所选鼠标，仅统计输入')
    mode.add_argument('--release', action='store_true', help='向所选配置发送恢复普通鼠标请求')
    parser.add_argument('--seconds', type=int, default=30)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 120:
        parser.error('--seconds 必须在 1 到 120 之间')
    result = {'profile': args.profile, 'actions': {}, 'released': False}
    bridge = hotkey = lease = None
    stopped = threading.Event()
    counts = Counter()

    def action(name='increase'):
        counts[name] += 1

    def stop(message):
        result['stop_reason'] = message
        stopped.set()
        if bridge:
            bridge.stop()

    try:
        check_heartrelay()
        lease = ReceiverLease()
        if not args.capture:
            transport = Win32MouseTransport()
            result['devices'] = transport.enumerate()
            if args.release:
                devices = [d for d in result['devices'] if d['profile'] == args.profile]
                if len(devices) != 1:
                    raise ValueError('恢复前必须确认唯一的目标接收器')
                handle = transport.open(devices[0]['driver_id'])
                try:
                    from mouse_native.profiles import hardware_key
                    if hardware_key(transport.hardware(handle)) != hardware_key(devices[0]['hardware']):
                        raise ValueError('设备身份已变化，拒绝操作')
                    transport.set_filter(handle, 0)
                    transport.set_event(handle, None)
                    result['stop_reason'] = '已向所选接收器发送释放请求'
                finally:
                    transport.close(handle)
        else:
            bridge = Interception(None, action, stop)
            if bridge.error:
                raise ValueError(bridge.error)
            devices = [d for d in bridge.refresh() if d['profile'] == args.profile]
            if len(devices) != 1:
                raise ValueError('所选配置必须恰有一只接收器，请检查插入状态')
            result['device'] = devices[0]
            hotkey = RecoveryHotkey(stop)
            bridge.start(devices[0]['driver_id'], expected_hardware=devices[0]['hardware'])
            print('已开始限时采集；请移动鼠标、单击左右键并双向滚轮。Ctrl+Alt+F11 可立即恢复。', flush=True)
            started = time.monotonic()
            stopped.wait(args.seconds)
            result['elapsed_seconds'] = round(time.monotonic() - started, 3)
            result['diagnostics'] = bridge.diagnostics()
            bridge.stop()
            result['after_stop'] = bridge.diagnostics()
            result['actions'] = dict(counts)
    except Exception as exc:
        result['error'] = str(exc)
    finally:
        try:
            if bridge:
                bridge.close()
            result['released'] = True
        finally:
            if hotkey:
                hotkey.close()
            if lease:
                lease.close()
        encoded = json.dumps(result, ensure_ascii=False, indent=2)
        print(encoded, flush=True)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded + '\n', encoding='utf8')
    return 1 if result.get('error') else 0


if __name__ == '__main__':
    raise SystemExit(main())
