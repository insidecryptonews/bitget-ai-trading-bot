from __future__ import annotations

from app.config import BotConfig
from app.exit_label_calibration_v2 import ExitLabelCalibrationV2


def _row(source: str, idx: int, *, mfe: float, mae: float, final_return: float, status: str = "matured") -> dict:
    return {
        "id": idx,
        "observation_id": idx,
        "source": source,
        "symbol": "XRPUSDT",
        "side": "LONG",
        "score": 88,
        "score_bucket": "80-89",
        "market_regime": "TREND_UP",
        "strategy": "breakout",
        "max_favorable_pct": mfe,
        "max_adverse_pct": mae,
        "final_return_pct": final_return,
        "bars_tracked": 30,
        "bars_to_mfe": 4,
        "bars_to_mae": 8,
        "first_barrier_hit": "TIME",
        "status": status,
    }


class FakeDb:
    def __init__(self, rows):
        self.rows = rows

    def fetch_signal_path_metrics_since(self, since, limit=50000):
        return list(self.rows)


def test_exit_calibration_separates_trade_signal_and_market_probe():
    rows = [
        *[_row("trade_signal", idx=i, mfe=0.45, mae=0.10, final_return=0.12) for i in range(1, 621)],
        *[_row("market_probe", idx=1000 + i, mfe=1.00, mae=0.05, final_return=0.20) for i in range(1, 621)],
    ]
    payload = ExitLabelCalibrationV2(BotConfig(), FakeDb(rows)).build(hours=24)
    sources = {row["source"]: row for row in payload["source_comparison"]}
    assert "trade_signal" in sources
    assert sources["market_probe"]["decision"] == "DO_NOT_USE_PROBES_FOR_POLICY"
    assert payload["best_trade_signal_shadow_exits"]


def test_market_probe_never_actionable_even_with_positive_edge():
    rows = [_row("market_probe", idx=i, mfe=1.00, mae=0.05, final_return=0.25) for i in range(1, 700)]
    payload = ExitLabelCalibrationV2(BotConfig(), FakeDb(rows)).build(hours=24)
    assert all(row["decision"] == "DO_NOT_USE_PROBES_FOR_POLICY" for row in payload["source_comparison"])
    assert not payload["best_trade_signal_shadow_exits"]


def test_net_ev_negative_rejects_trade_signal_candidate():
    rows = [_row("trade_signal", idx=i, mfe=0.05, mae=0.80, final_return=-0.20) for i in range(1, 700)]
    payload = ExitLabelCalibrationV2(BotConfig(), FakeDb(rows)).build(hours=24)
    assert any(row["decision"] == "REJECT" and row["reason"] in {"net_ev_not_positive", "net_pf_below_min"} for row in payload["rejected_exit_policies"])


def test_small_sample_is_need_more_data_or_watch_only():
    rows = [_row("trade_signal", idx=i, mfe=0.45, mae=0.10, final_return=0.12) for i in range(1, 100)]
    payload = ExitLabelCalibrationV2(BotConfig(), FakeDb(rows)).build(hours=24)
    decisions = {row["decision"] for row in payload["watch_only_exit_policies"]}
    assert decisions & {"NEED_MORE_DATA", "WATCH_ONLY"}


def test_time_high_blocks_even_if_some_tp_improves():
    rows = [_row("trade_signal", idx=i, mfe=0.05, mae=0.02, final_return=0.01) for i in range(1, 700)]
    payload = ExitLabelCalibrationV2(BotConfig(), FakeDb(rows)).build(hours=24)
    assert all(row["decision"] != "SHADOW_EXIT_CANDIDATE" for row in payload["best_trade_signal_shadow_exits"])
