"""ResearchOps V8.1 — Perp Availability Checker (research-only).

Confirms whether a given spot/token symbol is tradable as a USDT-M perp on
Bitget. Read-only public-data check; if the project DB does not expose a
listing, returns ``perp_available_bitget=False`` plus ``NEED_DATA`` so the
candidate cannot become actionable.

Hard contract:
- never sets leverage,
- never sets margin mode,
- never calls private endpoints,
- never executes orders.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE


@dataclass
class PerpAvailability:
    symbol: str
    perp_available_bitget: bool
    perp_symbol_bitget: str | None
    venue_count: int
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


def check_perp_availability(db: Any, *, symbol: str) -> PerpAvailability:
    """Return the perp availability for a token symbol.

    Uses ``db.bitget_perp_symbol(token)`` when present; else falls back to
    ``db.list_bitget_perp_symbols()`` and a substring match. If neither is
    available, returns ``False`` and notes the NEED_DATA reason.
    """
    token = symbol.upper().replace("USDT", "")
    notes: list[str] = []
    perp_symbol: str | None = None

    ok, value = _safe_call(db, "bitget_perp_symbol", symbol)
    if ok and isinstance(value, str) and value:
        perp_symbol = value.upper()
    else:
        ok2, lst = _safe_call(db, "list_bitget_perp_symbols")
        if ok2 and isinstance(lst, (list, tuple)):
            candidates = [str(s).upper() for s in lst]
            target = f"{token}USDT"
            if target in candidates:
                perp_symbol = target
            else:
                # try with token prefix
                for s in candidates:
                    if s.startswith(f"{token}") and s.endswith("USDT"):
                        perp_symbol = s
                        break
        else:
            notes.append("bitget_perp_listing_method_missing")

    venue_count = 0
    ok3, venues = _safe_call(db, "perp_venues_for_token", token)
    if ok3 and isinstance(venues, (list, tuple)):
        venue_count = len(set(str(v).lower() for v in venues))
    else:
        notes.append("perp_venues_method_missing")

    return PerpAvailability(
        symbol=symbol.upper(),
        perp_available_bitget=bool(perp_symbol),
        perp_symbol_bitget=perp_symbol,
        venue_count=int(venue_count),
        notes=notes,
    )


def batch_check_perp_availability(
    db: Any, *, symbols: Iterable[str]
) -> list[PerpAvailability]:
    return [check_perp_availability(db, symbol=s) for s in symbols]


def summarise_perp_audit(results: list[PerpAvailability]) -> dict[str, Any]:
    return {
        "total": len(results),
        "with_perp_bitget": sum(1 for r in results if r.perp_available_bitget),
        "without_perp_bitget": sum(1 for r in results if not r.perp_available_bitget),
        "missing_methods": sorted({n for r in results for n in r.notes}),
        "results": [r.as_dict() for r in results],
        "research_only": True,
        "paper_filter_enabled": False,
        "can_send_real_orders": False,
        "no_private_endpoints_used": True,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }
