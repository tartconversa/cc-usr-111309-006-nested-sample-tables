"""协议层黄金向量与边界测试：编码、哈希、有理数严格入选。"""

import hashlib
import unittest

from src import sampling_protocol as proto
from src.sampling_protocol import TWO_POW_64

SEED = "111309"  # 与 fixtures/sampling.json 登记的公开复现种子一致


class CanonicalEncodingTests(unittest.TestCase):
    def test_golden_key_encoding(self):
        # S + uint32_be 长度 + UTF-8，逐字段拼接，无分隔符
        self.assertEqual(
            proto.canonical_key(["s1", "a"]).hex(),
            "53000000027331" + "530000000161",
        )

    def test_null_distinct_from_empty_string(self):
        null_key = proto.canonical_key(["s2", None])
        empty_key = proto.canonical_key(["s2", ""])
        self.assertEqual(null_key.hex(), "530000000273324e")
        self.assertEqual(empty_key.hex(), "53000000027332" + "5300000000")
        self.assertNotEqual(null_key, empty_key)
        self.assertNotEqual(
            proto.stable_hash(SEED, null_key),
            proto.stable_hash(SEED, empty_key),
        )

    def test_utf8_length_is_byte_count_not_char_count(self):
        # "中" 三个 UTF-8 字节，长度前缀必须是 3 而非 1
        key = proto.canonical_key(["中"])
        self.assertEqual(key, b"S" + (3).to_bytes(4, "big") + "中".encode("utf-8"))

    def test_composite_key_ambiguity_is_resolved(self):
        # 无分隔符拼接在长度前缀下不产生歧义
        self.assertNotEqual(
            proto.canonical_key(["ab", "c"]),
            proto.canonical_key(["a", "bc"]),
        )

    def test_non_string_non_null_rejected(self):
        with self.assertRaises(proto.CanonicalKeyError):
            proto.canonical_key([1])
        with self.assertRaises(proto.CanonicalKeyError):
            proto.canonical_key([True])

    def test_empty_seed_rejected(self):
        with self.assertRaises(proto.CanonicalKeyError):
            proto.stable_hash("", proto.canonical_key(["a"]))


class GoldenHashVectorTests(unittest.TestCase):
    def test_hash_matches_independent_sha256_construction(self):
        key_bytes = proto.canonical_key(["s1", "a"])
        expected = int.from_bytes(
            hashlib.sha256(bytes.fromhex(SEED) + key_bytes).digest()[:8], "big"
        )
        self.assertEqual(proto.stable_hash(SEED, key_bytes), expected)

    def test_pinned_golden_hashes(self):
        # 黄金向量：重跑任何环境必须得到同一 64 位整数
        cases = {
            ("s1", "a"): 363586517285866758,
            ("s1", "b"): 17536828903218501481,
            ("s2", "c"): 18051571881053752452,
            ("s2", None): 4764697367225638483,
            ("s2", ""): 14331129749282958564,
            ("ab", "c"): 12317138956723924651,
            ("a", "bc"): 4716401533135510824,
        }
        for values, expected_h in cases.items():
            with self.subTest(values=values):
                self.assertEqual(
                    proto.stable_hash(SEED, proto.canonical_key(list(values))),
                    expected_h,
                )

    def test_hash_range_is_unsigned_64bit(self):
        for values in (["s1", "a"], ["s2", None], ["s2", ""]):
            h = proto.stable_hash(SEED, proto.canonical_key(values))
            self.assertGreaterEqual(h, 0)
            self.assertLess(h, TWO_POW_64)


class ThresholdUnitTests(unittest.TestCase):
    """需求给定的独立阈值单元例（虚构 h）。"""

    def test_h_zero_accepted_at_any_positive_fraction(self):
        for fraction in ((1, 4), (1, 2), (3, 4), (1, 1000)):
            self.assertTrue(proto.accepted(0, fraction), fraction)

    def test_h_halfway_strictly_out_at_half(self):
        h = 1 << 63
        self.assertFalse(proto.accepted(h, (1, 4)))
        # 严格小于：h*b == a*2^64 时不得入选
        self.assertFalse(proto.accepted(h, (1, 2)))
        self.assertTrue(proto.accepted(h, (3, 4)))

    def test_max_hash_only_accepted_at_full_fraction(self):
        h = TWO_POW_64 - 1
        self.assertFalse(proto.accepted(h, (1, 2)))
        self.assertFalse(proto.accepted(h, (999, 1000)))
        self.assertTrue(proto.accepted(h, (1, 1)))

    def test_zero_fraction_selects_nothing(self):
        self.assertFalse(proto.accepted(0, (0, 1)))

    def test_invalid_fractions_rejected(self):
        for bad in ((1, 0), (2, 1), (-1, 2), (1,)):
            with self.assertRaises(proto.FractionError):
                proto.accepted(0, bad)

    def test_equivalent_fractions_share_decision(self):
        h = 1 << 62
        self.assertEqual(proto.accepted(h, (2, 4)), proto.accepted(h, (1, 2)))


class NestedSubsetTests(unittest.TestCase):
    def test_quarter_set_is_subset_of_half_set(self):
        # 1/4 样本一定包含于 1/2：对所有 h 验证（抽样扫描，非浮点近似）
        for h in (0, 1, 1 << 62, (1 << 63) - 1, 1 << 63, TWO_POW_64 - 1):
            if proto.accepted(h, (1, 4)):
                self.assertTrue(proto.accepted(h, (1, 2)))

    def test_nested_chain_for_fixture_hashes(self):
        rows_keys = [["s1", "a"], ["s1", "b"], ["s2", "c"], ["s2", None], ["s2", ""]]
        sets = []
        for fraction in ((1, 4), (1, 2), (3, 4)):
            chosen = {
                tuple(values)
                for values in rows_keys
                if proto.accepted(proto.stable_hash(SEED, proto.canonical_key(values)), fraction)
            }
            sets.append(chosen)
        self.assertTrue(sets[0] <= sets[1] <= sets[2])
        self.assertEqual(sets[0], {("s1", "a")})
        self.assertEqual(sets[1], {("s1", "a"), ("s2", None)})


if __name__ == "__main__":
    unittest.main()
