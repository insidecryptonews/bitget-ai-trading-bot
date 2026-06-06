"""V8.2.9 — Score Gate Sandbox (research-only).

Tests what happens to LONG rebound candidates if the score gate is
removed / inverted / left as diagnostic, given that the score is
currently flagged as anti-calibrated in production. Never modifies the
production scoring engine or production thresholds.

Hard contract: research-only. The flag
``score_used_as_positive_gate`` is exposed so downstream audits can
detect any future attempt to promote an anti-calibrated score back
into a positive filter.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK


SCORE_GATE_CURRENT_72 = "current_score_gate_72"
SCORE_GATE_NO_GATE = "no_score_gate"
SCORE_GATE_DIAGNOSTIC_ONLY = "score_diagnostic_only"
SCORE_GATE_INVERSE_WARNING = "inverse_score_warning_only"

VARIANTS: tuple[str, ...] = (
    SCORE_GATE_CURRENT_72,
    SCORE_GATE_NO_GATE,
    SCORE_GATE_DIAGNOSTIC_ONLY,
    SCORE_GATE_INVERSE_WARNING,
)

COST_REALISTIC_PCT = 0.25
TRAIN_FRACTION = 0.60
VAL_FRACTION = 0.20

# Variant -> uses score as a positive gate?
GATE_VARIANTS_THAT_USE_SCORE_POSITIVELY: frozenset[str] = frozenset({
    SCORE_GATE_CURRENT_72,
})

# V8.2.9.1 — PF sentinel for the all-wins case (avoids float('inf')).
PF_SENTINEL_NO_LOSSES = 999.0


def _profit_factor(gross_profit: float, gross_loss: float) -> float:
    """Canonical PF — see ``rebound_long_strict_oos_v8_2_9`` for the rule."""
    loss_abs = abs(float(gross_loss))
    if loss_abs == 0.0:
        return PF_SENTINEL_NO_LOSSES if float(gross_profit) > 0 else 0.0
    return float(gross_profit) / loss_abs


def _passes_variant(row: dict[str, Any], variant: str) -> bool:
    score = row.get("score")
    if not isinstance(score, (int, float)):
        return variant in {SCORE_GATE_NO_GATE, SCORE_GATE_DIAGNOSTIC_ONLY,
                           SCORE_GATE_INVERSE_WARNING}
    s = float(score)
    if variant == SCORE_GATE_CURRENT_72:
        return s >= 72.0
    return True


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    nets: list[float] = []
    for r in rows:
        v = r.get("net_pnl_est")
        if v is None:
            v = r.get("baseline_net_pnl_est")
        if isinstance(v, (int, float)):
            nets.append(float(v))
    if not nets:
        return {
            "samples": 0, "winrate": 0.0, "net_ev_avg_pct": 0.0,
            "pf": 0.0, "max_loss_pct": 0.0,
            "net_ev_after_cost_pct": 0.0,
        }
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    pf = _profit_factor(sum(wins), sum(losses))
    n = len(nets)
    return {
        "samples": n,
        "winrate": len(wins) / n,
        "net_ev_avg_pct": sum(nets) / n,
        "pf": pf,
        "max_loss_pct": min(nets) if losses else 0.0,
        "net_ev_after_cost_pct": (sum(nets) / n) - COST_REALISTIC_PCT,
    }


def _split_temporal(rows: list[dict[str, Any]]) -> tuple[list, list, list]:
    if not rows:
        return [], [], []
    ordered = sorted(rows, key=lambda r: str(r.get("timestamp", "")))
    n = len(ordered)
    train_end = int(n * TRAIN_FRACTION)
    val_end = int(n * (TRAIN_FRACTION + VAL_FRACTION))
    return ordered[:train_end], ordered[train_end:val_end], ordered[val_end:]


def _duplicate_ratio(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    seen: dict[str, int] = {}
    for r in rows:
        key = "|".join([
            str(r.get("symbol", "")),
            str(r.get("timestamp", ""))[:16],
            str(r.get("side", "")),
        ])
        seen[key] = seen.get(key, 0) + 1
    dup = sum(c - 1 for c in seen.values() if c > 1)
    return dup / len(rows)


@dataclass
class VariantResult:
    variant: str
    samples: int
    winrate: float
    net_ev_avg_pct: float
    net_ev_after_cost_pct: float
    pf: float
    max_loss_pct: float
    duplicate_ratio: float
    train_samples: int
    validation_samples: int
    test_samples: int
    test_net_ev_after_cost_pct: float
    test_pf: float
    oos_status: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScoreGateSandboxReport:
    hours: int
    generated_at: str
    candidates_total: int = 0
    score_anti_calibrated_input: bool = True
    score_used_as_positive_gate: bool = False
    best_variant: str = ""
    variants: list[dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_score_gate_sandbox(
    candidates: Iterable[dict[str, Any]] | None = None,
    *,
    hours: int = 168,
    score_anti_calibrated: bool = True,
) -> ScoreGateSandboxReport:
    """Run the four score-gate variants over LONG rebound candidates.

    When ``score_anti_calibrated`` is true, ``score_used_as_positive_gate``
    is forced to false so downstream consumers cannot accidentally
    promote the score back into a real gate.
    """
    report = ScoreGateSandboxReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
        score_anti_calibrated_input=bool(score_anti_calibrated),
    )
    rows = list(candidates or [])
    report.candidates_total = len(rows)
    if not rows:
        return report
    best_variant = ""
    best_score = float("-inf")
    for v in VARIANTS:
        filtered = [r for r in rows if _passes_variant(r, v)]
        m = _metrics(filtered)
        dup = _duplicate_ratio(filtered)
        train, val, test = _split_temporal(filtered)
        train_m = _metrics(train)
        val_m = _metrics(val)
        test_m = _metrics(test)
        if test_m["samples"] < 15:
            oos = "NEED_MORE_DATA"
        elif test_m["net_ev_after_cost_pct"] > 0 and test_m["pf"] > 1.15:
            oos = "PASS"
        else:
            oos = "FAIL"
        vr = VariantResult(
            variant=v,
            samples=m["samples"],
            winrate=m["winrate"],
            net_ev_avg_pct=m["net_ev_avg_pct"],
            net_ev_after_cost_pct=m["net_ev_after_cost_pct"],
            pf=m["pf"],
            max_loss_pct=m["max_loss_pct"],
            duplicate_ratio=dup,
            train_samples=train_m["samples"],
            validation_samples=val_m["samples"],
            test_samples=test_m["samples"],
            test_net_ev_after_cost_pct=test_m["net_ev_after_cost_pct"],
            test_pf=test_m["pf"],
            oos_status=oos,
        )
        report.variants.append(vr.as_dict())
        if oos == "PASS" and test_m["net_ev_after_cost_pct"] > best_score:
            best_score = test_m["net_ev_after_cost_pct"]
            best_variant = v
    report.best_variant = best_variant
    # If the score is anti-calibrated the report MUST NOT advertise the
    # 72-threshold variant as a positive gate. The flag below stays false.
    if bool(score_anti_calibrated):
        report.score_used_as_positive_gate = False
    else:
        # Score is not anti-calibrated: best_variant might legitimately
        # be the 72-threshold one.
        report.score_used_as_positive_gate = (
            best_variant in GATE_VARIANTS_THAT_USE_SCORE_POSITIVELY
        )
    report.status = STATUS_OK
    return report
