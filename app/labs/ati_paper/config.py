"""Versioned, fail-closed configuration for ATI simulated paper execution."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import ACCOUNT_ID, EXECUTION_MODE, MODE, POLICY_VERSION, REPO_ROOT, SOURCE_POLICY_VERSION

DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "ati" / "ATI_PAPER_SIMULATION_V1.json"


@dataclass(frozen=True)
class InstrumentRule:
    symbol: str
    min_trade_num: float
    min_trade_usdt: float
    quantity_step: float
    volume_place: int
    price_place: int
    source: str


@dataclass(frozen=True)
class AtiPaperConfig:
    account_id: str
    initial_balance_usdt: float
    position_fraction: float
    fee_bps_per_side: float
    slippage_bps_per_side: float
    spread_bps_round_trip: float
    target_r_multiple: float
    max_holding_minutes: int
    trailing_enabled: bool
    trailing_activation_r: float
    trailing_distance_r: float
    poll_interval_seconds: int
    market_data_stale_after_seconds: int
    funding_mode: str
    instrument_rules: dict[str, InstrumentRule]
    policy_version: str = POLICY_VERSION
    source_policy_version: str = SOURCE_POLICY_VERSION
    sizing_method: str = "realized_equity_fraction"

    @property
    def entry_fee_fraction(self) -> float:
        return self.fee_bps_per_side / 10_000.0

    @property
    def exit_fee_fraction(self) -> float:
        return self.fee_bps_per_side / 10_000.0

    @property
    def adverse_slippage_fraction(self) -> float:
        return (self.slippage_bps_per_side + self.spread_bps_round_trip / 2.0) / 10_000.0


class AtiPaperConfigError(ValueError):
    pass


def _finite_positive(value: Any, name: str, *, allow_zero: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AtiPaperConfigError(f"ATI_PAPER_CONFIG_INVALID:{name}") from exc
    lower_ok = number >= 0 if allow_zero else number > 0
    if not math.isfinite(number) or not lower_ok:
        raise AtiPaperConfigError(f"ATI_PAPER_CONFIG_INVALID:{name}")
    return number


def load_config(path: Path | str | None = None) -> AtiPaperConfig:
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AtiPaperConfigError("ATI_PAPER_CONFIG_UNREADABLE") from exc
    if not isinstance(raw, dict):
        raise AtiPaperConfigError("ATI_PAPER_CONFIG_NOT_OBJECT")
    expected = {
        "policy_name": POLICY_VERSION,
        "source_policy": SOURCE_POLICY_VERSION,
        "mode": MODE,
        "execution_mode": EXECUTION_MODE,
        "account_id": ACCOUNT_ID,
        "can_send_real_orders": False,
        "paper_filter_enabled": False,
        "live_trading": False,
        "final_recommendation": "NO LIVE",
    }
    for key, value in expected.items():
        if raw.get(key) != value:
            raise AtiPaperConfigError(f"ATI_PAPER_CONFIG_CONTRACT:{key}")
    sizing = raw.get("sizing")
    execution = raw.get("execution")
    fallbacks = raw.get("instrument_rule_fallbacks")
    if not isinstance(sizing, dict) or not isinstance(execution, dict) or not isinstance(fallbacks, dict):
        raise AtiPaperConfigError("ATI_PAPER_CONFIG_SECTIONS_MISSING")
    if sizing.get("method") != "realized_equity_fraction":
        raise AtiPaperConfigError("ATI_PAPER_SIZING_METHOD_UNSUPPORTED")
    if sizing.get("compound_from") != "realized_equity_before_entry" or sizing.get("use_unrealized_pnl") is not False:
        raise AtiPaperConfigError("ATI_PAPER_SIZING_MUST_USE_REALIZED_EQUITY")
    if sizing.get("borrowing_enabled") is not False or float(sizing.get("notional_multiplier", 0)) != 1.0:
        raise AtiPaperConfigError("ATI_PAPER_BORROWING_OR_MULTIPLIER_BLOCKED")
    fraction = _finite_positive(sizing.get("position_fraction"), "position_fraction")
    if fraction > 1.0:
        raise AtiPaperConfigError("ATI_PAPER_POSITION_FRACTION_ABOVE_1X")
    if execution.get("ambiguity_rule") != "STOP_BEFORE_TP":
        raise AtiPaperConfigError("ATI_PAPER_AMBIGUITY_RULE_UNSAFE")
    rules: dict[str, InstrumentRule] = {}
    for symbol, item in fallbacks.items():
        if not isinstance(item, dict) or not str(symbol).endswith("USDT"):
            raise AtiPaperConfigError("ATI_PAPER_INSTRUMENT_RULE_INVALID")
        rules[str(symbol).upper()] = InstrumentRule(
            symbol=str(symbol).upper(),
            min_trade_num=_finite_positive(item.get("min_trade_num"), "min_trade_num"),
            min_trade_usdt=_finite_positive(item.get("min_trade_usdt"), "min_trade_usdt"),
            quantity_step=_finite_positive(item.get("quantity_step"), "quantity_step"),
            volume_place=int(item.get("volume_place")),
            price_place=int(item.get("price_place")),
            source=str(item.get("source") or "CONFIG_SNAPSHOT"),
        )
    if not rules:
        raise AtiPaperConfigError("ATI_PAPER_INSTRUMENT_RULES_EMPTY")
    return AtiPaperConfig(
        account_id=ACCOUNT_ID,
        initial_balance_usdt=_finite_positive(raw.get("initial_balance_usdt"), "initial_balance_usdt"),
        position_fraction=fraction,
        fee_bps_per_side=_finite_positive(execution.get("fee_bps_per_side"), "fee_bps", allow_zero=True),
        slippage_bps_per_side=_finite_positive(execution.get("slippage_bps_per_side"), "slippage_bps", allow_zero=True),
        spread_bps_round_trip=_finite_positive(execution.get("spread_bps_round_trip"), "spread_bps", allow_zero=True),
        target_r_multiple=_finite_positive(execution.get("target_r_multiple"), "target_r_multiple"),
        max_holding_minutes=max(1, int(execution.get("max_holding_minutes"))),
        trailing_enabled=bool(execution.get("trailing_enabled", False)),
        trailing_activation_r=_finite_positive(execution.get("trailing_activation_r"), "trailing_activation_r"),
        trailing_distance_r=_finite_positive(execution.get("trailing_distance_r"), "trailing_distance_r"),
        poll_interval_seconds=max(5, int(execution.get("poll_interval_seconds"))),
        market_data_stale_after_seconds=max(30, int(execution.get("market_data_stale_after_seconds"))),
        funding_mode=str(execution.get("funding_mode") or "UNKNOWN_UNLESS_VERIFIED"),
        instrument_rules=rules,
    )
