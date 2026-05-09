from datetime import datetime, timedelta, timezone

from app.config import BotConfig, PROJECT_ROOT
from app.database import Database
from app.research_lab import ResearchLab
from app.strategy_lab import (
    FUTURE_OUTCOME_FIELDS,
    StrategyLab,
    build_strategy_lab_candidates,
    evaluate_candidate,
    make_strategy_lab_walkforward_splits,
)


class DummyLogger:
    def __init__(self):
        self.messages = []

    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        self.messages.append(args[0] % args[1:] if args else "")

    def warning(self, *args, **kwargs):
        self.messages.append(args[0] % args[1:] if args else "")

    def error(self, *args, **kwargs):
        self.messages.append(args[0] % args[1:] if args else "")


def make_db(tmp_path):
    db = Database(BotConfig(), DummyLogger())
    db.sqlite_path = tmp_path / "strategy_lab.db"
    db.initialize()
    return db


def insert_labeled(
    db,
    *,
    index=0,
    label=1,
    ret=None,
    symbol="BTCUSDT",
    strategy="BREAKOUT",
    regime="TREND_UP",
    side="LONG",
    score=88,
    volume_relative=1.8,
):
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)
    ret = ret if ret is not None else (0.03 if label == 1 else -0.02 if label == -1 else 0.0)
    observation_id = db.record_signal_observation(
        {
            "timestamp": timestamp.isoformat(),
            "symbol": symbol,
            "side": side,
            "strategy_type": strategy,
            "confidence_score": score,
            "market_regime": regime,
            "entry_price": 100.0,
            "stop_loss": 98.0 if side == "LONG" else 102.0,
            "take_profit_1": 103.0 if side == "LONG" else 97.0,
            "take_profit_2": 105.0 if side == "LONG" else 95.0,
            "risk_reward_ratio": 1.5,
            "spread_pct": 0.0005,
            "volume_24h_usdt": 100_000_000,
            "rsi_14": 58 if side == "LONG" else 42,
            "macd_hist": 0.1 if side == "LONG" else -0.1,
            "normalized_atr": 0.012,
            "volume_relative": volume_relative,
            "distance_to_ema_21": 0.01 if side == "LONG" else -0.01,
            "distance_to_ema_50": 0.02 if side == "LONG" else -0.02,
            "distance_to_ema_200": 0.04 if side == "LONG" else -0.04,
            "momentum_5": 0.01 if side == "LONG" else -0.01,
            "momentum_15": 0.02 if side == "LONG" else -0.02,
            "range_width_pct": 0.03,
            "body_pct": 0.01,
            "btc_regime": regime,
            "btc_momentum_5": 0.01 if side == "LONG" else -0.01,
            "btc_momentum_15": 0.02 if side == "LONG" else -0.02,
            "eth_momentum_5": 0.01 if side == "LONG" else -0.01,
            "number_of_symbols_bullish": 7 if side == "LONG" else 3,
            "number_of_symbols_bearish": 3 if side == "LONG" else 7,
            "market_risk_on": 1,
            "market_risk_off": 0,
        }
    )
    label_id = db.record_signal_label(
        {
            "timestamp": timestamp.isoformat(),
            "observation_id": observation_id,
            "label": label,
            "first_barrier_hit": "TP1" if label == 1 else "SL" if label == -1 else "TIME",
            "bars_to_outcome": 4,
            "max_favorable_excursion": max(ret, 0.0),
            "max_adverse_excursion": min(ret, 0.0),
            "realized_return_pct": ret,
            "simulated_pnl": ret * 100,
            "would_have_won": int(label == 1),
        }
    )
    return observation_id, label_id


def make_rows(count=150, wins_until=None):
    rows = []
    for index in range(count):
        if wins_until is None:
            win = index % 3 != 0
        else:
            win = index < wins_until
        rows.append(
            {
                "timestamp": (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)).isoformat(),
                "symbol": "BTCUSDT",
                "side": "LONG",
                "strategy_type": "BREAKOUT",
                "market_regime": "TREND_UP",
                "confidence_score": 88,
                "volume_relative": 1.8,
                "spread_pct": 0.0005,
                "btc_momentum_5": 0.01,
                "btc_momentum_15": 0.02,
                "distance_to_ema_21": 0.01,
                "distance_to_ema_50": 0.02,
                "momentum_5": 0.01,
                "momentum_15": 0.02,
                "risk_reward_ratio": 1.5,
                "stop_distance_pct": 0.02,
                "tp1_to_sl_ratio": 1.5,
                "label": 1 if win else -1,
                "first_barrier_hit": "TP1" if win else "SL",
                "realized_return_pct": 0.02 if win else -0.02,
            }
        )
    return rows


def test_strategy_lab_walkforward_is_temporal_no_future_mixing():
    rows = make_rows(120)
    splits = make_strategy_lab_walkforward_splits(rows, min_block_size=20)
    assert splits
    for split in splits:
        assert split.train[-1]["timestamp"] < split.test[0]["timestamp"]


def test_strategy_lab_candidate_definitions_mark_future_features():
    rows = make_rows(120)
    candidates = build_strategy_lab_candidates(rows, safe_mode=True)
    leaking = [
        candidate
        for candidate in candidates
        if candidate.feature_names.intersection(FUTURE_OUTCOME_FIELDS) and not candidate.uses_future
    ]
    assert leaking == []


def test_strategy_lab_rejects_overfitting():
    rows = make_rows(180, wins_until=100)
    candidate = [item for item in build_strategy_lab_candidates(rows) if item.name == "NORMAL_ONLY"][0]
    evaluation = evaluate_candidate(candidate, rows)
    assert evaluation.status in {"REJECTED_OVERFITTING", "REJECTED_NO_EDGE", "REJECTED_UNSTABLE"}


def test_strategy_lab_rejects_small_sample(tmp_path):
    db = make_db(tmp_path)
    for index in range(20):
        insert_labeled(db, index=index, label=1)
    result = StrategyLab(db, DummyLogger()).run(limit=20, safe_mode=True)
    assert result.candidates_accepted == 0
    assert any(item.status == "REJECTED_TOO_FEW_SAMPLES" for item in result.evaluations)


def test_strategy_lab_safe_mode_caps_rows(tmp_path):
    db = make_db(tmp_path)
    for index in range(120):
        insert_labeled(db, index=index, label=1 if index % 2 == 0 else -1)
    result = StrategyLab(db, DummyLogger()).run(limit=100000, safe_mode=True)
    assert result.safe_mode is True
    assert result.rows_loaded <= 20000
    assert result.rows_loaded == 120


def test_strategy_lab_persists_tables(tmp_path):
    db = make_db(tmp_path)
    for index in range(130):
        insert_labeled(db, index=index, label=1 if index % 3 != 0 else -1)
    result = StrategyLab(db, DummyLogger()).run(limit=130, safe_mode=True)
    assert "STRATEGY LAB START" in result.to_text()
    assert "final recommendation: NO LIVE" in result.to_text()
    assert db.fetch_strategy_lab_candidates()
    assert db.fetch_strategy_lab_walkforward()
    assert db.fetch_strategy_lab_recommendations()


def test_strategy_lab_no_live_coupling():
    text = (PROJECT_ROOT / "app" / "strategy_lab.py").read_text(encoding="utf-8")
    assert "ExecutionEngine" not in text
    assert "RiskManager" not in text
    assert "BitgetClient" not in text
    assert "LIVE_TRADING=true" not in text


def test_research_lab_strategy_lab_command_method_works(tmp_path):
    db = make_db(tmp_path)
    for index in range(120):
        insert_labeled(db, index=index, label=1 if index % 2 == 0 else -1)
    text = ResearchLab(db, BotConfig(), DummyLogger(), reports_dir=tmp_path / "reports").strategy_lab(limit=120, safe_mode=True)
    assert "STRATEGY LAB START" in text
    assert "families tested" in text
    assert "STRATEGY LAB END" in text
    assert "NO LIVE" in text
