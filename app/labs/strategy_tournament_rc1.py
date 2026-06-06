"""Strategy Tournament RC1 (research-only).

Compares candidate trading strategies on a common, deduplicated candidate
set and ranks them with strict anti-overfit / anti-lookahead guards.
Born from the V8.2.9.4 forensic finding that the rebound-LONG universe
has NEGATIVE expected value once the dominant ``regime_now=TREND_DOWN``
cohort (0% winrate) is included, and that the only positive-EV cohort
(``regime_now=RISK_OFF``) is a single ~13h window dominated by one
symbol whose own EV is negative.

Hard contract (research-only):

- Never opens orders, never mutates runtime, never activates anything.
- Entry/cohort predicates may use ONLY ex-ante features. A whitelist is
  enforced; using ``ret_*`` / ``mfe_*`` / ``mae_*`` / ``first_barrier_hit``
  / ``baseline_net_pnl_est`` / ``training_label`` as an ENTRY feature
  raises ``ValueError``.
- The outcome column is used ONLY to SCORE a cohort that was already
  selected by an ex-ante predicate — never to pick entries.
- Temporal 60/20/20 split; selection logic must not look at test.
- Three-level cost stress (0.18 / 0.25 / 0.35) applied as additional
  cost over the baseline-net outcome.
- Single-symbol domination, time-cluster concentration and
  sign-mismatch ratio gate promotion.
- Max status is ``PAPER_SANDBOX_CANDIDATE_RESEARCH_ONLY`` — research
  label only, never an operational permission.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK


# ---------------------------------------------------------------------------
# Statuses (max allowed = PAPER_SANDBOX_CANDIDATE_RESEARCH_ONLY).
# ---------------------------------------------------------------------------
STATUS_REJECT = "REJECT"
STATUS_NEED_MORE_DATA = "NEED_MORE_DATA"
STATUS_WATCH_ONLY = "WATCH_ONLY"
STATUS_SINGLE_SYMBOL_RESEARCH_ONLY = "SINGLE_SYMBOL_RESEARCH_ONLY"
STATUS_SHADOW_SANDBOX_CANDIDATE = "SHADOW_SANDBOX_CANDIDATE"
STATUS_PAPER_SANDBOX_RESEARCH_ONLY = "PAPER_SANDBOX_CANDIDATE_RESEARCH_ONLY"

VALID_STATUSES: tuple[str, ...] = (
    STATUS_REJECT, STATUS_NEED_MORE_DATA, STATUS_WATCH_ONLY,
    STATUS_SINGLE_SYMBOL_RESEARCH_ONLY, STATUS_SHADOW_SANDBOX_CANDIDATE,
    STATUS_PAPER_SANDBOX_RESEARCH_ONLY,
)


# ---------------------------------------------------------------------------
# Ex-ante feature whitelist. A strategy's declared entry features must be
# a subset of this set. Forbidden ex-post fields raise ValueError.
# ---------------------------------------------------------------------------
EX_ANTE_FEATURES_WHITELIST: frozenset[str] = frozenset({
    "symbol", "side", "regime_before", "regime_now", "regime",
    "strategy", "score_bucket", "score_bucket_diagnostic",
    "volatility_bucket", "candidate_selected", "risk_approved",
    "candidate_reason", "detection_mode",
    # Prefix-only structural flags are allowed as ex-ante inputs because
    # they are computed strictly from bars BEFORE the signal timestamp.
    "higher_lows_prefix", "trend_recovering_prefix",
    "bounce_confirmation_prefix", "drawdown_proxy_prefix",
})

FORBIDDEN_ENTRY_FEATURES: frozenset[str] = frozenset({
    "ret_15m_pct", "ret_30m_pct", "ret_1h_pct", "ret_4h_pct", "ret_24h_pct",
    "mfe_pct", "mae_pct", "mfe_pct_outcome", "mae_pct_outcome",
    "first_barrier_hit", "barrier_result_outcome",
    "tp_before_sl", "sl_before_tp",
    "baseline_result", "baseline_gross_pnl", "baseline_net_pnl_est",
    "net_pnl_est", "outcome_winner_loser",
    "trailing_net_pnl_est", "campaign_net_pnl_est",
    "training_label",
})


# Cost model. The candidate outcome (``net_pnl_est``) already nets the
# baseline (~0.18%) cost. Realistic / stress apply ADDITIONAL cost.
COST_BASELINE_PCT = 0.18
COST_REALISTIC_EXTRA_PCT = 0.07   # → 0.25 total
COST_STRESS_EXTRA_PCT = 0.17      # → 0.35 total

# Sample minima.
MIN_TRAIN_SAMPLES = 30
MIN_VAL_SAMPLES = 15
MIN_TEST_SAMPLES = 15

# Promotion gates.
MIN_TEST_WINRATE = 0.50
MIN_TEST_PF = 1.15
MAX_SINGLE_SYMBOL_SHARE = 0.50
MAX_TIME_CLUSTER_SHARE = 0.30
MAX_SIGN_BUG_RATIO = 0.05

TRAIN_FRACTION = 0.60
VAL_FRACTION = 0.20

PF_SENTINEL_NO_LOSSES = 999.0

DOWN_REGIMES = frozenset({"TREND_DOWN", "RISK_OFF", "HIGH_VOLATILITY"})


def _profit_factor(gross_profit: float, gross_loss: float) -> float:
    loss_abs = abs(float(gross_loss))
    if loss_abs == 0.0:
        return PF_SENTINEL_NO_LOSSES if float(gross_profit) > 0 else 0.0
    return float(gross_profit) / loss_abs


@dataclass
class StrategySpec:
    """A declared candidate strategy.

    ``entry_features`` lists the ex-ante fields the cohort predicate
    reads — validated against the whitelist. ``predicate`` selects the
    cohort using ONLY those ex-ante fields.
    """
    name: str
    side: str
    logic: str
    entry_features: tuple[str, ...]
    predicate: Callable[[dict[str, Any]], bool]
    outcome_field: str = "net_pnl_est"
    single_symbol_research_only: bool = False

    def validate(self) -> None:
        for f in self.entry_features:
            if f in FORBIDDEN_ENTRY_FEATURES:
                raise ValueError(
                    f"strategy {self.name!r}: forbidden ex-post entry "
                    f"feature {f!r}"
                )
            if f not in EX_ANTE_FEATURES_WHITELIST:
                raise ValueError(
                    f"strategy {self.name!r}: entry feature {f!r} not in "
                    "ex-ante whitelist"
                )


@dataclass
class StrategyResult:
    name: str
    side: str
    logic: str
    entry_features: list[str]
    samples: int
    train_samples: int
    validation_samples: int
    test_samples: int
    winrate: float
    test_winrate: float
    pf: float
    test_pf: float
    net_ev_pct: float
    test_net_ev_pct: float
    test_net_ev_realistic_pct: float
    test_net_ev_stress_pct: float
    single_symbol_share: float
    time_cluster_share: float
    sign_bug_ratio: float
    status: str
    reason: str
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TournamentReport:
    hours: int
    generated_at: str
    candidates_input: int = 0
    strategies_evaluated: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)
    best_strategy: str = ""
    best_status: str = STATUS_NEED_MORE_DATA
    by_status: dict[str, int] = field(default_factory=dict)
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _outcome(row: dict[str, Any], field_name: str) -> float | None:
    v = row.get(field_name)
    if isinstance(v, (int, float)):
        return float(v)
    # Tolerate string-encoded floats from CSV ingestion.
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _split_temporal(rows: list[dict[str, Any]]) -> tuple[list, list, list]:
    if not rows:
        return [], [], []
    ordered = sorted(rows, key=lambda r: str(r.get("timestamp", "")))
    n = len(ordered)
    train_end = int(n * TRAIN_FRACTION)
    val_end = int(n * (TRAIN_FRACTION + VAL_FRACTION))
    return ordered[:train_end], ordered[train_end:val_end], ordered[val_end:]


def _metrics(rows: list[dict[str, Any]], outcome_field: str) -> dict[str, Any]:
    nets = [
        n for n in (_outcome(r, outcome_field) for r in rows) if n is not None
    ]
    if not nets:
        return {
            "samples": 0, "winrate": 0.0, "net_ev_pct": 0.0, "pf": 0.0,
            "net_ev_realistic_pct": 0.0, "net_ev_stress_pct": 0.0,
        }
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    pf = _profit_factor(sum(wins), sum(losses))
    n = len(nets)
    ev = sum(nets) / n
    return {
        "samples": n,
        "winrate": len(wins) / n,
        "net_ev_pct": ev,
        "pf": pf,
        "net_ev_realistic_pct": ev - COST_REALISTIC_EXTRA_PCT,
        "net_ev_stress_pct": ev - COST_STRESS_EXTRA_PCT,
    }


def _single_symbol_share(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    counts: dict[str, int] = {}
    for r in rows:
        s = str(r.get("symbol", "UNKNOWN")).upper()
        counts[s] = counts.get(s, 0) + 1
    return max(counts.values()) / len(rows)


def _time_cluster_share(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    counts: dict[str, int] = {}
    for r in rows:
        ts = str(r.get("timestamp", ""))
        bucket = ts[:13] if len(ts) >= 13 else ts  # hour bucket
        counts[bucket] = counts.get(bucket, 0) + 1
    return max(counts.values()) / len(rows)


def _decide_status(
    train_m: dict[str, Any],
    val_m: dict[str, Any],
    test_m: dict[str, Any],
    single_symbol_share: float,
    time_cluster_share: float,
    sign_bug_ratio: float,
    spec: StrategySpec,
) -> tuple[str, str]:
    if train_m["samples"] < MIN_TRAIN_SAMPLES:
        return STATUS_NEED_MORE_DATA, (
            f"train_samples={train_m['samples']}_below_{MIN_TRAIN_SAMPLES}"
        )
    if val_m["samples"] < MIN_VAL_SAMPLES:
        return STATUS_NEED_MORE_DATA, (
            f"validation_samples={val_m['samples']}_below_{MIN_VAL_SAMPLES}"
        )
    if test_m["samples"] < MIN_TEST_SAMPLES:
        return STATUS_NEED_MORE_DATA, (
            f"test_samples={test_m['samples']}_below_{MIN_TEST_SAMPLES}"
        )
    # Hard reject — train must be net-positive at realistic cost.
    if train_m["net_ev_realistic_pct"] <= 0:
        return STATUS_REJECT, "train_net_ev_not_positive_after_realistic_cost"
    if val_m["net_ev_realistic_pct"] <= 0:
        return STATUS_REJECT, (
            "validation_net_ev_not_positive_after_realistic_cost"
        )
    if test_m["net_ev_realistic_pct"] <= 0:
        return STATUS_REJECT, "test_net_ev_not_positive_after_realistic_cost"
    if test_m["winrate"] < MIN_TEST_WINRATE:
        return STATUS_REJECT, (
            f"test_winrate={test_m['winrate']:.2f}_below_{MIN_TEST_WINRATE}"
        )
    if test_m["pf"] < MIN_TEST_PF:
        return STATUS_REJECT, f"test_pf={test_m['pf']:.2f}_below_{MIN_TEST_PF}"
    # Outcome labels suspect → cannot trust the EV at all.
    if sign_bug_ratio > MAX_SIGN_BUG_RATIO:
        return STATUS_REJECT, (
            f"sign_bug_ratio={sign_bug_ratio:.3f}_above_{MAX_SIGN_BUG_RATIO}"
        )
    # Concentration checks.
    if time_cluster_share > MAX_TIME_CLUSTER_SHARE:
        return STATUS_WATCH_ONLY, (
            f"time_cluster_share={time_cluster_share:.2f}_above_"
            f"{MAX_TIME_CLUSTER_SHARE}"
        )
    if single_symbol_share > MAX_SINGLE_SYMBOL_SHARE:
        if spec.single_symbol_research_only:
            return STATUS_SINGLE_SYMBOL_RESEARCH_ONLY, (
                f"single_symbol_share={single_symbol_share:.2f}_marked_"
                "research_only"
            )
        return STATUS_WATCH_ONLY, (
            f"single_symbol_share={single_symbol_share:.2f}_above_"
            f"{MAX_SINGLE_SYMBOL_SHARE}"
        )
    # Survives realistic cost + concentration ok. Stress cost decides
    # whether it reaches the shadow / paper research labels.
    if test_m["net_ev_stress_pct"] <= 0:
        return STATUS_SHADOW_SANDBOX_CANDIDATE, (
            "survives_realistic_cost_but_not_stress_research_shadow_only"
        )
    return STATUS_PAPER_SANDBOX_RESEARCH_ONLY, (
        "survives_all_gates_research_label_only_no_live"
    )


# Status ranking for "best strategy" selection (higher = better).
_STATUS_RANK = {
    STATUS_REJECT: 0,
    STATUS_NEED_MORE_DATA: 1,
    STATUS_WATCH_ONLY: 2,
    STATUS_SINGLE_SYMBOL_RESEARCH_ONLY: 3,
    STATUS_SHADOW_SANDBOX_CANDIDATE: 4,
    STATUS_PAPER_SANDBOX_RESEARCH_ONLY: 5,
}


def run_tournament(
    candidates: Iterable[dict[str, Any]] | None,
    strategies: Iterable[StrategySpec],
    *,
    hours: int = 168,
    sign_bug_ratio_by_strategy: dict[str, float] | None = None,
) -> TournamentReport:
    """Run the strategy tournament. ``sign_bug_ratio_by_strategy`` lets a
    caller inject the outcome-label sign-bug ratio per strategy (from the
    V8.2.9.x sign-integrity audit) so a strategy built on untrustworthy
    labels is rejected."""
    report = TournamentReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    rows = list(candidates or [])
    report.candidates_input = len(rows)
    sign_map = dict(sign_bug_ratio_by_strategy or {})
    specs = list(strategies)
    for spec in specs:
        spec.validate()
    report.strategies_evaluated = len(specs)

    best_name = ""
    best_rank = -1
    best_test_ev = float("-inf")
    for spec in specs:
        cohort = [r for r in rows if _passes(spec, r)]
        train, val, test = _split_temporal(cohort)
        train_m = _metrics(train, spec.outcome_field)
        val_m = _metrics(val, spec.outcome_field)
        test_m = _metrics(test, spec.outcome_field)
        full_m = _metrics(cohort, spec.outcome_field)
        ssh = _single_symbol_share(test if test else cohort)
        tcs = _time_cluster_share(test if test else cohort)
        sbr = float(sign_map.get(spec.name, 0.0))
        status, reason = _decide_status(
            train_m, val_m, test_m, ssh, tcs, sbr, spec,
        )
        result = StrategyResult(
            name=spec.name,
            side=spec.side,
            logic=spec.logic,
            entry_features=list(spec.entry_features),
            samples=full_m["samples"],
            train_samples=train_m["samples"],
            validation_samples=val_m["samples"],
            test_samples=test_m["samples"],
            winrate=full_m["winrate"],
            test_winrate=test_m["winrate"],
            pf=full_m["pf"],
            test_pf=test_m["pf"],
            net_ev_pct=full_m["net_ev_pct"],
            test_net_ev_pct=test_m["net_ev_pct"],
            test_net_ev_realistic_pct=test_m["net_ev_realistic_pct"],
            test_net_ev_stress_pct=test_m["net_ev_stress_pct"],
            single_symbol_share=ssh,
            time_cluster_share=tcs,
            sign_bug_ratio=sbr,
            status=status,
            reason=reason,
        )
        report.results.append(result.as_dict())
        report.by_status[status] = report.by_status.get(status, 0) + 1
        rank = _STATUS_RANK.get(status, 0)
        if rank > best_rank or (
            rank == best_rank and test_m["net_ev_realistic_pct"] > best_test_ev
        ):
            best_rank = rank
            best_test_ev = test_m["net_ev_realistic_pct"]
            best_name = spec.name
            report.best_status = status
    report.best_strategy = best_name
    report.status = STATUS_OK if rows else STATUS_NEED_DATA
    return report


def _passes(spec: StrategySpec, row: dict[str, Any]) -> bool:
    try:
        return bool(spec.predicate(row))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Built-in candidate strategies derived from the V8.2.9.4 forensic read.
# Each predicate reads ONLY ex-ante fields.
# ---------------------------------------------------------------------------

def _norm(row: dict[str, Any], key: str) -> str:
    return str(row.get(key, "") or "").upper()


def default_strategy_suite() -> list[StrategySpec]:
    """The canonical research suite. All predicates are ex-ante only."""
    return [
        StrategySpec(
            name="rebound_long_all",
            side="LONG",
            logic="Any LONG rebound candidate after a down regime "
                  "(baseline universe).",
            entry_features=("side", "regime_before", "candidate_reason"),
            predicate=lambda r: _norm(r, "side") in ("LONG", ""),
        ),
        StrategySpec(
            name="rebound_long_regime_turned_up",
            side="LONG",
            logic="LONG rebound but ONLY when the current regime has "
                  "left the down-cluster (genuine turn, not falling knife).",
            entry_features=("side", "regime_now"),
            predicate=lambda r: (
                _norm(r, "side") in ("LONG", "")
                and _norm(r, "regime_now") not in DOWN_REGIMES
                and _norm(r, "regime_now") != ""
            ),
        ),
        StrategySpec(
            name="avoid_long_while_trend_down",
            side="NO_TRADE",
            logic="Do-not-trade filter: skip LONG entries while regime_now "
                  "is still TREND_DOWN (the 0%-winrate falling-knife cohort).",
            entry_features=("side", "regime_now"),
            # This 'strategy' selects the rows we would SKIP; scoring it
            # shows how bad that cohort is (evidence for the avoid rule).
            predicate=lambda r: _norm(r, "regime_now") == "TREND_DOWN",
        ),
        StrategySpec(
            name="rebound_long_risk_off_only",
            side="LONG",
            logic="LONG rebound restricted to regime_now=RISK_OFF "
                  "(the only raw-positive-EV cohort in V8.2.9.4).",
            entry_features=("side", "regime_now"),
            predicate=lambda r: (
                _norm(r, "side") in ("LONG", "")
                and _norm(r, "regime_now") == "RISK_OFF"
            ),
            single_symbol_research_only=True,
        ),
    ]
