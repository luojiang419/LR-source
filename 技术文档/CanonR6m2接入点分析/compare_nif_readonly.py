"""离线比较 AP 基线与另一份 NIF；明文仅在内存中，报告只含字段枚举及变化位置。"""

import argparse
import hashlib
import json
import struct
from pathlib import Path

from decode_nif_profile_readonly import CAPTURE_SHA256, MAX_SIZE, analyze, decode_outer

# 仅解释这份捕获样本中核实过的字段；不同布局不套用偏移。
ANCHORS = ((0x90, b"NSET"), (0xF8, b"NWST"), (0x150, b"WSET"),
           (0x42C, b"ACTS"), (0x474, b"PCST"), (0x480, b"PCHI"))
ENUMS = {"ctyp": {0: "INVALID", 1: "INFRASTRUCTURE", 2: "MICRO_AP_AUTO", 3: "MICRO_AP_MANUAL"},
         "mode": {1: "PTP_PC/EOS Utility", 2: "FTP", 7: "PTP_MOBILE", 17: "BROWSERREMOTE"}}


def changed_ranges(left, right, origin):
    """不包含值或子区域哈希，避免暴露凭据。"""
    ranges, start = [], None
    for i in range(max(len(left), len(right))):
        changed = i >= min(len(left), len(right)) or left[i] != right[i]
        if changed and start is None:
            start = i
        if not changed and start is not None:
            ranges.append({"original_offset": origin + start, "length": i - start})
            start = None
    if start is not None:
        ranges.append({"original_offset": origin + start, "length": max(len(left), len(right)) - start})
    return ranges


def compare(baseline, other):
    before_report = analyze(baseline)
    if before_report["input_sha256"] != CAPTURE_SHA256:
        raise ValueError("baseline must match the recorded AP capture SHA256")
    left, _, _, left_start, _ = decode_outer(baseline)
    right, _, _, right_start, _ = decode_outer(other)
    compatible = len(left) == len(right) and left_start == right_start
    if compatible:
        compatible = all(right[off:off + 12] == left[off:off + 12] for off, _ in ANCHORS)
    if compatible:
        compatible = all(right[row["header_offset"]:row["value_offset"]] ==
                         left[row["header_offset"]:row["value_offset"]]
                         for row in before_report["confirmed_fields"])
    fields = []
    if compatible:
        for row in before_report["confirmed_fields"]:
            value = struct.unpack_from("<I", right, row["value_offset"])[0]
            fields.append({"tag": row["tag"], "value_offset": row["value_offset"],
                           "before": row["uint32"], "after": value,
                           "after_enum": ENUMS.get(row["tag"], {}).get(value),
                           "changed": value != row["uint32"]})
    # 两个 DATA 起点不同时，差异偏移只以基线为参考，不伪称绝对对齐。
    ranges = changed_ranges(left[left_start:], right[right_start:], left_start)
    return {
        "baseline_sha256": CAPTURE_SHA256, "other_sha256": hashlib.sha256(other).hexdigest(),
        "baseline_size": len(baseline), "other_size": len(other),
        "raw_identical": baseline == other,
        "decoded_DATA_identical": left[left_start:] == right[right_start:],
        "same_known_header_layout": compatible, "field_candidates": fields,
        "decoded_DATA_changed_bytes": sum(row["length"] for row in ranges),
        "changed_ranges_relative_to_baseline_layout": ranges,
        "limitations": [
            "字段对应仅在已知容器和叶字段头一致时给出；不是通用 NIF 内层解析器",
            "不输出明文字节、字符串、网络地址、密码、设备标识或子区域哈希",
            "比较的是两次采集到的配置；不会将 profile0 预设成全局当前配置",
            "模式字段变化也不证明它控制新增向导；需结合采集前实际连接状态判断",
            "HEAD 的随机种子和 checksum 变化不参与 DATA 差异判断；未验证 checksum",
            "未加载 DLL、访问相机或写回配置；报告不构成机身恢复验证",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("other", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.resolve() in (args.baseline.resolve(), args.other.resolve()):
        parser.error("report must not overwrite either input")
    if any(p.stat().st_size > MAX_SIZE for p in (args.baseline, args.other)):
        parser.error("input exceeds the verified SDK limit")
    try:
        report = compare(args.baseline.read_bytes(), args.other.read_bytes())
    except ValueError as error:
        parser.error(str(error))
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("raw_identical", "decoded_DATA_identical", "same_known_header_layout", "field_candidates")}, ensure_ascii=False, indent=2))
