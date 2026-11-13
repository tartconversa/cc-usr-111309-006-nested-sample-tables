#!/usr/bin/env python
"""黄金演示：对 fixtures 合成数据运行确定性抽样并输出可核对报告。

隔离模式运行：

    python -I scripts/golden_demo.py [--output .workspace-output/demo]

只使用标准库；不运行任何统计预测模型。fixtures 为虚构合成数据，
无真实受试者、模型响应、轨迹或其他附件。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import sample_table as st  # noqa: E402
from src import sampling_protocol as proto  # noqa: E402
from src.sample_reader import load  # noqa: E402

SEED = "111309"
KEY_FIELDS = ("site", "id")
TWO_POW_64 = 1 << 64


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", default=str(ROOT / "fixtures" / "rows.jsonl"))
    parser.add_argument("--output", default=str(ROOT / ".workspace-output" / "demo"))
    args = parser.parse_args()

    rows = load(args.rows)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {}

    # 1. 哈希黄金向量（含独立 SHA256 构造核验） --------------------------------
    section("1. 哈希黄金向量 SHA256(seed || canonical_key) 前 64 位大端")
    golden = []
    for row in rows:
        values = st.extract_sample_key(row, KEY_FIELDS)
        key_bytes = proto.canonical_key(list(values))
        h = proto.stable_hash(SEED, key_bytes)
        golden.append(
            {
                "sample_key": list(values),
                "canonical_key_hex": key_bytes.hex(),
                "h": h,
                "h_hex": f"0x{h:016x}",
            }
        )
        print(f"key={list(values)!s:<22} canonical={key_bytes.hex()}  h={h}")
    report["golden_vectors"] = golden

    # 2. NULL 与空串不混同 ------------------------------------------------------
    section("2. NULL 与空字符串编码/哈希严格区分")
    null_bytes = proto.canonical_key(["s2", None])
    empty_bytes = proto.canonical_key(["s2", ""])
    null_vs_empty = {
        "null_encoding_hex": null_bytes.hex(),
        "empty_string_encoding_hex": empty_bytes.hex(),
        "null_h": proto.stable_hash(SEED, null_bytes),
        "empty_h": proto.stable_hash(SEED, empty_bytes),
        "distinct": null_bytes != empty_bytes,
    }
    print(json.dumps(null_vs_empty, indent=2, ensure_ascii=False))
    report["null_vs_empty_string"] = null_vs_empty

    # 3. 组合键歧义 -------------------------------------------------------------
    section("3. 组合键歧义：长度前缀保证 (ab,c) 与 (a,bc) 字节不同")
    k1, k2 = proto.canonical_key(["ab", "c"]), proto.canonical_key(["a", "bc"])
    ambiguity = {
        "(ab,c)_hex": k1.hex(),
        "(a,bc)_hex": k2.hex(),
        "distinct": k1 != k2,
        "h_ab_c": proto.stable_hash(SEED, k1),
        "h_a_bc": proto.stable_hash(SEED, k2),
    }
    print(json.dumps(ambiguity, indent=2, ensure_ascii=False))
    report["composite_key_ambiguity"] = ambiguity

    # 4. 虚构 h 阈值单元例 ------------------------------------------------------
    section("4. 独立阈值单元例（虚构 h，严格整数判定 h*b < a*2^64）")
    h_half = TWO_POW_64 // 2
    threshold_cases = [
        {"h": 0, "fraction": [1, 4], "accepted": proto.accepted(0, (1, 4))},
        {"h": 0, "fraction": [1, 2], "accepted": proto.accepted(0, (1, 2))},
        {"h": 0, "fraction": [3, 4], "accepted": proto.accepted(0, (3, 4))},
        {"h": h_half, "fraction": [1, 4], "accepted": proto.accepted(h_half, (1, 4))},
        {"h": h_half, "fraction": [1, 2], "accepted": proto.accepted(h_half, (1, 2))},
        {"h": h_half, "fraction": [3, 4], "accepted": proto.accepted(h_half, (3, 4))},
    ]
    for case in threshold_cases:
        print(f"h={case['h']:<20} fraction={case['fraction']}  accepted={case['accepted']}")
    report["threshold_unit_cases"] = threshold_cases

    # 5. 全局嵌套比例：选择依据 + 子集证明 --------------------------------------
    section("5. 全局 Bernoulli：1/4 ⊆ 1/2 ⊆ 3/4，输出选择依据")
    global_plan = st.GlobalPlan(SEED, KEY_FIELDS, [(1, 4), (1, 2), (3, 4)])
    global_result = st.build_global(rows, global_plan)
    for entry in global_result["selections"]:
        print(
            f"key={entry['sample_key']!s:<22} h={entry['h']:<20} "
            f"accepted={entry['accepted_fractions']}"
        )
    print("level_counts:", json.dumps(global_result["level_counts"], ensure_ascii=False))
    chosen_sets = [
        {tuple(line["sample_key"]) for line in global_result["snapshots"][name]}
        for name in ("1/4", "1/2", "3/4")
    ]
    nested_ok = chosen_sets[0] <= chosen_sets[1] <= chosen_sets[2]
    print("nested subset 1/4 ⊆ 1/2 ⊆ 3/4:", nested_ok)
    report["global"] = {
        "level_counts": global_result["level_counts"],
        "nested_subset_holds": nested_ok,
        "descriptor": global_result["descriptor"],
    }

    # 6. 分层口径：每层独立阈值与实际数 -----------------------------------------
    section("6. 分层 Bernoulli：s1@1/2, s2@3/4，分层实际数（不保证恰好 k 条）")
    stratified_plan = st.StratifiedPlan(
        SEED, KEY_FIELDS, "site", {"s1": (1, 2), "s2": (3, 4)}
    )
    stratified_result = st.build_stratified(rows, stratified_plan)
    for entry in stratified_result["selections"]:
        print(f"{entry['selection_basis']}  accepted={entry['accepted']}")
    print("stratum_actual:", json.dumps(stratified_result["stratum_actual"], ensure_ascii=False))
    report["stratified"] = {
        "stratum_actual": stratified_result["stratum_actual"],
        "selected_total": stratified_result["selected_total"],
        "descriptor": stratified_result["descriptor"],
    }

    # 7. 乱序输入：选择一致、排序固定 -------------------------------------------
    section("7. 乱序输入：选择结果一致，结果固定按 canonical_key 字节序")
    shuffled = [rows[3], rows[0], rows[4], rows[2], rows[1]]
    shuffled_result = st.build_global(shuffled, global_plan)
    order_ok = global_result["selections"] == shuffled_result["selections"]
    print("shuffled selection identical:", order_ok)
    print("canonical_key order:", [e["canonical_key_hex"] for e in global_result["selections"]])
    report["shuffled_input"] = {
        "selection_identical": order_ok,
        "ordered_canonical_keys": [e["canonical_key_hex"] for e in global_result["selections"]],
    }

    # 8. 追加 / 删除 / 修订对比 + 版本化目录 ------------------------------------
    section("8. 追加 / 删除 / 内容更正：目录版本对比（追加稳定）")
    catalog = output / "catalog_global"
    v1 = st.publish_catalog(catalog, global_result, mode="global")
    appended = rows + [{"site": "s2", "id": "d", "value": 5}]
    v2 = st.publish_catalog(catalog, st.build_global(appended, global_plan), mode="global")
    corrected = [dict(row) for row in rows]
    next(row for row in corrected if row["site"] == "s1" and row["id"] == "a")["value"] = 999
    v3 = st.publish_catalog(catalog, st.build_global(corrected, global_plan), mode="global")
    rerun = st.publish_catalog(catalog, st.build_global(corrected, global_plan), mode="global")
    versions = [v1, v2, v3, rerun]
    for item in versions:
        print(json.dumps(item, ensure_ascii=False))
    print("rerun byte-identical reuse:", rerun["reused"] and rerun["version"] == "v3")
    report["catalog_versions"] = versions

    # 9. bottom-k：另标非追加稳定并保存截止哈希 ---------------------------------
    section("9. bottom-k 对照：append_stable=false，保存 cutoff_hash")
    bk_plan = st.BottomKPlan(SEED, KEY_FIELDS, k=2)
    bk_result = st.build_bottom_k(rows, bk_plan)
    print("cutoff_hash:", bk_result["cutoff_hash"])
    print("append_stable:", bk_result["descriptor"]["append_stable"])
    st.publish_catalog(output / "catalog_bottom_k", bk_result, mode="bottom_k")
    report["bottom_k"] = {
        "cutoff_hash": bk_result["cutoff_hash"],
        "append_stable": bk_result["descriptor"]["append_stable"],
        "group_actual": bk_result["group_actual"],
    }

    # 10. 描述摘要 --------------------------------------------------------------
    section("10. 样本描述摘要（seed 与编码版本登记）")
    summary = {
        "encoding_version": proto.ENCODING_VERSION,
        "seed_hex": SEED,
        "selection_rule": "h*b < a*2**64 (integer-only, strict)",
        "global_descriptor": global_result["descriptor"],
        "stratified_descriptor": stratified_result["descriptor"],
        "disclaimer": (
            "本输出仅为可复现抽样基础设施；Bernoulli 稳定哈希不保证每层恰好 k 条，"
            "不能由少量样本宣称统计代表性，亦不运行任何统计预测模型"
        ),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    report["summary"] = summary

    (output / "golden_report.json").write_text(
        st.dumps_canonical(report) + "\n", encoding="utf-8"
    )
    print()
    print(f"报告与样本目录已写入: {output}")


if __name__ == "__main__":
    main()
