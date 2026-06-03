"""ResearchOps V7.5 — Modelo de liquidación Bitget research-only.

Sustituye la estimación naive `1/L × 0.95` del simulador de capital por una
distancia de liquidación basada en los tiers reales de margen de mantenimiento
de Bitget USDT-M perpetuos.

Contrato:
- nunca llama a `set_leverage`
- nunca llama a `set_margin_mode`
- nunca modifica configuración real
- nunca envía órdenes
- nunca usa endpoints privados de Bitget
- todos los outputs son `research_only=true` y `final_recommendation=NO LIVE`

Cálculo (cross / linear inverse uniforme, modelo conservador):

    distancia_liquidacion_pct ≈ (1 / L) - mmr + mmr_amount / notional

Para el modo `aislado`, la fórmula equivalente queda como aproximación; si en
el futuro queremos modelar isolated exacto, añadimos un parámetro.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .bitget_liquidation_tiers import (
    LAST_VERIFIED_DATE,
    TIER_SOURCE_FALLBACK,
    TIER_SOURCE_LOCAL,
    lookup_tier,
)


FINAL_RECOMMENDATION = "NO LIVE"

LIQUIDATION_TABLE_STALE_DAYS = 60


@dataclass
class LiquidationVerdict:
    symbol: str
    leverage: int
    notional_usdt: float
    capital_usdt: float
    maintenance_margin_rate: float
    maintenance_amount_usdt: float
    tier_source: str
    max_leverage_tier: int
    liquidation_distance_pct: float
    liquidation_risk: str           # LOW / MEDIUM / HIGH / CRITICAL
    blocks_scale_up: bool
    table_stale_days: int
    table_last_verified_date: str
    warnings: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _table_stale_days(today: datetime | None = None) -> int:
    today = today or datetime.now(timezone.utc)
    try:
        verified = datetime.fromisoformat(LAST_VERIFIED_DATE + "T00:00:00+00:00")
    except Exception:
        return 9999
    delta = today - verified
    return max(0, int(delta.days))


def _classify_risk(distance_pct: float) -> str:
    if distance_pct <= 0.5:
        return "CRITICAL"
    if distance_pct <= 2.0:
        return "HIGH"
    if distance_pct <= 5.0:
        return "MEDIUM"
    return "LOW"


def evaluate_liquidation(
    *,
    symbol: str,
    leverage: int,
    capital_usdt: float,
    margin_per_trade_usdt: float | None = None,
    today: datetime | None = None,
) -> LiquidationVerdict:
    """Devuelve la distancia a liquidación + clasificación de riesgo.

    `notional` = `margin_per_trade × leverage` cuando el caller pasa margen por
    trade. Si no lo pasa, usamos todo el capital como margen para el escenario
    más agresivo (cota superior de notional).
    """
    leverage = max(1, int(leverage))
    if margin_per_trade_usdt is None:
        margin = float(capital_usdt)
    else:
        margin = max(0.0, float(margin_per_trade_usdt))
    notional = margin * leverage
    tier = lookup_tier(symbol, notional)
    mmr = float(tier["maintenance_margin_rate"])
    mmr_amount = float(tier["maintenance_amount_usdt"])
    # Distancia conservadora: 1/L menos el mantenimiento, más el descuento del
    # maintenance_amount. Capada a no negativa.
    initial = 1.0 / leverage
    distance_frac = max(0.0, initial - mmr + (mmr_amount / notional if notional > 0 else 0.0))
    distance_pct = distance_frac * 100.0
    risk = _classify_risk(distance_pct)
    stale = _table_stale_days(today)
    warnings: list[str] = []
    if tier["tier_source"] == TIER_SOURCE_FALLBACK:
        warnings.append("symbol_not_in_local_table_using_conservative_fallback")
    if stale > LIQUIDATION_TABLE_STALE_DAYS:
        warnings.append(f"liquidation_table_stale_days={stale}_refresh_recommended")
    if leverage > tier["max_leverage_tier"]:
        warnings.append(
            f"leverage_{leverage}_above_tier_max_{tier['max_leverage_tier']}"
        )
    blocks_scale_up = risk in {"HIGH", "CRITICAL"} or stale > LIQUIDATION_TABLE_STALE_DAYS
    return LiquidationVerdict(
        symbol=str(symbol or "").upper(),
        leverage=leverage,
        notional_usdt=notional,
        capital_usdt=float(capital_usdt),
        maintenance_margin_rate=mmr,
        maintenance_amount_usdt=mmr_amount,
        tier_source=tier["tier_source"],
        max_leverage_tier=int(tier["max_leverage_tier"]),
        liquidation_distance_pct=distance_pct,
        liquidation_risk=risk,
        blocks_scale_up=blocks_scale_up,
        table_stale_days=stale,
        table_last_verified_date=LAST_VERIFIED_DATE,
        warnings=warnings,
    )


def render_liquidation_text(verdict: LiquidationVerdict) -> str:
    lines = [
        "LIQUIDATION MODEL BITGET START",
        f"symbol: {verdict.symbol}",
        f"leverage: {verdict.leverage}",
        f"notional_usdt: {verdict.notional_usdt:.4f}",
        f"capital_usdt: {verdict.capital_usdt:.4f}",
        f"maintenance_margin_rate: {verdict.maintenance_margin_rate:.4f}",
        f"maintenance_amount_usdt: {verdict.maintenance_amount_usdt:.4f}",
        f"tier_source: {verdict.tier_source}",
        f"max_leverage_tier: {verdict.max_leverage_tier}",
        f"liquidation_distance_pct: {verdict.liquidation_distance_pct:.4f}",
        f"liquidation_risk: {verdict.liquidation_risk}",
        f"blocks_scale_up: {str(verdict.blocks_scale_up).lower()}",
        f"table_last_verified_date: {verdict.table_last_verified_date}",
        f"table_stale_days: {verdict.table_stale_days}",
    ]
    if verdict.warnings:
        lines.append("warnings:")
        for warning in verdict.warnings:
            lines.append(f"- {warning}")
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "no_set_leverage_call: true",
        "no_set_margin_mode_call: true",
        "final_recommendation: NO LIVE",
        "LIQUIDATION MODEL BITGET END",
    ])
    return "\n".join(lines)
