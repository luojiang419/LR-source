"""只读读取 EDSDK 相机型号/固件；不读取序列号，不设置属性或发送相机命令。"""

import argparse
import ctypes as c
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def inspect_camera(dll_path: Path, *, network_mode: bool = False) -> dict:
    result = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "sdk_sha256": hashlib.sha256(dll_path.read_bytes()).hexdigest(),
        "scope": ("identity + SDK network communication mode 0x01000023; read only" if network_mode
                  else "identity only; no property writes, capture, or firmware update"),
        "calls": [],
        "cameras": [],
    }
    with os.add_dll_directory(str(dll_path.parent)):
        sdk = c.WinDLL(str(dll_path))
        u32, ref = c.c_uint32, c.c_void_p
        signatures = {
            "EdsInitializeSDK": [], "EdsTerminateSDK": [],
            "EdsGetCameraList": [c.POINTER(ref)],
            "EdsGetChildCount": [ref, c.POINTER(u32)],
            "EdsGetChildAtIndex": [ref, c.c_int32, c.POINTER(ref)],
            "EdsOpenSession": [ref], "EdsCloseSession": [ref],
            "EdsGetPropertySize": [ref, u32, c.c_int32, c.POINTER(u32), c.POINTER(u32)],
            "EdsGetPropertyData": [ref, u32, c.c_int32, u32, ref],
            "EdsRelease": [ref],
        }
        for name, args in signatures.items():
            fn = getattr(sdk, name)
            fn.argtypes, fn.restype = args, u32

        def call(name, *args):
            code = int(getattr(sdk, name)(*args))
            result["calls"].append({"api": name, "code": f"0x{code:08X}"})
            return code

        camera_list = ref()
        initialized = call("EdsInitializeSDK") == 0
        if not initialized:
            return result
        try:
            if call("EdsGetCameraList", c.byref(camera_list)):
                return result
            count = u32()
            if call("EdsGetChildCount", camera_list, c.byref(count)):
                return result
            result["camera_count"] = count.value
            for i in range(count.value):
                camera = ref()
                if call("EdsGetChildAtIndex", camera_list, i, c.byref(camera)):
                    continue
                opened = False
                entry = {"index": i}
                result["cameras"].append(entry)
                try:
                    # SDK 通信会话是可恢复连接，不写入持久设置；不抢占或终止 EOS Utility。
                    opened = call("EdsOpenSession", camera) == 0
                    entry["session_opened"] = opened
                    if not opened:
                        continue
                    for name, prop in (("product", 0x2), ("firmware", 0x7)):
                        dtype, size = u32(), u32()
                        if call("EdsGetPropertySize", camera, prop, 0, c.byref(dtype), c.byref(size)):
                            continue
                        if not 0 < size.value <= 1024:
                            entry[name + "_error"] = "unexpected size"
                            continue
                        data = c.create_string_buffer(size.value)
                        if call("EdsGetPropertyData", camera, prop, 0, size, data) == 0:
                            entry[name] = data.raw.split(b"\0", 1)[0].decode("utf-8", "replace")
                    if network_mode:
                        # 由匹配 DLL 的 D175 setter/cache 指令映射确认；不推断它是 AP/STA。
                        prop, dtype, size = 0x01000023, u32(), u32()
                        code = call("EdsGetPropertySize", camera, prop, 0, c.byref(dtype), c.byref(size))
                        info = {"sdk_property": "0x01000023", "size_query_code": f"0x{code:08X}"}
                        entry["network_communication_mode"] = info
                        if code == 0:
                            info.update(data_type=dtype.value, bytes=size.value)
                            if size.value == 4:
                                value = u32()
                                code = call("EdsGetPropertyData", camera, prop, 0, size, c.byref(value))
                                info["read_code"] = f"0x{code:08X}"
                                if code == 0:
                                    info["raw_uint32"] = value.value
                finally:
                    if opened:
                        call("EdsCloseSession", camera)
                    call("EdsRelease", camera)
        finally:
            if camera_list:
                call("EdsRelease", camera_list)
            call("EdsTerminateSDK")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dll", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--network-mode", action="store_true", help="追加只读私有 SDK 通信模式；取值不等于 AP/客户端")
    args = parser.parse_args()
    output = inspect_camera(args.dll.resolve(), network_mode=args.network_mode)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
