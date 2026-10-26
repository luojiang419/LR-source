"""离线格式边界及脱敏测试；不加载 SDK，不连接相机。"""
import json
import struct
import unittest

from decode_nif_profile_readonly import analyze, decode_outer, transform
from compare_nif_readonly import changed_ranges, compare


def pack(tag, payload):
    return struct.pack("<I4sI", len(payload), tag, 0) + payload


def fixture(enabled=1, tail=b"private-SSID-password-123"):
    seed = 0x1234567890ABCDEF
    head = pack(b"HEAD", pack(b"ofcn", struct.pack("<I", enabled)) +
                pack(b"ofcy", struct.pack("<Q", seed)))
    plain = head + pack(b"DATA", pack(b"NSET", tail))
    return transform(plain, len(head) + 12, len(plain), seed) if enabled else plain


class NifReaderTests(unittest.TestCase):
    def test_diff_offsets_and_length_changes_do_not_expose_values(self):
        rows = changed_ranges(b"secretAB12", b"secretCD1234", 144)
        self.assertEqual(rows, [{"original_offset": 150, "length": 2},
                                {"original_offset": 154, "length": 2}])
        self.assertNotIn("secret", json.dumps(rows))
        self.assertEqual(changed_ranges(b"abc", b"abc", 144), [])
        with self.assertRaisesRegex(ValueError, "baseline must match"):
            compare(fixture(), fixture())

    def test_transform_remainder_and_plaintext_not_reported(self):
        for enabled in (0, 1):
            for remainder in range(8):
                raw = fixture(enabled, b"private-SSID-password-123" + b"x" * remainder)
                report = analyze(raw)
                self.assertTrue(report["roundtrip_identical"])
                self.assertFalse(report["known_capture"])
                self.assertEqual(report["confirmed_fields"], [])
                self.assertNotIn("private-SSID", json.dumps(report))
                self.assertEqual(raw, decode_outer(raw)[1])

    def test_malformed_boundaries_and_missing_fields(self):
        good = fixture()
        bad = [b"", good[:11], good[:-1], good + b"x"]
        for offset in (0, 12, 28, 48):
            mutated = bytearray(good)
            struct.pack_into("<I", mutated, offset, 0xFFFFFFFF)
            bad.append(mutated)
        bad.append(pack(b"HEAD", pack(b"ofcn", struct.pack("<I", 1))) +
                   pack(b"DATA", pack(b"NSET", b"")))
        bad.append(pack(b"HEAD", pack(b"ofcn", struct.pack("<I", 0)) * 2) +
                   pack(b"DATA", pack(b"NSET", b"")))
        for raw in bad:
            with self.subTest(size=len(raw)):
                with self.assertRaises(ValueError):
                    analyze(raw)


if __name__ == "__main__":
    unittest.main()
