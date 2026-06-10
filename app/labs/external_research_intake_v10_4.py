"""ResearchOps V10.4 — External Research Intake (research-only backlog).

A structured intake for ideas from Perplexity, papers, GitHub, humans, Codex,
dashboards, etc. It ONLY classifies ideas into a research backlog. No idea can
ever enable the paper filter or live trading from here — the ceiling is
``SHADOW_ELIGIBLE`` (research). Pure; no network, no DB, no secrets.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE

# Backlog statuses (none is operationally actionable).
IDEA_ONLY = "IDEA_ONLY"
NEEDS_DATA = "NEEDS_DATA"
NEEDS_BACKTEST = "NEEDS_BACKTEST"
NEEDS_WALK_FORWARD = "NEEDS_WALK_FORWARD"
NEEDS_RISK_REVIEW = "NEEDS_RISK_REVIEW"  # V10.4.1 — unknown risk is not safe
REJECT_LOOKAHEAD = "REJECT_LOOKAHEAD_RISK"
REJECT_OVERFIT = "REJECT_OVERFIT_RISK"
REJECT_UNTRADABLE = "REJECT_UNTRADABLE"
SHADOW_ELIGIBLE = "SHADOW_ELIGIBLE"
PAPER_CANDIDATE_PENDING = "PAPER_CANDIDATE_PENDING_VALIDATION"  # backlog label only

_HIGH = {"high", "severe", "critical", "yes", "true", True}
# Only an EXPLICIT low/controlled assessment counts as safe. Unknown, empty,
# missing, medium or anything else blocks the path to shadow (Codex P1).
_LOW = {"low", "controlled", "mitigated", "none"}


@dataclass
class ResearchIdea:
    source_name: str = ""
    source_type: str = ""
    claim: str = ""
    market: str = ""
    symbols: list[str] = field(default_factory=list)
    side: str = ""
    timeframe: str = ""
    required_features: list[str] = field(default_factory=list)
    known_risks: list[str] = field(default_factory=list)
    lookahead_risk: str = "unknown"
    overfit_risk: str = "unknown"
    data_requirements: list[str] = field(default_factory=list)
    data_available: bool = False
    backtested: bool = False
    walk_forward_passed: bool = False
    tradable_on_bitget: bool = True
    validation_plan: str = ""
    promotion_gate: str = "research_backlog"
    final_status: str = IDEA_ONLY

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_high(v: Any) -> bool:
    return (str(v).strip().lower() in {"high", "severe", "critical", "yes", "true"}) or v is True


def _is_explicitly_low(v: Any) -> bool:
    """Unknown risk is not safe: only explicit low/controlled passes."""
    return str(v or "").strip().lower() in _LOW


def classify_idea(idea: ResearchIdea) -> str:
    """Conservative classification. Rejections first; ceiling SHADOW_ELIGIBLE.

    V10.4.1 (Codex P1): an idea whose lookahead/overfit risk is unknown,
    empty or missing can NEVER reach SHADOW_ELIGIBLE — at best it parks in
    NEEDS_RISK_REVIEW until a human explicitly assesses the risks as
    low/controlled.
    """
    if _is_high(idea.lookahead_risk):
        return REJECT_LOOKAHEAD
    if _is_high(idea.overfit_risk):
        return REJECT_OVERFIT
    if not idea.tradable_on_bitget or not idea.symbols or not (idea.side or "").strip():
        return REJECT_UNTRADABLE
    if idea.data_requirements and not idea.data_available:
        return NEEDS_DATA
    if not idea.backtested:
        return NEEDS_BACKTEST
    if not idea.walk_forward_passed:
        return NEEDS_WALK_FORWARD
    if not (_is_explicitly_low(idea.lookahead_risk) and _is_explicitly_low(idea.overfit_risk)):
        return NEEDS_RISK_REVIEW
    # Backtested + walk-forward passed + risks explicitly low => the MAX
    # research status is shadow. Never paper, never live.
    return SHADOW_ELIGIBLE


@dataclass
class IntakeReport:
    ideas_count: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    ideas: list[dict[str, Any]] = field(default_factory=list)
    shadow_eligible: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    paper_ready: bool = False
    live_ready: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_research_intake(ideas: list[ResearchIdea] | None) -> IntakeReport:
    rep = IntakeReport()
    items = list(ideas or [])
    rep.ideas_count = len(items)
    by_status: dict[str, int] = {}
    for idea in items:
        status = classify_idea(idea)
        idea.final_status = status
        by_status[status] = by_status.get(status, 0) + 1
        rep.ideas.append(idea.as_dict())
        label = f"{idea.source_name}:{idea.claim[:40]}"
        if status == SHADOW_ELIGIBLE:
            rep.shadow_eligible.append(label)
        elif status.startswith("REJECT_"):
            rep.rejected.append(f"{label} ({status})")
    rep.by_status = by_status
    # Hard invariant: intake never makes anything paper/live ready.
    rep.paper_ready = False
    rep.live_ready = False
    return rep
