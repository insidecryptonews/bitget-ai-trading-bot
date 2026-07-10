from __future__ import annotations

import json
from pathlib import Path

from app.labs import alpha_factory_v10_44 as AF
from app.labs import exit_factory_v10_44 as EF


def _bars(n: int = 260) -> list[dict]:
    price = 100.0
    ts0 = 1_700_100_000_000
    rows = []
    for i in range(n):
        open_ = price
        drift = 0.001 if (i % 40) < 25 else -0.0006
        close = price * (1 + drift)
        rows.append({
            "symbol": "BTCUSDT",
            "ts": ts0 + (i + 1) * 60_000,
            "available_at": ts0 + (i + 1) * 60_000,
            "open": open_, "close": close,
            "high": max(open_, close) * 1.001,
            "low": min(open_, close) * 0.999,
            "volume": 25.0, "buy_volume": 15.0 if close >= open_ else 8.0,
            "sell_volume": 10.0 if close >= open_ else 17.0,
            "n_trades": 30, "trade_count": 30, "max_trade": 3.0,
        })
        price = close
    return rows


def _alpha_payload() -> dict:
    return {
        "tool_version": "v10.44",
        "top_candidates": [{
            "candidate_id": "v1044_BTCUSDT_trend_breakout_long_scalp",
            "symbol": "BTCUSDT",
            "strategy_name": "trend_breakout_long",
            "side": "LONG",
            "status": "WATCH_ONLY",
            "metrics_test": {"net_EV": 0.001},
            "research_only": True,
            "final_recommendation": "NO LIVE",
        }],
        "final_recommendation": "NO LIVE",
    }


def test_exit_factory_replays_alpha_candidate_research_only(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(AF.CE, "_repo_root", lambda: tmp_path)
    out = tmp_path / "reports" / "research" / "v10_44_alpha_sprint"
    out.mkdir(parents=True)
    (out / "alpha_factory_v10_44.json").write_text(json.dumps(_alpha_payload()), encoding="utf-8")
    monkeypatch.setattr(EF.LAB, "_load_bars", lambda symbol, data_source: (_bars(), data_source, {}))

    report = EF.run_exit_factory(symbols="BTCUSDT", data_source="ws_persistent", write_reports=True)

    assert report["research_only"] is True
    assert report["can_send_real_orders"] is False
    assert report["paper_filter_enabled"] is False
    assert report["variants_tested"] > 0
    assert report["best_exit"]["entry_timing"] == "next_open"
    assert report["best_exit"]["same_bar_policy"] == "STOP_BEFORE_TP"
    assert (out / "exit_factory_v10_44.json").is_file()


def test_exit_factory_cli_renderer_keeps_no_live():
    text = EF.render_cli({
        "overall_verdict": "NO_EXIT_EDGE_ALL_REJECTED",
        "variants_tested": 0,
        "best_exit": None,
        "reports_dir": "reports/research/v10_44_alpha_sprint",
    })

    assert "research_only: true" in text
    assert "can_send_real_orders: false" in text
    assert "final_recommendation: NO LIVE" in text


def test_exit_factory_source_has_no_trading_side_effect_calls():
    source = Path(EF.__file__).read_text(encoding="utf-8")
    for token in ("place_order", "private_get", "private_post", "set_leverage",
                  "set_margin_mode", "ExecutionEngine.execute",
                  "PaperTrader.open_position", "LIVE_TRADING=True"):
        assert token not in source
