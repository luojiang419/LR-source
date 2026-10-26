"""R6m2 FIRv6 只读结构与签名检查；可选反例仅修改内存，不输出修改后的固件。

格式及公开验证密钥来源：lclevy/d8_oracle 的 d810_verif.py、fir_tool2.py。
此工具不提供解密、签名生成、机身访问或固件刷写。
"""

import argparse
import hashlib
import json
import struct
from pathlib import Path

from Crypto.Hash import SHA256
from Crypto.PublicKey import ECC
from Crypto.Signature import DSS


MODEL = 0x80000481
PUBLIC_KEY = bytes.fromhex(
    "04"
    "5e3b144079d39dc0ef01e72cb8d5eba8c4bec6f95b00bd0393d914f146c16330de731"
    "d6c646d5b648c1e4db8c1f36752ef41aefa71173c8deabb395a38521220"
)


def u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def checksum(data):
    return (~(sum(data[:0x20]) + sum(data[0x24:]))) & 0xFFFFFFFF


def inspect(data):
    if len(data) < 0x120 or len(data) > 100 * 1024 * 1024:
        raise ValueError("unsupported file length")
    if u32(data, 0) != MODEL:
        raise ValueError("this inspector admits EOS R6 Mark II only")
    if u32(data, 0x38) != len(data) or u32(data, 0x44) != 4:
        raise ValueError("invalid declared size or region count")
    regions = [struct.unpack_from("<II", data, 0x48 + i * 8) for i in range(4)]
    previous_end = 0
    for offset, length in regions:
        if not length or offset < previous_end or offset + length > len(data):
            raise ValueError("invalid, overlapping or out-of-bounds signed region")
        previous_end = offset + length
    updater_header, updater_start, firmware = (u32(data, n) for n in (0x24, 0x28, 0x30))
    if regions[:2] != [(0, 0x20), (0x24, 0x44)]:
        raise ValueError("unexpected header signature layout")
    if updater_header != 0x100 or updater_start != 0x120:
        raise ValueError("unexpected updater layout")
    if regions[2] != (updater_header, firmware - updater_header):
        raise ValueError("updater region disagrees with header")
    if regions[3] != (firmware, len(data) - firmware):
        raise ValueError("firmware region disagrees with header")
    if firmware + 0x30 > len(data):
        raise ValueError("firmware encryption header is truncated")
    decode_offset, encoded_offset, total = struct.unpack_from("<III", data, firmware)
    if (decode_offset, encoded_offset, total) != (0x10, 0x30, regions[3][1]):
        raise ValueError("unexpected firmware encryption layout")

    signature_parts = []
    for i in range(4):
        offset = 0x68 + i * 36
        length = u32(data, offset)
        if not 1 <= length <= 32:
            raise ValueError("invalid ECDSA scalar length")
        signature_parts.append(bytes(data[offset + 4:offset + 4 + length]).rjust(32, b"\0"))
    h1 = SHA256.new()
    for offset, length in regions[:3]:
        h1.update(memoryview(data)[offset:offset + length])
    h2 = SHA256.new(memoryview(data)[firmware:])
    key = ECC.import_key(PUBLIC_KEY, curve_name="P-256")
    valid = []
    for index, digest in enumerate((h1, h2)):
        signature = b"".join(signature_parts[index * 2:index * 2 + 2])
        try:
            DSS.new(key, "fips-186-3").verify(digest, signature)
            valid.append(True)
        except ValueError:
            valid.append(False)
    updater_length = u32(data, updater_header)
    payload_length = u32(data, firmware + decode_offset)
    if updater_start + updater_length != firmware or firmware + encoded_offset + payload_length != len(data):
        raise ValueError("encrypted lengths disagree with region boundaries")
    return {
        "kind": "ordinary offline FIR structure and public-key verification; not REA decompilation",
        "model_id": hex(MODEL),
        "version": bytes(data[0x10:0x15]).decode("ascii"),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "checksum_valid": u32(data, 0x20) == checksum(data),
        "signature_algorithm": "ECDSA / secp256r1 / SHA-256",
        "signature_valid": {"header_and_updater": valid[0], "firmware_payload": valid[1]},
        "signed_regions": [{"offset": o, "length": n} for o, n in regions],
        "encrypted_regions": {
            "updater": {"offset": updater_start, "length": updater_length, "iv": bytes(data[updater_header + 8:updater_header + 24]).hex()},
            "firmware": {"offset": firmware + encoded_offset, "length": payload_length, "iv": bytes(data[firmware + decode_offset + 8:firmware + decode_offset + 24]).hex()},
        },
        "limitations": ["Not decrypted; no menu or configuration symbols recovered.", "Public-key verification does not generate replacement signatures or observe the camera loader."],
    }


def negative_controls(data):
    controls = {}
    for name, offset, expected in (
        ("header_bit_flip_with_repaired_checksum", 0x0C, {"header_and_updater": False, "firmware_payload": True}),
        ("payload_bit_flip_with_repaired_checksum", u32(data, 0x30) + 0x40, {"header_and_updater": True, "firmware_payload": False}),
    ):
        changed = bytearray(data)
        changed[offset] ^= 1
        struct.pack_into("<I", changed, 0x20, checksum(changed))
        result = inspect(changed)
        assert result["checksum_valid"] and result["signature_valid"] == expected
        controls[name] = {"checksum_valid": True, "signature_valid": result["signature_valid"]}
    for name, offset, value in (("bad_region_count", 0x44, 1000), ("out_of_bounds_region", 0x64, 0xFFFFFFFF), ("bad_signature_length", 0x68, 33)):
        changed = bytearray(data)
        struct.pack_into("<I", changed, offset, value)
        try:
            inspect(changed)
        except ValueError as error:
            controls[name] = {"rejected": True, "reason": str(error)}
        else:
            raise AssertionError("malformed input was not rejected")
    return controls


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("firmware", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--negative-controls", action="store_true")
    args = parser.parse_args()
    original = args.firmware.read_bytes()
    result = inspect(original)
    if not result["checksum_valid"] or not all(result["signature_valid"].values()):
        raise SystemExit("input firmware failed verification")
    if args.negative_controls:
        result["negative_controls"] = negative_controls(original)
    assert hashlib.sha256(args.firmware.read_bytes()).hexdigest() == result["sha256"]
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
