"""ResearchOps V8.1 — Shortability Score (research-only).

Estimates how realistically a perp could be shorted at the candidate event
time given:

- spread bid/ask in bps,
- top-of-book depth in USD,
- 24h notional volume,
- funding sign (negative funding = paid to be short),
- liquidation tier (when available via :mod:`app.liquidation_model_bitget`).

If any critical input is missing, returns ``shortability_score=None`` and
``score_status="NEED_DATA"``. The score never authorises a trade — it is a
research label only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE


SCORE_STATUS_OK = "OK"
SCORE_STATUS_NEED_DATA = "NEED_DATA"
SCORE_STATUS_NO_PERP = "NO_PERP"

SHORTABILITY_THRESHOLD_LOW = 0.30  # under this is "LOW_SHORTABILITY"
SHORTABILITY_THRESHOLD_HIGH = 0.70


@dataclass
class ShortabilityResult:
    symbol: str
    score_status: str
    shortability_score: float | None
    components: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    no_private_endpoints_used: bool = True
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_call(db: Any, method: str, *args, **kwargs) -> tuple[bool, Any]:
    fn = getattr(db, method, None)
    if fn is None or not callable(fn):
        return False, None
    try:
        return True, fn(*args, **kwargs)
    except Exception:
        return False, None


def _normalise_spread(spread_bps: float) -> float:
    """Lower spread → higher score. 0 bps = 1.0, 50 bps = 0.0."""
    if spread_bps is None:
        return 0.0
    return max(0.0, min(1.0, 1.0 - float(spread_bps) / 50.0))


def _normalise_depth(depth_usd: float) -> float:
    """50k USD top-of-book depth = 0.5, 250k = 1.0."""
    if depth_usd is None:
        return 0.0
    if depth_usd <= 0:
        return 0.0
    return max(0.0, min(1.0, float(depth_usd) / 250_000.0))


def _normalise_volume(volume_24h_usd: float) -> float:
    """1M USD = 0.2, 10M = 0.7, 100M+ = 1.0 (log scale)."""
    if volume_24h_usd is None or volume_24h_usd <= 0:
        return 0.0
    import math
    score = max(0.0, math.log10(float(volume_24h_usd)) - 5.5) / 2.5
    return max(0.0, min(1.0, score))


def _normalise_funding_sign(funding_rate: float | None) -> float:
    """Negative funding (shorts get paid) bumps the score by 0.10. Positive
    funding (shorts pay) trims it by 0.10. Missing data → 0.0."""
    if funding_rate is None:
        return 0.0
    if funding_rate < 0:
        return 0.10
    if funding_rate > 0:
        return -0.10
    return 0.0


def compute_shortability(
    db: Any,
    *,
    symbol: str,
    perp_available: bool,
) -> ShortabilityResult:
    """Compute the score for a single symbol."""
    if not perp_available:
        return ShortabilityResult(
            symbol=symbol.upper(),
            score_status=SCORE_STATUS_NO_PERP,
            shortability_score=None,
            notes=["no perp available on bitget"],
        )

    components: dict[str, Any] = {}
    notes: list[str] = []

    ok_spread, spread = _safe_call(db, "latest_bid_ask_spread_bps", symbol)
    if not ok_spread or spread is None:
        notes.append("spread_missing")
        components["spread_bps"] = None
    else:
        components["spread_bps"] = float(spread)

    ok_depth, depth = _safe_call(db, "top_of_book_depth_usd", symbol)
    if not ok_depth or depth is None:
        notes.append("depth_missing")
        components["depth_usd"] = None
    else:
        components["depth_usd"] = float(depth)

    ok_vol, vol = _safe_call(db, "volume_24h_usd", symbol)
    if not ok_vol or vol is None:
        notes.append("volume_missing")
        components["volume_24h_usd"] = None
    else:
        components["volume_24h_usd"] = float(vol)

    ok_funding, funding = _safe_call(db, "latest_funding_rate", symbol)
    if not ok_funding or funding is None:
        notes.append("funding_missing")
        components["funding_rate"] = None
    else:
        components["funding_rate"] = float(funding)

    # Need at least spread + depth + volume to score honestly.
    if components["spread_bps"] is None or components["depth_usd"] is None \
            or components["volume_24h_usd"] is None:
        return ShortabilityResult(
            symbol=symbol.upper(),
            score_status=SCORE_STATUS_NEED_DATA,
            shortability_score=None,
            components=components,
            notes=notes,
        )

    s_spread = _normalise_spread(components["spread_bps"])
    s_depth = _normalise_depth(components["depth_usd"])
    s_vol = _normalise_volume(components["volume_24h_usd"])
    funding_adj = _normalise_funding_sign(components["funding_rate"])
    score = (0.4 * s_spread + 0.3 * s_depth + 0.3 * s_vol) + funding_adj
    score = max(0.0, min(1.0, score))

    components["s_spread"] = s_spread
    components["s_depth"] = s_depth
    components["s_volume"] = s_vol
    components["funding_adjustment"] = funding_adj

    return ShortabilityResult(
        symbol=symbol.upper(),
        score_status=SCORE_STATUS_OK,
        shortability_score=score,
        components=components,
        notes=notes,
    )


def batch_shortability(
    db: Any,
    *,
    symbols_with_perp: Iterable[tuple[str, bool]],
) -> list[ShortabilityResult]:
    return [compute_shortability(db, symbol=s, perp_available=ok) for s, ok in symbols_with_perp]


def summarise_shortability(results: list[ShortabilityResult]) -> dict[str, Any]:
    ok = [r for r in results if r.score_status == SCORE_STATUS_OK]
    return {
        "total": len(results),
        "ok": len(ok),
        "need_data": sum(1 for r in results if r.score_status == SCORE_STATUS_NEED_DATA),
        "no_perp": sum(1 for r in results if r.score_status == SCORE_STATUS_NO_PERP),
        "top": sorted(
            [r.as_dict() for r in ok if r.shortability_score is not None],
            key=lambda r: r.get("shortability_score") or 0.0,
            reverse=True,
        )[:10],
        "research_only": True,
        "paper_filter_enabled": False,
        "can_send_real_orders": False,
        "no_private_endpoints_used": True,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }
