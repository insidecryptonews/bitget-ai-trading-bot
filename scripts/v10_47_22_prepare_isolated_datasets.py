"""Prepare physically isolated discovery and sealed holdout partitions.

This is the only V10.47.22 process allowed to load the complete source series.
The external authority secret is generated in memory and deliberately discarded,
so tournament processes cannot open the resulting holdout.  Research only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.labs import edge_discovery_engine_v10_45_1 as ENG  # noqa: E402
from app.labs import public_data_backfill_v10_45_1 as BF  # noqa: E402
from app.labs.v10_46 import causal_tournament as CT  # noqa: E402
from app.labs.v10_46 import sealed_holdout as SH  # noqa: E402
from app.labs.v10_46.discovery_dataset import audit_dataset_isolation  # noqa: E402


DEFAULT_ROOT = ROOT / "external_data" / "staging" / "v10_47_22_isolated"
FACTORS = {"1m": 1, "5m": 5, "15m": 15}
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{uuid.uuid4().hex}")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def safe_output_root(value: str | None) -> Path:
    allowed = DEFAULT_ROOT.resolve()
    requested = Path(value) if value else DEFAULT_ROOT
    if not requested.is_absolute():
        requested = ROOT / requested
    requested = requested.resolve()
    if requested != allowed:
        raise RuntimeError("isolated datasets must use the dedicated staging root")
    if DEFAULT_ROOT.is_symlink() or any(parent.is_symlink() for parent in DEFAULT_ROOT.parents):
        raise RuntimeError("isolated dataset root may not traverse a symlink")
    requested.mkdir(parents=True, exist_ok=True)
    return requested


def verified_source(symbol: str) -> tuple[str, dict, list[dict], str | None, list[dict]]:
    for venue in ("bitget", "bybit"):
        verification = BF.verify_dataset(venue, symbol)
        if verification.get("ok"):
            main = BF.load_klines(venue, symbol)
            other = "bybit" if venue == "bitget" else "bitget"
            other_verification = BF.verify_dataset(other, symbol)
            reference = BF.load_klines(other, symbol) if other_verification.get("ok") else []
            return venue, verification, main, other if reference else None, reference
    raise RuntimeError(f"no verified 1m source dataset for {symbol}")


def source_record(venue: str, symbol: str) -> dict:
    current = BF.current_generation(venue, symbol)
    if current is None:
        raise RuntimeError(f"missing current generation for {venue}:{symbol}")
    return {
        "venue": venue,
        "symbol": symbol,
        "generation_id": current["generation_id"],
        "csv_path": Path(current["csv_path"]).resolve().relative_to(ROOT).as_posix(),
        "csv_sha256": sha256(Path(current["csv_path"])),
        "manifest_path": Path(current["manifest_path"]).resolve().relative_to(ROOT).as_posix(),
        "manifest_sha256": sha256(Path(current["manifest_path"])),
    }


def existing_complete(combo: Path) -> bool:
    manifest_path = combo / "dataset_manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    for record in manifest.get("files", []):
        path = combo / record.get("path", "")
        if not path.is_file() or sha256(path) != record.get("sha256"):
            return False
    isolation = audit_dataset_isolation(combo / "discovery", combo / "sealed_holdout")
    return bool(isolation["ok"] and manifest.get("status") == "COMPLETE")


def prepare_combo(output_root: Path, symbol: str, timeframe: str, *,
                  venue: str, verification: dict, bars_1m: list[dict],
                  reference_venue: str | None, reference_1m: list[dict]) -> dict:
    combo = output_root / symbol / timeframe
    if combo.exists():
        if existing_complete(combo):
            return json.loads((combo / "dataset_manifest.json").read_text(encoding="utf-8"))
        raise RuntimeError(f"incomplete existing combo requires manual review: {combo}")
    factor = FACTORS[timeframe]
    as_of = int(verification["as_of_ms"])
    bars = ENG.resample_bars(bars_1m, factor, as_of_ms=as_of) \
        if factor > 1 else list(bars_1m)
    reference = ENG.resample_bars(reference_1m, factor, as_of_ms=as_of) \
        if reference_1m and factor > 1 else list(reference_1m)
    split = CT.split_indices(len(bars))
    train_end = split["train"][1]
    validation_end = split["validation"][1]
    walk_forward_end = split["walk_forward"][1]
    partitions = {
        "train": bars[:train_end],
        "validation": bars[train_end:validation_end],
        "walk_forward": bars[validation_end:walk_forward_end],
    }
    holdout_rows = bars[walk_forward_end:]
    if any(not rows for rows in (*partitions.values(), holdout_rows)):
        raise RuntimeError(f"empty partition for {symbol}:{timeframe}")
    temporary = combo.with_name(combo.name + f".tmp.{uuid.uuid4().hex}")
    discovery = temporary / "discovery"
    sealed = temporary / "sealed_holdout"
    records: list[dict] = []
    for name, rows in partitions.items():
        path = discovery / name / "bars.json"
        atomic_bytes(path, json_bytes(rows))
        records.append({
            "path": path.relative_to(temporary).as_posix(),
            "sha256": sha256(path), "rows": len(rows),
            "first_ts": int(rows[0]["ts"]), "last_ts": int(rows[-1]["ts"]),
            "partition": name,
        })
    reference_discovery = [
        row for row in reference
        if int(row["ts"]) <= int(bars[walk_forward_end - 1]["ts"])
    ]
    if reference_discovery:
        reference_path = discovery / "reference" / "bars.json"
        atomic_bytes(reference_path, json_bytes(reference_discovery))
        records.append({
            "path": reference_path.relative_to(temporary).as_posix(),
            "sha256": sha256(reference_path), "rows": len(reference_discovery),
            "first_ts": int(reference_discovery[0]["ts"]),
            "last_ts": int(reference_discovery[-1]["ts"]),
            "partition": "reference_discovery",
        })
    holdout_path = sealed / "bars.json"
    atomic_bytes(holdout_path, json_bytes(holdout_rows))
    holdout_sha = sha256(holdout_path)
    authority_secret = secrets.token_bytes(32)
    commitment = SH.commitment_document(
        symbol=symbol, timeframe=timeframe, data_file="bars.json",
        data_sha256=holdout_sha,
        authority_key_sha256=hashlib.sha256(authority_secret).hexdigest(),
        n_bars=len(holdout_rows), index_range=tuple(split["holdout"]),
    )
    del authority_secret
    commitment_path = sealed / "commitment.json"
    atomic_bytes(commitment_path, json_bytes(commitment))
    records.extend([
        {"path": holdout_path.relative_to(temporary).as_posix(),
         "sha256": holdout_sha, "rows": len(holdout_rows),
         "partition": "holdout_sealed"},
        {"path": commitment_path.relative_to(temporary).as_posix(),
         "sha256": sha256(commitment_path), "partition": "holdout_commitment"},
    ])
    manifest = {
        "schema": "v10_47_22_isolated_dataset_manifest",
        "status": "COMPLETE",
        "symbol": symbol,
        "timeframe": timeframe,
        "venue": venue,
        "source_generation_id": verification["generation_id"],
        "source": source_record(venue, symbol),
        "reference_source": (
            source_record(reference_venue, symbol) if reference_venue else None
        ),
        "split": {key: list(value) if isinstance(value, tuple) else value
                  for key, value in split.items()},
        "files": records,
        "holdout_state": "SEALED",
        "authority_secret_persisted": False,
        "discovery_loader_has_holdout_reference": False,
        "research_only": True,
        "can_send_real_orders": False,
        "final_recommendation": "NO LIVE",
    }
    atomic_bytes(temporary / "dataset_manifest.json", json_bytes(manifest))
    isolation = audit_dataset_isolation(discovery, sealed)
    if not isolation["ok"]:
        raise RuntimeError(f"isolation audit failed: {isolation['problems']}")
    combo.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temporary, combo)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT,XRPUSDT,DOGEUSDT")
    parser.add_argument("--timeframes", default="1m,5m,15m")
    parser.add_argument("--output-root")
    args = parser.parse_args(argv)
    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    timeframes = [item.strip() for item in args.timeframes.split(",") if item.strip()]
    if any(timeframe not in FACTORS for timeframe in timeframes):
        raise RuntimeError("only 1m,5m,15m are allowed")
    output_root = safe_output_root(args.output_root)
    summary = {}
    for symbol in symbols:
        venue, verification, bars, reference_venue, reference = verified_source(symbol)
        for timeframe in timeframes:
            manifest = prepare_combo(
                output_root, symbol, timeframe, venue=venue,
                verification=verification, bars_1m=bars,
                reference_venue=reference_venue, reference_1m=reference,
            )
            summary[f"{symbol}:{timeframe}"] = {
                "status": manifest["status"],
                "holdout_state": manifest["holdout_state"],
                "source_generation_id": manifest["source_generation_id"],
            }
            print(
                f"{symbol} {timeframe}: COMPLETE discovery isolated, holdout SEALED",
                flush=True,
            )
    atomic_bytes(output_root / "preparation_summary.json", json_bytes(summary))
    print("research_only=true")
    print("can_send_real_orders=false")
    print("FINAL_RECOMMENDATION=NO LIVE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
