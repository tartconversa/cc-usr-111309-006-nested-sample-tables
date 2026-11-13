"""确定性样本表生成器：全局/分层 Bernoulli 稳定哈希抽样与目录发布。

性质（必须保持）
----------------
1. 追加稳定：新增原始行不改变任何旧行的入选结果，因为判定只依赖
   (seed, 不可变 sample_key, 比例)。
2. 删除与内容更正会反映在样本快照中：快照保存当前输入的内容指纹，
   重跑时重新计算；内容更正不改 sample_key，只改内容指纹，行的
   抽样身份不变（仍由键的哈希决定），但快照内容更新。
3. sample_key 相同但内容冲突的行被拒绝；不能用内容哈希代替键，
   否则修改内容会改变抽样身份。
4. 这是 Bernoulli 式稳定哈希样本，不保证每层恰好 k 条。
5. bottom-k 模式另标 ``append_stable=false``，并保存截止哈希，
   不与 Bernoulli 模式混称。
6. 输出按 canonical_key 字节序固定排序；重跑相同输入字节一致。

本模块不运行任何统计预测模型，只做可复现抽样基础设施。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # 包内导入：python -m src.sample_table
    from . import sampling_protocol as proto
except ImportError:  # 隔离模式直接运行：python -I src/sample_table.py
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src import sampling_protocol as proto

DESIGN_KIND_BERNOULLI = "bernoulli_stable_hash"
DESIGN_KIND_BOTTOM_K = "bottom_k_hash"
DEFAULT_STRATUM_TOKEN = "__default__"
NULL_STRATUM_TOKEN = "NULL"


class SampleTableError(ValueError):
    """样本表构建或发布失败。"""


# ---------------------------------------------------------------------------
# 确定性序列化与指纹
# ---------------------------------------------------------------------------

def dumps_canonical(value: Any) -> str:
    """规范 JSON 文本：sort_keys、无多余空白、不转义非 ASCII。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def row_content_fingerprint(row: Mapping[str, Any]) -> str:
    """单行内容指纹：整行规范 JSON 的 SHA256（不含抽样身份）。"""
    return hashlib.sha256(dumps_canonical(row).encode("utf-8")).hexdigest()


def input_fingerprint(rows: Sequence[Mapping[str, Any]]) -> str:
    """整批输入指纹：逐行规范 JSON 以 \\n 连接后的 SHA256。

    输入行顺序变化会改变该指纹，但不会改变抽样选择结果（选择只看键）。
    """
    buffer = b"\n".join(dumps_canonical(row).encode("utf-8") for row in rows)
    return hashlib.sha256(buffer).hexdigest()


def plan_fingerprint(plan: Mapping[str, Any]) -> str:
    """抽样方案指纹：方案规范 JSON 的 SHA256。"""
    return hashlib.sha256(dumps_canonical(plan).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 计划与记录
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GlobalPlan:
    """全局单一阈值，或多个嵌套比例（嵌套比例即分层口径中的不同档位）。"""

    seed_hex: str
    key_fields: tuple[str, ...]
    fractions: tuple[tuple[int, int], ...]
    stratum_field: None = None
    stratum_fractions: None = None
    default_fraction: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "key_fields", tuple(self.key_fields))
        object.__setattr__(
            self, "fractions", tuple(proto.normalize_fraction(item) for item in self.fractions)
        )

    def descriptor(self) -> dict[str, Any]:
        return {
            "design_kind": DESIGN_KIND_BERNOULLI,
            "encoding_version": proto.ENCODING_VERSION,
            "seed_hex": self.seed_hex,
            "sample_key_fields": list(self.key_fields),
            "threshold_mode": "global",
            "selection_rule": "h*b < a*2**64",
            "threshold_choice": (
                "全部记录共用同一组全局比例；未配置任何按层阈值，"
                "比例间为精确有理升序，小比例选择集是大比例选择集的子集"
            ),
            "fractions": [[a, b] for a, b in self.fractions],
            "append_stable": True,
            "exact_count_guarantee": False,
            "note": "Bernoulli 式稳定哈希样本，不保证每层恰好 k 条；仅为可复现抽样基础设施，不构成统计代表性声明",
        }


@dataclass(frozen=True)
class StratifiedPlan:
    """按预定 stratum 配置不同阈值；选择规则在描述中显式记录。"""

    seed_hex: str
    key_fields: tuple[str, ...]
    stratum_field: str
    stratum_fractions: Mapping[str, tuple[int, int]]
    default_fraction: tuple[int, int] | None = None
    fractions: None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "key_fields", tuple(self.key_fields))
        object.__setattr__(
            self,
            "stratum_fractions",
            {
                name: proto.normalize_fraction(fraction)
                for name, fraction in self.stratum_fractions.items()
            },
        )
        if self.default_fraction is not None:
            object.__setattr__(
                self, "default_fraction", proto.normalize_fraction(self.default_fraction)
            )

    def descriptor(self) -> dict[str, Any]:
        if self.default_fraction is None:
            choice = (
                f"按字段 {self.stratum_field} 的预定分层阈值选择；"
                "每个 stratum 的值必须在 stratum_fractions 中显式登记，未登记即报错"
            )
        else:
            choice = (
                f"按字段 {self.stratum_field} 的预定分层阈值选择；"
                f"已登记层使用各自比例，未登记层回退默认比例 {list(self.default_fraction)}"
            )
        return {
            "design_kind": DESIGN_KIND_BERNOULLI,
            "encoding_version": proto.ENCODING_VERSION,
            "seed_hex": self.seed_hex,
            "sample_key_fields": list(self.key_fields),
            "threshold_mode": "stratified",
            "stratum_field": self.stratum_field,
            "selection_rule": "h*b < a*2**64",
            "threshold_choice": choice,
            "stratum_fractions": {
                name: [a, b] for name, (a, b) in sorted(self.stratum_fractions.items())
            },
            "default_fraction": list(self.default_fraction) if self.default_fraction else None,
            "append_stable": True,
            "exact_count_guarantee": False,
            "note": "Bernoulli 式稳定哈希样本，不保证每层恰好 k 条；仅为可复现抽样基础设施，不构成统计代表性声明",
        }


@dataclass(frozen=True)
class BottomKPlan:
    """bottom-k 模式：取哈希最小的 k 行，另标非追加稳定并保存截止哈希。"""

    seed_hex: str
    key_fields: tuple[str, ...]
    k: int
    stratum_field: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "key_fields", tuple(self.key_fields))

    def descriptor(self) -> dict[str, Any]:
        return {
            "design_kind": DESIGN_KIND_BOTTOM_K,
            "encoding_version": proto.ENCODING_VERSION,
            "seed_hex": self.seed_hex,
            "sample_key_fields": list(self.key_fields),
            "threshold_mode": "bottom_k" + (f"_within_{self.stratum_field}" if self.stratum_field else ""),
            "k": self.k,
            "selection_rule": "选择哈希值最小的 k 条；截止哈希 cutoff_hash 随快照保存",
            "threshold_choice": "按哈希排序截断，不使用比例阈值；新增行可能挤掉旧行",
            "stratum_field": self.stratum_field,
            "append_stable": False,
            "exact_count_guarantee": True,
            "note": "bottom-k 不是追加稳定模式，禁止与 Bernoulli 稳定哈希样本混称同一性质",
        }


@dataclass(frozen=True)
class RowRecord:
    sample_key: tuple[Any, ...]
    canonical_key: bytes
    h: int
    row: Mapping[str, Any]
    content_fingerprint: str
    stratum: str | None = None


# ---------------------------------------------------------------------------
# 输入校验与键提取
# ---------------------------------------------------------------------------

def _validate_key_fields(rows: Sequence[Mapping[str, Any]], key_fields: Sequence[str]) -> None:
    if not key_fields:
        raise SampleTableError("sample_key_fields 不能为空")
    if len(set(key_fields)) != len(key_fields):
        raise SampleTableError("sample_key_fields 存在重复字段")
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise SampleTableError(f"第 {index} 行不是对象记录")
        missing = [field for field in key_fields if field not in row]
        if missing:
            raise SampleTableError(f"第 {index} 行缺少 sample_key 字段: {missing}")


def extract_sample_key(row: Mapping[str, Any], key_fields: Sequence[str]) -> tuple[Any, ...]:
    """按键字段顺序提取不可变 sample_key；缺失即错误，NULL 用 None 表示。"""
    return tuple(row[field] for field in key_fields)


def prepare_rows(
    rows: Sequence[Mapping[str, Any]],
    key_fields: Sequence[str],
    seed_hex: str,
    stratum_field: str | None = None,
) -> list[RowRecord]:
    """校验输入、拒绝同键内容冲突，返回未排序的记录（排序由调用方统一处理）。"""
    _validate_key_fields(rows, key_fields)
    if stratum_field is not None:
        for index, row in enumerate(rows):
            if stratum_field not in row:
                raise SampleTableError(f"第 {index} 行缺少分层字段 {stratum_field!r}")
    seen: dict[tuple[Any, ...], str] = {}
    records: list[RowRecord] = []
    for index, row in enumerate(rows):
        key_values = extract_sample_key(row, key_fields)
        key_bytes = proto.canonical_key(list(key_values))
        h = proto.stable_hash(seed_hex, key_bytes)
        fingerprint = row_content_fingerprint(row)
        if key_values in seen:
            if seen[key_values] != fingerprint:
                raise SampleTableError(
                    f"第 {index} 行 sample_key={list(key_values)!r} 与先前记录内容冲突；"
                    "相同 sample_key 的内容必须一致，且禁止以内容哈希代替 sample_key"
                )
            # 同键同内容是重复投递：抽样身份只有一个，静默折叠为一条
            continue
        seen[key_values] = fingerprint
        stratum = None
        if stratum_field is not None:
            raw = row[stratum_field]
            if raw is None:
                stratum = NULL_STRATUM_TOKEN
            elif isinstance(raw, str):
                stratum = raw
            else:
                raise SampleTableError(
                    f"第 {index} 行分层字段 {stratum_field!r} 必须是字符串或 NULL，"
                    f"实际为 {type(raw).__name__}"
                )
        records.append(
            RowRecord(
                sample_key=key_values,
                canonical_key=key_bytes,
                h=h,
                row=row,
                content_fingerprint=fingerprint,
                stratum=stratum,
            )
        )
    return records


def _sorted_records(records: Sequence[RowRecord]) -> list[RowRecord]:
    """canonical_key 字节序固定排序；字节相同即同键，已在 prepare 阶段去重。"""
    return sorted(records, key=lambda record: record.canonical_key)


# ---------------------------------------------------------------------------
# Bernoulli 构建
# ---------------------------------------------------------------------------

def build_global(rows: Sequence[Mapping[str, Any]], plan: GlobalPlan) -> dict[str, Any]:
    """全局 Bernoulli 抽样；支持多个嵌套比例档位。"""
    fractions = proto.sort_fractions(plan.fractions)
    proto.assert_nested(fractions)
    records = _sorted_records(prepare_rows(rows, plan.key_fields, plan.seed_hex))
    selections: list[dict[str, Any]] = []
    snapshots: dict[str, list[dict[str, Any]]] = {f"{a}/{b}": [] for a, b in fractions}
    for record in records:
        entry = {
            "sample_key": list(record.sample_key),
            "canonical_key_hex": record.canonical_key.hex(),
            "h": record.h,
            "accepted_fractions": [],
        }
        for a, b in fractions:
            if proto.accepted(record.h, (a, b)):
                entry["accepted_fractions"].append(f"{a}/{b}")
                snapshots[f"{a}/{b}"].append(_snapshot_line(record, fraction=(a, b)))
        selections.append(entry)
    level_counts = {name: len(lines) for name, lines in snapshots.items()}
    return {
        "descriptor": plan.descriptor(),
        "plan_fingerprint": plan_fingerprint(plan.descriptor()),
        "input_fingerprint": input_fingerprint(rows),
        "total_rows": len(records),
        "level_counts": level_counts,
        "selections": selections,
        "snapshots": snapshots,
    }


def _resolve_stratum_fraction(plan: StratifiedPlan, stratum: str) -> tuple[int, int]:
    fraction = plan.stratum_fractions.get(stratum)
    if fraction is None:
        if plan.default_fraction is None:
            raise SampleTableError(
                f"stratum {stratum!r} 未在 stratum_fractions 登记，且未配置默认比例"
            )
        return plan.default_fraction
    return fraction


def build_stratified(rows: Sequence[Mapping[str, Any]], plan: StratifiedPlan) -> dict[str, Any]:
    """分层 Bernoulli 抽样：每层独立阈值，单层不保证恰好 k 条。"""
    records = _sorted_records(
        prepare_rows(rows, plan.key_fields, plan.seed_hex, plan.stratum_field)
    )
    selections: list[dict[str, Any]] = []
    snapshot: list[dict[str, Any]] = []
    per_stratum_total: dict[str, int] = {}
    per_stratum_selected: dict[str, int] = {}
    per_stratum_fraction: dict[str, list[int]] = {}
    for record in records:
        assert record.stratum is not None
        a, b = _resolve_stratum_fraction(plan, record.stratum)
        per_stratum_total[record.stratum] = per_stratum_total.get(record.stratum, 0) + 1
        per_stratum_fraction.setdefault(record.stratum, [a, b])
        chosen = proto.accepted(record.h, (a, b))
        selections.append(
            {
                "sample_key": list(record.sample_key),
                "canonical_key_hex": record.canonical_key.hex(),
                "h": record.h,
                "stratum": record.stratum,
                "fraction": [a, b],
                "accepted": chosen,
                "selection_basis": f"stratum={record.stratum!r} -> {a}/{b}; h*{b} {'<' if chosen else '>='} {a}*2^64",
            }
        )
        if chosen:
            per_stratum_selected[record.stratum] = per_stratum_selected.get(record.stratum, 0) + 1
            snapshot.append(_snapshot_line(record, fraction=(a, b), stratum=record.stratum))
    stratum_actual = {}
    for name in sorted(per_stratum_total):
        stratum_actual[name] = {
            "fraction": per_stratum_fraction[name],
            "total": per_stratum_total[name],
            "selected": per_stratum_selected.get(name, 0),
        }
    return {
        "descriptor": plan.descriptor(),
        "plan_fingerprint": plan_fingerprint(plan.descriptor()),
        "input_fingerprint": input_fingerprint(rows),
        "total_rows": len(records),
        "selected_total": len(snapshot),
        "stratum_actual": stratum_actual,
        "selections": selections,
        "snapshot": snapshot,
    }


def _snapshot_line(
    record: RowRecord,
    fraction: tuple[int, int] | None = None,
    stratum: str | None = None,
) -> dict[str, Any]:
    line = {
        "sample_key": list(record.sample_key),
        "canonical_key_hex": record.canonical_key.hex(),
        "h": record.h,
        "content_fingerprint": record.content_fingerprint,
        "row": record.row,
    }
    if fraction is not None:
        line["fraction"] = [fraction[0], fraction[1]]
    if stratum is not None:
        line["stratum"] = stratum
    return line


# ---------------------------------------------------------------------------
# bottom-k（非追加稳定，单独标注）
# ---------------------------------------------------------------------------

def build_bottom_k(rows: Sequence[Mapping[str, Any]], plan: BottomKPlan) -> dict[str, Any]:
    """取哈希最小的 k 行；按层时每层各取 k 行。保存截止哈希。"""
    if isinstance(plan.k, bool) or not isinstance(plan.k, int) or plan.k <= 0:
        raise SampleTableError("bottom-k 的 k 必须是正整数")
    records = prepare_rows(rows, plan.key_fields, plan.seed_hex, plan.stratum_field)
    groups: dict[str | None, list[RowRecord]] = {}
    for record in records:
        groups.setdefault(record.stratum, []).append(record)
    snapshot: list[dict[str, Any]] = []
    cutoff: dict[str, int] = {}
    group_actual: dict[str, dict[str, int]] = {}
    for group_name, group_records in groups.items():
        ordered = sorted(group_records, key=lambda record: (record.h, record.canonical_key))
        chosen = ordered[: min(plan.k, len(ordered))]
        label = group_name if group_name is not None else "__all__"
        cutoff[label] = chosen[-1].h if chosen else 0
        group_actual[label] = {"total": len(ordered), "selected": len(chosen), "k": plan.k}
        for record in chosen:
            line = _snapshot_line(record, stratum=record.stratum)
            line["rank_basis"] = "bottom_k"
            snapshot.append(line)
    snapshot.sort(key=lambda line: bytes.fromhex(line["canonical_key_hex"]))
    return {
        "descriptor": plan.descriptor(),
        "plan_fingerprint": plan_fingerprint(plan.descriptor()),
        "input_fingerprint": input_fingerprint(rows),
        "total_rows": len(records),
        "selected_total": len(snapshot),
        "cutoff_hash": cutoff,
        "group_actual": group_actual,
        "snapshot": snapshot,
    }


# ---------------------------------------------------------------------------
# 样本目录发布（稳定追加、版本化、字节一致）
# ---------------------------------------------------------------------------

def _write_json(path: Path, value: Any) -> None:
    path.write_text(dumps_canonical(value) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, lines: Sequence[Mapping[str, Any]]) -> None:
    path.write_text(
        "".join(dumps_canonical(line) + "\n" for line in lines),
        encoding="utf-8",
    )


def publish_catalog(
    catalog_dir: str | Path,
    build_result: Mapping[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    """把构建结果发布为版本化样本目录。

    相同 (plan_fingerprint, input_fingerprint) 重跑返回既有版本且字节不变；
    输入或方案变化时追加新版本，旧版本保留可读。不写入时间戳或随机值。
    """
    if mode not in {"global", "stratified", "bottom_k"}:
        raise SampleTableError("未知目录模式")
    root = Path(catalog_dir)
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "catalog_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("plan_fingerprint") != build_result["plan_fingerprint"]:
            raise SampleTableError("样本目录的抽样方案指纹不可变更；新方案请使用新目录")
    else:
        manifest = {
            "catalog_format": "deterministic-sample-catalog/v1",
            "mode": mode,
            "plan_fingerprint": build_result["plan_fingerprint"],
            "descriptor": build_result["descriptor"],
            "versions": [],
        }
    target_fingerprint = build_result["input_fingerprint"]
    for version in manifest["versions"]:
        if version["input_fingerprint"] == target_fingerprint:
            return {"reused": True, **version}
    version_id = f"v{len(manifest['versions']) + 1}"
    version_dir = root / version_id
    version_dir.mkdir()
    _write_json(version_dir / "descriptor.json", build_result["descriptor"])

    if mode == "global":
        _write_json(
            version_dir / "manifest.json",
            {
                "version": version_id,
                "input_fingerprint": target_fingerprint,
                "plan_fingerprint": build_result["plan_fingerprint"],
                "total_rows": build_result["total_rows"],
                "level_counts": build_result["level_counts"],
            },
        )
        _write_jsonl(version_dir / "selection.jsonl", build_result["selections"])
        for name, lines in build_result["snapshots"].items():
            _write_jsonl(version_dir / f"snapshot_{name.replace('/', '_')}.jsonl", lines)
    elif mode == "stratified":
        _write_json(
            version_dir / "manifest.json",
            {
                "version": version_id,
                "input_fingerprint": target_fingerprint,
                "plan_fingerprint": build_result["plan_fingerprint"],
                "total_rows": build_result["total_rows"],
                "selected_total": build_result["selected_total"],
                "stratum_actual": build_result["stratum_actual"],
            },
        )
        _write_jsonl(version_dir / "selection.jsonl", build_result["selections"])
        _write_jsonl(version_dir / "snapshot.jsonl", build_result["snapshot"])
    else:
        _write_json(
            version_dir / "manifest.json",
            {
                "version": version_id,
                "input_fingerprint": target_fingerprint,
                "plan_fingerprint": build_result["plan_fingerprint"],
                "total_rows": build_result["total_rows"],
                "selected_total": build_result["selected_total"],
                "cutoff_hash": build_result["cutoff_hash"],
                "group_actual": build_result["group_actual"],
                "append_stable": False,
            },
        )
        _write_jsonl(version_dir / "snapshot.jsonl", build_result["snapshot"])

    version_entry = {
        "version": version_id,
        "input_fingerprint": target_fingerprint,
        "total_rows": build_result["total_rows"],
    }
    manifest["versions"].append(version_entry)
    _write_json(manifest_path, manifest)
    return {"reused": False, **version_entry}


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------

def _parse_fraction(text: str) -> tuple[int, int]:
    parts = text.split("/")
    if len(parts) != 2:
        raise SampleTableError("比例格式应为 a/b，例如 1/4")
    try:
        return proto.normalize_fraction((int(parts[0]), int(parts[1])))
    except ValueError as exc:
        raise SampleTableError(f"非法比例 {text!r}: {exc}") from exc


def _load_rows(path: str) -> list[Mapping[str, Any]]:
    # 延迟导入，保持核心模块不依赖加载器约定
    try:
        from .sample_reader import load
    except ImportError:
        from src.sample_reader import load

    rows = load(path)
    if not isinstance(rows, list):
        raise SampleTableError("抽样输入必须是记录列表（jsonl/csv）")
    return rows


def _key_fields_from_args(args) -> tuple[str, ...]:
    return tuple(field.strip() for field in args.key_fields.split(",") if field.strip())


def _add_common(parser) -> None:
    parser.add_argument("--rows", required=True, help="原始记录 jsonl/csv 路径")
    parser.add_argument("--key-fields", required=True, help="逗号分隔的 sample_key 字段顺序")
    parser.add_argument("--seed-hex", required=True, help="公开复现种子（十六进制）")
    parser.add_argument("--catalog", required=True, help="样本目录输出路径")


def main(argv: Sequence[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="确定性稳定哈希样本表生成器（不运行统计预测模型）")
    sub = parser.add_subparsers(dest="command", required=True)

    global_parser = sub.add_parser("global", help="全局 Bernoulli 阈值，支持多个嵌套比例")
    _add_common(global_parser)
    global_parser.add_argument("--fraction", action="append", required=True, help="比例 a/b，可重复提供嵌套档位")

    stratified_parser = sub.add_parser("stratified", help="按预定 stratum 的 Bernoulli 阈值")
    _add_common(stratified_parser)
    stratified_parser.add_argument("--stratum-field", required=True)
    stratified_parser.add_argument(
        "--stratum-fraction",
        action="append",
        required=True,
        metavar="STRATUM=a/b",
        help="分层比例，可重复提供",
    )
    stratified_parser.add_argument("--default-fraction", help="未登记 stratum 的回退比例 a/b")

    bottom_parser = sub.add_parser("bottom-k", help="bottom-k 模式（标注为非追加稳定）")
    _add_common(bottom_parser)
    bottom_parser.add_argument("--k", required=True, type=int)
    bottom_parser.add_argument("--stratum-field")

    args = parser.parse_args(argv)
    rows = _load_rows(args.rows)
    key_fields = _key_fields_from_args(args)

    if args.command == "global":
        plan = GlobalPlan(
            seed_hex=args.seed_hex,
            key_fields=key_fields,
            fractions=tuple(_parse_fraction(item) for item in args.fraction),
        )
        result = build_global(rows, plan)
        mode = "global"
    elif args.command == "stratified":
        stratum_fractions = {}
        for item in args.stratum_fraction:
            if "=" not in item:
                raise SampleTableError(f"分层比例格式应为 STRATUM=a/b，收到 {item!r}")
            name, raw = item.split("=", 1)
            stratum_fractions[name] = _parse_fraction(raw)
        plan = StratifiedPlan(
            seed_hex=args.seed_hex,
            key_fields=key_fields,
            stratum_field=args.stratum_field,
            stratum_fractions=stratum_fractions,
            default_fraction=_parse_fraction(args.default_fraction)
            if args.default_fraction
            else None,
        )
        result = build_stratified(rows, plan)
        mode = "stratified"
    else:
        plan = BottomKPlan(
            seed_hex=args.seed_hex,
            key_fields=key_fields,
            k=args.k,
            stratum_field=args.stratum_field,
        )
        result = build_bottom_k(rows, plan)
        mode = "bottom_k"

    publish = publish_catalog(args.catalog, result, mode=mode)
    print(dumps_canonical({"mode": mode, "publish": publish, "descriptor": result["descriptor"]}))


if __name__ == "__main__":
    main()
