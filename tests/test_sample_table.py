"""样本表构建、追加稳定、修订、分层、bottom-k 与目录发布测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from src import sample_table as st
from src import sample_catalog_reader as reader
from src import sampling_protocol as proto

SEED = "111309"
KEY_FIELDS = ("site", "id")
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "rows.jsonl"


def load_fixture_rows():
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


class BuildDeterminismTests(unittest.TestCase):
    def test_shuffled_input_identical_selection_and_sorted_output(self):
        rows = load_fixture_rows()
        plan = st.GlobalPlan(SEED, KEY_FIELDS, [(1, 4), (1, 2), (3, 4)])
        first = st.build_global(rows, plan)
        shuffled = [rows[3], rows[0], rows[4], rows[2], rows[1]]
        second = st.build_global(shuffled, plan)
        # 选择结果与行顺序无关；仅输入指纹随顺序变化
        self.assertEqual(first["selections"], second["selections"])
        self.assertEqual(first["snapshots"], second["snapshots"])
        self.assertNotEqual(first["input_fingerprint"], second["input_fingerprint"])
        for lines in first["snapshots"].values():
            keys = [bytes.fromhex(line["canonical_key_hex"]) for line in lines]
            self.assertEqual(keys, sorted(keys))

    def test_rerun_produces_identical_bytes(self):
        rows = load_fixture_rows()
        plan = st.GlobalPlan(SEED, KEY_FIELDS, [(1, 2)])
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp) / "cat"
            first = st.publish_catalog(catalog, st.build_global(rows, plan), mode="global")
            files_after_first = {
                p.relative_to(catalog).as_posix(): p.read_bytes()
                for p in catalog.rglob("*")
                if p.is_file()
            }
            second = st.publish_catalog(catalog, st.build_global(rows, plan), mode="global")
            files_after_second = {
                p.relative_to(catalog).as_posix(): p.read_bytes()
                for p in catalog.rglob("*")
                if p.is_file()
            }
            self.assertTrue(second["reused"])
            self.assertEqual(first["version"], second["version"])
            self.assertEqual(files_after_first, files_after_second)


class AppendRevisionTests(unittest.TestCase):
    def _row(self, site, identifier, value=0):
        return {"site": site, "id": identifier, "value": value}

    def test_appended_rows_never_change_old_decisions(self):
        base = [self._row("s1", "a"), self._row("s1", "b")]
        plan = st.GlobalPlan(SEED, KEY_FIELDS, [(1, 2)])
        before = {
            tuple(item["sample_key"]): item["accepted_fractions"]
            for item in st.build_global(base, plan)["selections"]
        }
        extended = base + [self._row("s2", "c"), self._row("s2", "d")]
        after = {
            tuple(item["sample_key"]): item["accepted_fractions"]
            for item in st.build_global(extended, plan)["selections"]
        }
        for key, decision in before.items():
            self.assertEqual(after[key], decision)

    def test_delete_reflected_in_snapshot_but_identity_of_survivors_unchanged(self):
        rows = load_fixture_rows()
        plan = st.GlobalPlan(SEED, KEY_FIELDS, [(1, 2)])
        full = st.build_global(rows, plan)
        removed_key = ("s2", None)
        reduced = [row for row in rows if not (row["site"] == "s2" and row["id"] is None)]
        rebuilt = st.build_global(reduced, plan)
        full_keys = {
            tuple(line["sample_key"]) for line in full["snapshots"]["1/2"]
        }
        rebuilt_keys = {
            tuple(line["sample_key"]) for line in rebuilt["snapshots"]["1/2"]
        }
        self.assertEqual(rebuilt_keys, full_keys - {removed_key})
        # 保留行的哈希与入选身份不变
        surviving = next(
            item for item in rebuilt["selections"] if tuple(item["sample_key"]) == ("s1", "a")
        )
        self.assertEqual(surviving["h"], proto.stable_hash(SEED, proto.canonical_key(["s1", "a"])))

    def test_content_correction_updates_snapshot_without_changing_sample_identity(self):
        rows = load_fixture_rows()
        plan = st.GlobalPlan(SEED, KEY_FIELDS, [(3, 4)])
        before = st.build_global(rows, plan)
        corrected = [dict(row) for row in rows]
        target = next(row for row in corrected if row["site"] == "s1" and row["id"] == "a")
        target["value"] = 999  # 内容更正，sample_key 不变
        after = st.build_global(corrected, plan)
        self.assertEqual(
            [item["h"] for item in before["selections"]],
            [item["h"] for item in after["selections"]],
        )
        self.assertEqual(before["level_counts"], after["level_counts"])
        line = next(
            line
            for line in after["snapshots"]["3/4"]
            if tuple(line["sample_key"]) == ("s1", "a")
        )
        self.assertEqual(line["row"]["value"], 999)
        self.assertNotEqual(before["input_fingerprint"], after["input_fingerprint"])

    def test_duplicate_key_content_conflict_rejected(self):
        bad = [
            self._row("s1", "a", value=1),
            self._row("s1", "a", value=2),
        ]
        plan = st.GlobalPlan(SEED, KEY_FIELDS, [(1, 2)])
        with self.assertRaises(st.SampleTableError):
            st.build_global(bad, plan)

    def test_identical_duplicate_rows_are_deduped(self):
        dup = [self._row("s1", "a"), self._row("s1", "a")]
        plan = st.GlobalPlan(SEED, KEY_FIELDS, [(1, 2)])
        result = st.build_global(dup, plan)
        self.assertEqual(result["total_rows"], 1)

    def test_missing_key_field_rejected(self):
        plan = st.GlobalPlan(SEED, KEY_FIELDS, [(1, 2)])
        with self.assertRaises(st.SampleTableError):
            st.build_global([{"site": "s1"}], plan)


class StratifiedTests(unittest.TestCase):
    def test_stratum_actual_counts_and_basis(self):
        rows = load_fixture_rows()
        plan = st.StratifiedPlan(SEED, KEY_FIELDS, "site", {"s1": (1, 2), "s2": (3, 4)})
        result = st.build_stratified(rows, plan)
        self.assertEqual(
            result["stratum_actual"],
            {
                "s1": {"fraction": [1, 2], "total": 2, "selected": 1},
                "s2": {"fraction": [3, 4], "total": 3, "selected": 1},
            },
        )
        # 不保证每层恰好 k 条：s2 共 3 行、比例 3/4，名义期望 2.25 行，实际仅 1 行
        self.assertNotEqual(result["stratum_actual"]["s2"]["selected"], 3)
        basis = result["selections"][0]["selection_basis"]
        self.assertIn("h*2", basis)
        self.assertIn("1*2^64", basis)

    def test_unregistered_stratum_fails_without_default(self):
        rows = load_fixture_rows() + [{"site": "s3", "id": "x", "value": 0}]
        plan = st.StratifiedPlan(SEED, KEY_FIELDS, "site", {"s1": (1, 2), "s2": (3, 4)})
        with self.assertRaises(st.SampleTableError):
            st.build_stratified(rows, plan)

    def test_default_fraction_covers_unregistered_stratum(self):
        rows = load_fixture_rows() + [{"site": "s3", "id": "x", "value": 0}]
        plan = st.StratifiedPlan(
            SEED, KEY_FIELDS, "site", {"s1": (1, 2), "s2": (3, 4)}, default_fraction=(1, 8)
        )
        result = st.build_stratified(rows, plan)
        self.assertEqual(result["stratum_actual"]["s3"]["fraction"], [1, 8])
        self.assertEqual(result["descriptor"]["threshold_mode"], "stratified")

    def test_null_stratum_is_a_distinct_registered_bucket(self):
        rows = [{"site": None, "id": "x", "value": 1}, {"site": "s1", "id": "y", "value": 2}]
        plan = st.StratifiedPlan(
            SEED, KEY_FIELDS, "site", {st.NULL_STRATUM_TOKEN: (1, 1), "s1": (0, 1)}
        )
        result = st.build_stratified(rows, plan)
        self.assertEqual(result["stratum_actual"][st.NULL_STRATUM_TOKEN]["selected"], 1)
        self.assertEqual(result["stratum_actual"]["s1"]["selected"], 0)


class BottomKTests(unittest.TestCase):
    def test_bottom_k_marked_non_append_stable_and_stores_cutoff(self):
        rows = load_fixture_rows()
        plan = st.BottomKPlan(SEED, KEY_FIELDS, k=2)
        result = st.build_bottom_k(rows, plan)
        self.assertFalse(result["descriptor"]["append_stable"])
        self.assertEqual(result["selected_total"], 2)
        cutoff = result["cutoff_hash"]["__all__"]
        chosen_h = sorted(line["h"] for line in result["snapshot"])
        self.assertEqual(chosen_h[-1], cutoff)
        # 截止哈希恰为第 k 小，且所有未选行哈希都更大
        all_h = sorted(
            proto.stable_hash(SEED, proto.canonical_key(list(st.extract_sample_key(row, KEY_FIELDS))))
            for row in rows
        )
        self.assertEqual(cutoff, all_h[1])

    def test_bottom_k_within_strata(self):
        rows = load_fixture_rows()
        plan = st.BottomKPlan(SEED, KEY_FIELDS, k=1, stratum_field="site")
        result = st.build_bottom_k(rows, plan)
        self.assertEqual(result["selected_total"], 2)
        self.assertEqual(set(result["cutoff_hash"]), {"s1", "s2"})


class CatalogPublishTests(unittest.TestCase):
    def test_versions_accumulate_and_old_versions_remain_readable(self):
        rows = load_fixture_rows()
        plan = st.GlobalPlan(SEED, KEY_FIELDS, [(1, 4), (1, 2), (3, 4)])
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp) / "cat"
            st.publish_catalog(catalog, st.build_global(rows, plan), mode="global")
            # 追加新行 -> v2；删除一条原始行 -> v3（删除必须反映在快照）
            extended = rows + [{"site": "s2", "id": "d", "value": 5}]
            st.publish_catalog(catalog, st.build_global(extended, plan), mode="global")
            deleted = [row for row in extended if not (row["site"] == "s1" and row["id"] == "a")]
            st.publish_catalog(catalog, st.build_global(deleted, plan), mode="global")
            versions = reader.list_versions(catalog)
            self.assertEqual([v["version"] for v in versions], ["v1", "v2", "v3"])
            v1 = reader.load_catalog(catalog, version="v1")
            latest = reader.load_catalog(catalog)
            self.assertEqual(latest["version"]["version"], "v3")
            self.assertEqual(v1["version"]["total_rows"], 5)
            self.assertIn("snapshot_1_2.jsonl", v1["snapshots"])
            # 描述始终标注 Bernoulli 与无精确数量保证
            self.assertEqual(v1["descriptor"]["design_kind"], st.DESIGN_KIND_BERNOULLI)
            self.assertFalse(v1["descriptor"]["exact_count_guarantee"])

    def test_stratified_catalog_roundtrip(self):
        rows = load_fixture_rows()
        plan = st.StratifiedPlan(SEED, KEY_FIELDS, "site", {"s1": (1, 2), "s2": (3, 4)})
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp) / "cat"
            st.publish_catalog(catalog, st.build_stratified(rows, plan), mode="stratified")
            loaded = reader.load_catalog(catalog)
            self.assertEqual(loaded["mode"], "stratified")
            self.assertEqual(loaded["version"]["selected_total"], 2)
            self.assertEqual(len(loaded["snapshots"]["snapshot.jsonl"]), 2)

    def test_bottom_k_catalog_carries_cutoff_and_flag(self):
        rows = load_fixture_rows()
        plan = st.BottomKPlan(SEED, KEY_FIELDS, k=1)
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp) / "cat"
            st.publish_catalog(catalog, st.build_bottom_k(rows, plan), mode="bottom_k")
            loaded = reader.load_catalog(catalog)
            self.assertIn("cutoff_hash", loaded["version"])
            self.assertFalse(loaded["descriptor"]["append_stable"])

    def test_plan_change_on_same_catalog_rejected(self):
        rows = load_fixture_rows()
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp) / "cat"
            st.publish_catalog(
                catalog,
                st.build_global(rows, st.GlobalPlan(SEED, KEY_FIELDS, [(1, 2)])),
                mode="global",
            )
            with self.assertRaises(st.SampleTableError):
                st.publish_catalog(
                    catalog,
                    st.build_global(rows, st.GlobalPlan(SEED, KEY_FIELDS, [(3, 4)])),
                    mode="global",
                )


if __name__ == "__main__":
    unittest.main()
