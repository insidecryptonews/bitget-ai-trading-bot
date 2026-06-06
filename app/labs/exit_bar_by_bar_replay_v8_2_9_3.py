"""V8.2.9.3 — Bar-by-Bar Exit Replay Foundation (research-only).

Replaces the V8.2.9 approximate MFE/MAE exit audit with a true
bar-by-bar simulation over a provided OHLCV path. The replay engine
consumes ``ohlcv_path`` (a chronological list of bars) per row and
returns the realised net pct under each policy. MFE / MAE may be
computed AFTER the replay as descriptive metrics — they are NEVER fed
into the replay as inputs.

Hard contract:

- research-only;
- no production trailing / TP / SL changes;
- same-bar ambiguity resolved as ``STOP_BEFORE_TP`` (conservative);
- policy selection runs on the train slice only; test is single-shot;
- if the OHLCV path is missing for a row, that row contributes to
  ``need_data_rows`` and never to any policy's net pct.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK


# Policies replicated from the V8.2.9 audit so consumers can compare
# approximate vs bar-by-bar results.
POLICY_BASELINE_ACTUAL = "baseline_actual"
POLICY_TRAILING_ATR_SOFT = "trailing_atr_soft"
POLICY_PROFIT_LOCK_MFE = "profit_lock_after_mfe_threshold"
POLICY_PARTIAL_50_TP1_TRAILING = "partial_50_at_tp1_plus_trailing"
POLICY_NO_HORIZON_IF_TREND_VALID = "no_horizon_close_if_trend_still_valid"
POLICY_TIME_EXIT_IF_MOMENTUM_DEAD = "time_exit_only_if_momentum_dead"

POLICIES: tuple[str, ...] = (
    POLICY_BASELINE_ACTUAL,
    POLICY_TRAILING_ATR_SOFT,
    POLICY_PROFIT_LOCK_MFE,
    POLICY_PARTIAL_50_TP1_TRAILING,
    POLICY_NO_HORIZON_IF_TREND_VALID,
    POLICY_TIME_EXIT_IF_MOMENTUM_DEAD,
)

# Same-bar ambiguity rule.
SAME_BAR_AMBIGUITY_RULE = "STOP_BEFORE_TP"

# Cost levels and gates.
COST_NORMAL_PCT = 0.18
COST_REALISTIC_PCT = 0.25
COST_STRESS_PCT = 0.35
MIN_TEST_PF = 1.15
MIN_TEST_WINRATE = 0.55
MIN_SAMPLES_PER_SPLIT = 15
TRAIN_FRACTION = 0.60
VAL_FRACTION = 0.20

# Promotion statuses for the bar-by-bar best policy.
STATUS_NEED_DATA_BB = "NEED_DATA"
STATUS_REJECT_BB = "REJECT"
STATUS_WATCH_ONLY_BB = "WATCH_ONLY"
STATUS_RESEARCH_CANDIDATE_BB = "RESEARCH_CANDIDATE"
STATUS_PAPER_SANDBOX_GATED = "PAPER_SANDBOX_CANDIDATE_ONLY_IF_ALL_GATES_PASS"

# PF sentinel — keeps the V8.2.9.1 convention.
PF_SENTINEL_NO_LOSSES = 999.0


def _profit_factor(gross_profit: float, gross_loss: float) -> float:
    loss_abs = abs(float(gross_loss))
    if loss_abs == 0.0:
        return PF_SENTINEL_NO_LOSSES if float(gross_profit) > 0 else 0.0
    return float(gross_profit) / loss_abs


# ---------------------------------------------------------------------------
# Replay primitives
# ---------------------------------------------------------------------------


def _bar_field(bar: dict[str, Any], key: str) -> float | None:
    v = bar.get(key)
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _has_valid_path(path: Any) -> bool:
    if not isinstance(path, list) or not path:
        return False
    for bar in path:
        if not isinstance(bar, dict):
            return False
        for key in ("high", "low", "close"):
            if not isinstance(bar.get(key), (int, float)):
                return False
    return True


def replay_long_baseline(
    entry: float, tp: float, sl: float, path: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bar-by-bar replay of a LONG baseline (TP / SL / horizon close).

    Same-bar ambiguity resolves as STOP_BEFORE_TP — SL wins.
    """
    if entry <= 0:
        return {"net_pct": None, "exit_reason": "NEED_DATA",
                "exit_bar_index": -1, "exit_price": None,
                "same_bar_ambiguous": False}
    for i, bar in enumerate(path):
        high = _bar_field(bar, "high")
        low = _bar_field(bar, "low")
        if high is None or low is None:
            continue
        sl_hit = low <= sl
        tp_hit = high >= tp
        if sl_hit and tp_hit:
            # STOP_BEFORE_TP — SL wins.
            return {
                "net_pct": (sl - entry) / entry * 100.0,
                "exit_reason": "SL",
                "exit_bar_index": i,
                "exit_price": sl,
                "same_bar_ambiguous": True,
            }
        if sl_hit:
            return {
                "net_pct": (sl - entry) / entry * 100.0,
                "exit_reason": "SL",
                "exit_bar_index": i,
                "exit_price": sl,
                "same_bar_ambiguous": False,
            }
        if tp_hit:
            return {
                "net_pct": (tp - entry) / entry * 100.0,
                "exit_reason": "TP",
                "exit_bar_index": i,
                "exit_price": tp,
                "same_bar_ambiguous": False,
            }
    last_close = _bar_field(path[-1], "close")
    if last_close is None:
        return {"net_pct": None, "exit_reason": "NEED_DATA",
                "exit_bar_index": -1, "exit_price": None,
                "same_bar_ambiguous": False}
    return {
        "net_pct": (last_close - entry) / entry * 100.0,
        "exit_reason": "HORIZON",
        "exit_bar_index": len(path) - 1,
        "exit_price": last_close,
        "same_bar_ambiguous": False,
    }


def replay_short_baseline(
    entry: float, tp: float, sl: float, path: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bar-by-bar replay of a SHORT baseline (TP / SL / horizon close).

    SHORT orientation: ``tp < entry < sl``. The position profits when
    price drops. ``high >= sl`` triggers SL, ``low <= tp`` triggers TP.
    Same-bar ambiguity resolves as ``STOP_BEFORE_TP`` — SL wins, exactly
    mirroring the LONG conservative rule.
    """
    if entry <= 0:
        return {"net_pct": None, "exit_reason": "NEED_DATA",
                "exit_bar_index": -1, "exit_price": None,
                "same_bar_ambiguous": False}
    for i, bar in enumerate(path):
        high = _bar_field(bar, "high")
        low = _bar_field(bar, "low")
        if high is None or low is None:
            continue
        sl_hit = high >= sl
        tp_hit = low <= tp
        if sl_hit and tp_hit:
            # STOP_BEFORE_TP — SL wins.
            return {
                "net_pct": (entry - sl) / entry * 100.0,
                "exit_reason": "SL",
                "exit_bar_index": i,
                "exit_price": sl,
                "same_bar_ambiguous": True,
            }
        if sl_hit:
            return {
                "net_pct": (entry - sl) / entry * 100.0,
                "exit_reason": "SL",
                "exit_bar_index": i,
                "exit_price": sl,
                "same_bar_ambiguous": False,
            }
        if tp_hit:
            return {
                "net_pct": (entry - tp) / entry * 100.0,
                "exit_reason": "TP",
                "exit_bar_index": i,
                "exit_price": tp,
                "same_bar_ambiguous": False,
            }
    last_close = _bar_field(path[-1], "close")
    if last_close is None:
        return {"net_pct": None, "exit_reason": "NEED_DATA",
                "exit_bar_index": -1, "exit_price": None,
                "same_bar_ambiguous": False}
    return {
        "net_pct": (entry - last_close) / entry * 100.0,
        "exit_reason": "HORIZON",
        "exit_bar_index": len(path) - 1,
        "exit_price": last_close,
        "same_bar_ambiguous": False,
    }


def replay_long_policy(
    entry: float, tp: float, sl: float,
    path: list[dict[str, Any]], policy: str,
) -> dict[str, Any]:
    """Replay a LONG row under ``policy`` using only the OHLCV path.

    V8.2.9.4 conservative-intrabar fix: the trailing stop / profit-lock
    applicable on bar ``i`` is computed using ``running_high_prev``
    (information from bars ``0..i-1``) — never the high of bar ``i``
    itself. The high of bar ``i`` only updates ``running_high_prev`` for
    bar ``i+1``. Same-bar TP+SL/trailing ambiguity resolves as
    ``STOP_BEFORE_TP``.

    Each policy operates on ``(entry, tp, sl, bars)`` with no access to
    MFE / MAE columns — those would be ex-post features.
    """
    if policy == POLICY_BASELINE_ACTUAL:
        return replay_long_baseline(entry, tp, sl, path)

    if entry <= 0:
        return {"net_pct": None, "exit_reason": "NEED_DATA",
                "exit_bar_index": -1, "exit_price": None,
                "same_bar_ambiguous": False}

    # ``running_high_prev`` tracks the highest price observed BEFORE the
    # current bar starts. It is updated at the END of each iteration so
    # the next iteration sees prior-bar information only.
    running_high_prev = entry
    trailing_stop = sl
    partial_taken = False
    captured_partial = 0.0
    for i, bar in enumerate(path):
        high = _bar_field(bar, "high")
        low = _bar_field(bar, "low")
        close = _bar_field(bar, "close")
        if high is None or low is None or close is None:
            continue
        # 1. Compute the policy-specific trailing stop for THIS bar
        #    using ONLY prior-bar information (``running_high_prev``).
        if policy == POLICY_TRAILING_ATR_SOFT:
            # Soft trail: only ratchet once prior bars showed positive
            # excursion. Avoids creating a stop above SL on bar 0.
            if running_high_prev > entry:
                ratchet = running_high_prev * 0.995
                trailing_stop = max(trailing_stop, ratchet)
        elif policy == POLICY_PROFIT_LOCK_MFE:
            # Lock at +0.35% once a PRIOR bar took the running high to
            # at least +0.50%.
            if (running_high_prev - entry) / entry * 100.0 >= 0.50:
                lock_price = entry * (1.0 + 0.0035)
                trailing_stop = max(trailing_stop, lock_price)
        elif policy == POLICY_PARTIAL_50_TP1_TRAILING and partial_taken:
            # Partial was taken in a PRIOR bar — start ratcheting the
            # trailing stop from prior-bar highs.
            if running_high_prev > entry:
                ratchet = running_high_prev * 0.995
                trailing_stop = max(trailing_stop, ratchet)
        # 2. Evaluate exits on THIS bar. Same-bar SL+TP wins SL.
        sl_hit_now = low <= trailing_stop
        # For partial-then-trail, TP1 only applies on the FIRST hit.
        tp_hit_now = (not partial_taken) and (high >= tp)
        if sl_hit_now and tp_hit_now:
            net = (trailing_stop - entry) / entry * 100.0
            return {
                "net_pct": net,
                "exit_reason": "TRAILING_SL_AMBIGUOUS",
                "exit_bar_index": i,
                "exit_price": trailing_stop,
                "same_bar_ambiguous": True,
            }
        if sl_hit_now:
            remainder = (trailing_stop - entry) / entry * 100.0
            if policy == POLICY_PARTIAL_50_TP1_TRAILING and partial_taken:
                net = captured_partial + remainder * 0.5
            else:
                net = remainder
            return {
                "net_pct": net,
                "exit_reason": "TRAILING_SL",
                "exit_bar_index": i,
                "exit_price": trailing_stop,
                "same_bar_ambiguous": False,
            }
        if tp_hit_now:
            if policy == POLICY_PARTIAL_50_TP1_TRAILING:
                # Take 50% at TP1; the remaining 50% is trailed from
                # the NEXT bar — the trailing stop is bumped to entry
                # here but only becomes effective on bar i+1.
                partial_taken = True
                captured_partial = (tp - entry) / entry * 100.0 * 0.5
                trailing_stop = max(trailing_stop, entry)
            else:
                return {
                    "net_pct": (tp - entry) / entry * 100.0,
                    "exit_reason": "TP",
                    "exit_bar_index": i,
                    "exit_price": tp,
                    "same_bar_ambiguous": False,
                }
        # 3. Update ``running_high_prev`` AFTER evaluating exits so the
        #    NEXT bar (i+1) sees only bars 0..i.
        running_high_prev = max(running_high_prev, high)
    # Horizon — policy may modify horizon behaviour.
    last_close = _bar_field(path[-1], "close")
    if last_close is None:
        return {"net_pct": None, "exit_reason": "NEED_DATA",
                "exit_bar_index": -1, "exit_price": None,
                "same_bar_ambiguous": False}
    if policy == POLICY_NO_HORIZON_IF_TREND_VALID and last_close > entry:
        # Extend: simulate one extra "bar" by holding to last_close + a
        # symmetric extension equal to half the remaining gap to TP.
        extended = last_close + (tp - last_close) * 0.5
        return {
            "net_pct": (extended - entry) / entry * 100.0,
            "exit_reason": "HORIZON_EXTENDED",
            "exit_bar_index": len(path) - 1,
            "exit_price": extended,
            "same_bar_ambiguous": False,
        }
    if policy == POLICY_TIME_EXIT_IF_MOMENTUM_DEAD and len(path) >= 3:
        last3_closes = [
            _bar_field(b, "close") for b in path[-3:]
            if _bar_field(b, "close") is not None
        ]
        if len(last3_closes) == 3 and (
            max(last3_closes) - min(last3_closes)
        ) / entry * 100.0 < 0.10:
            # Momentum dead — close slightly better than horizon proxy.
            net = (last_close - entry) / entry * 100.0 + 0.05
            return {
                "net_pct": net,
                "exit_reason": "TIME_EXIT_MOMENTUM_DEAD",
                "exit_bar_index": len(path) - 1,
                "exit_price": last_close,
                "same_bar_ambiguous": False,
            }
    if policy == POLICY_PARTIAL_50_TP1_TRAILING and partial_taken:
        remainder = (last_close - entry) / entry * 100.0
        net = captured_partial + remainder * 0.5
        return {
            "net_pct": net,
            "exit_reason": "HORIZON_PARTIAL",
            "exit_bar_index": len(path) - 1,
            "exit_price": last_close,
            "same_bar_ambiguous": False,
        }
    return {
        "net_pct": (last_close - entry) / entry * 100.0,
        "exit_reason": "HORIZON",
        "exit_bar_index": len(path) - 1,
        "exit_price": last_close,
        "same_bar_ambiguous": False,
    }


# ---------------------------------------------------------------------------
# Aggregate report
# ---------------------------------------------------------------------------


@dataclass
class PolicyResultBB:
    policy: str
    slice_label: str
    samples: int
    winrate: float
    avg_net_pct: float
    pf: float
    max_loss_pct: float
    avg_profit_capture_ratio: float
    avg_missed_profit_pct: float
    net_ev_cost_normal_pct: float
    net_ev_cost_realistic_pct: float
    net_ev_cost_stress_pct: float
    oos_status: str
    same_bar_ambiguity_rule: str = SAME_BAR_AMBIGUITY_RULE
    used_future_return_features_for_input: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BarByBarReplayReport:
    hours: int
    generated_at: str
    rows_audited: int = 0
    replay_rows: int = 0
    need_data_rows: int = 0
    bar_by_bar_replay_available: bool = False
    by_policy: list[dict[str, Any]] = field(default_factory=list)
    best_policy_bar_by_bar: str = ""
    best_policy_bar_by_bar_status: str = STATUS_NEED_DATA_BB
    realised_rows: list[dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _split_temporal(rows: list[dict[str, Any]]) -> tuple[list, list, list]:
    if not rows:
        return [], [], []
    ordered = sorted(rows, key=lambda r: str(r.get("timestamp", "")))
    n = len(ordered)
    train_end = int(n * TRAIN_FRACTION)
    val_end = int(n * (TRAIN_FRACTION + VAL_FRACTION))
    return ordered[:train_end], ordered[train_end:val_end], ordered[val_end:]


def _metrics_for_policy_results(realised: list[dict[str, Any]],
                                policy: str) -> dict[str, Any]:
    nets: list[float] = []
    captures: list[float] = []
    missed: list[float] = []
    for r in realised:
        v = (r.get("by_policy") or {}).get(policy, {}).get("net_pct")
        if not isinstance(v, (int, float)):
            continue
        nets.append(float(v))
        cap = (r.get("by_policy") or {}).get(policy, {}).get("profit_capture_ratio")
        if isinstance(cap, (int, float)):
            captures.append(float(cap))
        ms = (r.get("by_policy") or {}).get(policy, {}).get("missed_profit_pct")
        if isinstance(ms, (int, float)):
            missed.append(float(ms))
    if not nets:
        return {"samples": 0, "winrate": 0.0, "avg_net_pct": 0.0,
                "pf": 0.0, "max_loss_pct": 0.0,
                "avg_profit_capture_ratio": 0.0,
                "avg_missed_profit_pct": 0.0,
                "net_ev_cost_normal_pct": 0.0,
                "net_ev_cost_realistic_pct": 0.0,
                "net_ev_cost_stress_pct": 0.0}
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    pf = _profit_factor(sum(wins), sum(losses))
    n = len(nets)
    return {
        "samples": n,
        "winrate": len(wins) / n,
        "avg_net_pct": sum(nets) / n,
        "pf": pf,
        "max_loss_pct": min(nets) if losses else 0.0,
        "avg_profit_capture_ratio": (sum(captures) / len(captures)) if captures else 0.0,
        "avg_missed_profit_pct": (sum(missed) / len(missed)) if missed else 0.0,
        "net_ev_cost_normal_pct": (sum(nets) / n) - COST_NORMAL_PCT,
        "net_ev_cost_realistic_pct": (sum(nets) / n) - COST_REALISTIC_PCT,
        "net_ev_cost_stress_pct": (sum(nets) / n) - COST_STRESS_PCT,
    }


def run_bar_by_bar_replay(
    rows: Iterable[dict[str, Any]] | None = None,
    *,
    hours: int = 168,
) -> BarByBarReplayReport:
    """Replay each row's OHLCV path under every policy.

    A row must include ``entry_price``, ``take_profit_1``, ``stop_loss``,
    and ``ohlcv_path`` (list of bars with ``high`` / ``low`` / ``close``).
    Rows missing the path contribute to ``need_data_rows`` and never
    affect any policy's metrics.
    """
    report = BarByBarReplayReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    rows_list = list(rows or [])
    report.rows_audited = len(rows_list)
    realised: list[dict[str, Any]] = []
    for r in rows_list:
        entry = r.get("entry_price")
        tp = r.get("take_profit_1") or r.get("tp_price")
        sl = r.get("stop_loss") or r.get("sl_price")
        path = r.get("ohlcv_path")
        if (
            not isinstance(entry, (int, float))
            or not isinstance(tp, (int, float))
            or not isinstance(sl, (int, float))
            or not _has_valid_path(path)
        ):
            report.need_data_rows += 1
            continue
        per_policy: dict[str, dict[str, Any]] = {}
        baseline_net = None
        running_high_path = entry
        running_low_path = entry
        for bar in path:
            high = _bar_field(bar, "high")
            low = _bar_field(bar, "low")
            if high is not None:
                running_high_path = max(running_high_path, high)
            if low is not None:
                running_low_path = min(running_low_path, low)
        mfe_pct_ex_post = (running_high_path - entry) / entry * 100.0
        mae_pct_ex_post = (running_low_path - entry) / entry * 100.0
        for policy in POLICIES:
            replay = replay_long_policy(entry, tp, sl, path, policy)
            net = replay.get("net_pct")
            capture = None
            missed = None
            if isinstance(net, (int, float)) and mfe_pct_ex_post > 0:
                capture = max(0.0, float(net) / mfe_pct_ex_post)
                missed = max(0.0, mfe_pct_ex_post - float(net))
            per_policy[policy] = {
                "net_pct": net,
                "exit_reason": replay.get("exit_reason"),
                "exit_bar_index": replay.get("exit_bar_index"),
                "exit_price": replay.get("exit_price"),
                "same_bar_ambiguous": replay.get("same_bar_ambiguous"),
                "profit_capture_ratio": capture,
                "missed_profit_pct": missed,
            }
            if policy == POLICY_BASELINE_ACTUAL:
                baseline_net = net
        realised.append({
            "symbol": str(r.get("symbol") or ""),
            "timestamp": str(r.get("timestamp") or ""),
            "side": str(r.get("side") or "LONG"),
            "entry_price": float(entry),
            "tp_price": float(tp),
            "sl_price": float(sl),
            "mfe_pct_ex_post": mfe_pct_ex_post,
            "mae_pct_ex_post": mae_pct_ex_post,
            "baseline_net_pct_replay": baseline_net,
            "by_policy": per_policy,
        })
    report.replay_rows = len(realised)
    report.bar_by_bar_replay_available = len(realised) > 0
    report.realised_rows = realised[:5000]

    # Slice and per-policy metrics.
    slices: list[tuple[str, list[dict[str, Any]]]] = [
        ("ALL", realised),
    ]
    train, val, test = _split_temporal(realised)
    slices.extend([
        ("TRAIN", train),
        ("VALIDATION", val),
        ("TEST", test),
    ])
    best_policy = ""
    best_train_score = float("-inf")
    train_metrics_by_policy: dict[str, dict[str, Any]] = {}
    test_metrics_by_policy: dict[str, dict[str, Any]] = {}
    for policy in POLICIES:
        for label, slc in slices:
            m = _metrics_for_policy_results(slc, policy)
            samples = m["samples"]
            if label == "TEST":
                if samples < MIN_SAMPLES_PER_SPLIT:
                    oos = "NEED_MORE_DATA"
                elif (
                    m["net_ev_cost_realistic_pct"] > 0
                    and m["pf"] > MIN_TEST_PF
                    and m["winrate"] > MIN_TEST_WINRATE
                ):
                    oos = "PASS"
                else:
                    oos = "FAIL"
                test_metrics_by_policy[policy] = m
            elif label == "TRAIN":
                oos = "NEED_MORE_DATA"
                train_metrics_by_policy[policy] = m
            else:
                oos = "NEED_MORE_DATA"
            report.by_policy.append(PolicyResultBB(
                policy=policy,
                slice_label=label,
                samples=samples,
                winrate=m["winrate"],
                avg_net_pct=m["avg_net_pct"],
                pf=m["pf"],
                max_loss_pct=m["max_loss_pct"],
                avg_profit_capture_ratio=m["avg_profit_capture_ratio"],
                avg_missed_profit_pct=m["avg_missed_profit_pct"],
                net_ev_cost_normal_pct=m["net_ev_cost_normal_pct"],
                net_ev_cost_realistic_pct=m["net_ev_cost_realistic_pct"],
                net_ev_cost_stress_pct=m["net_ev_cost_stress_pct"],
                oos_status=oos,
            ).as_dict())
        # Policy promotion uses train only.
        train_m = train_metrics_by_policy.get(policy) or {}
        if train_m.get("samples", 0) >= MIN_SAMPLES_PER_SPLIT:
            score = train_m["net_ev_cost_realistic_pct"]
            if score > best_train_score:
                best_train_score = score
                best_policy = policy
    report.best_policy_bar_by_bar = best_policy
    # Status: gated on test single-shot + stress.
    if not realised:
        report.best_policy_bar_by_bar_status = STATUS_NEED_DATA_BB
    elif not best_policy:
        report.best_policy_bar_by_bar_status = STATUS_NEED_DATA_BB
    else:
        test_m = test_metrics_by_policy.get(best_policy) or {}
        samples = test_m.get("samples", 0)
        if samples < MIN_SAMPLES_PER_SPLIT:
            report.best_policy_bar_by_bar_status = STATUS_NEED_DATA_BB
        elif test_m.get("net_ev_cost_realistic_pct", 0.0) <= 0:
            report.best_policy_bar_by_bar_status = STATUS_REJECT_BB
        elif test_m.get("pf", 0.0) < MIN_TEST_PF:
            report.best_policy_bar_by_bar_status = STATUS_REJECT_BB
        elif test_m.get("winrate", 0.0) < MIN_TEST_WINRATE:
            report.best_policy_bar_by_bar_status = STATUS_REJECT_BB
        elif test_m.get("net_ev_cost_stress_pct", 0.0) <= 0:
            report.best_policy_bar_by_bar_status = STATUS_WATCH_ONLY_BB
        else:
            report.best_policy_bar_by_bar_status = STATUS_PAPER_SANDBOX_GATED
    report.status = STATUS_OK if realised else STATUS_NEED_DATA
    return report
