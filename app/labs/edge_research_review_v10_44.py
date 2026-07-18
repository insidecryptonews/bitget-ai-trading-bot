"""Read-only executable-outcome audit for Cross-Venue research signals.

The lab reconstructs one ex-ante evaluation per market episode, waits for a
public Bitget L1 quote, and evaluates fixed future horizons.  Future quotes are
counterfactual outcomes only; they are never features or runtime decisions.
"""

from __future__ import annotations

import bisect
import json
import math
import random
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


TOOL_VERSION = "V10.44_EDGE_RESEARCH_REVIEW_1"
FINAL_RECOMMENDATION = "NO LIVE"
DEFAULT_HORIZONS_MS = (100, 250, 500, 1000, 2000, 5000)
EPISODE_CLUSTER_GAP_MS = 5000
COST_SCENARIOS = {
    "CONSERVATIVE": {
        "total_bps": 18.0,
        "components": {
            "round_trip_taker_fee": 12.0,
            "round_trip_slippage": 3.0,
            "latency_reserve": 1.0,
            "impact_reserve": 0.5,
            "funding_reserve": 0.5,
            "basis_reserve": 1.0,
        },
    },
    "REALISTIC_BASE": {
        "total_bps": 15.5,
        "components": {
            "round_trip_taker_fee": 12.0,
            "round_trip_slippage": 3.0,
            "impact_reserve": 0.5,
        },
    },
    "BEST_DEFENSIBLE": {
        "total_bps": 14.5,
        "components": {
            "round_trip_taker_fee": 12.0,
            "round_trip_slippage": 2.0,
            "impact_reserve": 0.5,
        },
    },
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp_ms(value: Any) -> int | None:
    try:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        return None


def _safety() -> dict[str, Any]:
    return {
        "research_only": True,
        "counterfactual_outcomes_only": True,
        "outcome_data_used_as_feature": False,
        "paper_filter_enabled": False,
        "paper_ready": False,
        "live_ready": False,
        "can_send_real_orders": False,
        "activation": "disabled",
        "final_recommendation": FINAL_RECOMMENDATION,
    }


def _read_signals(db_path: Path, limit: int = 200_000) -> list[dict[str, Any]]:
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(
        f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=5,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        rows = conn.execute(
            "SELECT signal_id,symbol,direction,decision_ts,status,rejection_reason,"
            "payload_json FROM signals ORDER BY decision_ts LIMIT ?",
            (max(1, min(int(limit), 500_000)),),
        ).fetchall()
    finally:
        conn.close()
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        result.append({**dict(row), "payload": payload})
    return result


def _episode_candidates(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    reported: dict[str, int] = {}
    consensus_evaluations: list[dict[str, Any]] = []
    for row in rows:
        status = str(row.get("status") or "UNKNOWN")
        reported[status] = reported.get(status, 0) + 1
        payload = row.get("payload") or {}
        leaders = sorted(set(str(v) for v in (payload.get("leader_venues") or []) if v))
        if len(leaders) < 2:
            continue
        decision_ms = _timestamp_ms(row.get("decision_ts"))
        if decision_ms is None:
            continue
        core = (
            str(row.get("symbol") or "").upper(),
            str(row.get("direction") or "").upper(),
            tuple(leaders),
            str(payload.get("regime") or "UNCLASSIFIED_FORWARD"),
            str(payload.get("target_venue") or "bitget"),
        )
        consensus_evaluations.append({
            **row,
            "decision_ms": decision_ms,
            "leaders": leaders,
            "episode_core": core,
        })
    consensus_evaluations.sort(key=lambda row: (row["decision_ms"], row["signal_id"]))
    first_by_episode: dict[str, dict[str, Any]] = {}
    active: dict[tuple[str, str, tuple[str, ...], str, str], tuple[str, int]] = {}
    for row in consensus_evaluations:
        core = row["episode_core"]
        current = active.get(core)
        if current is None or row["decision_ms"] - current[1] > EPISODE_CLUSTER_GAP_MS:
            episode_key = "|".join((core[0], core[1], ",".join(core[2]), core[3], core[4],
                                    str(row["decision_ms"])))
            first_by_episode[episode_key] = {**row, "episode_key": episode_key,
                                             "episode_evaluations": 1}
        else:
            episode_key = current[0]
            first_by_episode[episode_key]["episode_evaluations"] += 1
        active[core] = (episode_key, row["decision_ms"])
    return list(first_by_episode.values()), {
        "raw_evaluations": sum(reported.values()),
        "consensus_evaluations": len(consensus_evaluations),
        "unique_consensus_episodes": len(first_by_episode),
        "duplicate_consensus_evaluations": len(consensus_evaluations) - len(first_by_episode),
        **{f"reported_{key}": value for key, value in sorted(reported.items())},
    }


def _quote_files(root: Path, symbols: set[str]) -> list[Path]:
    files: list[Path] = []
    for symbol in sorted(symbols):
        files.extend(sorted(
            root.joinpath("bitget", "normalized", symbol, "book_l1").glob("*/events.jsonl")
        ))
    return [path for path in files if path.is_file()]


def _load_quotes(root: Path, symbols: set[str]) -> tuple[dict[str, list[tuple[int, int, float, float]]], list[dict[str, Any]]]:
    quotes: dict[str, list[tuple[int, int, float, float]]] = {symbol: [] for symbol in symbols}
    seen_event_ids: set[str] = set()
    ordinal = 0
    evidence: list[dict[str, Any]] = []
    for path in _quote_files(root, symbols):
        accepted = 0
        malformed = 0
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        malformed += 1
                        continue
                    symbol = str(row.get("canonical_symbol") or row.get("symbol") or "").upper()
                    timestamp = row.get("local_receive_wall_ms")
                    bid = _finite(row.get("best_bid"))
                    ask = _finite(row.get("best_ask"))
                    try:
                        timestamp = int(timestamp)
                    except (TypeError, ValueError):
                        timestamp = None
                    if (symbol not in quotes or timestamp is None or bid is None or ask is None
                            or bid <= 0 or ask < bid):
                        malformed += 1
                        continue
                    event_id = str(row.get("event_id") or f"{path}:{line_number}")
                    if event_id in seen_event_ids:
                        continue
                    seen_event_ids.add(event_id)
                    ordinal += 1
                    quotes[symbol].append((timestamp, ordinal, bid, ask))
                    accepted += 1
        except OSError:
            malformed += 1
        evidence.append({
            "path": str(path), "bytes": path.stat().st_size,
            "mtime_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            "accepted_rows": accepted, "malformed_rows": malformed,
        })
    ordered = {symbol: sorted(rows) for symbol, rows in quotes.items()}
    return ordered, evidence


def _at_or_after(quotes: list[tuple[int, int, float, float]],
                 timestamp_ms: int) -> tuple[int, int, float, float] | None:
    index = bisect.bisect_left(quotes, (timestamp_ms, -1, -math.inf, -math.inf))
    return quotes[index] if index < len(quotes) else None


def _outcomes(episodes: list[dict[str, Any]], quotes: dict[str, list[tuple[int, int, float, float]]],
              horizons_ms: tuple[int, ...], fill_delay_ms: int) -> tuple[list[dict[str, Any]], int]:
    results: list[dict[str, Any]] = []
    missing = 0
    for episode in episodes:
        payload = episode.get("payload") or {}
        symbol = str(episode.get("symbol") or "").upper()
        direction = str(episode.get("direction") or "").upper()
        series = quotes.get(symbol) or []
        entry = _at_or_after(series, int(episode["decision_ms"]) + fill_delay_ms)
        if entry is None or direction not in {"LONG", "SHORT"}:
            missing += 1
            continue
        entry_ts, _, entry_bid, entry_ask = entry
        features = payload.get("features") or {}
        for horizon in horizons_ms:
            exit_quote = _at_or_after(series, int(episode["decision_ms"]) + horizon)
            if exit_quote is None or exit_quote[0] < entry_ts:
                continue
            exit_ts, _, exit_bid, exit_ask = exit_quote
            if direction == "LONG":
                entry_price, exit_price = entry_ask, exit_bid
                gross = (exit_price - entry_price) / entry_price * 10_000.0
            else:
                entry_price, exit_price = entry_bid, exit_ask
                gross = (entry_price - exit_price) / entry_price * 10_000.0
            if not math.isfinite(gross):
                continue
            row = {
                "episode_id": episode["episode_key"],
                "signal_id": episode.get("signal_id"),
                "symbol": symbol,
                "side": direction,
                "decision_ms": episode["decision_ms"],
                "entry_ms": entry_ts,
                "exit_ms": exit_ts,
                "horizon_ms": horizon,
                "leader_venues": episode["leaders"],
                "leader_count": len(episode["leaders"]),
                "decision_hour_utc": datetime.fromtimestamp(
                    episode["decision_ms"] / 1000.0, timezone.utc).hour,
                "average_leader_move_bps": _finite(features.get("average_leader_move_bps")),
                "target_move_bps": _finite(features.get("target_move_bps")),
                "spread_bps_at_decision": _finite(features.get("spread_bps")),
                "entry_price": entry_price,
                "exit_price": exit_price,
                "gross_executable_bps": gross,
            }
            for name, scenario in COST_SCENARIOS.items():
                row[f"net_{name.lower()}_bps"] = gross - float(scenario["total_bps"])
            results.append(row)
    filled_episodes = len({row["episode_id"] for row in results})
    missing += max(0, len(episodes) - filled_episodes - missing)
    return results, missing


def _profit_factor(values: list[float]) -> float | None:
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    if losses > 0:
        return gains / losses
    return None if gains <= 0 else 999.0


def _block_bootstrap_lower_bound(values: list[float], seed: int = 44,
                                 iterations: int = 1000) -> float | None:
    if len(values) < 3:
        return None
    rng = random.Random(seed)
    block = max(1, int(math.sqrt(len(values))))
    means: list[float] = []
    for _ in range(iterations):
        sampled: list[float] = []
        while len(sampled) < len(values):
            start = rng.randrange(len(values))
            sampled.extend(values[(start + offset) % len(values)] for offset in range(block))
        means.append(statistics.fmean(sampled[:len(values)]))
    means.sort()
    return means[max(0, int(0.05 * len(means)) - 1)]


def _metrics(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mean_bps": None, "median_bps": None,
                "win_rate": None, "profit_factor": None,
                "bootstrap_lower_bound_bps": None}
    return {
        "n": len(values),
        "mean_bps": statistics.fmean(values),
        "median_bps": statistics.median(values),
        "win_rate": sum(value > 0 for value in values) / len(values),
        "profit_factor": _profit_factor(values),
        "bootstrap_lower_bound_bps": _block_bootstrap_lower_bound(values),
    }


def _horizon_table(outcomes: list[dict[str, Any]], horizons: tuple[int, ...]) -> list[dict[str, Any]]:
    table: list[dict[str, Any]] = []
    for horizon in horizons:
        rows = [row for row in outcomes if row["horizon_ms"] == horizon]
        item: dict[str, Any] = {
            "horizon_ms": horizon,
            "gross_executable": _metrics([row["gross_executable_bps"] for row in rows]),
            "spread_embedded_in_executable_prices": True,
        }
        for name in COST_SCENARIOS:
            key = f"net_{name.lower()}_bps"
            item[name] = _metrics([row[key] for row in rows])
        table.append(item)
    return table


def _chronological_split(rows: list[dict[str, Any]], value_key: str) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: (row["decision_ms"], row["episode_id"]))
    n = len(ordered)
    train_end = int(n * 0.60)
    validation_end = int(n * 0.80)
    parts = {
        "train": ordered[:train_end],
        "validation": ordered[train_end:validation_end],
        "test": ordered[validation_end:],
    }
    result = {name: _metrics([row[value_key] for row in part]) for name, part in parts.items()}
    enough = all(result[name]["n"] >= 10 for name in parts)
    positive = enough and all((result[name]["mean_bps"] or 0) > 0 for name in parts)
    result["status"] = "WATCH_ONLY" if positive else ("REJECTED" if enough else "NEED_MORE_DATA")
    result["split_contract"] = "chronological_60_20_20_no_parameter_selection_on_test"
    return result


def _hypothesis_rows(rows: list[dict[str, Any]], horizon: int = 1000) -> list[dict[str, Any]]:
    base = [row for row in rows if row["horizon_ms"] == horizon]

    def low_consumption(row: dict[str, Any]) -> bool:
        leader = row.get("average_leader_move_bps")
        target = row.get("target_move_bps")
        return leader is not None and target is not None and abs(target) <= 0.5 * max(abs(leader), 1e-12)

    filters = {
        "current_consensus": lambda row: True,
        "three_venue_consensus": lambda row: row["leader_count"] >= 3,
        "extreme_leader_move_8bps": lambda row: abs(row.get("average_leader_move_bps") or 0) >= 8.0,
        "low_target_consumption": low_consumption,
        "tight_spread_0_5bps": lambda row: (
            row.get("spread_bps_at_decision") is not None
            and row["spread_bps_at_decision"] <= 0.5
        ),
    }
    result: list[dict[str, Any]] = []
    for name, predicate in filters.items():
        selected = [row for row in base if predicate(row)]
        metrics = _metrics([row["net_realistic_base_bps"] for row in selected])
        if metrics["n"] < 20:
            status = "NEED_MORE_DATA"
        elif ((metrics["mean_bps"] or 0) <= 0
              or (metrics["bootstrap_lower_bound_bps"] or -math.inf) <= 0):
            status = "REJECTED"
        else:
            status = "WATCH_ONLY"
        result.append({
            "hypothesis": name, "features": "EX_ANTE_ONLY",
            "fixed_horizon_ms": horizon, "metrics": metrics,
            "status": status, "promotion_allowed": False,
        })
    return result


def _group_table(rows: list[dict[str, Any]], horizon: int = 1000) -> list[dict[str, Any]]:
    selected = [row for row in rows if row["horizon_ms"] == horizon]
    groups: dict[tuple[str, str, str], list[float]] = {}
    for row in selected:
        for group_type, value in (
            ("symbol", row["symbol"]),
            ("side", row["side"]),
            ("hour_utc", f"{row['decision_hour_utc']:02d}"),
        ):
            groups.setdefault((group_type, value, "REALISTIC_BASE"), []).append(
                row["net_realistic_base_bps"])
    return [
        {"group_type": key[0], "group": key[1], "scenario": key[2], **_metrics(values)}
        for key, values in sorted(groups.items())
    ]


def _write_report(report: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "edge_research_review_v10_44.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    text_path = out_dir / "edge_research_review_v10_44.txt"
    lines = [
        f"verdict: {report['verdict']}",
        f"episodes: {report['coverage']['unique_consensus_episodes']}",
        f"filled_episodes: {report['coverage']['filled_episodes']}",
        f"best_diagnostic: {report.get('best_diagnostic')}",
        "research_only: true", "paper_filter_enabled: false",
        "can_send_real_orders: false", "final_recommendation: NO LIVE",
    ]
    temporary_text = text_path.with_suffix(".txt.tmp")
    temporary_text.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary_text.replace(text_path)


def run_edge_research_review(
    *,
    db_path: Path | str | None = None,
    events_root: Path | str | None = None,
    output_dir: Path | str | None = None,
    symbols: Iterable[str] | None = None,
    horizons_ms: Iterable[int] = DEFAULT_HORIZONS_MS,
    fill_delay_ms: int = 100,
    write_reports: bool = True,
) -> dict[str, Any]:
    root = _repo_root()
    database = Path(db_path) if db_path is not None else root / "data/runtime/cross_venue/cross_venue_paper.sqlite"
    events = Path(events_root) if events_root is not None else root / "external_data/staging/cross_venue_v1"
    out_dir = Path(output_dir) if output_dir is not None else root / "reports/research/edge_audit_v1044"
    horizons = tuple(sorted(set(max(1, int(value)) for value in horizons_ms)))
    requested_symbols = {str(symbol).upper() for symbol in (symbols or []) if str(symbol).strip()}
    try:
        raw_signals = _read_signals(database)
    except (OSError, sqlite3.Error, ValueError) as exc:
        report = {
            "tool_version": TOOL_VERSION, "generated_at": _utc_now(),
            "verdict": "NEED_DATA", "blockers": [f"READ_ONLY_LEDGER_ERROR:{type(exc).__name__}"],
            "coverage": {"raw_evaluations": 0, "unique_consensus_episodes": 0,
                         "filled_episodes": 0, "missing_fill_episodes": 0},
            **_safety(),
        }
        if write_reports:
            _write_report(report, out_dir)
        return report
    if requested_symbols:
        raw_signals = [row for row in raw_signals if str(row.get("symbol") or "").upper() in requested_symbols]
    episodes, funnel = _episode_candidates(raw_signals)
    symbol_set = {str(row.get("symbol") or "").upper() for row in episodes if row.get("symbol")}
    quotes, source_files = _load_quotes(events, symbol_set)
    outcome_rows, missing = _outcomes(episodes, quotes, horizons, max(0, int(fill_delay_ms)))
    filled = len({row["episode_id"] for row in outcome_rows})
    horizon_table = _horizon_table(outcome_rows, horizons)
    realistic = [
        (row["horizon_ms"], (row.get("REALISTIC_BASE") or {}).get("mean_bps"))
        for row in horizon_table
        if (row.get("REALISTIC_BASE") or {}).get("mean_bps") is not None
    ]
    best = max(realistic, key=lambda pair: pair[1], default=(None, None))
    fixed_1s = [row for row in outcome_rows if row["horizon_ms"] == 1000]
    chronological = _chronological_split(fixed_1s, "net_realistic_base_bps")
    hypotheses = _hypothesis_rows(outcome_rows)
    unavailable = [
        {"hypothesis": "spot_perp_lead", "status": "NEED_DATA", "missing": "SPOT_FEED"},
        {"hypothesis": "persistent_book_imbalance", "status": "NEED_DATA", "missing": "ORDERBOOK_FEATURE_LEDGER"},
        {"hypothesis": "trade_intensity", "status": "NEED_DATA", "missing": "TRADE_FEATURE_LEDGER"},
        {"hypothesis": "funding_oi_divergence", "status": "NEED_DATA", "missing": "SYNCHRONIZED_FUNDING_OI_FEATURES"},
    ]
    any_positive = any(
        (row.get("REALISTIC_BASE") or {}).get("mean_bps") is not None
        and row["REALISTIC_BASE"]["mean_bps"] > 0
        and (row["REALISTIC_BASE"].get("bootstrap_lower_bound_bps") or -math.inf) > 0
        for row in horizon_table
    )
    if not episodes or not outcome_rows:
        verdict = "NEED_DATA"
    elif any_positive and chronological["status"] == "WATCH_ONLY":
        verdict = "WATCH_ONLY_RESEARCH_ONLY"
    else:
        verdict = "REJECTED_CURRENT_SIGNAL_NO_EXECUTABLE_EDGE"
    report = {
        "tool_version": TOOL_VERSION,
        "generated_at": _utc_now(),
        "verdict": verdict,
        "method": {
            "signal_features": "EX_ANTE_LEDGER_PAYLOAD_ONLY",
            "episode_selection": "FIRST_TRUE_CONSENSUS_EVALUATION_PER_EX_ANTE_CORE_5S_CLUSTER",
            "entry": f"FIRST_PUBLIC_BITGET_L1_AT_OR_AFTER_DECISION_PLUS_{max(0, int(fill_delay_ms))}MS",
            "exit": "FIRST_PUBLIC_BITGET_L1_AT_OR_AFTER_FIXED_DECISION_HORIZON",
            "long_fill": "ENTRY_ASK_EXIT_BID",
            "short_fill": "ENTRY_BID_EXIT_ASK",
            "spread": "EMBEDDED_IN_EXECUTABLE_L1_PRICES_NOT_ADDED_AGAIN",
            "future_quotes": "COUNTERFACTUAL_OUTCOMES_ONLY_NOT_DECISION_FEATURES",
            "bootstrap": "DETERMINISTIC_MOVING_BLOCK_5_PERCENT_LOWER_BOUND",
        },
        "cost_scenarios": COST_SCENARIOS,
        "coverage": {
            **funnel,
            "filled_episodes": filled,
            "missing_fill_episodes": missing,
            "symbols": sorted(symbol_set),
            "quote_rows": sum(len(rows) for rows in quotes.values()),
            "quote_files": len(source_files),
        },
        "horizon_results": horizon_table,
        "best_diagnostic": {
            "horizon_ms": best[0], "realistic_base_mean_bps": best[1],
            "selection_is_hindsight_diagnostic_only": True,
            "promotion_allowed": False,
        },
        "chronological_60_20_20_at_1000ms": chronological,
        "hypotheses": hypotheses,
        "unavailable_hypotheses": unavailable,
        "groups_at_1000ms": _group_table(outcome_rows),
        "source_evidence": source_files,
        "claims": {
            "edge_validated": False,
            "current_filters_too_strict": False,
            "current_signal_arrives_too_late_or_move_not_realized": verdict.startswith("REJECTED"),
            "cost_model_double_counts_spread": False,
            "cost_reserves_are_all_realized_cash": False,
        },
        **_safety(),
    }
    if write_reports:
        _write_report(report, out_dir)
    return report


def render_cli(report: dict[str, Any]) -> str:
    coverage = report.get("coverage") or {}
    best = report.get("best_diagnostic") or {}
    return "\n".join([
        "EDGE RESEARCH REVIEW V10.44 START",
        f"verdict: {report.get('verdict')}",
        f"raw_evaluations: {coverage.get('raw_evaluations', 0)}",
        f"consensus_evaluations: {coverage.get('consensus_evaluations', 0)}",
        f"unique_consensus_episodes: {coverage.get('unique_consensus_episodes', 0)}",
        f"duplicate_consensus_evaluations: {coverage.get('duplicate_consensus_evaluations', 0)}",
        f"filled_episodes: {coverage.get('filled_episodes', 0)}",
        f"missing_fill_episodes: {coverage.get('missing_fill_episodes', 0)}",
        f"best_diagnostic_horizon_ms: {best.get('horizon_ms')}",
        f"best_realistic_base_mean_bps: {best.get('realistic_base_mean_bps')}",
        "research_only: true",
        "counterfactual_outcomes_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "paper_ready: false",
        "live_ready: false",
        "final_recommendation: NO LIVE",
        "EDGE RESEARCH REVIEW V10.44 END",
    ])
