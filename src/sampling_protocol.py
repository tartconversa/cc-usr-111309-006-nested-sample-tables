"""确定性 Bernoulli 稳定哈希抽样协议（编码版本 canonical-key/v1）。

本模块只定义协议原语：键编码、稳定哈希、有理数入选判定。
不读取任何外部数据，不执行任何统计预测或建模。

协议要点
--------
每个原始记录有不可变 sample_key，由 sample_key_fields 按配置顺序取值组成。
字段序列化（无分隔符拼接，长度前缀本身保证边界无歧义）：

* NULL       -> 单个字节 b"N"
* 字符串     -> b"S" + uint32 大端字节长度 + UTF-8 字节
* 空字符串   -> b"S\\x00\\x00\\x00\\x00"，与 NULL 的 b"N" 字节不同，绝不混同
* 其他类型   -> 拒绝，避免隐式类型转换制造键歧义

哈希：SHA256(seed_bytes + canonical_key)，取摘要前 8 字节按大端解释为
无符号 64 位整数 h，取值区间 [0, 2^64 - 1]。

比例为有理数 a/b（0 <= a <= b，b > 0），入选条件严格为
``h * b < a * 2^64``，全部使用整数运算，禁止以 h / 2^64 浮点比较。
"""

from __future__ import annotations

import hashlib
from fractions import Fraction
from typing import Iterable, Sequence
ENCODING_VERSION = "canonical-key/v1"
NULL_TAG = b"N"
STRING_TAG = b"S"
LENGTH_PREFIX_BYTES = 4
HASH_BYTES = 8
TWO_POW_64 = 1 << 64


class CanonicalKeyError(ValueError):
    """键字段无法按 canonical-key/v1 编码。"""


class FractionError(ValueError):
    """比例 a/b 不合法。"""


def encode_key_field(value) -> bytes:
    """编码单个 sample_key 字段。

    仅接受 None（NULL）与 str；空串是合法字符串，与 NULL 编码不同。
    """
    if value is None:
        return NULL_TAG
    if isinstance(value, str):
        payload = value.encode("utf-8")
        if len(payload) > 0xFFFFFFFF:
            raise CanonicalKeyError("字符串 UTF-8 字节长度超出 uint32 前缀范围")
        return STRING_TAG + len(payload).to_bytes(LENGTH_PREFIX_BYTES, "big") + payload
    raise CanonicalKeyError(
        f"不支持的键字段类型 {type(value).__name__!r}：协议仅允许 NULL 与字符串，"
        "拒绝隐式类型转换以避免键歧义"
    )


def canonical_key(values: Sequence[object]) -> bytes:
    """按字段顺序把 sample_key 序列化为确定字节串。"""
    if not isinstance(values, (list, tuple)):
        raise CanonicalKeyError("sample_key 必须是列表或元组")
    return b"".join(encode_key_field(value) for value in values)


def normalize_seed(seed_hex: str) -> bytes:
    """校验并解码公开复现种子（十六进制字符串，非空）。"""
    if not isinstance(seed_hex, str) or not seed_hex:
        raise CanonicalKeyError("seed_hex 必须是非空十六进制字符串")
    try:
        seed_bytes = bytes.fromhex(seed_hex)
    except ValueError as exc:
        raise CanonicalKeyError("seed_hex 不是合法十六进制字符串") from exc
    if not seed_bytes:
        raise CanonicalKeyError("seed_hex 解码后为空，拒绝空种子")
    return seed_bytes


def stable_hash(seed_hex: str, key_bytes: bytes) -> int:
    """SHA256(seed_bytes || canonical_key) 前 64 位，大端无符号整数。"""
    seed_bytes = normalize_seed(seed_hex)
    digest = hashlib.sha256(seed_bytes + key_bytes).digest()
    return int.from_bytes(digest[:HASH_BYTES], "big", signed=False)


def normalize_fraction(fraction: Sequence[int]) -> tuple[int, int]:
    """校验并约分比例，返回 (a, b)。"""
    if not isinstance(fraction, (list, tuple)) or len(fraction) != 2:
        raise FractionError("比例必须是形如 [a, b] 的两个整数")
    a, b = fraction
    if isinstance(a, bool) or isinstance(b, bool) or not isinstance(a, int) or not isinstance(b, int):
        raise FractionError("比例分子分母必须是整数")
    if b <= 0:
        raise FractionError("比例分母必须为正整数")
    if not 0 <= a <= b:
        raise FractionError("比例必须满足 0 <= a <= b")
    if a == 0:
        return (0, 1)
    divisor = _gcd(a, b)
    return (a // divisor, b // divisor)


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


def accepted(h: int, fraction: Sequence[int]) -> bool:
    """严格整数入选判定：h * b < a * 2^64。"""
    if isinstance(h, bool) or not isinstance(h, int):
        raise CanonicalKeyError("h 必须是整数")
    if not 0 <= h < TWO_POW_64:
        raise CanonicalKeyError("h 必须落在无符号 64 位整数区间 [0, 2^64-1]")
    a, b = normalize_fraction(fraction)
    return h * b < a * TWO_POW_64


def sort_fractions(fractions: Iterable[Sequence[int]]) -> list[tuple[int, int]]:
    """去重、约分并按有理数大小升序排列比例（精确比较，无浮点）。"""
    ordered = sorted({normalize_fraction(item) for item in fractions}, key=lambda ab: Fraction(ab[0], ab[1]))
    return ordered


def assert_nested(fractions: Sequence[tuple[int, int]]) -> None:
    """验证比例严格升序；升序比例下小样本必为大样本子集。"""
    for (a1, b1), (a2, b2) in zip(fractions, fractions[1:]):
        if a1 * b2 >= a2 * b1:
            raise FractionError("比例必须严格升序才能保证嵌套子集关系")
