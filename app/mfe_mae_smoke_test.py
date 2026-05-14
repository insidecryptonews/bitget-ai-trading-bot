from __future__ import annotations

from datetime import datetime, timezone

from .config import BotConfig
from .database import Database
from .market_data import MarketSnapshot
from .mfe_mae_tracker import MfeMaeTracker
from .signal_engine import Signal
from .utils import safe_int


START = "MFE MAE SMOKE TEST START"
END = "MFE MAE SMOKE TEST END"


class MfeMaeSmokeTest:
    """Local research-only smoke test. It never opens paper/live orders."""

    def __init__(self, config: BotConfig, db: Database, logger=None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def run(self) -> dict[str, object]:
        tracker = MfeMaeTracker(
            BotConfig(
                enable_mfe_mae_capture=True,
                enable_mfe_mae_market_probes=True,
                mfe_mae_probe_every_n_cycles=1,
                mfe_mae_probe_top_n_symbols=1,
                mfe_mae_probe_both_sides=True,
                mfe_mae_probe_max_per_cycle=2,
                mfe_mae_track_low_score_sample=True,
                mfe_mae_low_score_min=20,
                mfe_mae_low_score_sample_rate=1.0,
                mfe_mae_low_score_max_per_cycle=1,
                mfe_mae_max_active=max(10, self.config.mfe_mae_max_active),
                live_trading=False,
                dry_run=True,
                paper_trading=True,
            ),
            self.db,
            self.logger,
        )
        suffix = datetime.now(timezone.utc).strftime("%H%M%S%f")
        probe_symbol = f"SMOKEPROBE{suffix}"
        low_symbol = f"SMOKELOW{suffix}"
        snapshots = {
            probe_symbol: MarketSnapshot(symbol=probe_symbol, current_price=100.0, volume_24h_usdt=1000.0),
            low_symbol: MarketSnapshot(symbol=low_symbol, current_price=50.0, volume_24h_usdt=900.0),
        }
        before = self.db.get_paper_trade_summary()
        probe_result = tracker.register_market_probes(
            snapshots=snapshots,
            market_regime="SMOKE_TEST",
            cycle_count=1,
        )
        obs_id = self.db.record_signal_observation({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": low_symbol,
            "side": "LONG",
            "strategy_type": "SMOKE_LOW_SCORE",
            "confidence_score": 30,
            "market_regime": "SMOKE_TEST",
            "entry_price": 50.0,
            "stop_loss": 49.0,
            "take_profit_1": 51.0,
            "take_profit_2": 52.0,
        })
        low_signal = Signal(
            symbol=low_symbol,
            side="LONG",
            strategy_type="SMOKE_LOW_SCORE",
            confidence_score=30,
            entry_price=50.0,
            stop_loss=49.0,
            take_profit_1=51.0,
            take_profit_2=52.0,
            trailing_stop_enabled=False,
            trailing_stop_rule="",
            risk_reward_ratio=1.5,
            leverage_recommendation=1,
            position_size=0.0,
            reason="smoke low score sample",
        )
        low_result = tracker.register_low_score_samples(
            signals=[low_signal],
            snapshots=snapshots,
            observation_ids={low_symbol: obs_id},
            market_regime="SMOKE_TEST",
        )
        tracker.update_active({
            probe_symbol: MarketSnapshot(symbol=probe_symbol, current_price=100.35, volume_24h_usdt=1000.0),
            low_symbol: MarketSnapshot(symbol=low_symbol, current_price=50.20, volume_24h_usdt=900.0),
        })
        after = self.db.get_paper_trade_summary()
        summary = self.db.get_signal_path_metrics_summary_since("1970-01-01T00:00:00+00:00")
        by_source = self.db.get_signal_path_metrics_source_summary_since("1970-01-01T00:00:00+00:00")
        opened_paper = max(0, safe_int(after.get("open")) - safe_int(before.get("open")))
        result = {
            "created_market_probes": probe_result.market_probes_created,
            "created_low_score_samples": low_result.low_score_samples_tracked,
            "rows_total": safe_int(summary.get("total")),
            "active": safe_int(summary.get("active_count")),
            "matured": safe_int(summary.get("matured_count")),
            "by_source": by_source,
            "opened_paper_trades": opened_paper,
            "pass": safe_int(summary.get("total")) > 0 and opened_paper == 0 and not self.config.live_trading,
        }
        return result

    def to_text(self) -> str:
        result = self.run()
        lines = [
            START,
            f"created_market_probes: {safe_int(result.get('created_market_probes'))}",
            f"created_low_score_samples: {safe_int(result.get('created_low_score_samples'))}",
            f"rows_total: {safe_int(result.get('rows_total'))}",
            f"active: {safe_int(result.get('active'))}",
            f"matured: {safe_int(result.get('matured'))}",
            "by_source:",
            *_source_lines(result.get("by_source") or []),
            "safety:",
            f"- LIVE_TRADING={str(bool(self.config.live_trading)).lower()}",
            f"- DRY_RUN={str(bool(self.config.dry_run)).lower()}",
            f"- PAPER_TRADING={str(bool(self.config.paper_trading)).lower()}",
            f"- opened_paper_trades={safe_int(result.get('opened_paper_trades'))}",
            f"result: {'PASS' if result.get('pass') else 'FAIL'}",
            END,
        ]
        return "\n".join(lines)


def _source_lines(rows: list[dict]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        f"- {row.get('source')}: {safe_int(row.get('total'))}"
        for row in rows[:20]
    ]
