"""ResearchOps V7.5 — Tabla local de tiers de margen de mantenimiento Bitget USDT-M.

Datos públicos consolidados a partir de la documentación oficial de Bitget. Esta
tabla NO se usa en ejecución real. Solo en el `liquidation_model_bitget` para
investigación.

`LAST_VERIFIED_DATE`: fecha en la que se confirmó manualmente la tabla con
docs.bitget.com. Si la fecha tiene más de 60 días, el modelo emite WARN.

Estructura por símbolo: lista de tramos `(notional_upper_usdt, max_leverage,
maintenance_margin_rate, maintenance_amount_usdt)`. El último tramo no acota.

Fuente:
- https://www.bitget.com/api-doc/contract/market/Get-Symbol-Margin-Tier
- https://www.bitget.com/support/articles/12560603824775

Los valores reales pueden cambiar. El propio modelo expone `tier_source` para
que el dashboard distinga "tabla local" vs "fallback conservador".
"""

from __future__ import annotations

from typing import Any


LAST_VERIFIED_DATE = "2026-05-15"
TIER_SOURCE_LOCAL = "local_table_v1"
TIER_SOURCE_FALLBACK = "fallback_conservative"


# Tabla local. Tramos = (notional_upper_usdt, max_leverage, mmr, mmr_amount_usdt).
# Para símbolos no listados se usa el perfil "default_altcoin" (más conservador).
LIQUIDATION_TIERS: dict[str, list[tuple[float, int, float, float]]] = {
    "BTCUSDT": [
        (50_000.0, 125, 0.004, 0.0),
        (250_000.0, 100, 0.005, 50.0),
        (1_000_000.0, 50, 0.01, 1_300.0),
        (10_000_000.0, 20, 0.025, 16_300.0),
        (float("inf"), 5, 0.05, 266_300.0),
    ],
    "ETHUSDT": [
        (50_000.0, 100, 0.005, 0.0),
        (250_000.0, 75, 0.0065, 75.0),
        (1_000_000.0, 50, 0.01, 950.0),
        (5_000_000.0, 20, 0.025, 15_950.0),
        (float("inf"), 5, 0.05, 140_950.0),
    ],
    "SOLUSDT": [
        (20_000.0, 75, 0.0065, 0.0),
        (100_000.0, 50, 0.01, 70.0),
        (500_000.0, 25, 0.02, 1_070.0),
        (float("inf"), 10, 0.05, 16_070.0),
    ],
    "XRPUSDT": [
        (20_000.0, 50, 0.01, 0.0),
        (100_000.0, 25, 0.02, 200.0),
        (500_000.0, 10, 0.05, 3_200.0),
        (float("inf"), 5, 0.10, 28_200.0),
    ],
    "DOGEUSDT": [
        (20_000.0, 50, 0.01, 0.0),
        (100_000.0, 25, 0.02, 200.0),
        (500_000.0, 10, 0.05, 3_200.0),
        (float("inf"), 5, 0.10, 28_200.0),
    ],
    "BNBUSDT": [
        (20_000.0, 75, 0.0065, 0.0),
        (100_000.0, 50, 0.01, 70.0),
        (500_000.0, 25, 0.02, 1_070.0),
        (float("inf"), 10, 0.05, 16_070.0),
    ],
    "LINKUSDT": [
        (15_000.0, 50, 0.01, 0.0),
        (75_000.0, 25, 0.02, 150.0),
        (300_000.0, 10, 0.05, 2_400.0),
        (float("inf"), 5, 0.10, 17_400.0),
    ],
    "AVAXUSDT": [
        (15_000.0, 50, 0.01, 0.0),
        (75_000.0, 25, 0.02, 150.0),
        (300_000.0, 10, 0.05, 2_400.0),
        (float("inf"), 5, 0.10, 17_400.0),
    ],
    "ADAUSDT": [
        (20_000.0, 50, 0.01, 0.0),
        (100_000.0, 25, 0.02, 200.0),
        (500_000.0, 10, 0.05, 3_200.0),
        (float("inf"), 5, 0.10, 28_200.0),
    ],
    "DOTUSDT": [
        (15_000.0, 50, 0.01, 0.0),
        (75_000.0, 25, 0.02, 150.0),
        (300_000.0, 10, 0.05, 2_400.0),
        (float("inf"), 5, 0.10, 17_400.0),
    ],
}


DEFAULT_ALTCOIN_TIERS: list[tuple[float, int, float, float]] = [
    # Más conservador para símbolos no listados.
    (10_000.0, 25, 0.02, 0.0),
    (50_000.0, 10, 0.05, 300.0),
    (250_000.0, 5, 0.10, 2_800.0),
    (float("inf"), 3, 0.20, 27_800.0),
]


def lookup_tier(symbol: str, notional_usdt: float) -> dict[str, Any]:
    """Devuelve el tramo aplicable y la fuente. Nunca llama al exchange."""
    symbol = str(symbol or "").upper()
    table = LIQUIDATION_TIERS.get(symbol)
    if table is None:
        tiers = DEFAULT_ALTCOIN_TIERS
        source = TIER_SOURCE_FALLBACK
    else:
        tiers = table
        source = TIER_SOURCE_LOCAL
    notional = max(0.0, float(notional_usdt))
    for upper, max_lev, mmr, mmr_amount in tiers:
        if notional < upper:
            return {
                "symbol": symbol,
                "notional_upper_usdt": upper if upper != float("inf") else None,
                "max_leverage_tier": max_lev,
                "maintenance_margin_rate": mmr,
                "maintenance_amount_usdt": mmr_amount,
                "tier_source": source,
            }
    last = tiers[-1]
    return {
        "symbol": symbol,
        "notional_upper_usdt": None,
        "max_leverage_tier": last[1],
        "maintenance_margin_rate": last[2],
        "maintenance_amount_usdt": last[3],
        "tier_source": source,
    }
