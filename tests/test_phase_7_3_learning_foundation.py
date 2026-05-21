"""Tests for Phase 7.3 Learning Foundation (A/B/C/D) and 7.4 base.

Covers:
- Safety invariants: no real orders, no paper filter activation, no private endpoints
- Measurement parity: STOP_BEFORE_TP, net vs gross, OHLCV vs last_price
- Setup key correctness
- Signal Outcome Classifier (taken / rejected / no_trade)
- Candidate Shadow Monitor BNB rule + persistence + evaluation
- Quick Profit Exit Lab simulation
- Momentum Burst Lab feature lookahead + detection
- Candidate Incubator V2 promotion gates with BNB → SHADOW_CANDIDATE not PAPER
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.candidate_incubator_v2 import (
    GatesConfig,
    REC_PAPER_CANDIDATE_BLOCKED,
    REC_SHADOW_CANDIDATE,
    REC_WATCH,
    build_incubator_result,
    render_result_text,
)
from app.candidate_shadow_monitor import (
    CandidateShadowMonitor,
    DEFAULT_RULES,
    render_summary_text as render_shadow_text,
)
from app.config import load_config
from app.database import Database
from app.momentum_burst_lab import (
    BurstParams,
    add_burst_features,
    backtest_burst,
    detect_long_burst,
    detect_short_burst,
)
from app.outcome_engine import (
    EXIT_HORIZON_CLOSE,
    EXIT_QUICK_PROFIT,
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
    OutcomeResult,
    compare_outcomes,
    simulate_outcome_last_price,
    simulate_outcome_ohlcv,
)
from app.quick_profit_exit_lab import (
    BASELINE,
    DEFAULT_POLICIES,
    QuickProfitPolicy,
    build_trade_inputs_from_dataframe,
    render_lab_text,
    run_lab,
)
from app.setup_key import (
    build_setup_key,
    score_bucket,
    setup_key_from_observation,
)
from app.signal_outcome_classifier import (
    OUTCOME_CLEAN_LOSS,
    OUTCOME_CLEAN_WIN,
    OUTCOME_FEE_TOXIC,
    OUTCOME_MISSED_WINNER,
    OUTCOME_NO_TRADE_OK,
    classify_batch,
    classify_observation,
    summarize,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> Database:
    monkeypatch.setattr("app.database.PROJECT_ROOT", tmp_path)
    cfg = load_config()
    instance = Database(cfg, logging.getLogger("test"))
    instance.sqlite_path = tmp_path / "test.db"
    instance.initialize()
    return instance


@pytest.fixture()
def cfg_shadow_enabled():
    base = load_config()

    class _Cfg:
        pass

    cfg = _Cfg()
    for key in dir(base):
        if key.startswith("_"):
            continue
        try:
            setattr(cfg, key, getattr(base, key))
        except Exception:
            pass
    cfg.enable_candidate_shadow_monitor = True
    return cfg


def _candle_frame_with_uptrend(n: int = 80, base: float = 100.0) -> pd.DataFrame:
    rows = []
    price = base
    for i in range(n):
        ret = 0.001 + (0.005 if 20 <= i <= 24 else 0.0)
        open_p = price
        close = price * (1 + ret)
        high = max(open_p, close) * 1.0005
        low = min(open_p, close) * 0.9995
        rows.append({
            "timestamp": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=5 * i),
            "open": open_p, "high": high, "low": low, "close": close,
            "volume": 1000.0 + (3500 if 20 <= i <= 24 else 0),
            "quote_volume": close * 1000.0,
        })
        price = close
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 1. Safety invariants
# ---------------------------------------------------------------------------


def test_default_config_does_not_send_real_orders():
    cfg = load_config()
    assert cfg.paper_trading is True
    assert cfg.live_trading is False
    assert cfg.dry_run is True
    assert cfg.enable_paper_policy_filter is False
    assert cfg.can_send_real_orders is False
    assert cfg.enable_candidate_shadow_monitor is False


def test_shadow_monitor_disabled_by_default_does_not_register(db):
    cfg = load_config()
    monitor = CandidateShadowMonitor(cfg, db)
    assert monitor.enabled is False
    rid = monitor.register_signal(
        observation_id=1, symbol="BNBUSDT", side="LONG", regime="RISK_ON",
        score=85, timeframe="5m", strategy="BREAKOUT",
        entry_price=300, stop_loss=295, take_profit_1=310, take_profit_2=315,
    )
    assert rid == 0
    assert db.count_shadow_candidates() == 0


def test_shadow_monitor_never_calls_bitget_or_paper_filter(cfg_shadow_enabled, db):
    """Sanity: the monitor module has no place_order/private/paper_filter code path."""
    import inspect
    import app.candidate_shadow_monitor as mod
    source = inspect.getsource(mod)
    forbidden = ["place_order", "private_post", "private_get", "set_leverage", "set_margin_mode", "paper_filter_enabled = True", "ENABLE_PAPER_POLICY_FILTER=True"]
    for needle in forbidden:
        assert needle not in source, f"Forbidden token found in candidate_shadow_monitor.py: {needle}"


def test_signal_outcome_classifier_has_no_runtime_side_effects():
    import inspect
    import app.signal_outcome_classifier as mod
    source = inspect.getsource(mod)
    forbidden = ["place_order", "private_post", "private_get", "place_tpsl_order"]
    for needle in forbidden:
        assert needle not in source


# ---------------------------------------------------------------------------
# 2. Measurement parity: STOP_BEFORE_TP, net vs gross
# ---------------------------------------------------------------------------


def test_stop_before_tp_same_bar_rule_applies_worst_case():
    # One bar where high >= tp AND low <= stop. Worst case: stop fires.
    candles = pd.DataFrame([
        {"timestamp": pd.Timestamp("2026-01-01", tz="UTC"),
         "open": 100.0, "high": 103.0, "low": 97.0, "close": 100.0, "volume": 1000.0},
    ])
    outcome = simulate_outcome_ohlcv(
        side="LONG", entry_price=100.0, stop_loss=98.0, take_profit=102.0,
        candles=candles, max_holding_bars=5,
    )
    assert outcome.exit_reason == EXIT_STOP_LOSS
    assert outcome.same_bar_stop_tp_applied is True


def test_net_return_is_gross_minus_total_cost_bps():
    candles = pd.DataFrame([
        {"timestamp": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=5 * i),
         "open": 100.0 + i * 0.1, "high": 100.5 + i * 0.1, "low": 99.9 + i * 0.1,
         "close": 100.3 + i * 0.1, "volume": 1000.0}
        for i in range(10)
    ])
    outcome = simulate_outcome_ohlcv(
        side="LONG", entry_price=100.0, stop_loss=98.0, take_profit=200.0,
        candles=candles, max_holding_bars=5,
    )
    # HORIZON_CLOSE: gross_return - total_cost_bps/100 = net
    assert outcome.exit_reason == EXIT_HORIZON_CLOSE
    delta = outcome.gross_return_pct - outcome.total_cost_bps / 100.0
    assert abs(delta - outcome.net_return_pct) < 1e-6


def test_last_price_simulator_can_miss_intrabar_stop():
    """A wick on bar 0 that paper-style 'last price' stream does not see."""
    candles = pd.DataFrame([
        {"timestamp": pd.Timestamp("2026-01-01", tz="UTC"),
         "open": 100.0, "high": 100.5, "low": 97.0, "close": 99.8, "volume": 1000.0},
        {"timestamp": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=5),
         "open": 99.8, "high": 100.2, "low": 99.5, "close": 100.0, "volume": 1000.0},
    ])
    ohlcv_outcome = simulate_outcome_ohlcv(
        side="LONG", entry_price=100.0, stop_loss=98.0, take_profit=110.0,
        candles=candles, max_holding_bars=5,
    )
    # last-price stream only sees the closes
    last_outcome = simulate_outcome_last_price(
        side="LONG", entry_price=100.0, stop_loss=98.0, take_profit=110.0,
        last_prices=[99.8, 100.0], max_holding_bars=5,
    )
    assert ohlcv_outcome.exit_reason == EXIT_STOP_LOSS
    assert last_outcome.exit_reason == EXIT_HORIZON_CLOSE
    comparison = compare_outcomes(ohlcv_outcome, last_outcome)
    assert comparison["ohlcv_captures_wick"] is True
    assert comparison["research_only"] is True


def test_outcome_engine_rejects_invalid_side():
    with pytest.raises(ValueError):
        simulate_outcome_ohlcv(side="WRONG", entry_price=100.0, stop_loss=98.0,
                               take_profit=102.0, candles=pd.DataFrame([{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1.0}]))


# ---------------------------------------------------------------------------
# 3. Setup key
# ---------------------------------------------------------------------------


def test_setup_key_includes_all_dimensions():
    key = build_setup_key(
        symbol="bnbusdt", side="long", regime="risk_on", score=85,
        timeframe="5M", strategy="breakout", exit_policy="current_exit",
        source="trade_signal",
    )
    assert key.symbol == "BNBUSDT"
    assert key.side == "LONG"
    assert key.regime == "RISK_ON"
    assert key.score_bucket == "85-89"
    assert key.timeframe == "5m"
    assert key.strategy == "BREAKOUT"
    assert key.exit_policy == "current_exit"
    assert key.source == "trade_signal"
    text = key.as_string()
    for piece in ("BNBUSDT", "LONG", "RISK_ON", "85-89", "5m", "BREAKOUT", "current_exit", "trade_signal"):
        assert piece in text


def test_score_bucket_mapping():
    assert score_bucket(0) == "0-49"
    assert score_bucket(69) == "50-69"
    assert score_bucket(70) == "70-74"
    assert score_bucket(85) == "85-89"
    assert score_bucket(100) == "95-100"
    assert score_bucket(None) == "NA"


def test_setup_key_from_observation():
    obs = {
        "symbol": "BNBUSDT", "side": "LONG", "market_regime": "RISK_ON",
        "confidence_score": 82, "strategy_type": "BREAKOUT",
    }
    key = setup_key_from_observation(obs, timeframe="5m")
    assert key.score_bucket == "80-84"


# ---------------------------------------------------------------------------
# 4. Signal Outcome Classifier
# ---------------------------------------------------------------------------


def _observation(observation_id: int = 1, side: str = "LONG", score: int = 85, operated: int = 1, regime: str = "RISK_ON") -> dict:
    return {
        "id": observation_id, "symbol": "BNBUSDT", "side": side,
        "market_regime": regime, "confidence_score": score,
        "timeframe": "5m", "strategy_type": "BREAKOUT",
        "entry_price": 100.0, "stop_loss": 99.0,
        "take_profit_1": 102.0, "take_profit_2": 103.5,
        "operated": operated, "source": "trade_signal",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }


def _label(observation_id: int = 1, realized_frac: float = 0.012, mfe: float = 0.012, mae: float = -0.003, barrier: str = "TP1", label: int = 1) -> dict:
    return {
        "observation_id": observation_id,
        "label": label,
        "first_barrier_hit": barrier,
        "bars_to_outcome": 6,
        "max_favorable_excursion": mfe,
        "max_adverse_excursion": mae,
        "realized_return_pct": realized_frac,
        "timestamp": "2026-01-01T00:30:00+00:00",
    }


def test_classifier_clean_win_when_tp_hit_with_net_positive():
    outcome = classify_observation(_observation(1), _label(1))
    assert outcome.outcome_class == OUTCOME_CLEAN_WIN
    assert outcome.net_return_pct > 0


def test_classifier_clean_loss_when_sl_hit():
    outcome = classify_observation(
        _observation(2),
        _label(2, realized_frac=-0.008, mfe=0.001, mae=-0.008, barrier="SL", label=-1),
    )
    assert outcome.outcome_class == OUTCOME_CLEAN_LOSS


def test_classifier_fee_toxic_when_gross_positive_net_negative():
    outcome = classify_observation(
        _observation(3),
        _label(3, realized_frac=0.001, mfe=0.001, barrier="TIME", label=0),
    )
    assert outcome.outcome_class == OUTCOME_FEE_TOXIC
    assert outcome.realized_return_pct > 0
    assert outcome.net_return_pct < 0


def test_classifier_no_trade_ok():
    obs = _observation(4, side="NO_TRADE", operated=0)
    outcome = classify_observation(obs)
    assert outcome.outcome_class == OUTCOME_NO_TRADE_OK


def test_classifier_missed_winner_for_rejected_signal_with_positive_hypothetical_label():
    obs = _observation(5, operated=0)
    outcome = classify_observation(obs, _label(5))
    assert outcome.outcome_class == OUTCOME_MISSED_WINNER


def test_classifier_batch_and_summary_count_classes():
    obs1 = _observation(10)
    obs2 = _observation(11, operated=0)
    labels = {10: _label(10), 11: _label(11)}
    results = classify_batch([obs1, obs2], labels)
    summary = summarize(results)
    assert summary["total"] == 2
    assert summary["by_class"].get(OUTCOME_CLEAN_WIN, 0) == 1
    assert summary["by_class"].get(OUTCOME_MISSED_WINNER, 0) == 1


# ---------------------------------------------------------------------------
# 5. Candidate Shadow Monitor — BNB only
# ---------------------------------------------------------------------------


def test_shadow_registers_bnb_long_risk_on_score_ge_80(cfg_shadow_enabled, db):
    monitor = CandidateShadowMonitor(cfg_shadow_enabled, db)
    rid = monitor.register_signal(
        observation_id=1, symbol="BNBUSDT", side="LONG", regime="RISK_ON",
        score=82, timeframe="5m", strategy="BREAKOUT",
        entry_price=300, stop_loss=295, take_profit_1=310, take_profit_2=315,
    )
    assert rid > 0
    rows = db.fetch_shadow_candidates()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BNBUSDT"
    assert rows[0]["status"] == "PENDING"


def test_shadow_does_not_register_bnb_score_below_80(cfg_shadow_enabled, db):
    monitor = CandidateShadowMonitor(cfg_shadow_enabled, db)
    rid = monitor.register_signal(
        observation_id=1, symbol="BNBUSDT", side="LONG", regime="RISK_ON",
        score=75, timeframe="5m", strategy="BREAKOUT",
        entry_price=300, stop_loss=295, take_profit_1=310, take_profit_2=315,
    )
    assert rid == 0


def test_shadow_does_not_register_bnb_short(cfg_shadow_enabled, db):
    monitor = CandidateShadowMonitor(cfg_shadow_enabled, db)
    rid = monitor.register_signal(
        observation_id=1, symbol="BNBUSDT", side="SHORT", regime="RISK_ON",
        score=85, timeframe="5m", strategy="BREAKOUT",
        entry_price=300, stop_loss=305, take_profit_1=290, take_profit_2=285,
    )
    assert rid == 0


def test_shadow_does_not_register_non_bnb(cfg_shadow_enabled, db):
    monitor = CandidateShadowMonitor(cfg_shadow_enabled, db)
    rid = monitor.register_signal(
        observation_id=1, symbol="BTCUSDT", side="LONG", regime="RISK_ON",
        score=85, timeframe="5m", strategy="BREAKOUT",
        entry_price=50000, stop_loss=49500, take_profit_1=50800, take_profit_2=51200,
    )
    assert rid == 0


def test_shadow_does_not_register_non_risk_on(cfg_shadow_enabled, db):
    monitor = CandidateShadowMonitor(cfg_shadow_enabled, db)
    for regime in ("TREND_UP", "TREND_DOWN", "CHOPPY_MARKET", "RANGE", "RISK_OFF"):
        rid = monitor.register_signal(
            observation_id=1, symbol="BNBUSDT", side="LONG", regime=regime,
            score=85, timeframe="5m", strategy="BREAKOUT",
            entry_price=300, stop_loss=295, take_profit_1=310, take_profit_2=315,
        )
        assert rid == 0, f"unexpected register in regime={regime}"


def test_shadow_summary_text_includes_no_live(cfg_shadow_enabled, db):
    monitor = CandidateShadowMonitor(cfg_shadow_enabled, db)
    summary = monitor.summary(hours=24)
    text = render_shadow_text(summary)
    assert "NO LIVE" in text
    assert "paper_filter_enabled: false" in text


# ---------------------------------------------------------------------------
# 6. Quick Profit Exit Lab
# ---------------------------------------------------------------------------


def test_quick_profit_lab_does_not_affect_runtime():
    df = _candle_frame_with_uptrend()
    inputs = build_trade_inputs_from_dataframe(
        df, side="LONG", entry_indices=[10], stop_pct=0.8, tp_pct=2.0,
    )
    result = run_lab(inputs)
    names = [s.policy for s in result.summaries]
    assert "baseline" in names
    assert "quick_profit_040" in names
    assert result.research_only is True
    assert result.final_recommendation == "NO LIVE"


def test_quick_profit_policy_can_cut_too_early_vs_baseline():
    df = _candle_frame_with_uptrend()
    inputs = build_trade_inputs_from_dataframe(
        df, side="LONG", entry_indices=[10], stop_pct=0.8, tp_pct=2.0,
    )
    result = run_lab(inputs)
    baseline = next(s for s in result.summaries if s.policy == "baseline")
    qp040 = next(s for s in result.summaries if s.policy == "quick_profit_040")
    assert baseline.gross_ev_pct >= qp040.gross_ev_pct
    # Note: cut_too_early count comes from comparing this policy to baseline on
    # the trade-by-trade basis. With strongly trending synthetic data it should
    # register at least one cut.
    assert qp040.count_cut_too_early >= 1


def test_quick_profit_expected_move_to_cost_ratio_computable():
    df = _candle_frame_with_uptrend()
    inputs = build_trade_inputs_from_dataframe(
        df, side="LONG", entry_indices=[5], stop_pct=0.6, tp_pct=0.40,
    )
    # tp=0.40% with cost ~0.18% gives ratio ~2.22 — slightly below 3x floor.
    cost = 0.18
    ratio = 0.40 / cost
    assert pytest.approx(ratio, abs=0.01) == 2.22
    result = run_lab(inputs)
    assert result.summaries  # ran


# ---------------------------------------------------------------------------
# 7. Momentum Burst Lab
# ---------------------------------------------------------------------------


def test_burst_features_no_lookahead():
    """Features at index 50 must be identical between full DF and DF[:51]."""
    np.random.seed(7)
    rows = []
    price = 100.0
    for i in range(80):
        ret = np.random.randn() * 0.001
        if 30 <= i <= 34:
            ret = 0.008
        open_p = price
        close = price * (1 + ret)
        high = max(open_p, close) * 1.0005
        low = min(open_p, close) * 0.9995
        rows.append({"timestamp": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=i),
                     "open": open_p, "high": high, "low": low, "close": close,
                     "volume": 3500 if 30 <= i <= 34 else 1000})
        price = close
    df = pd.DataFrame(rows)
    full = add_burst_features(df, bar_minutes=1)
    prefix = add_burst_features(df.iloc[:51].copy(), bar_minutes=1)
    for col in ("return_5m", "acceleration", "relative_volume", "distance_to_ema_21", "normalized_atr"):
        a, b = full.iloc[50][col], prefix.iloc[50][col]
        if pd.notna(a) and pd.notna(b):
            assert abs(float(a) - float(b)) < 1e-9, f"lookahead suspect on {col}"


def test_burst_long_detects_accelerating_pump():
    np.random.seed(1)
    rows = []
    price = 100.0
    bursts = {25: 0.004, 26: 0.006, 27: 0.008, 28: 0.010, 29: 0.012}
    for i in range(60):
        ret = bursts.get(i, 0.0)
        open_p = price
        close = price * (1 + ret)
        high = max(open_p, close) * 1.0005
        low = min(open_p, close) * 0.9995
        rows.append({"timestamp": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=i),
                     "open": open_p, "high": high, "low": low, "close": close,
                     "volume": 3500 if i in bursts else 1000})
        price = close
    feat = add_burst_features(pd.DataFrame(rows), bar_minutes=1)
    params = BurstParams(
        min_return_5m_pct=1.5,
        min_acceleration_pct=0.1,
        min_relative_volume=2.0,
        max_distance_ema_21_pct=5.0,
    )
    signals = detect_long_burst(feat, params=params, suggested_tp_pct=1.5)
    assert signals, "expected at least one long burst signal"
    for sig in signals:
        assert sig.expected_move_to_cost_ratio >= params.min_expected_move_to_cost_ratio


def test_burst_blocks_fee_toxic_expected_move():
    """If suggested_tp_pct / cost_pct < 3, the detector should reject."""
    np.random.seed(2)
    rows = []
    price = 100.0
    bursts = {25: 0.004, 26: 0.006, 27: 0.008, 28: 0.010, 29: 0.012}
    for i in range(60):
        ret = bursts.get(i, 0.0)
        open_p = price
        close = price * (1 + ret)
        high = max(open_p, close) * 1.0005
        low = min(open_p, close) * 0.9995
        rows.append({"timestamp": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=i),
                     "open": open_p, "high": high, "low": low, "close": close,
                     "volume": 3500 if i in bursts else 1000})
        price = close
    feat = add_burst_features(pd.DataFrame(rows), bar_minutes=1)
    params = BurstParams(
        min_return_5m_pct=1.5, min_acceleration_pct=0.1, min_relative_volume=2.0,
        max_distance_ema_21_pct=5.0,
        min_expected_move_to_cost_ratio=3.0, cost_round_trip_pct=0.18,
    )
    # suggested_tp_pct=0.40% → ratio 2.22 < 3 → no signals
    signals = detect_long_burst(feat, params=params, suggested_tp_pct=0.40)
    assert signals == []


def test_burst_short_detects_accelerating_dump():
    np.random.seed(3)
    rows = []
    price = 100.0
    dumps = {25: -0.004, 26: -0.006, 27: -0.008, 28: -0.010, 29: -0.012}
    for i in range(60):
        ret = dumps.get(i, 0.0)
        open_p = price
        close = price * (1 + ret)
        high = max(open_p, close) * 1.0005
        low = min(open_p, close) * 0.9995
        rows.append({"timestamp": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=i),
                     "open": open_p, "high": high, "low": low, "close": close,
                     "volume": 3500 if i in dumps else 1000})
        price = close
    feat = add_burst_features(pd.DataFrame(rows), bar_minutes=1)
    params = BurstParams(
        min_return_5m_pct=1.5, min_acceleration_pct=0.1, min_relative_volume=2.0,
        max_distance_ema_21_pct=5.0,
    )
    signals = detect_short_burst(feat, params=params, suggested_tp_pct=1.5)
    assert signals, "expected at least one short burst signal"
    for sig in signals:
        assert sig.side == "SHORT"
        assert sig.return_5m_pct < 0


# ---------------------------------------------------------------------------
# 8. Candidate Incubator V2 — BNB → SHADOW_CANDIDATE not PAPER
# ---------------------------------------------------------------------------


def test_incubator_groups_by_setup_key():
    obs_bnb = [
        {"id": i, "symbol": "BNBUSDT", "side": "LONG", "market_regime": "RISK_ON",
         "confidence_score": 85, "timeframe": "5m", "strategy_type": "BREAKOUT",
         "entry_price": 100, "stop_loss": 99, "take_profit_1": 101.5,
         "operated": 1, "source": "trade_signal",
         "timestamp": "2026-01-01T00:00:00+00:00"}
        for i in range(50)
    ]
    obs_btc = [
        {"id": 1000 + i, "symbol": "BTCUSDT", "side": "LONG", "market_regime": "RISK_ON",
         "confidence_score": 85, "timeframe": "5m", "strategy_type": "BREAKOUT",
         "entry_price": 50000, "stop_loss": 49500, "take_profit_1": 50750,
         "operated": 1, "source": "trade_signal",
         "timestamp": "2026-01-02T00:00:00+00:00"}
        for i in range(50)
    ]
    labels = {
        o["id"]: {"observation_id": o["id"], "realized_return_pct": 0.01,
                  "max_favorable_excursion": 0.012, "max_adverse_excursion": -0.001,
                  "first_barrier_hit": "TP1", "bars_to_outcome": 5,
                  "label": 1, "timestamp": "2026-01-01T00:30:00+00:00"}
        for o in obs_bnb + obs_btc
    }
    result = build_incubator_result(obs_bnb + obs_btc, labels)
    setup_keys = {s.setup_key.split("|")[0] for s in result.setups}
    assert "BNBUSDT" in setup_keys
    assert "BTCUSDT" in setup_keys


def test_incubator_bnb_with_decent_sample_is_shadow_candidate_not_paper():
    import random
    random.seed(42)
    obs = []
    labels = {}
    for i in range(250):
        obs.append({
            "id": i, "symbol": "BNBUSDT", "side": "LONG", "market_regime": "RISK_ON",
            "confidence_score": 85, "timeframe": "5m", "strategy_type": "BREAKOUT",
            "entry_price": 100, "stop_loss": 99, "take_profit_1": 101.5,
            "operated": 1, "source": "trade_signal",
            "timestamp": f"2026-{(i // 30) % 12 + 1:02d}-01T00:00:00+00:00",
        })
        realized = random.choice([0.015, 0.015, 0.015, 0.015, -0.010, -0.010])
        labels[i] = {
            "observation_id": i, "realized_return_pct": realized,
            "max_favorable_excursion": max(realized, 0.005),
            "max_adverse_excursion": min(realized, -0.005),
            "first_barrier_hit": "TP1" if realized > 0 else "SL",
            "bars_to_outcome": 8,
            "timestamp": f"2026-{(i // 30) % 12 + 1:02d}-01T00:30:00+00:00",
        }
    result = build_incubator_result(obs, labels)
    bnb = [s for s in result.setups if s.symbol == "BNBUSDT"]
    assert bnb, "BNB setup missing"
    target = bnb[0]
    assert target.recommendation == REC_SHADOW_CANDIDATE
    assert target.recommendation != REC_PAPER_CANDIDATE_BLOCKED, "must not auto-promote to paper"
    assert result.paper_filter_enabled is False
    assert result.can_send_real_orders is False


def test_incubator_low_sample_rejects_or_watches():
    obs = [
        {"id": i, "symbol": "DOGEUSDT", "side": "LONG", "market_regime": "RISK_ON",
         "confidence_score": 85, "timeframe": "5m", "strategy_type": "BREAKOUT",
         "entry_price": 1.0, "stop_loss": 0.99, "take_profit_1": 1.02,
         "operated": 1, "source": "trade_signal",
         "timestamp": "2026-01-01T00:00:00+00:00"}
        for i in range(20)
    ]
    labels = {
        o["id"]: {"observation_id": o["id"], "realized_return_pct": 0.005,
                  "max_favorable_excursion": 0.005, "max_adverse_excursion": -0.001,
                  "first_barrier_hit": "TP1", "bars_to_outcome": 4,
                  "label": 1, "timestamp": "2026-01-01T00:20:00+00:00"}
        for o in obs
    }
    result = build_incubator_result(obs, labels)
    assert result.setups[0].recommendation != REC_PAPER_CANDIDATE_BLOCKED


def test_incubator_render_text_contains_no_live():
    result = build_incubator_result([], {})
    text = render_result_text(result)
    assert "NO LIVE" in text
    assert "paper_filter_enabled: false" in text
    assert "can_send_real_orders: false" in text


# ---------------------------------------------------------------------------
# 9. New DB tables
# ---------------------------------------------------------------------------


def test_new_tables_created(db):
    assert db.table_exists("shadow_candidates")
    assert db.table_exists("signal_outcomes")
    shadow_cols = set(db.get_table_columns("shadow_candidates"))
    assert {"setup_key", "status", "entry_price", "net_return_pct"}.issubset(shadow_cols)
    outcomes_cols = set(db.get_table_columns("signal_outcomes"))
    assert {"outcome_class", "suggested_fix", "setup_key"}.issubset(outcomes_cols)


def test_signal_outcomes_table_records_and_fetches(db):
    payload = {
        "created_at": "2026-01-01T00:00:00+00:00",
        "observation_id": 1,
        "setup_key": "BNBUSDT|LONG|RISK_ON|85-89|5m|BREAKOUT|current_exit|trade_signal",
        "outcome_class": "CLEAN_WIN",
        "suggested_fix": "none",
        "realized_return_pct": 1.2,
        "net_return_pct": 1.02,
        "total_cost_pct": 0.18,
        "mfe": 1.5, "mae": -0.2,
        "first_barrier_hit": "TP1",
        "expected_move_pct": 1.5,
        "expected_move_to_cost_ratio": 8.3,
        "operated": 1, "has_label": 1, "notes": "smoke",
    }
    new_id = db.record_signal_outcome(payload)
    assert new_id > 0
    rows = db.fetch_signal_outcomes(outcome_class="CLEAN_WIN")
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# 10. Hardening (Codex audit) — idempotency, market_probe block, approximation,
#     execution-path spies
# ---------------------------------------------------------------------------


def test_shadow_candidate_record_is_idempotent_with_observation_id(db):
    payload = {
        "created_at": "2026-01-01T00:00:00+00:00",
        "signal_timestamp": "2026-01-01T00:00:00+00:00",
        "observation_id": 42, "symbol": "BNBUSDT", "side": "LONG", "regime": "RISK_ON",
        "score": 85, "score_bucket": "85-89", "timeframe": "5m", "strategy": "BREAKOUT",
        "source": "trade_signal",
        "setup_key": "BNBUSDT|LONG|RISK_ON|85-89|5m|BREAKOUT|current_exit|trade_signal",
        "entry_price": 300, "stop_loss": 295, "take_profit_1": 310, "take_profit_2": 315,
        "expected_move_pct": 3.33, "expected_move_to_cost_ratio": 18.5, "status": "PENDING",
    }
    id1 = db.record_shadow_candidate(payload)
    id2 = db.record_shadow_candidate(payload)
    assert id1 == id2
    assert db.count_shadow_candidates() == 1


def test_shadow_candidate_record_is_idempotent_with_composite_fallback(db):
    payload = {
        "created_at": "2026-01-01T00:00:00+00:00",
        "signal_timestamp": "2026-02-02T00:00:00+00:00",
        "observation_id": 0,  # composite fallback path
        "symbol": "BNBUSDT", "side": "LONG", "regime": "RISK_ON",
        "score": 85, "score_bucket": "85-89", "timeframe": "5m", "strategy": "BREAKOUT",
        "source": "trade_signal",
        "setup_key": "BNBUSDT|LONG|RISK_ON|85-89|5m|BREAKOUT|current_exit|trade_signal",
        "entry_price": 300, "stop_loss": 295, "take_profit_1": 310, "take_profit_2": 315,
        "expected_move_pct": 3.33, "expected_move_to_cost_ratio": 18.5, "status": "PENDING",
    }
    id1 = db.record_shadow_candidate(payload)
    id2 = db.record_shadow_candidate(payload)
    assert id1 == id2
    assert db.count_shadow_candidates() == 1


def test_signal_outcome_record_is_idempotent_by_observation_and_setup_key(db):
    outcome = {
        "created_at": "2026-01-01T00:00:00+00:00",
        "observation_id": 7,
        "setup_key": "BNBUSDT|LONG|RISK_ON|85-89|5m|BREAKOUT|current_exit|trade_signal",
        "outcome_class": "CLEAN_WIN", "suggested_fix": "none",
        "realized_return_pct": 1.0, "net_return_pct": 0.82, "total_cost_pct": 0.18,
        "mfe": 1.5, "mae": -0.2, "first_barrier_hit": "TP1",
        "expected_move_pct": 1.5, "expected_move_to_cost_ratio": 8.3,
        "operated": 1, "has_label": 1, "notes": "smoke",
    }
    oid1 = db.record_signal_outcome(outcome)
    oid2 = db.record_signal_outcome(outcome)
    assert oid1 == oid2
    assert len(db.fetch_signal_outcomes()) == 1


def test_market_probe_with_great_metrics_is_research_only_not_actionable():
    from app.candidate_incubator_v2 import (
        REC_NOT_ACTIONABLE_RESEARCH_ONLY,
        REC_PAPER_CANDIDATE_BLOCKED,
        REC_SHADOW_CANDIDATE,
    )
    import random
    random.seed(7)
    obs, labels = [], {}
    for i in range(300):
        obs.append({
            "id": i, "symbol": "BNBUSDT", "side": "LONG", "market_regime": "RISK_ON",
            "confidence_score": 85, "timeframe": "5m", "strategy_type": "BREAKOUT",
            "entry_price": 100, "stop_loss": 99, "take_profit_1": 101.5,
            "operated": 0, "source": "market_probe",
            "timestamp": f"2026-{(i // 30) % 12 + 1:02d}-01T00:00:00+00:00",
        })
        realized = random.choice([0.02, 0.02, 0.02, 0.02, -0.005])
        labels[i] = {
            "observation_id": i, "realized_return_pct": realized,
            "max_favorable_excursion": max(realized, 0.005),
            "max_adverse_excursion": min(realized, -0.005),
            "first_barrier_hit": "TP1" if realized > 0 else "SL",
            "bars_to_outcome": 5,
            "timestamp": f"2026-{(i // 30) % 12 + 1:02d}-01T00:30:00+00:00",
        }
    result = build_incubator_result(obs, labels)
    assert result.setups, "expected at least one setup"
    for s in result.setups:
        assert s.recommendation == REC_NOT_ACTIONABLE_RESEARCH_ONLY
        assert s.recommendation != REC_SHADOW_CANDIDATE
        assert s.recommendation != REC_PAPER_CANDIDATE_BLOCKED


def test_quick_profit_trailing_policy_is_marked_approximation_only():
    from app.quick_profit_exit_lab import DEFAULT_POLICIES
    trailing = next(p for p in DEFAULT_POLICIES if p.name == "trailing_after_080")
    assert trailing.is_approximation_only is True
    assert trailing.approximation_reason


def test_quick_profit_euro_net_policy_is_marked_approximation_in_summary():
    from app.quick_profit_exit_lab import QuickProfitPolicy, run_lab, render_lab_text
    df = _candle_frame_with_uptrend()
    inputs = build_trade_inputs_from_dataframe(
        df, side="LONG", entry_indices=[5], stop_pct=0.8, tp_pct=2.0,
    )
    custom = QuickProfitPolicy(name="euro_net_1eur", euro_net_threshold=1.0, notional_usdt=36.0)
    result = run_lab(inputs, policies=(custom,))
    summary = result.summaries[0]
    assert summary.approximation_only is True
    assert summary.warning
    text = render_lab_text(result)
    assert "APPROXIMATION_ONLY" in text
    assert "do_not_use_approximation_only_metrics_for_live_decisions" in text


def test_quick_profit_render_text_flags_trailing_warning():
    df = _candle_frame_with_uptrend()
    inputs = build_trade_inputs_from_dataframe(
        df, side="LONG", entry_indices=[10], stop_pct=0.8, tp_pct=2.0,
    )
    result = run_lab(inputs)
    text = render_lab_text(result)
    assert "trailing_after_080" in text
    assert "approximation_only=true" in text
    assert "APPROXIMATION_ONLY" in text


# --- Execution-path spies (monkeypatch) -----------------------------------


def _make_spy(call_log, name):
    def _spy(*args, **kwargs):
        call_log.append(name)
        raise AssertionError(f"FORBIDDEN call detected: {name}")
    return _spy


def test_candidate_shadow_monitor_does_not_instantiate_bitget_client(cfg_shadow_enabled, db, monkeypatch):
    import app.bitget_client as bc
    call_log: list[str] = []
    monkeypatch.setattr(bc.BitgetClient, "__init__", _make_spy(call_log, "BitgetClient.__init__"))
    # Also spy execution surface
    import app.execution_engine as ee
    import app.paper_trader as pt
    monkeypatch.setattr(ee.ExecutionEngine, "execute", _make_spy(call_log, "ExecutionEngine.execute"))
    monkeypatch.setattr(pt.PaperTrader, "open_position", _make_spy(call_log, "PaperTrader.open_position"))

    monitor = CandidateShadowMonitor(cfg_shadow_enabled, db)
    # Register signal (matches rule)
    rid = monitor.register_signal(
        observation_id=1, symbol="BNBUSDT", side="LONG", regime="RISK_ON",
        score=85, timeframe="5m", strategy="BREAKOUT",
        entry_price=300, stop_loss=295, take_profit_1=310, take_profit_2=315,
    )
    assert rid > 0
    # Summary should not touch any execution path either
    monitor.summary(hours=24)
    assert call_log == [], f"forbidden execution call(s) detected: {call_log}"


def test_quick_profit_exit_lab_does_not_touch_execution(monkeypatch):
    import app.bitget_client as bc
    import app.execution_engine as ee
    import app.paper_trader as pt
    call_log: list[str] = []
    monkeypatch.setattr(bc.BitgetClient, "__init__", _make_spy(call_log, "BitgetClient.__init__"))
    monkeypatch.setattr(ee.ExecutionEngine, "execute", _make_spy(call_log, "ExecutionEngine.execute"))
    monkeypatch.setattr(pt.PaperTrader, "open_position", _make_spy(call_log, "PaperTrader.open_position"))

    df = _candle_frame_with_uptrend()
    inputs = build_trade_inputs_from_dataframe(df, side="LONG", entry_indices=[5, 15], stop_pct=0.8, tp_pct=2.0)
    result = run_lab(inputs)
    assert result.summaries
    assert call_log == [], f"forbidden execution call(s) detected: {call_log}"


def test_momentum_burst_lab_does_not_touch_execution(monkeypatch):
    import app.bitget_client as bc
    import app.execution_engine as ee
    import app.paper_trader as pt
    call_log: list[str] = []
    monkeypatch.setattr(bc.BitgetClient, "__init__", _make_spy(call_log, "BitgetClient.__init__"))
    monkeypatch.setattr(ee.ExecutionEngine, "execute", _make_spy(call_log, "ExecutionEngine.execute"))
    monkeypatch.setattr(pt.PaperTrader, "open_position", _make_spy(call_log, "PaperTrader.open_position"))

    np.random.seed(13)
    rows = []
    price = 100.0
    bursts = {25: 0.004, 26: 0.006, 27: 0.008, 28: 0.010, 29: 0.012}
    for i in range(60):
        ret = bursts.get(i, 0.0)
        open_p = price
        close = price * (1 + ret)
        high = max(open_p, close) * 1.0005
        low = min(open_p, close) * 0.9995
        rows.append({"timestamp": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=i),
                     "open": open_p, "high": high, "low": low, "close": close,
                     "volume": 3500 if i in bursts else 1000})
        price = close
    feat = add_burst_features(pd.DataFrame(rows), bar_minutes=1)
    params = BurstParams(min_return_5m_pct=1.5, min_acceleration_pct=0.1,
                         min_relative_volume=2.0, max_distance_ema_21_pct=5.0)
    detect_long_burst(feat, params=params, suggested_tp_pct=1.5)
    backtest_burst(feat, side="LONG", params=params, stop_pct=0.6, take_profit_pct=1.5, max_holding_bars=20)
    assert call_log == [], f"forbidden execution call(s) detected: {call_log}"


def test_candidate_incubator_v2_does_not_touch_execution_or_paper_filter(monkeypatch):
    import app.bitget_client as bc
    import app.execution_engine as ee
    import app.paper_trader as pt
    call_log: list[str] = []
    monkeypatch.setattr(bc.BitgetClient, "__init__", _make_spy(call_log, "BitgetClient.__init__"))
    monkeypatch.setattr(ee.ExecutionEngine, "execute", _make_spy(call_log, "ExecutionEngine.execute"))
    monkeypatch.setattr(pt.PaperTrader, "open_position", _make_spy(call_log, "PaperTrader.open_position"))

    obs = [
        {"id": i, "symbol": "BNBUSDT", "side": "LONG", "market_regime": "RISK_ON",
         "confidence_score": 85, "timeframe": "5m", "strategy_type": "BREAKOUT",
         "entry_price": 100, "stop_loss": 99, "take_profit_1": 101.5,
         "operated": 1, "source": "trade_signal",
         "timestamp": "2026-01-01T00:00:00+00:00"}
        for i in range(30)
    ]
    labels = {
        o["id"]: {"observation_id": o["id"], "realized_return_pct": 0.01,
                  "max_favorable_excursion": 0.012, "max_adverse_excursion": -0.001,
                  "first_barrier_hit": "TP1", "bars_to_outcome": 5,
                  "label": 1, "timestamp": "2026-01-01T00:30:00+00:00"}
        for o in obs
    }
    result = build_incubator_result(obs, labels)
    # Sanity: result was produced AND paper filter / can_send_real_orders flags
    # are forced False at the result level (defense in depth).
    assert result.paper_filter_enabled is False
    assert result.can_send_real_orders is False
    assert call_log == [], f"forbidden execution call(s) detected: {call_log}"


def test_signal_outcome_classifier_does_not_touch_execution(monkeypatch):
    import app.bitget_client as bc
    import app.execution_engine as ee
    import app.paper_trader as pt
    call_log: list[str] = []
    monkeypatch.setattr(bc.BitgetClient, "__init__", _make_spy(call_log, "BitgetClient.__init__"))
    monkeypatch.setattr(ee.ExecutionEngine, "execute", _make_spy(call_log, "ExecutionEngine.execute"))
    monkeypatch.setattr(pt.PaperTrader, "open_position", _make_spy(call_log, "PaperTrader.open_position"))

    obs = _observation(99)
    classify_observation(obs, _label(99))
    classify_batch([obs], {99: _label(99)})
    summarize([classify_observation(obs, _label(99))])
    assert call_log == [], f"forbidden execution call(s) detected: {call_log}"
