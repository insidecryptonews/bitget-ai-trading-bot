"""Physically isolated discovery dataset loader (research only, no live).

The loader is deliberately rooted at ``data_root/discovery`` and knows only the
TRAIN, VALIDATION and WALK_FORWARD partitions.  Holdout data is handled by a
different module and is never referenced or loaded here.
"""

from __future__ import annotations

import copy
import json
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
        if supplied.is_symlink():
            raise DiscoveryDatasetError("discovery root symlink is forbidden")
        root = supplied.resolve(strict=True)
        if not root.is_dir():
            raise DiscoveryDatasetError("discovery root is not a directory")
        self.root = root

    def _load_partition(self, name: str) -> tuple[dict, ...]:
        if name not in ("train", "validation", "walk_forward"):
            raise DiscoveryDatasetError(f"unknown discovery partition: {name}")
        path = self.root / name / "bars.json"
        if _has_symlink_component(path, self.root):
            raise DiscoveryDatasetError(f"symlink forbidden in {name}")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise DiscoveryDatasetError(f"partition escapes discovery root: {name}")
        try:
            rows = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DiscoveryDatasetError(f"cannot read {name}: {exc}") from exc
        if not isinstance(rows, list) or not rows:
            raise DiscoveryDatasetError(f"partition {name} must contain bars")
        required = {"ts", "open", "high", "low", "close"}
        last_ts = None
        clean: list[dict] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or not required.issubset(row):
                raise DiscoveryDatasetError(f"invalid bar at {name}[{index}]")
            ts = int(row["ts"])
            if last_ts is not None and ts <= last_ts:
                raise DiscoveryDatasetError(f"non-monotone timestamps in {name}")
            last_ts = ts
            clean.append(copy.deepcopy(row))
        return tuple(clean)

    def load(self) -> DiscoveryPartitions:
        return DiscoveryPartitions(
            train=self._load_partition("train"),
            validation=self._load_partition("validation"),
            walk_forward=self._load_partition("walk_forward"),
            source_root=str(self.root),
        )

    def __repr__(self) -> str:
        return f"DiscoveryDatasetLoader(root={self.root!s})"


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
