"""Physically isolated discovery dataset loader (research only, no live).

The loader is deliberately rooted at ``data_root/discovery`` and knows only the
TRAIN, VALIDATION and WALK_FORWARD partitions.  Holdout data is handled by a
different module and is never referenced or loaded here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path


class DiscoveryDatasetError(RuntimeError):
    """Fail-closed discovery dataset contract violation."""


@dataclass(frozen=True)
class DiscoveryPartitions:
    train: tuple[dict, ...]
    validation: tuple[dict, ...]
    walk_forward: tuple[dict, ...]
    source_root: str
    partition_sha256: tuple[tuple[str, str], ...] = ()

    def as_mutable(self) -> tuple[list[dict], list[dict], list[dict]]:
        return (
            copy.deepcopy(list(self.train)),
            copy.deepcopy(list(self.validation)),
            copy.deepcopy(list(self.walk_forward)),
        )


def _has_symlink_component(path: Path, stop: Path) -> bool:
    current = path
    while True:
        if current.is_symlink():
            return True
        if current == stop or current.parent == current:
            return False
        current = current.parent


class DiscoveryDatasetLoader:
    """Read only the three discovery partitions from an isolated root."""

    __slots__ = ("root",)

    def __init__(self, discovery_root: str | os.PathLike[str]):
        supplied = Path(discovery_root)
        if supplied.name != "discovery":
            raise DiscoveryDatasetError("loader root must be the discovery directory")
        absolute = supplied.absolute()
        anchor = Path(absolute.anchor)
        if supplied.is_symlink() or _has_symlink_component(absolute, anchor):
            raise DiscoveryDatasetError("discovery root symlink is forbidden")
        root = supplied.resolve(strict=True)
        if not root.is_dir():
            raise DiscoveryDatasetError("discovery root is not a directory")
        self.root = root

    def _load_partition(self, name: str) -> tuple[tuple[dict, ...], str]:
        if name not in ("train", "validation", "walk_forward"):
            raise DiscoveryDatasetError(f"unknown discovery partition: {name}")
        path = self.root / name / "bars.json"
        if _has_symlink_component(path, self.root):
            raise DiscoveryDatasetError(f"symlink forbidden in {name}")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise DiscoveryDatasetError(f"partition escapes discovery root: {name}")
        try:
            raw = resolved.read_bytes()
            rows = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DiscoveryDatasetError(f"cannot read {name}: {exc}") from exc
        if not isinstance(rows, list) or not rows:
            raise DiscoveryDatasetError(f"partition {name} must contain bars")
        required = {"ts", "open", "high", "low", "close"}
        last_ts = None
        clean: list[dict] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or not required.issubset(row):
                raise DiscoveryDatasetError(f"invalid bar at {name}[{index}]")
            if type(row["ts"]) is not int:
                raise DiscoveryDatasetError(f"invalid timestamp at {name}[{index}]")
            ts = row["ts"]
            if last_ts is not None and ts <= last_ts:
                raise DiscoveryDatasetError(f"non-monotone timestamps in {name}")
            try:
                op, hi, lo, close = (
                    float(row[field]) for field in ("open", "high", "low", "close")
                )
                volume = float(row.get("volume", 0.0))
            except (TypeError, ValueError) as exc:
                raise DiscoveryDatasetError(
                    f"invalid numeric bar at {name}[{index}]"
                ) from exc
            if (
                    not all(math.isfinite(value) for value in (op, hi, lo, close, volume))
                    or min(op, hi, lo, close) <= 0
                    or volume < 0
                    or hi < max(op, close)
                    or lo > min(op, close)
                    or hi < lo):
                raise DiscoveryDatasetError(f"invalid OHLCV at {name}[{index}]")
            last_ts = ts
            clean.append(copy.deepcopy(row))
        return tuple(clean), hashlib.sha256(raw).hexdigest()

    def load(self) -> DiscoveryPartitions:
        names = ("train", "validation", "walk_forward")
        paths = [self.root / name / "bars.json" for name in names]
        identities = [_file_identity(path.resolve(strict=True)) for path in paths]
        if len(set(identities)) != len(identities):
            raise DiscoveryDatasetError("partition files must have distinct identities")
        loaded = {name: self._load_partition(name) for name in names}
        train, validation, walk_forward = (
            loaded[name][0] for name in names
        )
        if train[-1]["ts"] >= validation[0]["ts"] \
                or validation[-1]["ts"] >= walk_forward[0]["ts"]:
            raise DiscoveryDatasetError("discovery partitions overlap or are unordered")
        return DiscoveryPartitions(
            train=train, validation=validation, walk_forward=walk_forward,
            source_root=str(self.root),
            partition_sha256=tuple(
                (name, loaded[name][1]) for name in names
            ),
        )

    def __repr__(self) -> str:
        return f"DiscoveryDatasetLoader(root={self.root!s})"


def verify_discovery_partitions(partitions: DiscoveryPartitions,
                                manifest_path: str | os.PathLike[str]) -> dict:
    """Bind loaded partitions, current files and the dataset manifest."""
    if not isinstance(partitions, DiscoveryPartitions):
        raise DiscoveryDatasetError("discovery partitions type invalid")
    source_root = Path(partitions.source_root).resolve(strict=True)
    supplied_manifest = Path(manifest_path)
    absolute_manifest = supplied_manifest.absolute()
    if supplied_manifest.is_symlink() or _has_symlink_component(
            absolute_manifest, Path(absolute_manifest.anchor)):
        raise DiscoveryDatasetError("dataset manifest symlink is forbidden")
    manifest_file = supplied_manifest.resolve(strict=True)
    if source_root.name != "discovery" or manifest_file.parent != source_root.parent:
        raise DiscoveryDatasetError("dataset manifest is not bound to discovery root")
    try:
        manifest_raw = manifest_file.read_bytes()
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiscoveryDatasetError(f"dataset manifest unreadable: {exc}") from exc
    if not isinstance(manifest, dict):
        raise DiscoveryDatasetError("dataset manifest must be an object")
    loaded_hashes = dict(partitions.partition_sha256)
    names = ("train", "validation", "walk_forward")
    if set(loaded_hashes) != set(names):
        raise DiscoveryDatasetError("loaded partition hashes are incomplete")
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, list):
        raise DiscoveryDatasetError("dataset manifest files must be a list")
    relevant_entries = [
        row for row in manifest_files
        if isinstance(row, dict) and row.get("partition") in names
    ]
    entries = {row.get("partition"): row for row in relevant_entries}
    if len(relevant_entries) != len(names) or set(entries) != set(names):
        raise DiscoveryDatasetError("manifest partition entries are incomplete")
    partition_rows = {
        "train": partitions.train,
        "validation": partitions.validation,
        "walk_forward": partitions.walk_forward,
    }
    verified: list[dict] = []
    for name in names:
        path = source_root / name / "bars.json"
        if _has_symlink_component(path, source_root):
            raise DiscoveryDatasetError(f"symlink forbidden in {name}")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(source_root):
            raise DiscoveryDatasetError(f"partition escapes discovery root: {name}")
        current_raw = resolved.read_bytes()
        current_sha = hashlib.sha256(current_raw).hexdigest()
        try:
            current_rows = json.loads(current_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DiscoveryDatasetError(f"partition unreadable: {name}") from exc
        entry = entries[name]
        rows = partition_rows[name]
        expected_path = f"discovery/{name}/bars.json"
        if entry.get("path") != expected_path \
                or entry.get("sha256") != current_sha \
                or loaded_hashes[name] != current_sha \
                or entry.get("rows") != len(rows) \
                or entry.get("first_ts") != rows[0]["ts"] \
                or entry.get("last_ts") != rows[-1]["ts"] \
                or current_rows != list(rows):
            raise DiscoveryDatasetError(f"partition evidence mismatch: {name}")
        verified.append({"partition": name, "sha256": current_sha, "rows": len(rows)})
    return {
        "status": "DISCOVERY_PARTITIONS_VERIFIED",
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "partitions": verified,
        "research_only": True,
        "final_recommendation": "NO LIVE",
    }


def load_verified_reference(discovery_root: str | os.PathLike[str],
                            manifest_path: str | os.PathLike[str]) -> tuple[
                                dict[int, float] | None, dict]:
    """Load optional cross-venue reference data only when manifest-bound."""
    supplied_root = Path(discovery_root)
    absolute_root = supplied_root.absolute()
    if supplied_root.is_symlink() or _has_symlink_component(
            absolute_root, Path(absolute_root.anchor)):
        raise DiscoveryDatasetError("reference discovery root symlink is forbidden")
    root = supplied_root.resolve(strict=True)
    supplied_manifest = Path(manifest_path)
    absolute_manifest = supplied_manifest.absolute()
    if supplied_manifest.is_symlink() or _has_symlink_component(
            absolute_manifest, Path(absolute_manifest.anchor)):
        raise DiscoveryDatasetError("reference manifest symlink is forbidden")
    manifest_file = supplied_manifest.resolve(strict=True)
    if root.name != "discovery" or manifest_file.parent != root.parent:
        raise DiscoveryDatasetError("reference manifest is not bound to discovery root")
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiscoveryDatasetError(f"reference manifest unreadable: {exc}") from exc
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, list):
        raise DiscoveryDatasetError("reference manifest files must be a list")
    entries = [
        row for row in manifest_files
        if isinstance(row, dict) and row.get("partition") == "reference_discovery"
    ]
    path = root / "reference" / "bars.json"
    if not entries:
        if path.exists() or path.is_symlink():
            raise DiscoveryDatasetError("unmanifested reference data is forbidden")
        return None, {
            "status": "REFERENCE_NOT_AVAILABLE", "rows": 0,
            "research_only": True, "final_recommendation": "NO LIVE",
        }
    if len(entries) != 1:
        raise DiscoveryDatasetError("reference manifest entry must be unique")
    if _has_symlink_component(path, root):
        raise DiscoveryDatasetError("reference symlink is forbidden")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise DiscoveryDatasetError("reference escapes discovery root")
    reference_source = manifest.get("reference_source")
    primary_source = manifest.get("source")
    if not isinstance(reference_source, dict) or not isinstance(primary_source, dict) \
            or reference_source.get("symbol") != manifest.get("symbol") \
            or primary_source.get("symbol") != manifest.get("symbol") \
            or not isinstance(reference_source.get("venue"), str) \
            or reference_source.get("venue") == primary_source.get("venue"):
        raise DiscoveryDatasetError("reference source contract invalid")
    reference_identity = _file_identity(resolved)
    primary_identities = {
        _file_identity((root / name / "bars.json").resolve(strict=True))
        for name in ("train", "validation", "walk_forward")
    }
    if reference_identity in primary_identities:
        raise DiscoveryDatasetError("reference file identity aliases primary data")
    raw = resolved.read_bytes()
    try:
        rows = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiscoveryDatasetError("reference data unreadable") from exc
    entry = entries[0]
    if not isinstance(rows, list) or not rows \
            or entry.get("path") != "discovery/reference/bars.json" \
            or entry.get("sha256") != hashlib.sha256(raw).hexdigest() \
            or entry.get("rows") != len(rows) \
            or entry.get("first_ts") != rows[0].get("ts") \
            or entry.get("last_ts") != rows[-1].get("ts"):
        raise DiscoveryDatasetError("reference evidence mismatch")
    reference: dict[int, float] = {}
    last_ts = None
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or type(row.get("ts")) is not int \
                or not isinstance(row.get("close"), (int, float)) \
                or isinstance(row.get("close"), bool) \
                or not math.isfinite(float(row["close"])) \
                or float(row["close"]) <= 0 \
                or (last_ts is not None and row["ts"] <= last_ts):
            raise DiscoveryDatasetError(f"invalid reference row at index {index}")
        last_ts = row["ts"]
        reference[int(row["ts"])] = float(row["close"])
    return reference, {
        "status": "REFERENCE_VERIFIED", "rows": len(reference),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "source_venue": reference_source["venue"],
        "primary_venue": primary_source["venue"],
        "research_only": True, "final_recommendation": "NO LIVE",
    }


def load_verified_holdout_commitment(
        discovery_root: str | os.PathLike[str],
        manifest_path: str | os.PathLike[str]) -> tuple[dict, dict]:
    """Load and bind holdout metadata without opening the sealed bar file."""
    supplied_root = Path(discovery_root)
    absolute_root = supplied_root.absolute()
    if supplied_root.is_symlink() or _has_symlink_component(
            absolute_root, Path(absolute_root.anchor)):
        raise DiscoveryDatasetError("holdout discovery root symlink is forbidden")
    root = supplied_root.resolve(strict=True)
    supplied_manifest = Path(manifest_path)
    absolute_manifest = supplied_manifest.absolute()
    if supplied_manifest.is_symlink() or _has_symlink_component(
            absolute_manifest, Path(absolute_manifest.anchor)):
        raise DiscoveryDatasetError("holdout manifest symlink is forbidden")
    manifest_file = supplied_manifest.resolve(strict=True)
    if root.name != "discovery" or manifest_file.parent != root.parent:
        raise DiscoveryDatasetError("holdout manifest is not bound to discovery root")
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiscoveryDatasetError(f"holdout manifest unreadable: {exc}") from exc
    files = manifest.get("files")
    if not isinstance(files, list):
        raise DiscoveryDatasetError("holdout manifest files must be a list")
    commitment_entries = [
        row for row in files
        if isinstance(row, dict) and row.get("partition") == "holdout_commitment"
    ]
    sealed_entries = [
        row for row in files
        if isinstance(row, dict) and row.get("partition") == "holdout_sealed"
    ]
    if len(commitment_entries) != 1 or len(sealed_entries) != 1:
        raise DiscoveryDatasetError("holdout manifest entries must be unique")
    commitment_entry, sealed_entry = commitment_entries[0], sealed_entries[0]
    if commitment_entry.get("path") != "sealed_holdout/commitment.json" \
            or sealed_entry.get("path") != "sealed_holdout/bars.json":
        raise DiscoveryDatasetError("holdout manifest paths invalid")
    combo_root = root.parent
    path = combo_root / "sealed_holdout" / "commitment.json"
    if _has_symlink_component(path, combo_root):
        raise DiscoveryDatasetError("holdout commitment symlink is forbidden")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(combo_root) or resolved.parent != (
            combo_root / "sealed_holdout").resolve(strict=True):
        raise DiscoveryDatasetError("holdout commitment escapes dataset root")
    raw = resolved.read_bytes()
    if hashlib.sha256(raw).hexdigest() != commitment_entry.get("sha256"):
        raise DiscoveryDatasetError("holdout commitment hash mismatch")
    try:
        commitment = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiscoveryDatasetError("holdout commitment unreadable") from exc
    split = manifest.get("split")
    holdout_range = split.get("holdout") if isinstance(split, dict) else None
    if not isinstance(commitment, dict) or not (
            commitment.get("schema") == "v10_47_20_holdout_commitment"
            and commitment.get("state") == "SEALED"
            and commitment.get("symbol") == manifest.get("symbol")
            and commitment.get("timeframe") == manifest.get("timeframe")
            and commitment.get("data_file") == "bars.json"
            and commitment.get("research_only") is True
            and commitment.get("final_recommendation") == "NO LIVE"
            and commitment.get("index_range") == holdout_range
            and type(commitment.get("n_bars")) is int
            and commitment["n_bars"] > 0
            and isinstance(holdout_range, list)
            and len(holdout_range) == 2
            and all(type(value) is int for value in holdout_range)
            and holdout_range[1] - holdout_range[0] == commitment["n_bars"]
            and sealed_entry.get("rows") == commitment["n_bars"]
            and sealed_entry.get("sha256") == commitment.get("commitment_sha256")):
        raise DiscoveryDatasetError("holdout commitment contract mismatch")
    return copy.deepcopy(commitment), {
        "status": "HOLDOUT_COMMITMENT_VERIFIED",
        "commitment_file_sha256": hashlib.sha256(raw).hexdigest(),
        "sealed_data_sha256": commitment["commitment_sha256"],
        "n_bars": commitment["n_bars"],
        "index_range": copy.deepcopy(commitment["index_range"]),
        "sealed_data_opened": False,
        "research_only": True,
        "final_recommendation": "NO LIVE",
    }


def _file_identity(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return int(stat.st_dev), int(stat.st_ino)


def audit_dataset_isolation(discovery_root: str | os.PathLike[str],
                            sealed_root: str | os.PathLike[str]) -> dict:
    """Audit path/file separation without reading holdout data."""
    discovery = Path(discovery_root).resolve(strict=True)
    sealed = Path(sealed_root).resolve(strict=True)
    problems: list[str] = []
    if discovery == sealed or discovery.is_relative_to(sealed) or sealed.is_relative_to(discovery):
        problems.append("roots_not_separate")
    discovery_files = [p for p in discovery.rglob("*") if p.is_file()]
    sealed_files = [p for p in sealed.rglob("*") if p.is_file()]
    d_resolved = {str(p.resolve(strict=True)) for p in discovery_files}
    h_resolved = {str(p.resolve(strict=True)) for p in sealed_files}
    shared_paths = sorted(d_resolved & h_resolved)
    d_ids = {_file_identity(p) for p in discovery_files}
    h_ids = {_file_identity(p) for p in sealed_files}
    shared_ids = sorted(d_ids & h_ids)
    if shared_paths:
        problems.append("shared_paths")
    if shared_ids:
        problems.append("shared_file_ids")
    commitment_path = sealed / "commitment.json"
    try:
        commitment = json.loads(commitment_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        commitment = {}
        problems.append("commitment_unreadable")
    if commitment.get("state") != "SEALED":
        problems.append("holdout_not_sealed")
    return {
        "ok": not problems,
        "discovery_root": str(discovery),
        "sealed_root": str(sealed),
        "shared_paths": shared_paths,
        "shared_file_ids": [list(item) for item in shared_ids],
        "loader_references_holdout": False,
        "holdout_state": commitment.get("state", "UNKNOWN"),
        "problems": problems,
        "research_only": True,
        "final_recommendation": "NO LIVE",
    }
