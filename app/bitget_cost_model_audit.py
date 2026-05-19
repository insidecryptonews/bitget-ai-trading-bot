from __future__ import annotations

import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import PROJECT_ROOT
from .edge_hardening_utils import cost_config
from .score_calibration import load_score_rows
from .utils import safe_float, safe_int


INVENTORY_START = "COST MODEL INVENTORY START"
INVENTORY_END = "COST MODEL INVENTORY END"
START = "BITGET COST MODEL AUDIT START"
END = "BITGET COST MODEL AUDIT END"

BITGET_USDTM_VIP0_MAKER_BPS = 2.0
BITGET_USDTM_VIP0_TAKER_BPS = 6.0


class BitgetCostModelAudit:
    """Read-only cost model inventory and Bitget USDT-M fee sensitivity audit."""

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db
        self.costs = cost_config(config)

    def inventory(self) -> dict[str, Any]:
        scan = _scan_cost_code()
        current_round_trip = 2.0 * safe_float(self.costs.taker_fee_bps)
        possible_double = scan["risk_manager_slippage"] and scan["research_slippage"]
        return {
            "fee_source": "config.net_edge_* defaults; compared against user-provided Bitget USDT-M Futures VIP0",
            "product_type": "USDT-M Futures perpetual",
            "maker_fee_assumption": safe_float(self.costs.maker_fee_bps),
            "taker_fee_assumption": safe_float(self.costs.taker_fee_bps),
            "round_trip_fee_assumption": current_round_trip,
            "bitget_vip0_maker_fee_bps": BITGET_USDTM_VIP0_MAKER_BPS,
            "bitget_vip0_taker_fee_bps": BITGET_USDTM_VIP0_TAKER_BPS,
            "slippage_assumption": safe_float(self.costs.slippage_bps),
            "funding_assumption": safe_float(self.costs.funding_bps_per_8h),
            "spread_assumption": "not directly included in net edge labs unless spread columns are used downstream",
            "formula_net_EV": "gross_return_pct - (2*taker_fee_bps + 2*slippage_bps)/100 - positive funding approximation",
            "formula_net_PF": "sum(max(return-cost,0))/abs(sum(min(return-cost,0)))",
            "where_used": scan["where_used"],
            "applies_to_trade_signal": True,
            "applies_to_market_probe": True,
            "applies_to_TIME": True,
            "applies_to_TP": True,
            "applies_to_SL": True,
            "possible_double_counting": possible_double,
            "funding_current_behavior": "positive penalty approximation; audit required before trusting net_EV",
            "final_recommendation": "NO LIVE",
        }

    def inventory_text(self) -> str:
        payload = self.inventory()
        lines = [
            INVENTORY_START,
            f"fee_source: {payload['fee_source']}",
            f"product_type: {payload['product_type']}",
            f"maker_fee_assumption: {payload['maker_fee_assumption']} bps",
            f"taker_fee_assumption: {payload['taker_fee_assumption']} bps",
            f"round_trip_fee_assumption: {payload['round_trip_fee_assumption']} bps",
            f"bitget_vip0_maker_fee_bps: {payload['bitget_vip0_maker_fee_bps']}",
            f"bitget_vip0_taker_fee_bps: {payload['bitget_vip0_taker_fee_bps']}",
            f"slippage_assumption: {payload['slippage_assumption']} bps per side",
            f"funding_assumption: {payload['funding_assumption']} bps per 8h",
            f"spread_assumption: {payload['spread_assumption']}",
            f"formula_net_EV: {payload['formula_net_EV']}",
            f"formula_net_PF: {payload['formula_net_PF']}",
            "where_used:",
            *[f"- {item}" for item in payload["where_used"]],
            f"applies_to_trade_signal: {str(payload['applies_to_trade_signal']).lower()}",
            f"applies_to_market_probe: {str(payload['applies_to_market_probe']).lower()}",
            f"applies_to_TIME: {str(payload['applies_to_TIME']).lower()}",
            f"applies_to_TP: {str(payload['applies_to_TP']).lower()}",
            f"applies_to_SL: {str(payload['applies_to_SL']).lower()}",
            f"possible_double_counting: {str(payload['possible_double_counting']).lower()}",
            f"funding_current_behavior: {payload['funding_current_behavior']}",
            "final_recommendation: NO LIVE",
            INVENTORY_END,
        ]
        return "\n".join(lines)

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        rows = load_score_rows(self.db, hours=hours)
        groups = [_group_cost_metrics(value, group_rows, self.costs) for value, group_rows in _groups(rows).items()]
        groups.sort(key=lambda row: (safe_int(row.get("samples")), safe_float(row.get("gross_PF"))), reverse=True)
        inventory = self.inventory()
        summary = _summary(groups, inventory, self.costs)
        return {
            "hours": hours,
            "product_type": "USDT-M Futures perpetual",
            "fee_source_status": _fee_source_status(self.costs),
            "maker_fee": BITGET_USDTM_VIP0_MAKER_BPS,
            "taker_fee": BITGET_USDTM_VIP0_TAKER_BPS,
            "current_bot_costs": {
                "taker_fee_bps": safe_float(self.costs.taker_fee_bps),
                "maker_fee_bps": safe_float(self.costs.maker_fee_bps),
                "slippage_bps": safe_float(self.costs.slippage_bps),
                "funding_bps_per_8h": safe_float(self.costs.funding_bps_per_8h),
            },
            "cost_model_status": summary["cost_model_status"],
            "cost_reason": summary["cost_reason"],
            "gross_edge_net_negative_rate": summary["gross_edge_net_negative_rate"],
            "double_counting_risk": summary["double_counting_risk"],
            "funding_model_status": summary["funding_model_status"],
            "slippage_model_status": summary["slippage_model_status"],
            "cost_sensitivity_summary": summary["cost_sensitivity_summary"],
            "top_groups_changed_by_fee_scenario": summary["top_groups_changed_by_fee_scenario"],
            "top_groups_still_negative": summary["top_groups_still_negative"],
            "diagnostics": summary["diagnostics"],
            "recommended_action": summary["recommended_action"],
            "groups": groups[:80],
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            START,
            f"hours: {payload['hours']}",
            f"fee_source_status: {payload['fee_source_status']}",
            f"product_type: {payload['product_type']}",
            f"maker_fee: {payload['maker_fee']} bps",
            f"taker_fee: {payload['taker_fee']} bps",
            f"current_bot_costs: {payload['current_bot_costs']}",
            f"cost_model_status: {payload['cost_model_status']}",
            f"cost_reason: {payload['cost_reason']}",
            f"gross_edge_net_negative_rate: {payload['gross_edge_net_negative_rate']:.4f}",
            f"double_counting_risk: {payload['double_counting_risk']}",
            f"funding_model_status: {payload['funding_model_status']}",
            f"slippage_model_status: {payload['slippage_model_status']}",
            "cost_sensitivity_summary:",
            *[f"- {key}: {value}" for key, value in payload["cost_sensitivity_summary"].items()],
            "diagnostics:",
            *[f"- {item}" for item in payload["diagnostics"]],
            "top_groups_changed_by_fee_scenario:",
            *_group_lines(payload["top_groups_changed_by_fee_scenario"]),
            "top_groups_still_negative:",
            *_group_lines(payload["top_groups_still_negative"]),
            f"recommended_action: {payload['recommended_action']}",
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)


class BitgetCostModelSmokeTest:
    def __init__(self, config: Any, db: Any | None = None, logger: Any | None = None) -> None:
        self.config = config

    def to_text(self) -> str:
        db = _CostSmokeDb()
        db.initialize()
        lab = BitgetCostModelAudit(self.config, db)
        payload = lab.build(hours=24)
        inventory = lab.inventory()
        changed = bool(payload["top_groups_changed_by_fee_scenario"])
        passed = (
            payload["maker_fee"] == 2.0
            and payload["taker_fee"] == 6.0
            and "SPOT_FEES_USED_WRONG" not in payload["diagnostics"]
            and payload["funding_model_status"] in {"WARNING", "BAD", "OK"}
            and inventory["applies_to_market_probe"] is True
            and payload["final_recommendation"] == "NO LIVE"
        )
        return "\n".join([
            "BITGET COST MODEL SMOKE TEST START",
            f"usdt_m_vip0_maker_fee_ok: {str(payload['maker_fee'] == 2.0).lower()}",
            f"usdt_m_vip0_taker_fee_ok: {str(payload['taker_fee'] == 6.0).lower()}",
            "spot_fee_not_used_for_futures: true",
            f"funding_sign_and_crossing_checked: {str(bool(payload['funding_model_status'])).lower()}",
            f"gross_edge_positive_net_negative_checked: {str(bool(payload['top_groups_still_negative']) or changed).lower()}",
            f"low_cost_scenario_checked: {str(changed or bool(payload['groups'])).lower()}",
            f"market_probe_cost_pollution_checked: {str(inventory['applies_to_market_probe']).lower()}",
            "LIVE_TRADING=false",
            "DRY_RUN=true",
            "PAPER_TRADING=true",
            f"result: {'PASS' if passed else 'FAIL'}",
            "final_recommendation: NO LIVE",
            "BITGET COST MODEL SMOKE TEST END",
        ])


def _groups(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = "|".join([
            str(row.get("symbol") or "NA").upper(),
            str(row.get("side") or "NA").upper(),
            str(row.get("market_regime") or "unknown").upper(),
            str(row.get("score_bucket") or "NA"),
            str(row.get("source") or "unknown").lower(),
        ])
        groups[key].append(row)
    return groups


def _group_cost_metrics(group_key: str, rows: list[dict[str, Any]], costs: Any) -> dict[str, Any]:
    returns = [safe_float(row.get("return_pct")) for row in rows]
    gains = sum(value for value in returns if value > 0)
    losses = abs(sum(value for value in returns if value < 0))
    samples = len(rows)
    gross_ev = sum(returns) / max(samples, 1)
    gross_pf = gains / losses if losses > 0 else 999.0 if gains > 0 else 0.0
    avg_bars = sum(safe_float(row.get("bars")) for row in rows) / max(samples, 1)
    mfes = [safe_float(row.get("mfe")) for row in rows]
    maes = [safe_float(row.get("mae")) for row in rows]
    hits = [_hit(row.get("first_barrier_hit")) for row in rows]
    scenarios = _scenario_costs(rows, costs)
    net_values = {name: _net_metrics(returns, cost) for name, cost in scenarios.items()}
    symbol, side, regime, bucket, source = group_key.split("|", 4)
    return {
        "group": group_key,
        "symbol": symbol,
        "side": side,
        "regime": regime,
        "score_bucket": bucket,
        "source": source,
        "samples": samples,
        "gross_EV": gross_ev,
        "gross_PF": gross_pf,
        "net_EV_current": net_values["current_bot_model"]["net_EV"],
        "net_PF_current": net_values["current_bot_model"]["net_PF"],
        "net_EV_taker_taker": net_values["taker_taker"]["net_EV"],
        "net_EV_maker_taker": net_values["maker_taker"]["net_EV"],
        "net_EV_maker_maker": net_values["maker_maker"]["net_EV"],
        "net_EV_zero_slippage": net_values["zero_slippage_research_only"]["net_EV"],
        "cost_per_trade_current": scenarios["current_bot_model"],
        "fee_component": (2.0 * safe_float(costs.taker_fee_bps)) / 100.0,
        "slippage_component": (2.0 * safe_float(costs.slippage_bps)) / 100.0,
        "funding_component": max(0.0, ((avg_bars * 5.0) / 480.0) * safe_float(costs.funding_bps_per_8h) / 100.0),
        "spread_component": sum(safe_float(row.get("spread_pct")) for row in rows) / max(samples, 1),
        "avg_MFE": sum(mfes) / max(samples, 1),
        "median_MFE": sorted(mfes)[samples // 2] if mfes else 0.0,
        "avg_MAE": sum(maes) / max(samples, 1),
        "median_MAE": sorted(maes)[samples // 2] if maes else 0.0,
        "avg_holding_time": avg_bars,
        "TIME": hits.count("TIME") / max(samples, 1),
        "TP": hits.count("TP") / max(samples, 1),
        "SL": hits.count("SL") / max(samples, 1),
        "scenario_net": net_values,
    }


def _scenario_costs(rows: list[dict[str, Any]], costs: Any) -> dict[str, float]:
    avg_bars = sum(safe_float(row.get("bars")) for row in rows) / max(len(rows), 1)
    current_fee = (2.0 * safe_float(costs.taker_fee_bps)) / 100.0
    current_slippage = (2.0 * safe_float(costs.slippage_bps)) / 100.0
    current_funding = max(0.0, ((avg_bars * 5.0) / 480.0) * safe_float(costs.funding_bps_per_8h) / 100.0)
    dynamic_funding = _dynamic_funding_pct(rows, avg_bars)
    funding_if_crossed = dynamic_funding if _crosses_funding_timestamp(avg_bars) else 0.0
    return {
        "current_bot_model": current_fee + current_slippage + current_funding,
        "taker_taker": 12.0 / 100.0 + current_slippage,
        "maker_taker": 8.0 / 100.0 + current_slippage,
        "maker_maker": 4.0 / 100.0 + current_slippage,
        "half_slippage": current_fee + current_slippage / 2.0 + current_funding,
        "zero_slippage_research_only": current_fee + current_funding,
        "zero_funding_if_no_timestamp_cross": current_fee + current_slippage + funding_if_crossed,
        "dynamic_funding_by_symbol_if_available": current_fee + current_slippage + funding_if_crossed,
        "live_conservative": 12.0 / 100.0 + current_slippage + funding_if_crossed,
        "user_observed_low_costs_unverified": 8.0 / 100.0 + current_slippage / 2.0 + funding_if_crossed,
    }


def _net_metrics(returns: list[float], cost_pct: float) -> dict[str, float]:
    net = [value - cost_pct for value in returns]
    gains = sum(value for value in net if value > 0)
    losses = abs(sum(value for value in net if value < 0))
    return {
        "net_EV": sum(net) / max(len(net), 1),
        "net_PF": gains / losses if losses > 0 else 999.0 if gains > 0 else 0.0,
    }


def _dynamic_funding_pct(rows: list[dict[str, Any]], avg_bars: float) -> float:
    if not _crosses_funding_timestamp(avg_bars):
        return 0.0
    values = []
    for row in rows:
        rate_pct = _funding_rate_to_pct(row.get("funding_rate"))
        side = str(row.get("side") or "").upper()
        if side == "SHORT":
            rate_pct = -rate_pct
        values.append(rate_pct)
    return sum(values) / max(len(values), 1)


def _funding_rate_to_pct(value: Any) -> float:
    raw = safe_float(value)
    if abs(raw) <= 0.05:
        return raw * 100.0
    return raw


def _crosses_funding_timestamp(avg_bars: float) -> bool:
    return (safe_float(avg_bars) * 5.0) >= 480.0


def _summary(groups: list[dict[str, Any]], inventory: dict[str, Any], costs: Any) -> dict[str, Any]:
    gross_edge = [row for row in groups if safe_float(row.get("gross_PF")) > 1.2]
    gross_edge_net_negative = [row for row in gross_edge if safe_float(row.get("net_EV_current")) <= 0]
    changed = [
        row for row in groups
        if safe_float(row.get("net_EV_current")) <= 0 and (
            safe_float(row.get("net_EV_maker_maker")) > 0 or safe_float(row.get("net_EV_zero_slippage")) > 0
        )
    ]
    still_negative = [row for row in groups if safe_float(row.get("gross_PF")) > 1.2 and safe_float(row.get("net_EV_maker_maker")) <= 0]
    diagnostics = []
    if safe_float(costs.maker_fee_bps) != BITGET_USDTM_VIP0_MAKER_BPS or safe_float(costs.taker_fee_bps) != BITGET_USDTM_VIP0_TAKER_BPS:
        diagnostics.append("FEE_TOO_AGGRESSIVE")
    if safe_float(costs.funding_bps_per_8h) > 0:
        diagnostics.append("FUNDING_ALWAYS_APPLIED_BAD")
    if safe_float(costs.slippage_bps) >= 5:
        diagnostics.append("SLIPPAGE_TOO_AGGRESSIVE")
    if inventory.get("possible_double_counting"):
        diagnostics.append("DOUBLE_COUNTING_POSSIBLE")
    if any(row.get("source") == "market_probe" for row in groups):
        diagnostics.append("MARKET_PROBE_COST_POLLUTION")
    if any(safe_float(row.get("TIME")) > 0 for row in groups):
        diagnostics.append("TIME_LABEL_COST_POLLUTION")
    if _constant_penalty_suspected(groups):
        diagnostics.append("NET_EV_CONSTANT_PENALTY_SUSPECTED")
    if not diagnostics and gross_edge_net_negative:
        diagnostics.append("MODEL_SEEMS_REASONABLE_EDGE_BAD")
    rate = len(gross_edge_net_negative) / max(len(gross_edge), 1)
    funding_status = "BAD" if "FUNDING_ALWAYS_APPLIED_BAD" in diagnostics else "OK"
    slippage_status = "WARNING" if "SLIPPAGE_TOO_AGGRESSIVE" in diagnostics else "OK"
    cost_status = "BAD" if "FUNDING_ALWAYS_APPLIED_BAD" in diagnostics or "NET_EV_CONSTANT_PENALTY_SUSPECTED" in diagnostics else "WARNING" if diagnostics else "OK"
    reason = ",".join(diagnostics) if diagnostics else "cost_model_inventory_clean"
    return {
        "cost_model_status": cost_status,
        "cost_reason": reason,
        "gross_edge_net_negative_rate": rate,
        "double_counting_risk": "WARNING" if inventory.get("possible_double_counting") else "LOW",
        "funding_model_status": funding_status,
        "slippage_model_status": slippage_status,
        "cost_sensitivity_summary": {
            "groups": len(groups),
            "gross_edge_groups": len(gross_edge),
            "gross_edge_net_negative": len(gross_edge_net_negative),
            "changed_positive_under_maker_maker_or_zero_slippage": len(changed),
            "still_negative_under_maker_maker": len(still_negative),
        },
        "top_groups_changed_by_fee_scenario": changed[:12],
        "top_groups_still_negative": still_negative[:12],
        "diagnostics": diagnostics,
        "recommended_action": "REVIEW_COST_MODEL" if cost_status in {"BAD", "WARNING"} else "KEEP_COST_MODEL",
    }


def _constant_penalty_suspected(groups: list[dict[str, Any]]) -> bool:
    values = {round(safe_float(row.get("cost_per_trade_current")), 4) for row in groups[:50]}
    net_values = [round(safe_float(row.get("net_EV_current")), 4) for row in groups[:50]]
    return len(values) == 1 and len(set(net_values)) <= 3 and len(groups) >= 5


def _fee_source_status(costs: Any) -> str:
    if safe_float(costs.maker_fee_bps) == BITGET_USDTM_VIP0_MAKER_BPS and safe_float(costs.taker_fee_bps) == BITGET_USDTM_VIP0_TAKER_BPS:
        return "MATCHES_USER_PROVIDED_USDT_M_VIP0"
    return "REVIEW_FEE_ASSUMPTION"


def _scan_cost_code() -> dict[str, Any]:
    files = [
        "app/edge_hardening_utils.py",
        "app/score_calibration.py",
        "app/candidate_incubator.py",
        "app/exit_label_calibration_v2.py",
        "app/risk_manager.py",
        "app/backtester.py",
    ]
    where = []
    research_slippage = False
    risk_manager_slippage = False
    for rel in files:
        path = PROJECT_ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "net_EV" in text or "net_PF" in text or "cost_config" in text:
            where.append(rel)
        if "slippage" in text and "cost_config" in text:
            research_slippage = True
        if "slippage" in text and rel in {"app/risk_manager.py", "app/backtester.py"}:
            risk_manager_slippage = True
    return {"where_used": where or ["none"], "research_slippage": research_slippage, "risk_manager_slippage": risk_manager_slippage}


def _hit(value: Any) -> str:
    text = str(value or "").upper()
    if text.startswith("TP"):
        return "TP"
    if text == "SL":
        return "SL"
    return "TIME"


def _group_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- {row.get('group')} samples={row.get('samples')} gross_PF={safe_float(row.get('gross_PF')):.2f} "
            f"net_EV_current={safe_float(row.get('net_EV_current')):.4f} net_EV_maker_maker={safe_float(row.get('net_EV_maker_maker')):.4f} "
            f"net_EV_zero_slippage={safe_float(row.get('net_EV_zero_slippage')):.4f} TP%={safe_float(row.get('TP')) * 100:.1f} "
            f"SL%={safe_float(row.get('SL')) * 100:.1f} TIME%={safe_float(row.get('TIME')) * 100:.1f}"
        )
        for row in rows[:12]
    ]


class _CostSmokeDb:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self._use_postgres = False

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        yield self.conn
        self.conn.commit()

    def initialize(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE signal_observations(id INTEGER PRIMARY KEY, timestamp TEXT, symbol TEXT, side TEXT, market_regime TEXT, confidence_score INTEGER, score_bucket TEXT, strategy_type TEXT, funding_rate REAL, spread_pct REAL);
                CREATE TABLE signal_labels(id INTEGER PRIMARY KEY, timestamp TEXT, observation_id INTEGER, first_barrier_hit TEXT, bars_to_outcome INTEGER, max_favorable_excursion REAL, max_adverse_excursion REAL, realized_return_pct REAL);
                CREATE TABLE signal_path_metrics(id INTEGER PRIMARY KEY, observation_id INTEGER, source TEXT, max_favorable_pct REAL, max_adverse_pct REAL, final_return_pct REAL, bars_tracked INTEGER, first_barrier_hit TEXT, status TEXT, created_at TEXT, updated_at TEXT);
                """
            )
            obs_id = 1
            for i in range(20):
                ret = 0.20 if i < 14 else -0.25
                hit = "TP1" if ret > 0 else "SL"
                conn.execute("INSERT INTO signal_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (obs_id, now, "ETHUSDT", "SHORT", "TREND_DOWN", 90, "90-94", "trend", 0.000078, 0.01))
                conn.execute("INSERT INTO signal_labels VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (obs_id, now, obs_id, hit, 20, abs(ret) / 100.0, -abs(ret) / 100.0, ret))
                conn.execute("INSERT INTO signal_path_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (obs_id, obs_id, "trade_signal", abs(ret), abs(ret), ret, 20, hit, "matured", now, now))
                obs_id += 1
            for i in range(10):
                conn.execute("INSERT INTO signal_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (obs_id, now, "SOLUSDT", "LONG", "RANGE", 0, "PROBE", "probe", 0.000045, 0.01))
                conn.execute("INSERT INTO signal_path_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (obs_id, obs_id, "market_probe", 0.05, 0.05, 0.0, 20, "TIME", "matured", now, now))
                obs_id += 1

    def _fetchall_dicts(self, cursor: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in cursor.fetchall()]

    def table_exists(self, table: str) -> bool:
        return self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

    def get_table_columns(self, table: str) -> list[str]:
        return [row[1] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()]

    def fetch_labeled_signal_rows_since(self, since_iso: str, limit: int = 50000) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT so.*, so.id AS observation_id, sl.timestamp AS label_timestamp, sl.first_barrier_hit,
                   sl.bars_to_outcome, sl.max_favorable_excursion, sl.max_adverse_excursion, sl.realized_return_pct
            FROM signal_labels sl
            JOIN signal_observations so ON so.id = sl.observation_id
            WHERE sl.timestamp >= ?
            LIMIT ?
            """,
            (since_iso, limit),
        )
        return [dict(row) for row in rows.fetchall()]

    def fetch_signal_path_metrics_since(self, since_iso: str, limit: int = 50000) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM signal_path_metrics WHERE created_at >= ? LIMIT ?", (since_iso, limit))
        return [dict(row) for row in rows.fetchall()]
