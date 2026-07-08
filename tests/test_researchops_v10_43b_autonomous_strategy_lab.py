"""V10.43B Autonomous Strategy Lab: honest auto-reject, no win-rate selection."""

from __future__ import annotations

import random

from app.labs import autonomous_strategy_lab_v10_43b as LAB

T0 = 1_700_000_000_000
BAR = 60_000


def noise_bars(n, seed=1):
    rng = random.Random(seed)
    price, bars = 100.0, []
    for i in range(n):
        drift = rng.uniform(-0.0012, 0.0012)
        new = price * (1 + drift)
        ntr = int(rng.uniform(15, 35) + rng.uniform(15, 35))
        bars.append({"ts": T0 + i * BAR, "bar_close_ts": T0 + i * BAR,
                     "available_at": T0 + i * BAR, "open": price,
                     "high": max(price, new) * 1.0005, "low": min(price, new) * 0.9995,
                     "close": new, "volume": ntr, "buy_volume": ntr * 0.5,
                     "sell_volume": ntr * 0.5, "n_trades": ntr, "max_trade": ntr / 5,
                     "symbol": "BTCUSDT"})
        price = new
    return bars


def test_no_data_is_insufficient_sample(monkeypatch):
    monkeypatch.setattr(LAB, "_load_bars", lambda s, ds: ([], "ws", {}))
    r = LAB.run_lab("BTCUSDT", data_source="ws", write_reports=False)
    assert r["verdict"] in ("NO_WS_DATA", "INSUFFICIENT_SAMPLE")
    assert r["can_send_real_orders"] is False


def test_noise_generates_candidates_all_rejected(monkeypatch):
    monkeypatch.setattr(LAB, "_load_bars", lambda s, ds: (noise_bars(600, 3), "ws", {}))
    r = LAB.run_lab("BTCUSDT", data_source="ws", write_reports=False)
    assert r["candidates_generated"] >= 12
    # nothing should be promoted on pure noise
    assert r["watchlist_or_better"] == 0
    assert r["verdict"] == "NO_EDGE_ALL_REJECTED"
    for c in r.get("top_rejection_reasons", []):
        assert "reason" in c


def test_candidates_have_required_fields_and_valid_verdicts(monkeypatch):
    monkeypatch.setattr(LAB, "_load_bars", lambda s, ds: (noise_bars(600, 5), "ws", {}))
    r = LAB.run_lab("BTCUSDT", data_source="ws", write_reports=False)
    best = r["best"]
    for k in ("strategy_name", "family", "side", "sample_size", "net_EV",
              "net_EV_lower_bound", "profit_factor", "win_rate", "max_drawdown",
              "cost_sensitivity", "slippage_stress_lb", "baseline_comparison",
              "verdict", "rejection_reason"):
        assert k in best, k
    # every verdict must be from the allowed set
    # (run_lab returns summary; verify via reports would need write; check best)
    assert best["verdict"] in LAB.VERDICTS


def test_ranking_is_by_lower_bound_not_win_rate(monkeypatch):
    monkeypatch.setattr(LAB, "_load_bars", lambda s, ds: (noise_bars(600, 7), "ws", {}))
    # patch write to capture the full candidate list order
    captured = {}
    orig = LAB._write
    monkeypatch.setattr(LAB, "_write",
                        lambda summ, cands, prom, cnts, lead: captured.update(cands=cands))
    LAB.run_lab("BTCUSDT", data_source="ws", write_reports=True)
    cands = captured["cands"]
    lbs = [c["net_EV_lower_bound"] for c in cands if c["net_EV_lower_bound"] is not None]
    assert lbs == sorted(lbs, reverse=True)        # sorted by lower bound desc
    LAB._write = orig


def test_slippage_and_cost_sensitivity_measured(monkeypatch):
    monkeypatch.setattr(LAB, "_load_bars", lambda s, ds: (noise_bars(600, 9), "ws", {}))
    captured = {}
    monkeypatch.setattr(LAB, "_write",
                        lambda summ, cands, prom, cnts, lead: captured.update(cands=cands))
    LAB.run_lab("BTCUSDT", data_source="ws", write_reports=True)
    # at least one candidate with enough sample carries a cost_sensitivity value
    scored = [c for c in captured["cands"] if c["net_EV"] is not None]
    assert scored
    assert any(c["cost_sensitivity"] is not None for c in scored)


def test_strategy_names_reproducible(monkeypatch):
    monkeypatch.setattr(LAB, "_load_bars", lambda s, ds: (noise_bars(600, 3), "ws", {}))
    a = LAB.run_lab("BTCUSDT", data_source="ws", write_reports=False)
    b = LAB.run_lab("BTCUSDT", data_source="ws", write_reports=False)
    assert a["best"]["strategy_name"] == b["best"]["strategy_name"]


def test_ws_tournament_no_ws_data(monkeypatch):
    monkeypatch.setattr(LAB.WS, "load_ws_bars", lambda *a, **k: {"bars": [], "meta": {}})
    r = LAB.run_ws_tournament("BTCUSDT", write_reports=False)
    assert r["verdict"] == "NO_WS_DATA" and r["can_send_real_orders"] is False


def test_ws_tournament_small_sample_insufficient(monkeypatch):
    monkeypatch.setattr(LAB, "MIN_OOS", 20)
    monkeypatch.setattr(LAB.WS, "load_ws_bars",
                        lambda *a, **k: {"bars": noise_bars(120, 3), "meta": {}})
    monkeypatch.setattr(LAB.WS, "ws_forward_dataset_view",
                        lambda *a, **k: {"verdict": "TOO_GAPPY", "reliability": "NOT_RELIABLE_GAPS",
                                         "max_contiguous_run": 120})
    r = LAB.run_ws_tournament("BTCUSDT", write_reports=False)
    assert r["verdict"] in LAB.WS_TOUR_VERDICTS
    assert r["micro_live_ready"] is False


def test_lead_lag_no_invented_correlations(monkeypatch):
    monkeypatch.setattr(LAB, "_load_bars", lambda s, ds: (noise_bars(600, 3), "ws", {}))
    captured = {}
    monkeypatch.setattr(LAB, "_write",
                        lambda summ, cands, prom, cnts, lead: captured.update(lead=lead))
    LAB.run_lab("BTCUSDT", data_source="ws", write_reports=True)
    lead = captured["lead"]
    assert lead["multi_symbol_lead_lag"].startswith("WAITING_DATA")
    assert lead["final_recommendation"] == "NO LIVE"
