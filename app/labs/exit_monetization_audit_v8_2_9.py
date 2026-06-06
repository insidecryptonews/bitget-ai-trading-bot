"""V8.2.9 — Exit Monetization Audit (research-only).

VPS observation: LONG/SHORT entries look reasonable on BTC, but several
operations close on ``HORIZON_CLOSE`` or capture little benefit despite
positive MFE. This audit answers: is the bot entering better than it
monetises? It compares the realised outcome against several
research-only exit policies on the SAME observed bar path, all without
touching production.

Hard contract:

- Pure analytic; never opens orders, never modifies exit rules.
- MFE / MAE / barrier-hit / horizon_close are inspected to evaluate
  what the bar path would have delivered under each policy. They are
  NOT used as feature inputs for the entry detector. They are used
  here strictly to score outcomes retrospectively (ex-post).
- Same-bar ambiguity is resolved conservatively
  (``STOP_BEFORE_TP``).
- Policy selection is performed on the train slice only. The test
  slice is evaluated once and never used for selection.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK


# Policies enumerated by the audit. Each policy is a pure function that
# takes the observed bar path (entry, TP, SL, MFE, MAE, first barrier,
# horizon close flag, bars) and returns a research-only realised net
# return. NONE of the policies write to production state.
POLICY_BASELINE_ACTUAL = "baseline_actual"
POLICY_HOLD_TO_HORIZON_LONGER = "hold_to_horizon_longer"
POLICY_PARTIAL_50_TP1_TRAILING = "partial_50_at_tp1_plus_trailing"
POLICY_TRAILING_ATR_SOFT = "trailing_atr_soft"
POLICY_TRAILING_STRUCTURE_SWING = "trailing_structure_swing"
POLICY_PROFIT_LOCK_MFE_THRESHOLD = "profit_lock_after_mfe_threshold"
POLICY_NO_HORIZON_IF_TREND_VALID = "no_horizon_close_if_trend_still_valid"
POLICY_TIME_EXIT_IF_MOMENTUM_DEAD = "time_exit_only_if_momentum_dead"

POLICIES: tuple[str, ...] = (
    POLICY_BASELINE_ACTUAL,
    POLICY_HOLD_TO_HORIZON_LONGER,
    POLICY_PARTIAL_50_TP1_TRAILING,
    POLICY_TRAILING_ATR_SOFT,
    POLICY_TRAILING_STRUCTURE_SWING,
    POLICY_PROFIT_LOCK_MFE_THRESHOLD,
    POLICY_NO_HORIZON_IF_TREND_VALID,
    POLICY_TIME_EXIT_IF_MOMENTUM_DEAD,
)

COST_NORMAL_PCT = 0.18
COST_REALISTIC_PCT = 0.25
COST_STRESS_PCT = 0.35

# Same-bar ambiguity rule: when MFE and SL touch in the same candle,
# the conservative rule is SL fires first (STOP_BEFORE_TP).
SAME_BAR_AMBIGUITY_RULE = "STOP_BEFORE_TP"

TRAIN_FRACTION = 0.60
VAL_FRACTION = 0.20

# Promotion gates for exit policies (research-only).
MIN_TEST_PF = 1.15
MIN_TEST_WINRATE = 0.55
MIN_SAMPLES_PER_SPLIT = 15
HORIZON_PROBLEM_THRESHOLD = 0.30  # >30% of HORIZON_CLOSE rows missed >0.5% MFE
MISSED_PROFIT_THRESHOLD_PCT = 0.50

# V8.2.9.1 — PF sentinel for the all-wins case (avoids float('inf')).
PF_SENTINEL_NO_LOSSES = 999.0


def _profit_factor(gross_profit: float, gross_loss: float) -> float:
    """Canonical PF — see ``rebound_long_strict_oos_v8_2_9`` for the rule."""
    loss_abs = abs(float(gross_loss))
    if loss_abs == 0.0:
        return PF_SENTINEL_NO_LOSSES if float(gross_profit) > 0 else 0.0
    return float(gross_profit) / loss_abs


@dataclass
class ExitAuditRow:
    side: str
    entry_time: str
    entry_price: float | None
    exit_time: str
    exit_price: float | None
    outcome: str
    net_pct: float | None
    mfe_pct: float | None
    mae_pct: float | None
    bars: int | None
    tp_pct: float | None
    sl_pct: float | None
    closed_by_horizon: bool
    profit_capture_ratio: float | None
    missed_profit_pct: float | None
    is_missed_profit_candidate: bool
    same_bar_ambiguous: bool
    same_bar_resolution: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PolicyResult:
    policy: str
    slice_label: str  # ALL / LONG / SHORT / REBOUND_LONG / TREND_SHORT /
                     # TRAIN / VALIDATION / TEST
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
    used_future_return_features_for_input: bool = False
    same_bar_ambiguity_rule: str = SAME_BAR_AMBIGUITY_RULE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExitMonetizationReport:
    hours: int
    generated_at: str
    rows_audited: int = 0
    rows_with_outcome: int = 0
    horizon_close_count: int = 0
    horizon_close_with_high_mfe: int = 0
    horizon_close_problem_detected: bool = False
    avg_profit_capture_ratio: float = 0.0
    avg_missed_profit_pct: float = 0.0
    rows: list[dict[str, Any]] = field(default_factory=list)
    policies: list[dict[str, Any]] = field(default_factory=list)
    best_policy: str = ""
    best_policy_test_status: str = "NEED_MORE_DATA"
    differences_long_vs_short: dict[str, Any] = field(default_factory=dict)
    differences_rebound_vs_trend: dict[str, Any] = field(default_factory=dict)
    answers: dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---- Helpers ---------------------------------------------------------------

def _closed_by_horizon(row: dict[str, Any]) -> bool:
    """True if the row was closed by horizon (no TP / SL hit)."""
    if row.get("closed_by_horizon") is True:
        return True
    if str(row.get("exit_reason", "")).upper() in {"HORIZON_CLOSE", "TIMEOUT"}:
        return True
    barrier = str(row.get("first_barrier_hit", "")).upper()
    if barrier == "":
        return False
    return barrier in {"HORIZON", "HORIZON_CLOSE", "TIMEOUT"}


def _same_bar_ambiguous(row: dict[str, Any]) -> bool:
    if row.get("same_bar_ambiguous") is True:
        return True
    return (
        bool(row.get("tp_before_sl"))
        and bool(row.get("sl_before_tp"))
    )


def _side_signed(side: str, value: float | None) -> float | None:
    """Return ``value`` signed by side direction. LONG positive when value
    is positive in long terms. SHORT mirrors."""
    if value is None:
        return None
    if str(side).upper() == "SHORT":
        return -float(value)
    return float(value)


def _net_pct_observed(row: dict[str, Any]) -> float | None:
    v = row.get("net_pct")
    if isinstance(v, (int, float)):
        return float(v)
    v = row.get("baseline_net_pnl_est")
    return float(v) if isinstance(v, (int, float)) else None


def build_exit_audit_row(row: dict[str, Any]) -> ExitAuditRow:
    """Build an ``ExitAuditRow`` from one dataset / log row.

    MFE / MAE are read here purely to score the EX-POST outcome of the
    bar path. They are NEVER fed back into the entry detector.
    """
    side = str(row.get("side", "")).upper()
    mfe = row.get("mfe_pct")
    mae = row.get("mae_pct")
    net = _net_pct_observed(row)
    bars = row.get("bars")
    horizon = _closed_by_horizon(row)
    same_bar = _same_bar_ambiguous(row)
    mfe_f = float(mfe) if isinstance(mfe, (int, float)) else None
    mae_f = float(mae) if isinstance(mae, (int, float)) else None
    capture = None
    missed = None
    if isinstance(net, float) and mfe_f is not None and mfe_f > 0:
        capture = max(0.0, net / mfe_f)
        missed = max(0.0, mfe_f - net)
    is_missed = bool(
        horizon
        and mfe_f is not None
        and mfe_f >= MISSED_PROFIT_THRESHOLD_PCT
        and (net is None or net < mfe_f * 0.5)
    )
    return ExitAuditRow(
        side=side,
        entry_time=str(row.get("entry_time") or row.get("timestamp") or ""),
        entry_price=float(row["entry_price"])
        if isinstance(row.get("entry_price"), (int, float)) else None,
        exit_time=str(row.get("exit_time") or ""),
        exit_price=float(row["exit_price"])
        if isinstance(row.get("exit_price"), (int, float)) else None,
        outcome=str(row.get("first_barrier_hit") or row.get("baseline_result") or ""),
        net_pct=net,
        mfe_pct=mfe_f,
        mae_pct=mae_f,
        bars=int(bars) if isinstance(bars, (int, float)) else None,
        tp_pct=float(row.get("tp_pct"))
        if isinstance(row.get("tp_pct"), (int, float)) else None,
        sl_pct=float(row.get("sl_pct"))
        if isinstance(row.get("sl_pct"), (int, float)) else None,
        closed_by_horizon=horizon,
        profit_capture_ratio=capture,
        missed_profit_pct=missed,
        is_missed_profit_candidate=is_missed,
        same_bar_ambiguous=same_bar,
        same_bar_resolution=SAME_BAR_AMBIGUITY_RULE if same_bar else "",
    )


# Policy realised-net-pct estimators. Each takes an ExitAuditRow and
# returns an estimated net pct under that policy. They use MFE / MAE
# strictly to score the bar path retrospectively.
def _policy_baseline(r: ExitAuditRow) -> float | None:
    return r.net_pct


def _policy_hold_to_horizon_longer(r: ExitAuditRow) -> float | None:
    """Hold longer assumes the bar path keeps the existing winners and
    rescues a small fraction of MFE on stop-out winners."""
    if r.net_pct is None:
        return None
    if r.closed_by_horizon and r.mfe_pct is not None and r.mfe_pct > r.net_pct:
        # Captures an extra 25% of the unrealised MFE.
        return r.net_pct + (r.mfe_pct - r.net_pct) * 0.25
    return r.net_pct


def _policy_partial_50_tp1_trailing(r: ExitAuditRow) -> float | None:
    """Take 50% at TP1, trail the rest. Approximated as the average of
    the realised outcome and 70% of MFE."""
    if r.net_pct is None:
        return None
    if r.same_bar_ambiguous:
        # Conservative: same as baseline.
        return r.net_pct
    if r.mfe_pct is not None and r.mfe_pct > 0:
        trailing = max(r.net_pct, r.mfe_pct * 0.7)
        return (r.net_pct + trailing) / 2.0
    return r.net_pct


def _policy_trailing_atr_soft(r: ExitAuditRow) -> float | None:
    if r.net_pct is None:
        return None
    if r.same_bar_ambiguous:
        return r.net_pct
    if r.mfe_pct is not None and r.mfe_pct > 0:
        return max(r.net_pct, r.mfe_pct * 0.60)
    return r.net_pct


def _policy_trailing_structure_swing(r: ExitAuditRow) -> float | None:
    if r.net_pct is None:
        return None
    if r.same_bar_ambiguous:
        return r.net_pct
    if r.mfe_pct is not None and r.mfe_pct > 0:
        return max(r.net_pct, r.mfe_pct * 0.55)
    return r.net_pct


def _policy_profit_lock_after_mfe(r: ExitAuditRow) -> float | None:
    if r.net_pct is None:
        return None
    if r.same_bar_ambiguous:
        return r.net_pct
    if r.mfe_pct is not None and r.mfe_pct >= 0.50:
        return max(r.net_pct, 0.35)
    return r.net_pct


def _policy_no_horizon_if_trend_valid(r: ExitAuditRow) -> float | None:
    if r.net_pct is None:
        return None
    # Without an explicit trend-still-valid signal, we approximate: if
    # horizon-closed AND MFE > 0.6 we assume trend was valid and add
    # +0.15.
    if r.closed_by_horizon and r.mfe_pct is not None and r.mfe_pct >= 0.60:
        return r.net_pct + 0.15
    return r.net_pct


def _policy_time_exit_if_momentum_dead(r: ExitAuditRow) -> float | None:
    if r.net_pct is None:
        return None
    if r.closed_by_horizon and r.mfe_pct is not None and r.mfe_pct < 0.15:
        # Closing early when momentum is dead saves slippage.
        return r.net_pct + 0.05
    return r.net_pct


POLICY_FUNCS = {
    POLICY_BASELINE_ACTUAL: _policy_baseline,
    POLICY_HOLD_TO_HORIZON_LONGER: _policy_hold_to_horizon_longer,
    POLICY_PARTIAL_50_TP1_TRAILING: _policy_partial_50_tp1_trailing,
    POLICY_TRAILING_ATR_SOFT: _policy_trailing_atr_soft,
    POLICY_TRAILING_STRUCTURE_SWING: _policy_trailing_structure_swing,
    POLICY_PROFIT_LOCK_MFE_THRESHOLD: _policy_profit_lock_after_mfe,
    POLICY_NO_HORIZON_IF_TREND_VALID: _policy_no_horizon_if_trend_valid,
    POLICY_TIME_EXIT_IF_MOMENTUM_DEAD: _policy_time_exit_if_momentum_dead,
}


def _metrics_for_policy(
    audit_rows: list[ExitAuditRow], policy: str,
) -> dict[str, Any]:
    fn = POLICY_FUNCS[policy]
    nets: list[float] = []
    captures: list[float] = []
    missed: list[float] = []
    for ar in audit_rows:
        v = fn(ar)
        if v is None:
            continue
        nets.append(float(v))
        if ar.profit_capture_ratio is not None:
            captures.append(ar.profit_capture_ratio)
        if ar.missed_profit_pct is not None:
            missed.append(ar.missed_profit_pct)
    if not nets:
        return {
            "samples": 0, "winrate": 0.0, "avg_net_pct": 0.0,
            "pf": 0.0, "max_loss_pct": 0.0,
            "avg_profit_capture_ratio": 0.0,
            "avg_missed_profit_pct": 0.0,
            "net_ev_cost_normal_pct": 0.0,
            "net_ev_cost_realistic_pct": 0.0,
            "net_ev_cost_stress_pct": 0.0,
        }
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


def _split_temporal(rows: list[ExitAuditRow]) -> tuple[list, list, list]:
    if not rows:
        return [], [], []
    ordered = sorted(rows, key=lambda r: r.entry_time)
    n = len(ordered)
    train_end = int(n * TRAIN_FRACTION)
    val_end = int(n * (TRAIN_FRACTION + VAL_FRACTION))
    return ordered[:train_end], ordered[train_end:val_end], ordered[val_end:]


def _is_rebound_long(row: dict[str, Any], audit: ExitAuditRow) -> bool:
    if audit.side != "LONG":
        return False
    if row.get("is_rebound_candidate") is True:
        return True
    return str(row.get("rebound_label") or "").lower() in {"good", "bad"}


def _is_trend_short(row: dict[str, Any], audit: ExitAuditRow) -> bool:
    if audit.side != "SHORT":
        return False
    regime = str(row.get("regime", "")).upper()
    return regime in {"TREND_DOWN", "RISK_OFF"}


def run_exit_monetization_audit(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    rows: Iterable[dict[str, Any]] | None = None,
) -> ExitMonetizationReport:
    """Run the audit. ``rows`` may be a pre-fetched list of completed
    observations (with realised MFE/MAE and a barrier/horizon outcome).
    """
    report = ExitMonetizationReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    if rows is None:
        from .counterfactual_training_dataset import build_dataset
        dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)
    if not dataset:
        return report
    raw_rows: list[tuple[dict[str, Any], ExitAuditRow]] = []
    horizon_count = 0
    horizon_high_mfe = 0
    captures_all: list[float] = []
    missed_all: list[float] = []
    for r in dataset:
        ar = build_exit_audit_row(r)
        if ar.net_pct is None:
            continue
        raw_rows.append((r, ar))
        if ar.closed_by_horizon:
            horizon_count += 1
            if ar.mfe_pct is not None and ar.mfe_pct >= MISSED_PROFIT_THRESHOLD_PCT:
                horizon_high_mfe += 1
        if ar.profit_capture_ratio is not None:
            captures_all.append(ar.profit_capture_ratio)
        if ar.missed_profit_pct is not None:
            missed_all.append(ar.missed_profit_pct)
    report.rows_audited = len(dataset)
    report.rows_with_outcome = len(raw_rows)
    report.horizon_close_count = horizon_count
    report.horizon_close_with_high_mfe = horizon_high_mfe
    if horizon_count > 0:
        report.horizon_close_problem_detected = (
            (horizon_high_mfe / horizon_count) >= HORIZON_PROBLEM_THRESHOLD
        )
    report.avg_profit_capture_ratio = (
        sum(captures_all) / len(captures_all) if captures_all else 0.0
    )
    report.avg_missed_profit_pct = (
        sum(missed_all) / len(missed_all) if missed_all else 0.0
    )
    # Cap CSV row payload at 5000 for safety.
    report.rows = [ar.as_dict() for _, ar in raw_rows[:5000]]
    audit_rows_all = [ar for _, ar in raw_rows]
    audit_rows_long = [ar for _, ar in raw_rows if ar.side == "LONG"]
    audit_rows_short = [ar for _, ar in raw_rows if ar.side == "SHORT"]
    audit_rows_rebound = [ar for r, ar in raw_rows if _is_rebound_long(r, ar)]
    audit_rows_trend_short = [ar for r, ar in raw_rows if _is_trend_short(r, ar)]
    # Policy evaluation across each slice.
    slices: list[tuple[str, list[ExitAuditRow]]] = [
        ("ALL", audit_rows_all),
        ("LONG", audit_rows_long),
        ("SHORT", audit_rows_short),
        ("REBOUND_LONG", audit_rows_rebound),
        ("TREND_SHORT", audit_rows_trend_short),
    ]
    # Train/val/test splits over ALL — used for policy promotion.
    train, val, test = _split_temporal(audit_rows_all)
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
            m = _metrics_for_policy(slc, policy)
            samples = m["samples"]
            oos = "NEED_MORE_DATA"
            if label == "TEST":
                if samples >= MIN_SAMPLES_PER_SPLIT:
                    if (
                        m["net_ev_cost_realistic_pct"] > 0
                        and m["pf"] > MIN_TEST_PF
                        and m["winrate"] > MIN_TEST_WINRATE
                    ):
                        oos = "PASS"
                    else:
                        oos = "FAIL"
                test_metrics_by_policy[policy] = m
            if label == "TRAIN":
                train_metrics_by_policy[policy] = m
            report.policies.append(PolicyResult(
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
        # Policy promotion uses TRAIN ONLY — never test.
        train_m = train_metrics_by_policy.get(policy) or {}
        if train_m.get("samples", 0) >= MIN_SAMPLES_PER_SPLIT:
            score = train_m["net_ev_cost_realistic_pct"]
            if score > best_train_score:
                best_train_score = score
                best_policy = policy
    report.best_policy = best_policy or POLICY_BASELINE_ACTUAL
    # Look up the test-side status of the train-best policy (this is the
    # "single-shot" out-of-sample evaluation).
    if best_policy:
        test_m = test_metrics_by_policy.get(best_policy) or {}
        if test_m.get("samples", 0) >= MIN_SAMPLES_PER_SPLIT:
            if (
                test_m["net_ev_cost_realistic_pct"] > 0
                and test_m["pf"] > MIN_TEST_PF
                and test_m["winrate"] > MIN_TEST_WINRATE
            ):
                report.best_policy_test_status = "PASS"
            else:
                report.best_policy_test_status = "FAIL"
        else:
            report.best_policy_test_status = "NEED_MORE_DATA"
    # Diff LONG vs SHORT — baseline policy.
    long_m = _metrics_for_policy(audit_rows_long, POLICY_BASELINE_ACTUAL)
    short_m = _metrics_for_policy(audit_rows_short, POLICY_BASELINE_ACTUAL)
    report.differences_long_vs_short = {
        "long_avg_net_pct": long_m["avg_net_pct"],
        "short_avg_net_pct": short_m["avg_net_pct"],
        "long_winrate": long_m["winrate"],
        "short_winrate": short_m["winrate"],
        "long_samples": long_m["samples"],
        "short_samples": short_m["samples"],
    }
    rebound_m = _metrics_for_policy(audit_rows_rebound, POLICY_BASELINE_ACTUAL)
    trend_m = _metrics_for_policy(audit_rows_trend_short, POLICY_BASELINE_ACTUAL)
    report.differences_rebound_vs_trend = {
        "rebound_long_avg_net_pct": rebound_m["avg_net_pct"],
        "trend_short_avg_net_pct": trend_m["avg_net_pct"],
        "rebound_long_samples": rebound_m["samples"],
        "trend_short_samples": trend_m["samples"],
    }
    # Plain-language answers consumed by the summary.
    answers = {
        "entries_better_than_exits": (
            report.avg_profit_capture_ratio < 0.5
            and report.avg_missed_profit_pct >= 0.30
        ),
        "horizon_close_killing_profit": report.horizon_close_problem_detected,
        "tp_too_conservative": report.avg_missed_profit_pct >= 0.40,
        "trailing_improves_after_cost": False,
        "any_exit_paper_sandbox_candidate": False,
    }
    if best_policy and best_policy != POLICY_BASELINE_ACTUAL:
        train_best = train_metrics_by_policy.get(best_policy) or {}
        test_best = test_metrics_by_policy.get(best_policy) or {}
        if (
            train_best.get("net_ev_cost_realistic_pct", 0.0) > 0
            and report.best_policy_test_status == "PASS"
        ):
            answers["trailing_improves_after_cost"] = True
            if (test_best.get("net_ev_cost_stress_pct", 0.0) or 0.0) > 0:
                answers["any_exit_paper_sandbox_candidate"] = True
    report.answers = answers
    report.status = STATUS_OK
    return report
