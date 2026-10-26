"""限指定 R6m2/1.7.0 与 SDK 哈希，只读一个 WFT 配置；原始数据仅存 .runtime。"""

import argparse
import ctypes as c
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

SDK_SHA256 = "1c23a9984bd671c4bab8470430e1ab9269de00f3e3405cc9213249e3f7fb454e"
EXPECTED_MAX = 0x2000  # CPtpCamera 内部固定临时缓冲；不是由调用者容量参数控制。
ALLOCATION = 1024 * 1024


def inspect(dll_path, output_dir, set_number, *, register_local_sdk=False):
    if hashlib.sha256(dll_path.read_bytes()).hexdigest() != SDK_SHA256:
        raise ValueError("SDK hash mismatch; the private ABI was verified only for the recorded DLL")
    if c.sizeof(c.c_void_p) != 8:
        raise ValueError("64-bit Python is required")
    private_root = (Path(__file__).resolve().parents[2] / ".runtime").resolve()
    if not output_dir.is_relative_to(private_root):
        raise ValueError("raw configuration must remain inside this workspace's .runtime")
    if output_dir.exists():
        raise ValueError("output directory already exists; choose a new path to preserve evidence")
    output_dir.mkdir(parents=True)
    result = {"observed_at": datetime.now(timezone.utc).isoformat(), "sdk_sha256": SDK_SHA256,
              "scope": "host-local SDK registration when requested; identity and private network reads; no camera property writes/WFT Set/Change/Delete/capture/firmware calls",
              "set_number": set_number, "calls": [], "raw_saved": False,
              "raw_saved_scope": "EdsGetWftData response only; separate private property snapshots may be saved"}
    with os.add_dll_directory(str(dll_path.parent)):
        sdk = c.WinDLL(str(dll_path))
        u32, ref = c.c_uint32, c.c_void_p
        signatures = {
            "EdsInitializeSDK": [], "EdsTerminateSDK": [],
            "EdsGetCameraList": [c.POINTER(ref)], "EdsGetChildCount": [ref, c.POINTER(u32)],
            "EdsGetChildAtIndex": [ref, c.c_int32, c.POINTER(ref)],
            "EdsOpenSession": [ref], "EdsCloseSession": [ref], "EdsRelease": [ref],
            "EdsGetPropertySize": [ref, u32, c.c_int32, c.POINTER(u32), c.POINTER(u32)],
            "EdsGetPropertyData": [ref, u32, c.c_int32, u32, ref],
            "EdsGetWftData": [ref, u32, c.POINTER(u32), ref],
        }
        if register_local_sdk:
            signatures["EdsSetPropertyData"] = [ref, u32, c.c_int32, u32, ref]
        for name, args in signatures.items():
            fn = getattr(sdk, name)
            fn.argtypes, fn.restype = args, u32

        def call(name, *args):
            code = int(getattr(sdk, name)(*args))
            result["calls"].append({"api": name, "code": f"0x{code:08X}"})
            return code

        camera_list, camera = ref(), ref()
        initialized, opened = False, False
        try:
            if call("EdsInitializeSDK"):
                return result
            initialized = True
            if call("EdsGetCameraList", c.byref(camera_list)):
                return result
            count = u32()
            if call("EdsGetChildCount", camera_list, c.byref(count)):
                return result
            result["camera_count"] = count.value
            if count.value != 1:
                result["stop_reason"] = "exactly one camera is required"
                return result
            if call("EdsGetChildAtIndex", camera_list, 0, c.byref(camera)):
                return result
            opened = call("EdsOpenSession", camera) == 0
            if not opened:
                return result
            for name, prop in (("product", 2), ("firmware", 7)):
                dtype, size = u32(), u32()
                if call("EdsGetPropertySize", camera, prop, 0, c.byref(dtype), c.byref(size)):
                    return result
                if not 0 < size.value <= 1024:
                    raise ValueError("unexpected identity size")
                data = c.create_string_buffer(size.value)
                if call("EdsGetPropertyData", camera, prop, 0, size, data):
                    return result
                result[name] = data.raw.split(b"\0", 1)[0].decode("utf-8", "replace")
            if result.get("product") != "Canon EOS R6m2" or result.get("firmware") != "1.7.0":
                result["stop_reason"] = "camera model/firmware mismatch"
                return result
            if register_local_sdk:
                # 匹配 DLL 的 0x180007850 分支仅设置本地对象 +0x388，不发送 PTP。
                # 常量来自本机佳能 NESDK_MAUI EOSCamera.connect，不使用未知机身设置。
                local_value = u32(0xC53)
                code = call("EdsSetPropertyData", camera, 0x01000000, 0x22E1, 4, c.byref(local_value))
                result["local_sdk_registration"] = {"code": f"0x{code:08X}", "effect": "process-local SDK camera object access flag; destroyed with SDK instance"}
                if code:
                    result["stop_reason"] = "local SDK registration failed"
                    return result
                result["private_properties"] = {}
                for label, prop in (("communication_mode", 0x01000023), ("network_set_info", 0x0100005A),
                                    ("network_info", 0x0100005B), ("action_info", 0x0100005C)):
                    dtype, property_size = u32(), u32()
                    code = call("EdsGetPropertySize", camera, prop, 0, c.byref(dtype), c.byref(property_size))
                    info = {"sdk_property": f"0x{prop:08X}", "size_query_code": f"0x{code:08X}"}
                    result["private_properties"][label] = info
                    if code or not 0 < property_size.value <= ALLOCATION:
                        continue
                    info.update(bytes=property_size.value, data_type=dtype.value)
                    data = c.create_string_buffer(property_size.value)
                    code = call("EdsGetPropertyData", camera, prop, 0, property_size, data)
                    info["read_code"] = f"0x{code:08X}"
                    if code:
                        continue
                    payload = data.raw
                    (output_dir / (label + "-private.bin")).write_bytes(payload)
                    info["raw_sha256"] = hashlib.sha256(payload).hexdigest()
                    if label == "communication_mode" and len(payload) == 4:
                        info["raw_uint32"] = int.from_bytes(payload, "little")
                    c.memset(c.addressof(data), 0, c.sizeof(data))
            size = u32(EXPECTED_MAX)
            buffer = c.create_string_buffer(ALLOCATION + 64)
            guard = bytes([0xA5]) * 64
            c.memmove(c.addressof(buffer) + ALLOCATION, guard, 64)
            code = call("EdsGetWftData", camera, set_number, c.byref(size), buffer)
            result["reported_size"] = size.value
            result["guard_intact"] = buffer.raw[ALLOCATION:ALLOCATION + 64] == guard
            # 原 SDK 在失败时也会拷贝内部临时缓冲；失败数据可能未初始化，绝不导出。
            if code == 0 and result["guard_intact"] and 0 < size.value <= EXPECTED_MAX:
                payload = buffer.raw[:size.value]
                (output_dir / "wft-private.bin").write_bytes(payload)
                result.update(raw_saved=True, raw_bytes=len(payload), raw_sha256=hashlib.sha256(payload).hexdigest())
            else:
                result["stop_reason"] = "read failed or returned an unexpected size/guard; no raw bytes saved"
            c.memset(c.addressof(buffer), 0, c.sizeof(buffer))
        finally:
            if opened:
                call("EdsCloseSession", camera)
            if camera:
                call("EdsRelease", camera)
            if camera_list:
                call("EdsRelease", camera_list)
            if initialized:
                call("EdsTerminateSDK")
            (output_dir / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dll", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--set-number", type=int, choices=[0, 1], default=0, help="佳能网络工具界面SET1减1得到编号0；仅允许已分析的0/1，不枚举未知范围")
    parser.add_argument("--register-local-sdk", action="store_true", help="执行已确认仅修改本地 SDK 对象的佳能网络工具初始化，再读取私有属性")
    args = parser.parse_args()
    print(json.dumps(inspect(args.dll.resolve(), args.output_dir.resolve(), args.set_number,
                             register_local_sdk=args.register_local_sdk), ensure_ascii=False, indent=2))
