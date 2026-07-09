from __future__ import annotations

from pathlib import Path

from app.labs import alpha_factory_v10_44 as AF
from app.labs import shadow_simulation_tournament_v10_40 as SH


def _bars(n: int = 260) -> list[dict]:
    out = []
    price = 100.0
    ts0 = 1_700_000_000_000
    for i in range(n):
        drift = 0.0009 if (i // 35) % 2 == 0 else -0.0007
        pulse = 0.0025 if i % 47 == 0 else (-0.002 if i % 53 == 0 else 0.0)
        open_ = price
        close = price * (1 + drift + pulse)
        high = max(open_, close) * 1.0015
        low = min(open_, close) * 0.9985
        buy = 10.0 + (7.0 if close >= open_ else 1.0)
        sell = 10.0 + (7.0 if close < open_ else 1.0)
        out.append({
            "symbol": "BTCUSDT",
            "ts": ts0 + (i + 1) * 60_000,
            "available_at": ts0 + (i + 1) * 60_000,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": buy + sell,
            "buy_volume": buy,
            "sell_volume": sell,
            "n_trades": 20 + (i % 9),
            "trade_count": 20 + (i % 9),
            "max_trade": 2.5 + (i % 5),
        })
        price = close
    return out


def test_alpha_features_are_prefix_only_under_future_mutation():
    bars = _bars()
    before = AF.build_alpha_features(bars)
    mutated = [dict(b) for b in bars]
    for b in mutated[80:]:
        b["close"] *= 1.5
        b["high"] *= 1.5
        b["low"] *= 1.5
    after = AF.build_alpha_features(mutated)

    for key in ("ret_5m_prefix", "trend_score", "flow_imbalance_10", "range_position_20"):
        assert before[60][key] == after[60][key]
    assert before[60]["available_at"] == before[60]["ts"]


def test_same_bar_tp_and_sl_uses_stop_before_tp():
    bars = _bars(5)
    bars[2]["open"] = 100.0
    bars[2]["high"] = 102.0
    bars[2]["low"] = 98.0
    bars[2]["close"] = 101.0

    out = SH.simulate_trade(
        bars, 1, "long", tp_pct=0.01, sl_pct=0.01,
        time_bars=3, trailing_pct=None, entry_mode="next_open")

    assert out is not None
    assert out["exit_reason"] == "SL"
    assert out["net_return"] < 0


def test_alpha_factory_runs_research_only_with_injected_bars(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(AF.CE, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(AF.LAB, "_load_bars", lambda symbol, data_source: (_bars(), data_source, {"test": True}))

    report = AF.run_alpha_factory(
        symbols="BTCUSDT", data_source="ws_persistent",
        max_runtime_minutes=1, write_reports=True, max_candidates=20)

    assert report["research_only"] is True
    assert report["can_send_real_orders"] is False
    assert report["paper_filter_enabled"] is False
    assert report["final_recommendation"] == "NO LIVE"
    assert report["strategies_tested"] > 0
    assert report["best_candidate"]["entry_timing"] == "signal_bar_close_then_next_open"
    assert report["best_candidate"]["features_ex_ante_only"] is True
    assert (tmp_path / "reports" / "research" / "v10_44_alpha_sprint" / "alpha_factory_v10_44.json").is_file()


def test_cost_stress_failure_blocks_paper_candidate():
    ok = {"valid_trades": 80, "net_EV": 0.003, "profit_factor": 1.4,
          "net_EV_lower_bound": 0.001, "max_drawdown": -0.01}
    stress = {"base": ok, "stress_0_25": {**ok, "net_EV": -0.001}}

    status, blockers = AF._classify(ok, ok, ok, stress, baseline_lb=0.0, n_tests=120)

    assert status != "PAPER_CANDIDATE_RESEARCH_ONLY"
    assert "stress_0_25_net_ev_fail" in blockers


def test_alpha_factory_source_has_no_trading_side_effect_calls():
    source = Path(AF.__file__).read_text(encoding="utf-8")
    forbidden = [
        "place_order", "private_get", "private_post", "set_leverage",
        "set_margin_mode", "ExecutionEngine.execute", "PaperTrader.open_position",
        "LIVE_TRADING=True", "ENABLE_PAPER_POLICY_FILTER=True",
        "can_send_real_orders=True",
    ]
    for token in forbidden:
        assert token not in source

