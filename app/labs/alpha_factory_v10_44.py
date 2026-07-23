"""ResearchOps V10.44 - Max Alpha Discovery Sprint (research only).

This module searches a larger but still transparent space of mechanical,
ex-ante strategy hypotheses over local bar data. It is intentionally offline:
no network, no exchange client, no DB writes, no paper filter and NO LIVE.

The sprint is ambitious in breadth, not permissive in conclusions. Every
candidate is evaluated chronologically (train/validation/test), entered at
next-bar open, charged costs, stress-tested, compared with baselines and ranked
by a lower-bound net EV. The highest possible output is a research incubator
candidate, never an executable signal.
"""

from __future__ import annotations

import csv
import json
import math
import os
import statistics as st
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import autonomous_strategy_lab_v10_43b as LAB
from . import continuous_edge_factory_v10_38 as CE
from . import shadow_simulation_tournament_v10_40 as SH

TOOL_VERSION = "v10.44"
OUTPUT_SUBDIR = ("reports", "research", "v10_44_alpha_sprint")
MIN_BARS = 180
MIN_TEST_SIGNALS = 20
MIN_VALIDATION_SIGNALS = 15
MIN_TOTAL_SIGNALS = 45
MAX_CANDIDATES_DEFAULT = 240
SLIP_STRESS_MULT = 1.5
COST_STRESS = {
    "base": {},
    "stress_0_22": {"fee_bps": 7.0, "slippage_bps": 4.0, "spread_bps": 1.0},
    "stress_0_25": {"fee_bps": 8.0, "slippage_bps": 4.5, "spread_bps": 1.0},
    "stress_0_35": {"fee_bps": 11.0, "slippage_bps": 5.5, "spread_bps": 2.0},
}

FORBIDDEN_FEATURE_PREFIXES = ("ret_", "mfe", "mae", "future", "label",
                              "outcome", "barrier", "pnl")


class RuntimeBudgetExceeded(RuntimeError):
    """Cooperative stop between deterministic replay chunks."""


def _safety() -> dict[str, Any]:
    return {
        "research_only": True,
        "shadow_only": True,
        "paper_ready": False,
        "live_ready": False,
        "can_send_real_orders": False,
        "paper_filter_enabled": False,
        "edge_validated": False,
        "not_actionable": True,
        "no_orders": True,
        "changes_sizing": False,
        "changes_leverage": False,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _repo_out() -> Path:
    return CE._repo_root().joinpath(*OUTPUT_SUBDIR)


def _round(x: Any, nd: int = 8) -> float | None:
    try:
        if x is None or not math.isfinite(float(x)):
            return None
        return round(float(x), nd)
    except Exception:
        return None


def _mean(xs: list[float]) -> float:
    return st.mean(xs) if xs else 0.0


def _pf(xs: list[float]) -> float:
    wins = sum(x for x in xs if x > 0)
    losses = abs(sum(x for x in xs if x < 0))
    if losses == 0:
        return 999.0 if wins > 0 else 0.0
    return wins / losses


def _dd(xs: list[float]) -> float:
    peak = cur = dd = 0.0
    for x in xs:
        cur += x
        peak = max(peak, cur)
        dd = min(dd, cur - peak)
    return dd


def _lower_bound(xs: list[float], tests: int = 1) -> float | None:
    n = len(xs)
    if not xs:
        return None
    sd = st.pstdev(xs) if n > 1 else 0.0
    penalty = 1.65 * sd / math.sqrt(max(n, 1))
    multiple = math.sqrt(max(math.log(max(tests, 2)), 0.0)) * sd / max(math.sqrt(max(n, 1)), 1.0)
    return _mean(xs) - penalty - multiple


def _bars_meta(bars: list[dict]) -> dict[str, Any]:
    if not bars:
        return {"n_bars": 0, "coverage_minutes": 0, "max_contiguous_run": 0, "gap_count": 0}
    bars = sorted(bars, key=lambda b: int(b.get("ts", 0)))
    max_run = cur = 1
    gaps = 0
    for a, b in zip(bars, bars[1:]):
        step = int(b.get("ts", 0)) - int(a.get("ts", 0))
        if step <= 2 * 60_000:
            cur += 1
        else:
            gaps += 1
            max_run = max(max_run, cur)
            cur = 1
    max_run = max(max_run, cur)
    span = max(0, int(bars[-1].get("ts", 0)) - int(bars[0].get("ts", 0)))
    return {"n_bars": len(bars), "coverage_minutes": round(span / 60_000, 2),
            "max_contiguous_run": max_run, "gap_count": gaps}


def build_alpha_features(bars: list[dict]) -> list[dict[str, Any]]:
    """Point-in-time feature rows.

    Uses only bars up to and including i. The row is available at the completed
    bar timestamp, and every strategy enters at i+1 open via V10.40 simulator.
    """
    feats: list[dict[str, Any]] = []
    closes: list[float] = []
    vols: list[float] = []
    buy_vols: list[float] = []
    sell_vols: list[float] = []
    trade_counts: list[float] = []
    ranges: list[float] = []
    for i, b in enumerate(bars):
        close = float(b.get("close", 0.0) or 0.0)
        open_ = float(b.get("open", close) or close)
        high = float(b.get("high", close) or close)
        low = float(b.get("low", close) or close)
        volume = float(b.get("volume", 0.0) or 0.0)
        buy_v = float(b.get("buy_volume", 0.0) or 0.0)
        sell_v = float(b.get("sell_volume", 0.0) or 0.0)
        tc = float(b.get("trade_count", b.get("n_trades", 0.0)) or 0.0)
        closes.append(close)
        vols.append(volume)
        buy_vols.append(buy_v)
        sell_vols.append(sell_v)
        trade_counts.append(tc)
        ranges.append((high - low) / close if close > 0 else 0.0)
        def ret(n: int) -> float:
            if len(closes) <= n or closes[-1 - n] <= 0:
                return 0.0
            return close / closes[-1 - n] - 1.0
        def sma(vals: list[float], n: int) -> float:
            w = vals[-n:]
            return sum(w) / len(w) if w else 0.0
        def z_last(vals: list[float], n: int) -> float:
            w = vals[-n:]
            if len(w) < 3:
                return 0.0
            sd = st.pstdev(w)
            return (w[-1] - st.mean(w)) / sd if sd > 1e-12 else 0.0
        ma8 = sma(closes, 8)
        ma21 = sma(closes, 21)
        ma55 = sma(closes, 55)
        vol_ma = sma(vols, 30) or 1e-12
        recent_ret = [closes[j] / closes[j - 1] - 1 for j in range(max(1, len(closes) - 30), len(closes))
                      if closes[j - 1] > 0]
        rv = st.pstdev(recent_ret) if len(recent_ret) > 2 else 0.0
        win20 = closes[-20:]
        hi20 = max(win20) if win20 else close
        lo20 = min(win20) if win20 else close
        rng20 = max(hi20 - lo20, 1e-12)
        body = (close - open_) / close if close > 0 else 0.0
        upper = (high - max(open_, close)) / close if close > 0 else 0.0
        lower = (min(open_, close) - low) / close if close > 0 else 0.0
        buy_sum = sum(buy_vols[-10:])
        sell_sum = sum(sell_vols[-10:])
        flow = (buy_sum - sell_sum) / (buy_sum + sell_sum) if (buy_sum + sell_sum) else 0.0
        f = {
            "idx": i,
            "ts": int(b.get("ts", 0)),
            "available_at": int(b.get("available_at", b.get("ts", 0))),
            "close": close,
            "ret_1m_prefix": ret(1),
            "ret_3m_prefix": ret(3),
            "ret_5m_prefix": ret(5),
            "ret_15m_prefix": ret(15),
            "ret_30m_prefix": ret(30),
            "ema_slope_fast": (ma8 / ma21 - 1.0) if ma21 > 0 else 0.0,
            "ema_slope_slow": (ma21 / ma55 - 1.0) if ma55 > 0 else 0.0,
            "range_position_20": (close - lo20) / rng20,
            "volume_z": z_last(vols, 30),
            "trade_count_z": z_last(trade_counts, 30),
            "flow_imbalance_10": flow,
            "realized_volatility_30": rv,
            "range_z": z_last(ranges, 30),
            "body_pct": body,
            "upper_wick_pct": upper,
            "lower_wick_pct": lower,
            "compression": rv / (sma(ranges, 60) or 1e-12),
            "volume_acceleration": volume / vol_ma - 1.0,
            "hour_utc": int((int(b.get("ts", 0)) // 3_600_000) % 24),
            "session_bucket": int((int(b.get("ts", 0)) // 3_600_000) % 24 // 8),
        }
        f["trend_score"] = f["ema_slope_fast"] + 0.5 * f["ema_slope_slow"]
        f["breakout_pressure"] = f["range_position_20"] + max(f["volume_z"], 0.0) * 0.1
        f["reversal_pressure"] = (1.0 - f["range_position_20"]) + max(-f["ret_5m_prefix"], 0.0) * 10
        f["sell_reversal_pressure"] = f["range_position_20"] + max(f["ret_5m_prefix"], 0.0) * 10
        f["symbol_regime"] = "trend" if abs(f["trend_score"]) > max(rv, 0.0005) else "chop"
        feats.append(f)
    return feats


def _quantiles(feats: list[dict], split: int) -> dict[str, float]:
    keys = [
        "ret_3m_prefix", "ret_5m_prefix", "ret_15m_prefix", "ema_slope_fast",
        "ema_slope_slow", "range_position_20", "volume_z", "trade_count_z",
        "flow_imbalance_10", "realized_volatility_30", "range_z", "body_pct",
        "upper_wick_pct", "lower_wick_pct", "compression", "volume_acceleration",
        "trend_score", "breakout_pressure", "reversal_pressure", "sell_reversal_pressure",
    ]
    q: dict[str, float] = {}
    train = feats[:split]
    for k in keys:
        vals = sorted(float(f.get(k, 0.0) or 0.0) for f in train)
        if not vals:
            q[k + "_q20"] = q[k + "_q50"] = q[k + "_q80"] = q[k + "_q90"] = 0.0
            continue
        def pick(p: float) -> float:
            return vals[min(max(int(len(vals) * p), 0), len(vals) - 1)]
        q[k + "_q20"] = pick(0.20)
        q[k + "_q50"] = pick(0.50)
        q[k + "_q80"] = pick(0.80)
        q[k + "_q90"] = pick(0.90)
    return q


def _rule_defs() -> list[dict[str, Any]]:
    """Transparent strategy hypotheses. Outcomes are never used as inputs."""
    return [
        {"name": "trend_breakout_long", "family": "trend_breakout", "side": "long",
         "conds": [("trend_score", ">", "trend_score_q80"),
                   ("range_position_20", ">", "range_position_20_q80"),
                   ("volume_z", ">", "volume_z_q50")]},
        {"name": "trend_breakdown_short", "family": "trend_breakout", "side": "short",
         "conds": [("trend_score", "<", "trend_score_q20"),
                   ("range_position_20", "<", "range_position_20_q20"),
                   ("volume_z", ">", "volume_z_q50")]},
        {"name": "flow_continuation_long", "family": "aggressive_flow", "side": "long",
         "conds": [("flow_imbalance_10", ">", "flow_imbalance_10_q80"),
                   ("ret_3m_prefix", ">", "ret_3m_prefix_q50")]},
        {"name": "flow_continuation_short", "family": "aggressive_flow", "side": "short",
         "conds": [("flow_imbalance_10", "<", "flow_imbalance_10_q20"),
                   ("ret_3m_prefix", "<", "ret_3m_prefix_q50")]},
        {"name": "volume_squeeze_breakout_long", "family": "squeeze_breakout", "side": "long",
         "conds": [("compression", "<", "compression_q50"),
                   ("ret_5m_prefix", ">", "ret_5m_prefix_q80"),
                   ("volume_acceleration", ">", "volume_acceleration_q80")]},
        {"name": "volume_squeeze_breakdown_short", "family": "squeeze_breakout", "side": "short",
         "conds": [("compression", "<", "compression_q50"),
                   ("ret_5m_prefix", "<", "ret_5m_prefix_q20"),
                   ("volume_acceleration", ">", "volume_acceleration_q80")]},
        {"name": "capitulation_rebound_long", "family": "mean_reversion", "side": "long",
         "conds": [("ret_15m_prefix", "<", "ret_15m_prefix_q20"),
                   ("lower_wick_pct", ">", "lower_wick_pct_q80"),
                   ("range_position_20", "<", "range_position_20_q50")]},
        {"name": "failed_pump_reversal_short", "family": "mean_reversion", "side": "short",
         "conds": [("ret_15m_prefix", ">", "ret_15m_prefix_q80"),
                   ("upper_wick_pct", ">", "upper_wick_pct_q80"),
                   ("range_position_20", ">", "range_position_20_q50")]},
        {"name": "pullback_in_uptrend_long", "family": "pullback", "side": "long",
         "conds": [("ema_slope_slow", ">", "ema_slope_slow_q80"),
                   ("ret_3m_prefix", "<", "ret_3m_prefix_q50"),
                   ("range_position_20", ">", "range_position_20_q20")]},
        {"name": "bounce_fail_in_downtrend_short", "family": "pullback", "side": "short",
         "conds": [("ema_slope_slow", "<", "ema_slope_slow_q20"),
                   ("ret_3m_prefix", ">", "ret_3m_prefix_q50"),
                   ("range_position_20", "<", "range_position_20_q80")]},
    ]


def _feature_leakage_guard(rule: dict[str, Any]) -> None:
    bad = []
    for feat, _, key in rule.get("conds", []):
        low = str(feat).lower()
        if low.startswith(FORBIDDEN_FEATURE_PREFIXES) and not low.endswith("_prefix"):
            bad.append(feat)
        if any(tok in str(key).lower() for tok in ("label", "outcome", "future", "pnl", "barrier")):
            bad.append(str(key))
    if bad:
        raise ValueError(f"forbidden outcome/leakage features: {bad}")


def _rule_fn(rule: dict[str, Any], q: dict[str, float]):
    _feature_leakage_guard(rule)
    side = rule["side"]
    conds = list(rule["conds"])
    def fn(f, p, thr, rng):
        for feat, op, qkey in conds:
            v = f.get(feat)
            if not isinstance(v, (int, float)):
                return None
            target = q.get(qkey, 0.0)
            if op == ">" and not (v > target):
                return None
            if op == "<" and not (v < target):
                return None
        return side
    return fn


def _exit_grid() -> list[dict[str, Any]]:
    return [
        {"exit_name": "scalp_04_04_t15", "tp": 0.004, "sl": 0.004, "trail": None, "horizon": 15},
        {"exit_name": "scalp_06_04_trail_t30", "tp": 0.006, "sl": 0.004, "trail": 0.003, "horizon": 30},
        {"exit_name": "balanced_08_06_t45", "tp": 0.008, "sl": 0.006, "trail": None, "horizon": 45},
        {"exit_name": "runner_12_06_trail_t60", "tp": 0.012, "sl": 0.006, "trail": 0.005, "horizon": 60},
        {"exit_name": "tight_stop_06_03_t30", "tp": 0.006, "sl": 0.003, "trail": None, "horizon": 30},
    ]


def _split_ranges(n: int) -> dict[str, tuple[int, int]]:
    train_end = int(n * 0.60)
    val_end = int(n * 0.80)
    return {"train": (0, train_end), "validation": (train_end, val_end), "test": (val_end, n)}


def _metrics_from_outcomes(outs: list[dict], n_tests: int) -> dict[str, Any]:
    valid = [o for o in outs if o and o.get("valid")]
    invalid = [o for o in outs if o and not o.get("valid")]
    xs = [float(o.get("net_return", 0.0) or 0.0) for o in valid]
    wins = [x for x in xs if x > 0]
    return {
        "signals": len(outs),
        "valid_trades": len(valid),
        "invalid_outcomes": len(invalid),
        "net_EV": _round(_mean(xs)),
        "net_EV_lower_bound": _round(_lower_bound(xs, tests=n_tests)),
        "profit_factor": _round(_pf(xs), 4),
        "win_rate": _round(len(wins) / len(xs), 4) if xs else None,
        "max_drawdown": _round(_dd(xs)),
        "avg_bars_held": _round(_mean([float(o.get("bars_held", 0.0) or 0.0) for o in valid]), 3),
        "data_gap_count": sum(1 for o in outs if o and o.get("exit_reason") == "DATA_GAP"),
        "stale_exit_count": sum(1 for o in outs if o and o.get("exit_reason") == "STALE_EXIT"),
        "tp_count": sum(1 for o in valid if o.get("exit_reason") == "TP"),
        "sl_count": sum(1 for o in valid if o.get("exit_reason") == "SL"),
        "time_count": sum(1 for o in valid if o.get("exit_reason") == "TIME"),
        "trail_count": sum(1 for o in valid if o.get("exit_reason") == "TRAIL"),
    }


def _simulate_candidate(rule: dict[str, Any], exit_cfg: dict[str, Any],
                        feats: list[dict], bars: list[dict],
                        q: dict[str, float], costs: dict | None = None,
                        n_tests: int = 1,
                        deadline_epoch_seconds: float | None = None) -> dict[str, Any]:
    fn = _rule_fn(rule, q)
    outs: list[dict] = []
    rows: list[dict[str, Any]] = []
    last_i = -999
    for i, f in enumerate(feats[:-1]):
        if i % 1024 == 0 and deadline_epoch_seconds is not None:
            if time.time() >= deadline_epoch_seconds:
                raise RuntimeBudgetExceeded("runtime_budget_exhausted_during_candidate")
        if i - last_i < 3:
            continue
        side = fn(f, feats[i - 1] if i else None, q, None)
        if side is None:
            continue
        last_i = i
        out = SH.simulate_trade(
            bars, i, side, tp_pct=exit_cfg["tp"], sl_pct=exit_cfg["sl"],
            time_bars=exit_cfg["horizon"], trailing_pct=exit_cfg.get("trail"),
            costs=costs, entry_mode="next_open")
        if out is None:
            continue
        out["signal_i"] = i
        out["signal_ts"] = f.get("ts")
        outs.append(out)
        rows.append({"i": i, "ts": f.get("ts"), "side": side, "exit": out.get("exit_reason"),
                     "net_return": out.get("net_return"), "valid": out.get("valid")})
    ranges = _split_ranges(len(feats))
    by = {}
    for name, (lo, hi) in ranges.items():
        part = [o for o in outs if lo <= int(o.get("signal_i", -1)) < hi]
        by[name] = _metrics_from_outcomes(part, n_tests=n_tests)
    all_m = _metrics_from_outcomes(outs, n_tests=n_tests)
    return {"outcomes": outs, "rows": rows, "metrics_all": all_m, "metrics_by_split": by}


def _reprice_simulation(simulation: dict[str, Any], *, costs: dict | None,
                        n_features: int, n_tests: int) -> dict[str, Any]:
    """Reprice an identical path; cost stress must not resimulate bars."""
    round_trip = SH._round_trip(costs)
    outcomes: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for original in simulation.get("outcomes") or []:
        outcome = dict(original)
        outcome["fees"] = round_trip
        outcome["net_return"] = (
            float(outcome.get("gross_return") or 0.0) - round_trip
            if outcome.get("valid") else 0.0
        )
        outcomes.append(outcome)
        rows.append({
            "i": outcome.get("signal_i"), "ts": outcome.get("signal_ts"),
            "side": outcome.get("side"), "exit": outcome.get("exit_reason"),
            "net_return": outcome.get("net_return"), "valid": outcome.get("valid"),
        })
    ranges = _split_ranges(n_features)
    by = {
        name: _metrics_from_outcomes(
            [o for o in outcomes if lo <= int(o.get("signal_i", -1)) < hi],
            n_tests=n_tests,
        )
        for name, (lo, hi) in ranges.items()
    }
    return {
        "outcomes": outcomes,
        "rows": rows,
        "metrics_all": _metrics_from_outcomes(outcomes, n_tests=n_tests),
        "metrics_by_split": by,
        "path_reused": True,
        "cost_only_repricing": True,
    }


def _baseline_metrics(feats: list[dict], bars: list[dict], q: dict[str, float]) -> dict[str, Any]:
    baselines = {}
    for name, (kind, fn) in SH._policies().items():
        if kind != "baseline":
            continue
        r = SH.run_policy(name, kind, fn, feats, bars, int(len(feats) * 0.60), q,
                          tp_pct=0.006, sl_pct=0.006, time_bars=30,
                          trailing_pct=0.004, costs=None, cooldown=5,
                          entry_mode="next_open")
        baselines[name] = r.get("metrics", {})
    lbs = [m.get("net_EV_lower_bound") for m in baselines.values()
           if isinstance(m.get("net_EV_lower_bound"), (int, float))]
    return {"baselines": baselines, "best_baseline_lower_bound": max(lbs) if lbs else 0.0}


def _classify(m: dict, val: dict, test: dict, stress: dict[str, dict],
              baseline_lb: float, n_tests: int) -> tuple[str, list[str]]:
    blockers: list[str] = []
    n = int(test.get("valid_trades") or 0)
    if n < MIN_TEST_SIGNALS:
        blockers.append("test_sample_too_small")
    if int(val.get("valid_trades") or 0) < MIN_VALIDATION_SIGNALS:
        blockers.append("validation_sample_too_small")
    if int(m.get("valid_trades") or 0) < MIN_TOTAL_SIGNALS:
        blockers.append("total_sample_too_small")
    if (test.get("net_EV") is None) or float(test.get("net_EV") or 0.0) <= 0:
        blockers.append("test_net_ev_not_positive")
    if (test.get("profit_factor") is None) or float(test.get("profit_factor") or 0.0) <= 1.05:
        blockers.append("test_pf_too_low")
    if (test.get("net_EV_lower_bound") is None) or float(test.get("net_EV_lower_bound") or 0.0) <= 0:
        blockers.append("test_lower_bound_not_positive")
    if float(test.get("net_EV_lower_bound") or -9) <= float(baseline_lb or 0.0):
        blockers.append("does_not_beat_baseline_lower_bound")
    if float(test.get("max_drawdown") or 0.0) < -0.08:
        blockers.append("drawdown_too_large")
    if n_tests > 100 and n < 40:
        blockers.append("multiple_testing_small_sample")
    for name, sm in stress.items():
        if name == "base":
            continue
        if sm.get("net_EV") is None or float(sm.get("net_EV") or 0.0) <= 0:
            blockers.append(f"{name}_net_ev_fail")
            break
    if blockers:
        if all(b.endswith("sample_too_small") or b == "multiple_testing_small_sample" for b in blockers):
            return "NEEDS_MORE_DATA", blockers
        if test.get("net_EV") and float(test.get("net_EV") or 0) > 0:
            return "WATCH_ONLY", blockers
        return "REJECTED", blockers
    if n >= 80 and float(test.get("net_EV_lower_bound") or 0) > 0:
        return "PAPER_CANDIDATE_RESEARCH_ONLY", ["manual_review_required", "paper_filter_still_disabled"]
    return "INCUBATE", ["promising_but_needs_more_forward_data", "manual_review_required"]


def run_alpha_factory(symbols: str = "BTCUSDT", data_source: str = "ws_persistent",
                      max_runtime_minutes: float = 60.0,
                      write_reports: bool = True,
                      max_candidates: int = MAX_CANDIDATES_DEFAULT) -> dict[str, Any]:
    started = time.time()
    deadline = started + max(0.01, float(max_runtime_minutes)) * 60
    syms = [s.strip().upper() for s in str(symbols or "BTCUSDT").split(",") if s.strip()] or ["BTCUSDT"]
    all_candidates: list[dict[str, Any]] = []
    datasets: list[dict[str, Any]] = []
    errors: list[str] = []
    stage_timings: list[dict[str, Any]] = []
    for symbol in syms:
        symbol_started = time.perf_counter()
        try:
            bars, eff_source, meta = LAB._load_bars(symbol, data_source)
        except Exception as exc:
            bars, eff_source, meta = [], data_source, {"load_error": str(exc)[:180]}
        bmeta = _bars_meta(bars)
        datasets.append({"symbol": symbol, "requested_source": data_source,
                         "effective_source": eff_source, "meta": meta, **bmeta})
        if len(bars) < MIN_BARS:
            errors.append(f"{symbol}: insufficient bars {len(bars)}<{MIN_BARS}")
            continue
        feature_started = time.perf_counter()
        feats = build_alpha_features(bars)
        split = int(len(feats) * 0.60)
        q = _quantiles(feats, split)
        baseline = _baseline_metrics(feats, bars, q)
        search_started = time.perf_counter()
        rules = _rule_defs()
        exits = _exit_grid()
        total_tests = len(rules) * len(exits)
        tested = 0
        runtime_exhausted = False
        for rule in rules:
            for exit_cfg in exits:
                if tested >= max_candidates:
                    break
                if (time.time() - started) > float(max_runtime_minutes) * 60:
                    errors.append("runtime_budget_exhausted")
                    runtime_exhausted = True
                    break
                tested += 1
                # the multiple-testing penalty is applied to EVERY lower bound so
                # the ranking key is honestly "adjusted for 50 hypotheses tested"
                try:
                    sim = _simulate_candidate(
                        rule, exit_cfg, feats, bars, q,
                        n_tests=total_tests, deadline_epoch_seconds=deadline,
                    )
                except RuntimeBudgetExceeded:
                    errors.append("runtime_budget_exhausted_during_candidate")
                    runtime_exhausted = True
                    break
                stress_metrics: dict[str, dict] = {}
                for sname, costs in COST_STRESS.items():
                    ss = sim if sname == "base" else _reprice_simulation(
                        sim, costs=costs or None, n_features=len(feats), n_tests=total_tests,
                    )
                    stress_metrics[sname] = ss["metrics_by_split"]["test"]
                all_m = sim["metrics_all"]
                val_m = sim["metrics_by_split"]["validation"]
                test_m = sim["metrics_by_split"]["test"]
                status, blockers = _classify(
                    all_m, val_m, test_m, stress_metrics,
                    float(baseline["best_baseline_lower_bound"] or 0.0), total_tests)
                cid = f"v1044_{symbol}_{rule['name']}_{exit_cfg['exit_name']}"
                all_candidates.append({
                    "candidate_id": cid,
                    "symbol": symbol,
                    "data_source": eff_source,
                    "strategy_name": rule["name"],
                    "family": rule["family"],
                    "side": rule["side"].upper(),
                    "entry_rule": " AND ".join(f"{a} {b} train:{c}" for a, b, c in rule["conds"]),
                    "entry_timing": "signal_bar_close_then_next_open",
                    "exit_policy": exit_cfg["exit_name"],
                    "exit_config": exit_cfg,
                    "features_ex_ante_only": True,
                    "outcome_source": "bar_by_bar_replay_next_open",
                    "same_bar_policy": "STOP_BEFORE_TP",
                    "ranking_key": "test.net_EV_lower_bound_adjusted",
                    "metrics_all": all_m,
                    "metrics_validation": val_m,
                    "metrics_test": test_m,
                    "cost_stress": stress_metrics,
                    "baseline_best_lower_bound": _round(baseline["best_baseline_lower_bound"]),
                    "status": status,
                    "blockers": blockers,
                    "score": _candidate_score(test_m, val_m, stress_metrics, blockers),
                    **_safety(),
                })
            if runtime_exhausted:
                break
        if runtime_exhausted or "runtime_budget_exhausted" in errors:
            break
        stage_timings.append({
            "symbol": symbol,
            "feature_and_baseline_seconds": round(search_started - feature_started, 3),
            "candidate_search_seconds": round(time.perf_counter() - search_started, 3),
            "total_symbol_seconds": round(time.perf_counter() - symbol_started, 3),
            "cost_stress_mode": "REPRICE_IDENTICAL_PATH",
        })
    all_candidates.sort(key=lambda c: (float(c["metrics_test"].get("net_EV_lower_bound") or -9),
                                       float(c["metrics_test"].get("net_EV") or -9),
                                       c["score"]), reverse=True)
    counts = {s: sum(1 for c in all_candidates if c["status"] == s)
              for s in ("PAPER_CANDIDATE_RESEARCH_ONLY", "INCUBATE",
                        "WATCH_ONLY", "NEEDS_MORE_DATA", "REJECTED")}
    summary = {
        "tool_version": TOOL_VERSION,
        "ran_at": _now(),
        "symbols": syms,
        "data_source": data_source,
        "runtime_seconds": round(time.time() - started, 3),
        "stage_timings": stage_timings,
        "datasets": datasets,
        "strategies_tested": len(all_candidates),
        "candidate_status_counts": counts,
        "top_candidates": all_candidates[:15],
        "best_candidate": all_candidates[0] if all_candidates else None,
        "errors": errors,
        "overall_verdict": _overall(counts, errors),
        "heavy_run_completed": "runtime_budget_exhausted" not in errors,
        "no_lookahead_contract": "features prefix-only; outcomes bars[i+1:]; entry next open",
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
        **_safety(),
    }
    if write_reports:
        _write_reports(summary, all_candidates)
        summary["reports_dir"] = str(_repo_out()).replace("\\", "/")
    return summary


def _candidate_score(test_m: dict, val_m: dict, stress: dict[str, dict], blockers: list[str]) -> int:
    score = 0
    if (test_m.get("net_EV") or 0) > 0:
        score += 25
    if (test_m.get("net_EV_lower_bound") or 0) > 0:
        score += 25
    if (test_m.get("profit_factor") or 0) > 1.10:
        score += 15
    if (val_m.get("net_EV") or 0) > 0:
        score += 10
    if all((m.get("net_EV") or -1) > 0 for k, m in stress.items() if k != "base"):
        score += 15
    score += min(10, int((test_m.get("valid_trades") or 0) / 10))
    score -= min(40, len(blockers) * 6)
    return max(0, min(100, score))


def _overall(counts: dict[str, int], errors: list[str]) -> str:
    if errors and not any(counts.values()):
        return "NEED_DATA"
    if counts.get("PAPER_CANDIDATE_RESEARCH_ONLY", 0):
        return "PAPER_CANDIDATE_RESEARCH_ONLY_BLOCKED_MANUAL_REVIEW"
    if counts.get("INCUBATE", 0):
        return "INCUBATE_RESEARCH_ONLY"
    if counts.get("WATCH_ONLY", 0):
        return "WATCH_ONLY"
    if counts.get("NEEDS_MORE_DATA", 0):
        return "NEEDS_MORE_DATA"
    return "NO_EDGE_ALL_REJECTED"


def _write_reports(summary: dict[str, Any], candidates: list[dict[str, Any]]) -> None:
    out = _repo_out()
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "alpha_factory_v10_44.json.tmp"
    tmp.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, out / "alpha_factory_v10_44.json")
    with open(out / "alpha_factory_candidates_v10_44.csv", "w", newline="", encoding="utf-8") as fh:
        fields = ["candidate_id", "symbol", "strategy_name", "family", "side",
                  "exit_policy", "status", "score", "blockers", "test_net_EV",
                  "test_net_EV_lower_bound", "test_pf", "test_trades",
                  "validation_net_EV", "cost_stress_025_net_EV"]
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for c in candidates:
            tm = c.get("metrics_test") or {}
            vm = c.get("metrics_validation") or {}
            cs = (c.get("cost_stress") or {}).get("stress_0_25") or {}
            w.writerow({
                "candidate_id": c.get("candidate_id"), "symbol": c.get("symbol"),
                "strategy_name": c.get("strategy_name"), "family": c.get("family"),
                "side": c.get("side"), "exit_policy": c.get("exit_policy"),
                "status": c.get("status"), "score": c.get("score"),
                "blockers": "|".join(c.get("blockers") or []),
                "test_net_EV": tm.get("net_EV"),
                "test_net_EV_lower_bound": tm.get("net_EV_lower_bound"),
                "test_pf": tm.get("profit_factor"),
                "test_trades": tm.get("valid_trades"),
                "validation_net_EV": vm.get("net_EV"),
                "cost_stress_025_net_EV": cs.get("net_EV"),
            })
    (out / "alpha_factory_v10_44.md").write_text(_memo(summary, candidates), encoding="utf-8")


def _memo(summary: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    lines = [
        "# V10.44 Alpha Factory",
        "",
        f"- ran_at: {summary.get('ran_at')}",
        f"- symbols: {','.join(summary.get('symbols') or [])}",
        f"- data_source: {summary.get('data_source')}",
        f"- strategies_tested: {summary.get('strategies_tested')}",
        f"- overall_verdict: {summary.get('overall_verdict')}",
        f"- final_recommendation: {FINAL_RECOMMENDATION_NO_LIVE}",
        "",
        "## Dataset",
    ]
    for d in summary.get("datasets") or []:
        lines.append(f"- {d.get('symbol')}: bars={d.get('n_bars')} max_run={d.get('max_contiguous_run')} gaps={d.get('gap_count')} source={d.get('effective_source')}")
    lines.extend(["", "## Top Candidates"])
    if not candidates:
        lines.append("- none")
    for c in candidates[:10]:
        tm = c.get("metrics_test") or {}
        lines.append(
            f"- {c['candidate_id']}: {c['status']} score={c['score']} "
            f"test_EV={tm.get('net_EV')} lb={tm.get('net_EV_lower_bound')} "
            f"PF={tm.get('profit_factor')} trades={tm.get('valid_trades')} "
            f"blockers={','.join(c.get('blockers') or []) or 'NONE'}")
    lines.extend(["", "Research only. Not actionable. Paper filter remains disabled. NO LIVE."])
    return "\n".join(lines) + "\n"


def render_cli(summary: dict[str, Any], title: str = "ALPHA FACTORY V10.44") -> str:
    lines = [f"{title} START"]
    lines.append(f"overall_verdict: {summary.get('overall_verdict')}")
    lines.append(f"symbols: {','.join(summary.get('symbols') or [])}")
    lines.append(f"data_source: {summary.get('data_source')}")
    lines.append(f"strategies_tested: {summary.get('strategies_tested')}")
    lines.append(f"candidate_status_counts: {json.dumps(summary.get('candidate_status_counts') or {}, default=str)}")
    best = summary.get("best_candidate") or {}
    lines.append(f"best_candidate: {best.get('candidate_id') or 'NONE'}")
    lines.append(f"best_status: {best.get('status') or 'NONE'}")
    if best:
        tm = best.get("metrics_test") or {}
        lines.append(f"best_test_net_EV: {tm.get('net_EV')}")
        lines.append(f"best_test_net_EV_lower_bound: {tm.get('net_EV_lower_bound')}")
        lines.append(f"best_test_pf: {tm.get('profit_factor')}")
        lines.append(f"best_blockers: {','.join(best.get('blockers') or []) or 'NONE'}")
    lines.append(f"reports_dir: {summary.get('reports_dir', str(_repo_out()).replace(chr(92), '/'))}")
    lines.append("research_only: true")
    lines.append("paper_filter_enabled: false")
    lines.append("can_send_real_orders: false")
    lines.append("paper_ready: false")
    lines.append("live_ready: false")
    lines.append("final_recommendation: NO LIVE")
    lines.append(f"{title} END")
    return "\n".join(lines)
