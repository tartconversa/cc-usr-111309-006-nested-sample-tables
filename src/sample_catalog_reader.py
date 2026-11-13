"""样本目录读取器：只加载已发布的样本目录，不做计算、不重跑抽样。

与 sample_reader.load 一样属于纯加载工具；任何选择/哈希逻辑都在
sampling_protocol 与 sample_table 中，本模块不得重新实现。
支持读取当前与旧版本目录，并校验结果文件的 canonical_key 字节序。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CATALOG_MANIFEST = "catalog_manifest.json"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def list_versions(catalog_dir: str | Path) -> list[dict[str, Any]]:
    """返回目录清单中登记的全部版本（按发布顺序，旧版本保留可读）。"""
    root = Path(catalog_dir)
    manifest = _read_json(root / CATALOG_MANIFEST)
    return list(manifest["versions"])


def load_catalog(catalog_dir: str | Path, version: str | None = None) -> dict[str, Any]:
    """加载样本目录；version=None 时读取最新版本。

    返回目录描述、版本清单、版本摘要、选择明细与全部快照行。
    本函数只反序列化文件，并校验快照排序，不执行任何抽样计算。
    """
    root = Path(catalog_dir)
    manifest = _read_json(root / CATALOG_MANIFEST)
    if not manifest["versions"]:
        raise ValueError(f"样本目录 {root} 没有任何已发布版本")
    if version is None:
        version = manifest["versions"][-1]["version"]
    version_dir = root / version
    if not version_dir.is_dir():
        raise FileNotFoundError(f"版本目录不存在: {version_dir}")
    version_manifest = _read_json(version_dir / "manifest.json")
    if version_manifest["version"] != version:
        raise ValueError(f"版本目录 {version} 内部清单不一致")
    if version_manifest["plan_fingerprint"] != manifest["plan_fingerprint"]:
        raise ValueError(f"版本 {version} 的方案指纹与目录清单不一致")

    result: dict[str, Any] = {
        "mode": manifest["mode"],
        "descriptor": manifest["descriptor"],
        "plan_fingerprint": manifest["plan_fingerprint"],
        "catalog_versions": list(manifest["versions"]),
        "version": version_manifest,
        "selection": [],
        "snapshots": {},
    }
    selection_path = version_dir / "selection.jsonl"
    if selection_path.exists():
        result["selection"] = _read_jsonl(selection_path)
    for path in sorted(version_dir.glob("snapshot*.jsonl")):
        lines = _read_jsonl(path)
        _assert_canonical_order(lines, path.name)
        result["snapshots"][path.name] = lines
    return result


def _assert_canonical_order(lines: list[dict[str, Any]], name: str) -> None:
    keys = [bytes.fromhex(line["canonical_key_hex"]) for line in lines]
    if keys != sorted(keys):
        raise ValueError(f"{name} 未按 canonical_key 字节序排列")
