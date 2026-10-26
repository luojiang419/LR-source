"""离线核验 NIF 外层及 XOR；仅解释已核对哈希的实机样本，不输出配置明文。"""

import argparse
import hashlib
import json
import struct
from pathlib import Path

XOR_CONSTANT = 0xADB6B4B0CFCDCFCC
CAPTURE_SHA256 = "60224a81eb378750b858d18cbaa2e25b75450f9bd4f95e9c2c1b95b52e2e12d6"
MAX_SIZE = 8192


def block(buf, offset, end):
    if not 0 <= offset <= end <= len(buf) or end - offset < 12:
        raise ValueError("truncated container header")
    size, tag, subid = struct.unpack_from("<I4sI", buf, offset)
    if size > end - offset - 12:
        raise ValueError("container exceeds its outer boundary")
    return size, tag, subid, offset + 12 + size


def transform(blob, start, end, seed):
    if not 0 <= start <= end <= len(blob):
        raise ValueError("invalid transformation range")
    key = struct.pack("<Q", seed ^ XOR_CONSTANT)
    out = bytearray(blob)
    for i in range(start, end):
        out[i] ^= key[(i - start) % 8]
    return bytes(out)


def decode_outer(blob):
    """仅验证 HEAD/DATA 外层；内层某些声明长度不兼容，禁止递归重建。"""
    if not 24 <= len(blob) <= MAX_SIZE:
        raise ValueError("unexpected NIF size")
    _, tag, _, head_end = block(blob, 0, len(blob))
    if tag != b"HEAD":
        raise ValueError("HEAD required")
    fields = {}
    pos = 12
    while pos < head_end:
        size, tag, _, next_pos = block(blob, pos, head_end)
        if tag in (b"ofcn", b"ofcy"):
            if tag in fields:
                raise ValueError("duplicate transformation field")
            expected = 4 if tag == b"ofcn" else 8
            if size != expected:
                raise ValueError("invalid transformation field size")
            fields[tag] = int.from_bytes(blob[pos + 12:next_pos], "little")
        pos = next_pos
    size, tag, _, data_end = block(blob, head_end, len(blob))
    if tag != b"DATA" or data_end != len(blob):
        raise ValueError("single final DATA container required")
    if fields.get(b"ofcn") not in (0, 1):
        raise ValueError("unsupported or missing transformation switch")
    if fields[b"ofcn"] and b"ofcy" not in fields:
        raise ValueError("missing transformation seed")
    start = head_end + 12
    enabled = bool(fields[b"ofcn"])
    decoded = transform(blob, start, data_end, fields[b"ofcy"]) if enabled else blob
    restored = transform(decoded, start, data_end, fields[b"ofcy"]) if enabled else decoded
    if restored != blob:
        raise ValueError("byte identity verification failed")
    if size < 12 or decoded[start + 4:start + 8] != b"NSET":
        raise ValueError("decoded root is not NSET")
    return decoded, restored, head_end, start, enabled


def analyze(blob):
    decoded, restored, data_offset, start, enabled = decode_outer(blob)
    digest = hashlib.sha256(blob).hexdigest()
    known = digest == CAPTURE_SHA256
    confirmed = []
    # 偏移来自静态 parser/托管枚举及这一份固定哈希样本；不推广到其他固件或槽。
    if known:
        leaves = (
            (0xC8, b"smtd", 2, "InWireless", "NSET 传输方法"),
            (0xD8, b"nwnm", 1, None, "内嵌 NWST 数量"),
            (0xE8, b"acnm", 1, None, "内嵌 ACTS 数量"),
            (0x130, b"nmtd", 2, "InWireless", "NWST 传输方法"),
            (0x15C, b"ctyp", 2, "MICRO_AP_AUTO", "相机自动接入点"),
            (0x464, b"mode", 1, "PTP_PC", "EOS Utility 功能角色"),
        )
        for offset, tag, expected, enum_name, meaning in leaves:
            size, actual_tag, _, _ = block(decoded, offset, len(decoded))
            value = struct.unpack_from("<I", decoded, offset + 12)[0]
            if size != 4 or actual_tag != tag or value != expected:
                raise ValueError("captured profile field verification failed")
            confirmed.append({"tag": tag.decode("ascii"), "header_offset": offset,
                              "value_offset": offset + 12, "uint32": value,
                              "enum": enum_name, "meaning": meaning})
    return {
        "input_sha256": digest, "input_size": len(blob),
        "known_capture": known, "set_number": 0 if known else None,
        "obfuscation_enabled": enabled, "DATA_header_offset": data_offset,
        "DATA_payload_offset": start, "DATA_size": len(blob) - start,
        "roundtrip_identical": restored == blob,
        "roundtrip_sha256": hashlib.sha256(restored).hexdigest(),
        "confirmed_fields": confirmed,
        "conclusion": "捕获的 SET0 已保存相机自动接入点及 EOS Utility；未证明控制新增设备的全局默认入口" if known else "未知样本仅验证外层与变换可逆，不解释字段语义",
        "limitations": [
            "算法来自固定 SHA 的 EdsNIFParse.dll 静态汇编；未执行目标 DLL",
            "XOR 可逆不等于 checksum 正确、机身允许写入或恢复成功；未重算 chsm",
            "NWST/IPV4/IPV6 内层声明长度与实际边界有差异，不递归重建或重新序列化",
            "明文仅在内存中，不输出字符串、地址、设备标识、密码、种子或 XOR key",
            "未知哈希样本不复用固定字段偏移；未进行任何相机调用",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.input.stat().st_size > MAX_SIZE:
        parser.error("input exceeds the verified SDK buffer limit")
    if args.report and args.report.resolve() == args.input.resolve():
        parser.error("report must not replace the input")
    try:
        result = analyze(args.input.read_bytes())
    except ValueError as error:
        parser.error(str(error))
    if args.report:
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in
                      ("input_sha256", "input_size", "known_capture", "roundtrip_identical", "confirmed_fields")},
                     ensure_ascii=False, indent=2))
