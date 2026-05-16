from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import BotConfig, PROJECT_ROOT
from .utils import iso_utc, json_dumps, safe_float, sanitize


class Database:
    def __init__(self, config: BotConfig, logger) -> None:
        self.config = config
        self.logger = logger
        self.sqlite_path = PROJECT_ROOT / "bot_state.db"
        self._postgres = None
        self._postgres_dict_row = None
        self._use_postgres = False

        if config.database_url and config.use_postgres_if_available:
            try:
                import psycopg  # type: ignore
                from psycopg.rows import dict_row  # type: ignore

                self._postgres = psycopg
                self._postgres_dict_row = dict_row
                self._use_postgres = True
            except Exception:
                self.logger.warning("DATABASE_URL existe, pero psycopg no está disponible. Usando SQLite local.")

    def initialize(self) -> None:
        if self._use_postgres:
            self._initialize_postgres()
        else:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                self._create_tables(conn)

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        if self._use_postgres:
            assert self._postgres is not None
            connect_kwargs = {"row_factory": self._postgres_dict_row} if self._postgres_dict_row is not None else {}
            with self._postgres.connect(self.config.database_url, **connect_kwargs) as conn:
                yield conn
        else:
            conn = sqlite3.connect(self.sqlite_path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def _initialize_postgres(self) -> None:
        with self._connect() as conn:
            self._create_tables(conn)
            conn.commit()

    def _execute(self, conn: Any, sql: str, params: tuple[Any, ...] = ()) -> None:
        if self._use_postgres:
            sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
            sql = sql.replace("?", "%s")
        conn.execute(sql, params)

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        if isinstance(row, dict):
            return dict(row)
        try:
            return dict(row)
        except (TypeError, ValueError) as exc:
            raise TypeError(
                "La fila de base de datos no es convertible a dict. "
                "En PostgreSQL debe usarse psycopg.rows.dict_row."
            ) from exc

    def _fetchall_dicts(self, cursor: Any) -> list[dict[str, Any]]:
        return [self._row_to_dict(row) for row in cursor.fetchall()]

    def _inserted_id(self, cursor: Any) -> int:
        if self._use_postgres:
            row = cursor.fetchone()
            return int(self._row_value(row, "id", 0, 0) or 0)
        return int(getattr(cursor, "lastrowid", 0) or 0)

    @staticmethod
    def _row_value(row: Any, key: str, index: int, default: Any = None) -> Any:
        if row is None:
            return default
        if isinstance(row, dict):
            return row.get(key, default)
        if isinstance(row, sqlite3.Row):
            return row[key]
        return row[index] if len(row) > index else default

    def _create_tables(self, conn: Any) -> None:
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                mode TEXT,
                symbol TEXT,
                strategy_type TEXT,
                side TEXT,
                entry REAL,
                stop_loss REAL,
                take_profit_1 REAL,
                take_profit_2 REAL,
                trailing_stop TEXT,
                size REAL,
                leverage INTEGER,
                risk_amount REAL,
                confidence_score INTEGER,
                reason TEXT,
                status TEXT,
                realized_pnl REAL DEFAULT 0,
                unrealized_pnl REAL DEFAULT 0,
                fees REAL DEFAULT 0,
                slippage REAL DEFAULT 0,
                error_message TEXT,
                raw_signal_json TEXT,
                raw_order_response_sanitized TEXT
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                level TEXT,
                event_type TEXT,
                message TEXT,
                payload_json TEXT
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS latency_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                metric_name TEXT,
                component TEXT,
                duration_ms REAL,
                payload_json TEXT,
                created_at TEXT NOT NULL
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT NOT NULL
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS signal_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT,
                side TEXT,
                strategy_type TEXT,
                confidence_score INTEGER,
                market_regime TEXT,
                entry_price REAL,
                stop_loss REAL,
                take_profit_1 REAL,
                take_profit_2 REAL,
                risk_reward_ratio REAL,
                leverage_recommendation INTEGER,
                spread_pct REAL,
                volume_24h_usdt REAL,
                funding_rate REAL,
                open_interest REAL,
                timeframe_alignment TEXT,
                confirmations_json TEXT,
                warnings_json TEXT,
                rsi_14 REAL,
                macd_hist REAL,
                atr_14 REAL,
                normalized_atr REAL,
                volume_relative REAL,
                distance_to_ema_21 REAL,
                distance_to_ema_50 REAL,
                distance_to_ema_200 REAL,
                momentum_5 REAL,
                momentum_15 REAL,
                range_width_pct REAL,
                body_pct REAL,
                upper_wick_pct REAL,
                lower_wick_pct REAL,
                bullish_rejection INTEGER,
                bearish_rejection INTEGER,
                btc_regime TEXT,
                btc_momentum_5 REAL,
                btc_momentum_15 REAL,
                btc_normalized_atr REAL,
                eth_momentum_5 REAL,
                number_of_symbols_bullish INTEGER,
                number_of_symbols_bearish INTEGER,
                market_risk_on INTEGER,
                market_risk_off INTEGER,
                operated INTEGER DEFAULT 0,
                block_reason TEXT,
                selected_by_allocator INTEGER DEFAULT 0,
                risk_manager_approved INTEGER DEFAULT 0,
                meta_probability REAL,
                meta_decision TEXT,
                shadow_strategy INTEGER DEFAULT 0,
                strategy_variant_id INTEGER,
                variant_params_json TEXT,
                original_side TEXT,
                original_strategy_type TEXT,
                score_bucket TEXT,
                kronos_predicted_return_pct REAL,
                kronos_direction TEXT,
                kronos_confidence_score REAL,
                kronos_disagreement INTEGER,
                kronos_prediction_id INTEGER,
                raw_signal_json TEXT,
                raw_features_json TEXT
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS signal_labels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                observation_id INTEGER NOT NULL,
                label INTEGER,
                first_barrier_hit TEXT,
                bars_to_outcome INTEGER,
                max_favorable_excursion REAL,
                max_adverse_excursion REAL,
                realized_return_pct REAL,
                simulated_pnl REAL,
                would_have_won INTEGER,
                raw_label_json TEXT
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS signal_path_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observation_id INTEGER UNIQUE,
                symbol TEXT,
                side TEXT,
                score INTEGER,
                score_bucket TEXT,
                market_regime TEXT,
                source TEXT,
                probe_key TEXT,
                reject_reason TEXT,
                priority INTEGER DEFAULT 0,
                entry_price REAL,
                current_price REAL,
                max_favorable_pct REAL DEFAULT 0,
                max_adverse_pct REAL DEFAULT 0,
                final_return_pct REAL DEFAULT 0,
                bars_tracked INTEGER DEFAULT 0,
                bars_to_mfe INTEGER DEFAULT 0,
                bars_to_mae INTEGER DEFAULT 0,
                first_barrier_hit TEXT,
                would_hit_tp_025 INTEGER DEFAULT 0,
                would_hit_tp_050 INTEGER DEFAULT 0,
                would_hit_tp_075 INTEGER DEFAULT 0,
                would_hit_tp_100 INTEGER DEFAULT 0,
                would_hit_tp_150 INTEGER DEFAULT 0,
                would_hit_sl_025 INTEGER DEFAULT 0,
                would_hit_sl_050 INTEGER DEFAULT 0,
                would_hit_sl_075 INTEGER DEFAULT 0,
                would_hit_sl_100 INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                matured_at TEXT
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS strategy_variants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                params_json TEXT,
                enabled INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS strategy_variant_results (
                variant_id INTEGER PRIMARY KEY,
                total_labels INTEGER DEFAULT 0,
                tp1_count INTEGER DEFAULT 0,
                tp2_count INTEGER DEFAULT 0,
                sl_count INTEGER DEFAULT 0,
                time_count INTEGER DEFAULT 0,
                win_rate REAL DEFAULT 0,
                profit_factor REAL DEFAULT 0,
                avg_return REAL DEFAULT 0,
                max_drawdown_estimated REAL DEFAULT 0,
                score REAL DEFAULT 0,
                last_updated TEXT NOT NULL
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS signal_explanations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observation_id INTEGER,
                label_id INTEGER,
                symbol TEXT,
                side TEXT,
                strategy_type TEXT,
                label INTEGER,
                first_barrier_hit TEXT,
                primary_reason TEXT,
                secondary_reasons_json TEXT,
                failure_type TEXT,
                confidence REAL,
                explanation_text TEXT,
                recommended_action TEXT,
                created_at TEXT NOT NULL
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS signal_price_paths (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observation_id INTEGER,
                label_id INTEGER,
                max_favorable_excursion_pct REAL,
                max_adverse_excursion_pct REAL,
                time_to_max_favorable INTEGER,
                time_to_max_adverse INTEGER,
                time_to_sl INTEGER,
                time_to_tp1 INTEGER,
                time_to_tp2 INTEGER,
                candles_until_exit INTEGER,
                did_price_move_in_favor_first INTEGER,
                did_price_move_against_first INTEGER,
                adverse_before_favorable_pct REAL,
                favorable_before_adverse_pct REAL,
                close_vs_entry_pct REAL,
                volatility_during_trade REAL,
                volume_during_trade_relative REAL,
                btc_move_during_trade REAL,
                eth_move_during_trade REAL,
                created_at TEXT NOT NULL
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS signal_counterfactuals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observation_id INTEGER,
                label_id INTEGER,
                scenario_name TEXT,
                params_json TEXT,
                would_trade INTEGER,
                simulated_side TEXT,
                simulated_sl REAL,
                simulated_tp1 REAL,
                simulated_tp2 REAL,
                simulated_label INTEGER,
                simulated_first_barrier_hit TEXT,
                simulated_return_pct REAL,
                avoided_loss INTEGER,
                improved_result INTEGER,
                explanation TEXT,
                created_at TEXT NOT NULL
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS stop_loss_failure_clusters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cluster_name TEXT,
                symbol TEXT,
                side TEXT,
                strategy_type TEXT,
                market_regime TEXT,
                score_bucket TEXT,
                total_sl INTEGER,
                total_tp INTEGER,
                total_time INTEGER,
                avg_adverse_excursion REAL,
                avg_favorable_before_sl REAL,
                reverse_would_have_helped_count INTEGER,
                wider_stop_would_have_helped_count INTEGER,
                closer_tp_would_have_helped_count INTEGER,
                no_trade_filter_would_have_helped_count INTEGER,
                primary_reason TEXT,
                recommended_rule TEXT,
                confidence REAL,
                created_at TEXT NOT NULL
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS win_clusters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cluster_name TEXT,
                symbol TEXT,
                side TEXT,
                strategy_type TEXT,
                market_regime TEXT,
                score_bucket TEXT,
                total_tp INTEGER,
                total_sl INTEGER,
                total_time INTEGER,
                win_rate REAL,
                profit_factor REAL,
                expectancy REAL,
                common_features_json TEXT,
                recommended_rule TEXT,
                confidence REAL,
                created_at TEXT NOT NULL
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS research_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_name TEXT,
                rule_type TEXT,
                condition_json TEXT,
                action TEXT,
                affected_symbols_json TEXT,
                affected_strategies_json TEXT,
                total_labels INTEGER,
                tp_count INTEGER,
                sl_count INTEGER,
                time_count INTEGER,
                win_rate REAL,
                profit_factor REAL,
                expectancy REAL,
                time_ratio REAL,
                evidence_score REAL,
                overfit_risk REAL,
                recommendation TEXT,
                explanation TEXT,
                created_at TEXT NOT NULL
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS virtual_research_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observation_id INTEGER,
                label_id INTEGER,
                variant_name TEXT,
                params_json TEXT,
                symbol TEXT,
                strategy_type TEXT,
                market_regime TEXT,
                virtual_side TEXT,
                entry_price REAL,
                stop_loss REAL,
                take_profit_1 REAL,
                take_profit_2 REAL,
                outcome TEXT,
                label INTEGER,
                return_pct REAL,
                bars_to_outcome INTEGER,
                created_at TEXT NOT NULL
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS virtual_strategy_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                variant_name TEXT,
                params_json TEXT,
                symbol TEXT,
                strategy_type TEXT,
                market_regime TEXT,
                total_trades INTEGER,
                tp_count INTEGER,
                sl_count INTEGER,
                time_count INTEGER,
                profit_factor REAL,
                expectancy REAL,
                decisive_win_rate REAL,
                max_drawdown_estimated REAL,
                score REAL,
                created_at TEXT,
                last_updated TEXT NOT NULL
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS kronos_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT,
                observation_id INTEGER,
                model_name TEXT,
                tokenizer_name TEXT,
                lookback INTEGER,
                pred_len INTEGER,
                current_close REAL,
                predicted_close REAL,
                predicted_return_pct REAL,
                predicted_range_pct REAL,
                direction TEXT,
                confidence_score REAL,
                volatility_score REAL,
                forecast_json TEXT,
                created_at TEXT NOT NULL
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS market_context_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                source TEXT,
                event_type TEXT,
                symbol TEXT,
                severity TEXT,
                title TEXT,
                summary TEXT,
                raw_json TEXT,
                created_at TEXT NOT NULL
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS market_catalysts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                catalyst_id TEXT UNIQUE,
                title TEXT,
                category TEXT,
                symbols TEXT,
                regimes TEXT,
                direction TEXT,
                severity TEXT,
                confidence REAL,
                source TEXT,
                source_url_hash TEXT,
                published_at TEXT,
                start_at TEXT,
                end_at TEXT,
                summary TEXT,
                raw_ref TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS strategy_lab_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                candidate_name TEXT,
                family TEXT,
                params_json TEXT,
                status TEXT,
                reason TEXT,
                total_samples INTEGER DEFAULT 0,
                train_samples INTEGER DEFAULT 0,
                test_samples INTEGER DEFAULT 0,
                in_sample_profit_factor REAL DEFAULT 0,
                out_of_sample_profit_factor REAL DEFAULT 0,
                expectancy REAL DEFAULT 0,
                decisive_win_rate REAL DEFAULT 0,
                drawdown_estimated REAL DEFAULT 0,
                sl_rate REAL DEFAULT 0,
                tp_rate REAL DEFAULT 0,
                time_rate REAL DEFAULT 0,
                stability_score REAL DEFAULT 0,
                overfit_penalty REAL DEFAULT 0,
                conservative_score REAL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS strategy_lab_walkforward (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                candidate_name TEXT,
                window_index INTEGER,
                train_start TEXT,
                train_end TEXT,
                test_start TEXT,
                test_end TEXT,
                train_samples INTEGER DEFAULT 0,
                test_samples INTEGER DEFAULT 0,
                train_profit_factor REAL DEFAULT 0,
                test_profit_factor REAL DEFAULT 0,
                test_expectancy REAL DEFAULT 0,
                test_drawdown REAL DEFAULT 0,
                test_time_rate REAL DEFAULT 0,
                passed INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS strategy_lab_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                recommendation_type TEXT,
                candidate_name TEXT,
                condition_json TEXT,
                action TEXT,
                evidence_score REAL DEFAULT 0,
                explanation TEXT,
                created_at TEXT NOT NULL
            )
            """,
        )
        self._execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS research_autopilot_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT,
                ended_at TEXT,
                status TEXT,
                duration_seconds REAL DEFAULT 0,
                phase2_limit INTEGER DEFAULT 0,
                batch_size INTEGER DEFAULT 0,
                virtual_limit INTEGER DEFAULT 0,
                virtual_concurrency INTEGER DEFAULT 0,
                processed INTEGER DEFAULT 0,
                explanations_created INTEGER DEFAULT 0,
                counterfactuals_created INTEGER DEFAULT 0,
                clusters_updated INTEGER DEFAULT 0,
                rules_generated INTEGER DEFAULT 0,
                virtual_trades_simulated INTEGER DEFAULT 0,
                errors INTEGER DEFAULT 0,
                failure_reason TEXT,
                created_at TEXT NOT NULL
            )
            """,
        )
        self._ensure_research_columns(conn)
        self._create_indexes(conn)

    def _ensure_research_columns(self, conn: Any) -> None:
        columns = {
            "shadow_strategy": "INTEGER DEFAULT 0",
            "strategy_variant_id": "INTEGER",
            "variant_params_json": "TEXT",
            "original_side": "TEXT",
            "original_strategy_type": "TEXT",
            "score_bucket": "TEXT",
            "kronos_predicted_return_pct": "REAL",
            "kronos_direction": "TEXT",
            "kronos_confidence_score": "REAL",
            "kronos_disagreement": "INTEGER",
            "kronos_prediction_id": "INTEGER",
        }
        if self._use_postgres:
            for name, spec in columns.items():
                self._execute(conn, f"ALTER TABLE signal_observations ADD COLUMN IF NOT EXISTS {name} {spec}")
            self._execute(conn, "ALTER TABLE virtual_strategy_summary ADD COLUMN IF NOT EXISTS created_at TEXT")
            for name, spec in {
                "source": "TEXT",
                "probe_key": "TEXT",
                "reject_reason": "TEXT",
                "priority": "INTEGER DEFAULT 0",
                "catalyst_active": "INTEGER DEFAULT 0",
                "catalyst_id": "TEXT",
                "catalyst_category": "TEXT",
                "catalyst_direction": "TEXT",
                "catalyst_severity": "TEXT",
            }.items():
                self._execute(conn, f"ALTER TABLE signal_path_metrics ADD COLUMN IF NOT EXISTS {name} {spec}")
            return
        cur = conn.execute("PRAGMA table_info(signal_observations)")
        existing = {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in cur.fetchall()}
        for name, spec in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE signal_observations ADD COLUMN {name} {spec}")
        cur = conn.execute("PRAGMA table_info(virtual_strategy_summary)")
        existing_summary = {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in cur.fetchall()}
        if "created_at" not in existing_summary:
            conn.execute("ALTER TABLE virtual_strategy_summary ADD COLUMN created_at TEXT")
        cur = conn.execute("PRAGMA table_info(signal_path_metrics)")
        existing_path_metrics = {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in cur.fetchall()}
        for name, spec in {
            "source": "TEXT",
            "probe_key": "TEXT",
            "reject_reason": "TEXT",
            "priority": "INTEGER DEFAULT 0",
            "catalyst_active": "INTEGER DEFAULT 0",
            "catalyst_id": "TEXT",
            "catalyst_category": "TEXT",
            "catalyst_direction": "TEXT",
            "catalyst_severity": "TEXT",
        }.items():
            if name not in existing_path_metrics:
                conn.execute(f"ALTER TABLE signal_path_metrics ADD COLUMN {name} {spec}")

    def _create_indexes(self, conn: Any) -> None:
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_signal_labels_observation_id ON signal_labels(observation_id)",
            "CREATE INDEX IF NOT EXISTS idx_signal_labels_timestamp ON signal_labels(timestamp)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_path_metrics_observation ON signal_path_metrics(observation_id)",
            "CREATE INDEX IF NOT EXISTS idx_signal_path_metrics_created ON signal_path_metrics(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_signal_path_metrics_symbol ON signal_path_metrics(symbol)",
            "CREATE INDEX IF NOT EXISTS idx_signal_path_metrics_regime ON signal_path_metrics(market_regime)",
            "CREATE INDEX IF NOT EXISTS idx_signal_path_metrics_score_bucket ON signal_path_metrics(score_bucket)",
            "CREATE INDEX IF NOT EXISTS idx_signal_path_metrics_side ON signal_path_metrics(side)",
            "CREATE INDEX IF NOT EXISTS idx_signal_path_metrics_status ON signal_path_metrics(status)",
            "CREATE INDEX IF NOT EXISTS idx_signal_path_metrics_source ON signal_path_metrics(source)",
            "CREATE INDEX IF NOT EXISTS idx_signal_path_metrics_probe_key ON signal_path_metrics(probe_key)",
            "CREATE INDEX IF NOT EXISTS idx_signal_path_metrics_catalyst ON signal_path_metrics(catalyst_active, catalyst_id)",
            "CREATE INDEX IF NOT EXISTS idx_signal_explanations_observation_label ON signal_explanations(observation_id, label_id)",
            "CREATE INDEX IF NOT EXISTS idx_signal_price_paths_observation_label ON signal_price_paths(observation_id, label_id)",
            "CREATE INDEX IF NOT EXISTS idx_signal_counterfactuals_observation_label_scenario ON signal_counterfactuals(observation_id, label_id, scenario_name)",
            "CREATE INDEX IF NOT EXISTS idx_stop_loss_failure_clusters_name ON stop_loss_failure_clusters(cluster_name)",
            "CREATE INDEX IF NOT EXISTS idx_win_clusters_name ON win_clusters(cluster_name)",
            "CREATE INDEX IF NOT EXISTS idx_research_rules_name ON research_rules(rule_name)",
            "CREATE INDEX IF NOT EXISTS idx_virtual_research_trade_key ON virtual_research_trades(variant_name, observation_id, label_id)",
            "CREATE INDEX IF NOT EXISTS idx_virtual_strategy_summary_variant ON virtual_strategy_summary(variant_name)",
            "CREATE INDEX IF NOT EXISTS idx_kronos_predictions_observation ON kronos_predictions(observation_id)",
            "CREATE INDEX IF NOT EXISTS idx_kronos_predictions_symbol_time ON kronos_predictions(symbol, timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_strategy_lab_candidates_run ON strategy_lab_candidates(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_strategy_lab_walkforward_run ON strategy_lab_walkforward(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_strategy_lab_recommendations_run ON strategy_lab_recommendations(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_research_autopilot_runs_started ON research_autopilot_runs(started_at)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_market_catalysts_catalyst_id ON market_catalysts(catalyst_id)",
            "CREATE INDEX IF NOT EXISTS idx_market_catalysts_window ON market_catalysts(start_at, end_at)",
            "CREATE INDEX IF NOT EXISTS idx_market_catalysts_category ON market_catalysts(category)",
            "CREATE INDEX IF NOT EXISTS idx_market_catalysts_direction ON market_catalysts(direction)",
            "CREATE INDEX IF NOT EXISTS idx_latency_metrics_time_name ON latency_metrics(timestamp, metric_name)",
            "CREATE INDEX IF NOT EXISTS idx_events_timestamp_type ON events(timestamp, event_type)",
            "CREATE INDEX IF NOT EXISTS idx_signal_observations_timestamp ON signal_observations(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_signal_observations_score_time ON signal_observations(confidence_score, timestamp)",
        ]
        for sql in indexes:
            self._execute(conn, sql)

    def record_event(self, event_type: str, message: str, level: str = "INFO", payload: Any | None = None) -> None:
        with self._connect() as conn:
            self._execute(
                conn,
                "INSERT INTO events(timestamp, level, event_type, message, payload_json) VALUES (?, ?, ?, ?, ?)",
                (iso_utc(), level, event_type, message, json_dumps(payload or {})),
            )

    def record_latency_metric(
        self,
        metric_name: str,
        duration_ms: float,
        *,
        component: str = "",
        payload: Any | None = None,
    ) -> None:
        sql = """
            INSERT INTO latency_metrics(timestamp, metric_name, component, duration_ms, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """
        with self._connect() as conn:
            self._execute(
                conn,
                sql,
                (iso_utc(), str(metric_name or "unknown")[:120], str(component or "")[:120], float(duration_ms or 0.0), json_dumps(payload or {}), iso_utc()),
            )

    def fetch_latency_metrics_since(self, since_iso: str, limit: int = 50000) -> list[dict[str, Any]]:
        sql = """
            SELECT *
            FROM latency_metrics
            WHERE timestamp >= ?
            ORDER BY timestamp ASC, id ASC
            LIMIT ?
        """
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        try:
            with self._connect() as conn:
                return self._fetchall_dicts(conn.execute(sql, (since_iso, max(1, int(limit or 50000)))))
        except Exception:
            return []

    def record_trade(
        self,
        *,
        mode: str,
        signal: Any,
        status: str,
        risk_amount: float = 0.0,
        raw_order_response: Any | None = None,
        error_message: str = "",
    ) -> int:
        payload = asdict(signal) if is_dataclass(signal) else dict(signal)
        raw_signal_json = json_dumps(payload)
        sql = """
            INSERT INTO trades(
                timestamp, mode, symbol, strategy_type, side, entry, stop_loss, take_profit_1,
                take_profit_2, trailing_stop, size, leverage, risk_amount, confidence_score,
                reason, status, realized_pnl, unrealized_pnl, fees, slippage, error_message,
                raw_signal_json, raw_order_response_sanitized
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        if self._use_postgres:
            sql = sql.replace("?", "%s") + " RETURNING id"
        with self._connect() as conn:
            cur = conn.execute(
                sql,
                (
                    iso_utc(),
                    mode,
                    payload.get("symbol"),
                    payload.get("strategy_type"),
                    payload.get("side"),
                    payload.get("entry_price"),
                    payload.get("stop_loss"),
                    payload.get("take_profit_1"),
                    payload.get("take_profit_2"),
                    str(payload.get("trailing_stop_enabled")),
                    payload.get("position_size", 0),
                    payload.get("leverage_recommendation", 0),
                    risk_amount,
                    payload.get("confidence_score", 0),
                    payload.get("reason", ""),
                    status,
                    0.0,
                    0.0,
                    payload.get("estimated_fees", 0.0),
                    payload.get("estimated_slippage", 0.0),
                    error_message,
                    raw_signal_json,
                    json_dumps(sanitize(raw_order_response or {})),
                ),
            )
            return self._inserted_id(cur)

    def record_signal_observation(self, observation: dict[str, Any]) -> int:
        allowed = {
            "timestamp",
            "symbol",
            "side",
            "strategy_type",
            "confidence_score",
            "market_regime",
            "entry_price",
            "stop_loss",
            "take_profit_1",
            "take_profit_2",
            "risk_reward_ratio",
            "leverage_recommendation",
            "spread_pct",
            "volume_24h_usdt",
            "funding_rate",
            "open_interest",
            "timeframe_alignment",
            "confirmations_json",
            "warnings_json",
            "rsi_14",
            "macd_hist",
            "atr_14",
            "normalized_atr",
            "volume_relative",
            "distance_to_ema_21",
            "distance_to_ema_50",
            "distance_to_ema_200",
            "momentum_5",
            "momentum_15",
            "range_width_pct",
            "body_pct",
            "upper_wick_pct",
            "lower_wick_pct",
            "bullish_rejection",
            "bearish_rejection",
            "btc_regime",
            "btc_momentum_5",
            "btc_momentum_15",
            "btc_normalized_atr",
            "eth_momentum_5",
            "number_of_symbols_bullish",
            "number_of_symbols_bearish",
            "market_risk_on",
            "market_risk_off",
            "operated",
            "block_reason",
            "selected_by_allocator",
            "risk_manager_approved",
            "meta_probability",
            "meta_decision",
            "shadow_strategy",
            "strategy_variant_id",
            "variant_params_json",
            "original_side",
            "original_strategy_type",
            "score_bucket",
            "kronos_predicted_return_pct",
            "kronos_direction",
            "kronos_confidence_score",
            "kronos_disagreement",
            "kronos_prediction_id",
            "raw_signal_json",
            "raw_features_json",
        }
        payload = {key: observation.get(key) for key in allowed if key in observation}
        payload.setdefault("timestamp", iso_utc())
        columns = list(payload.keys())
        placeholders = ", ".join(["?"] * len(columns))
        sql = f"INSERT INTO signal_observations({', '.join(columns)}) VALUES ({placeholders})"
        if self._use_postgres:
            sql = sql.replace("?", "%s") + " RETURNING id"
        with self._connect() as conn:
            cur = conn.execute(sql, tuple(payload[col] for col in columns))
            return self._inserted_id(cur)

    def update_signal_observation(self, observation_id: int, **updates: Any) -> None:
        allowed = {
            "operated",
            "block_reason",
            "selected_by_allocator",
            "risk_manager_approved",
            "meta_probability",
            "meta_decision",
            "shadow_strategy",
            "strategy_variant_id",
            "variant_params_json",
            "original_side",
            "original_strategy_type",
            "score_bucket",
            "kronos_predicted_return_pct",
            "kronos_direction",
            "kronos_confidence_score",
            "kronos_disagreement",
            "kronos_prediction_id",
        }
        payload = {key: value for key, value in updates.items() if key in allowed}
        if not payload:
            return
        assignments = ", ".join(f"{key}=?" for key in payload)
        sql = f"UPDATE signal_observations SET {assignments} WHERE id=?"
        params = tuple(payload.values()) + (observation_id,)
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            conn.execute(sql, params)

    def record_signal_label(self, label: dict[str, Any]) -> int:
        payload = {
            "timestamp": label.get("timestamp", iso_utc()),
            "observation_id": label.get("observation_id"),
            "label": label.get("label"),
            "first_barrier_hit": label.get("first_barrier_hit"),
            "bars_to_outcome": label.get("bars_to_outcome"),
            "max_favorable_excursion": label.get("max_favorable_excursion"),
            "max_adverse_excursion": label.get("max_adverse_excursion"),
            "realized_return_pct": label.get("realized_return_pct"),
            "simulated_pnl": label.get("simulated_pnl"),
            "would_have_won": label.get("would_have_won"),
            "raw_label_json": json_dumps(label.get("raw_label_json", label)),
        }
        columns = list(payload.keys())
        placeholders = ", ".join(["?"] * len(columns))
        sql = f"INSERT INTO signal_labels({', '.join(columns)}) VALUES ({placeholders})"
        if self._use_postgres:
            sql = sql.replace("?", "%s") + " RETURNING id"
        with self._connect() as conn:
            cur = conn.execute(sql, tuple(payload[col] for col in columns))
            return self._inserted_id(cur)

    def ensure_strategy_variant(self, name: str, params: dict[str, Any], enabled: bool = True) -> int:
        params_json = json_dumps(params)
        with self._connect() as conn:
            sql = "SELECT id FROM strategy_variants WHERE name=?"
            if self._use_postgres:
                sql = sql.replace("?", "%s")
            row = conn.execute(sql, (name,)).fetchone()
            if row:
                return int(self._row_value(row, "id", 0, 0) or 0)
            insert_sql = "INSERT INTO strategy_variants(name, params_json, enabled, created_at) VALUES (?, ?, ?, ?)"
            if self._use_postgres:
                insert_sql = insert_sql.replace("?", "%s") + " RETURNING id"
            cur = conn.execute(insert_sql, (name, params_json, int(enabled), iso_utc()))
            return self._inserted_id(cur)

    def fetch_strategy_variants(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM strategy_variants"
        params: tuple[Any, ...] = ()
        if enabled_only:
            sql += " WHERE enabled=?"
            params = (1,)
        sql += " ORDER BY id ASC"
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql, params))

    def fetch_strategy_variant_results(self) -> list[dict[str, Any]]:
        sql = """
            SELECT sv.id, sv.name, sv.params_json, sv.enabled, svr.*
            FROM strategy_variants sv
            LEFT JOIN strategy_variant_results svr ON svr.variant_id = sv.id
            ORDER BY COALESCE(svr.score, 0) DESC, sv.id ASC
        """
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql))

    def fetch_strategy_variant_labeled_rows(self) -> list[dict[str, Any]]:
        sql = """
            SELECT so.*, sl.label, sl.first_barrier_hit, sl.bars_to_outcome,
                   sl.max_favorable_excursion, sl.max_adverse_excursion,
                   sl.realized_return_pct, sl.simulated_pnl, sl.would_have_won,
                   sv.name AS variant_name, sv.params_json AS strategy_variant_params_json
            FROM signal_observations so
            JOIN signal_labels sl ON sl.observation_id = so.id
            LEFT JOIN strategy_variants sv ON sv.id = so.strategy_variant_id
            WHERE COALESCE(so.shadow_strategy, 0) = 1
            ORDER BY so.timestamp ASC
        """
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql))

    def upsert_strategy_variant_result(self, result: dict[str, Any]) -> None:
        payload = {
            "variant_id": result.get("variant_id"),
            "total_labels": result.get("total_labels", 0),
            "tp1_count": result.get("tp1_count", 0),
            "tp2_count": result.get("tp2_count", 0),
            "sl_count": result.get("sl_count", 0),
            "time_count": result.get("time_count", 0),
            "win_rate": result.get("win_rate", 0.0),
            "profit_factor": result.get("profit_factor", 0.0),
            "avg_return": result.get("avg_return", 0.0),
            "max_drawdown_estimated": result.get("max_drawdown_estimated", 0.0),
            "score": result.get("score", 0.0),
            "last_updated": result.get("last_updated", iso_utc()),
        }
        if self._use_postgres:
            sql = """
                INSERT INTO strategy_variant_results(
                    variant_id, total_labels, tp1_count, tp2_count, sl_count, time_count,
                    win_rate, profit_factor, avg_return, max_drawdown_estimated, score, last_updated
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (variant_id) DO UPDATE SET
                    total_labels=EXCLUDED.total_labels,
                    tp1_count=EXCLUDED.tp1_count,
                    tp2_count=EXCLUDED.tp2_count,
                    sl_count=EXCLUDED.sl_count,
                    time_count=EXCLUDED.time_count,
                    win_rate=EXCLUDED.win_rate,
                    profit_factor=EXCLUDED.profit_factor,
                    avg_return=EXCLUDED.avg_return,
                    max_drawdown_estimated=EXCLUDED.max_drawdown_estimated,
                    score=EXCLUDED.score,
                    last_updated=EXCLUDED.last_updated
            """
        else:
            sql = """
                INSERT OR REPLACE INTO strategy_variant_results(
                    variant_id, total_labels, tp1_count, tp2_count, sl_count, time_count,
                    win_rate, profit_factor, avg_return, max_drawdown_estimated, score, last_updated
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
        with self._connect() as conn:
            conn.execute(sql, tuple(payload.values()))

    def fetch_signal_observations(self, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM signal_observations ORDER BY timestamp ASC"
        params: tuple[Any, ...] = ()
        if limit:
            sql += " LIMIT ?"
            params = (limit,)
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            return self._fetchall_dicts(cur)

    def get_signal_observation_summary(self) -> dict[str, int]:
        sql = """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN COALESCE(operated, 0) = 1 THEN 1 ELSE 0 END) AS operated_count,
                SUM(CASE WHEN COALESCE(selected_by_allocator, 0) = 1 THEN 1 ELSE 0 END) AS selected_by_allocator_count,
                SUM(CASE WHEN COALESCE(risk_manager_approved, 0) = 1 THEN 1 ELSE 0 END) AS risk_manager_approved_count,
                SUM(CASE WHEN COALESCE(shadow_strategy, 0) = 1 THEN 1 ELSE 0 END) AS shadow_strategy_count,
                SUM(CASE WHEN side = 'NO_TRADE' THEN 1 ELSE 0 END) AS no_trade_count,
                SUM(CASE WHEN side = 'LONG' THEN 1 ELSE 0 END) AS long_count,
                SUM(CASE WHEN side = 'SHORT' THEN 1 ELSE 0 END) AS short_count
            FROM signal_observations
        """
        try:
            with self._connect() as conn:
                row = conn.execute(sql).fetchone()
                return {
                    "total": int(self._row_value(row, "total", 0, 0) or 0),
                    "operated_count": int(self._row_value(row, "operated_count", 1, 0) or 0),
                    "selected_by_allocator_count": int(self._row_value(row, "selected_by_allocator_count", 2, 0) or 0),
                    "risk_manager_approved_count": int(self._row_value(row, "risk_manager_approved_count", 3, 0) or 0),
                    "shadow_strategy_count": int(self._row_value(row, "shadow_strategy_count", 4, 0) or 0),
                    "no_trade_count": int(self._row_value(row, "no_trade_count", 5, 0) or 0),
                    "long_count": int(self._row_value(row, "long_count", 6, 0) or 0),
                    "short_count": int(self._row_value(row, "short_count", 7, 0) or 0),
                }
        except Exception:
            return {
                "total": 0,
                "operated_count": 0,
                "selected_by_allocator_count": 0,
                "risk_manager_approved_count": 0,
                "shadow_strategy_count": 0,
                "no_trade_count": 0,
                "long_count": 0,
                "short_count": 0,
            }

    def get_training_observation_summary_since(self, since_iso: str, min_score: int = 72, limit: int = 5) -> dict[str, Any]:
        summary_sql = """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN side = 'LONG' THEN 1 ELSE 0 END) AS long_count,
                SUM(CASE WHEN side = 'SHORT' THEN 1 ELSE 0 END) AS short_count,
                SUM(CASE WHEN side = 'NO_TRADE' THEN 1 ELSE 0 END) AS no_trade_count,
                SUM(CASE WHEN COALESCE(confidence_score, 0) >= ? AND side IN ('LONG', 'SHORT') THEN 1 ELSE 0 END) AS high_score_count,
                SUM(CASE WHEN COALESCE(operated, 0) = 1 THEN 1 ELSE 0 END) AS operated_count,
                SUM(CASE WHEN COALESCE(selected_by_allocator, 0) = 1 THEN 1 ELSE 0 END) AS selected_count,
                SUM(CASE WHEN COALESCE(risk_manager_approved, 0) = 1 THEN 1 ELSE 0 END) AS risk_approved_count
            FROM signal_observations
            WHERE timestamp >= ?
        """
        regime_sql = """
            SELECT COALESCE(market_regime, 'NA') AS key, COUNT(*) AS count
            FROM signal_observations
            WHERE timestamp >= ?
            GROUP BY COALESCE(market_regime, 'NA')
            ORDER BY count DESC
            LIMIT ?
        """
        symbol_sql = """
            SELECT COALESCE(symbol, 'NA') AS key, COUNT(*) AS count, MAX(COALESCE(confidence_score, 0)) AS max_score
            FROM signal_observations
            WHERE timestamp >= ?
              AND COALESCE(confidence_score, 0) >= ?
              AND side IN ('LONG', 'SHORT')
            GROUP BY COALESCE(symbol, 'NA')
            ORDER BY count DESC, max_score DESC
            LIMIT ?
        """
        if self._use_postgres:
            summary_sql = summary_sql.replace("?", "%s")
            regime_sql = regime_sql.replace("?", "%s")
            symbol_sql = symbol_sql.replace("?", "%s")
        try:
            with self._connect() as conn:
                row = conn.execute(summary_sql, (int(min_score), since_iso)).fetchone()
                regimes = self._fetchall_dicts(conn.execute(regime_sql, (since_iso, int(limit))))
                symbols = self._fetchall_dicts(conn.execute(symbol_sql, (since_iso, int(min_score), int(limit))))
            return {
                "total": int(self._row_value(row, "total", 0, 0) or 0),
                "long_count": int(self._row_value(row, "long_count", 1, 0) or 0),
                "short_count": int(self._row_value(row, "short_count", 2, 0) or 0),
                "no_trade_count": int(self._row_value(row, "no_trade_count", 3, 0) or 0),
                "high_score_count": int(self._row_value(row, "high_score_count", 4, 0) or 0),
                "operated_count": int(self._row_value(row, "operated_count", 5, 0) or 0),
                "selected_count": int(self._row_value(row, "selected_count", 6, 0) or 0),
                "risk_approved_count": int(self._row_value(row, "risk_approved_count", 7, 0) or 0),
                "regimes": regimes,
                "top_symbols": symbols,
            }
        except Exception:
            return {
                "total": 0,
                "long_count": 0,
                "short_count": 0,
                "no_trade_count": 0,
                "high_score_count": 0,
                "operated_count": 0,
                "selected_count": 0,
                "risk_approved_count": 0,
                "regimes": [],
                "top_symbols": [],
            }

    def get_signal_label_summary(self) -> dict[str, float]:
        sql = """
            SELECT
                COUNT(*) AS total_labels,
                SUM(CASE WHEN sl.first_barrier_hit = 'TIME' THEN 1 ELSE 0 END) AS time_count,
                SUM(CASE WHEN sl.first_barrier_hit = 'SL' THEN 1 ELSE 0 END) AS sl_count,
                SUM(CASE WHEN sl.first_barrier_hit = 'TP1' THEN 1 ELSE 0 END) AS tp1_count,
                SUM(CASE WHEN sl.first_barrier_hit = 'TP2' THEN 1 ELSE 0 END) AS tp2_count,
                AVG(CASE WHEN sl.first_barrier_hit = 'TIME' THEN COALESCE(sl.realized_return_pct, 0) ELSE NULL END) AS avg_return_time,
                AVG(CASE WHEN sl.first_barrier_hit = 'SL' THEN COALESCE(sl.realized_return_pct, 0) ELSE NULL END) AS avg_return_sl,
                AVG(CASE WHEN sl.first_barrier_hit = 'TP1' THEN COALESCE(sl.realized_return_pct, 0) ELSE NULL END) AS avg_return_tp1,
                AVG(CASE WHEN sl.first_barrier_hit = 'TP2' THEN COALESCE(sl.realized_return_pct, 0) ELSE NULL END) AS avg_return_tp2,
                AVG(COALESCE(sl.realized_return_pct, 0)) AS avg_return_all,
                SUM(CASE WHEN COALESCE(sl.realized_return_pct, 0) > 0 THEN COALESCE(sl.realized_return_pct, 0) ELSE 0 END) AS gains,
                SUM(CASE WHEN COALESCE(sl.realized_return_pct, 0) < 0 THEN COALESCE(sl.realized_return_pct, 0) ELSE 0 END) AS losses,
                SUM(CASE WHEN sl.first_barrier_hit IN ('TP1', 'TP2', 'SL') THEN 1 ELSE 0 END) AS decisive_count,
                SUM(CASE WHEN sl.first_barrier_hit IN ('TP1', 'TP2', 'SL') AND COALESCE(sl.label, 0) = 1 THEN 1 ELSE 0 END) AS decisive_wins,
                SUM(CASE WHEN COALESCE(so.shadow_strategy, 0) = 1 THEN 1 ELSE 0 END) AS shadow_labels_count
            FROM signal_labels sl
            LEFT JOIN signal_observations so ON so.id = sl.observation_id
        """
        try:
            with self._connect() as conn:
                row = conn.execute(sql).fetchone()
                return self._signal_label_summary_from_row(row)
        except Exception:
            base_sql = """
                SELECT
                    COUNT(*) AS total_labels,
                    SUM(CASE WHEN sl.first_barrier_hit = 'TIME' THEN 1 ELSE 0 END) AS time_count,
                    SUM(CASE WHEN sl.first_barrier_hit = 'SL' THEN 1 ELSE 0 END) AS sl_count,
                    SUM(CASE WHEN sl.first_barrier_hit = 'TP1' THEN 1 ELSE 0 END) AS tp1_count,
                    SUM(CASE WHEN sl.first_barrier_hit = 'TP2' THEN 1 ELSE 0 END) AS tp2_count,
                    AVG(CASE WHEN sl.first_barrier_hit = 'TIME' THEN COALESCE(sl.realized_return_pct, 0) ELSE NULL END) AS avg_return_time,
                    AVG(CASE WHEN sl.first_barrier_hit = 'SL' THEN COALESCE(sl.realized_return_pct, 0) ELSE NULL END) AS avg_return_sl,
                    AVG(CASE WHEN sl.first_barrier_hit = 'TP1' THEN COALESCE(sl.realized_return_pct, 0) ELSE NULL END) AS avg_return_tp1,
                    AVG(CASE WHEN sl.first_barrier_hit = 'TP2' THEN COALESCE(sl.realized_return_pct, 0) ELSE NULL END) AS avg_return_tp2,
                    AVG(COALESCE(sl.realized_return_pct, 0)) AS avg_return_all,
                    SUM(CASE WHEN COALESCE(sl.realized_return_pct, 0) > 0 THEN COALESCE(sl.realized_return_pct, 0) ELSE 0 END) AS gains,
                    SUM(CASE WHEN COALESCE(sl.realized_return_pct, 0) < 0 THEN COALESCE(sl.realized_return_pct, 0) ELSE 0 END) AS losses,
                    SUM(CASE WHEN sl.first_barrier_hit IN ('TP1', 'TP2', 'SL') THEN 1 ELSE 0 END) AS decisive_count,
                    SUM(CASE WHEN sl.first_barrier_hit IN ('TP1', 'TP2', 'SL') AND COALESCE(sl.label, 0) = 1 THEN 1 ELSE 0 END) AS decisive_wins
                FROM signal_labels sl
            """
            try:
                with self._connect() as conn:
                    row = conn.execute(base_sql).fetchone()
                    return self._signal_label_summary_from_row(row, include_shadow=False)
            except Exception:
                return self._empty_signal_label_summary()

    def _signal_label_summary_from_row(self, row: Any, include_shadow: bool = True) -> dict[str, float]:
        total = float(self._row_value(row, "total_labels", 0, 0) or 0)
        gains = float(self._row_value(row, "gains", 10, 0.0) or 0.0)
        losses = abs(float(self._row_value(row, "losses", 11, 0.0) or 0.0))
        decisive_count = float(self._row_value(row, "decisive_count", 12, 0) or 0)
        decisive_wins = float(self._row_value(row, "decisive_wins", 13, 0) or 0)
        shadow = float(self._row_value(row, "shadow_labels_count", 14, 0) or 0) if include_shadow else 0.0
        if losses > 0:
            profit_factor = gains / losses
        else:
            profit_factor = 999.0 if gains > 0 else 0.0
        return {
            "total_labels": total,
            "time_count": float(self._row_value(row, "time_count", 1, 0) or 0),
            "sl_count": float(self._row_value(row, "sl_count", 2, 0) or 0),
            "tp1_count": float(self._row_value(row, "tp1_count", 3, 0) or 0),
            "tp2_count": float(self._row_value(row, "tp2_count", 4, 0) or 0),
            "avg_return_time": float(self._row_value(row, "avg_return_time", 5, 0.0) or 0.0),
            "avg_return_sl": float(self._row_value(row, "avg_return_sl", 6, 0.0) or 0.0),
            "avg_return_tp1": float(self._row_value(row, "avg_return_tp1", 7, 0.0) or 0.0),
            "avg_return_tp2": float(self._row_value(row, "avg_return_tp2", 8, 0.0) or 0.0),
            "avg_return_all": float(self._row_value(row, "avg_return_all", 9, 0.0) or 0.0),
            "profit_factor": profit_factor,
            "decisive_win_rate": decisive_wins / max(decisive_count, 1.0),
            "shadow_labels_count": shadow,
            "normal_labels_count": max(0.0, total - shadow),
        }

    @staticmethod
    def _empty_signal_label_summary() -> dict[str, float]:
        return {
            "total_labels": 0.0,
            "time_count": 0.0,
            "sl_count": 0.0,
            "tp1_count": 0.0,
            "tp2_count": 0.0,
            "avg_return_time": 0.0,
            "avg_return_sl": 0.0,
            "avg_return_tp1": 0.0,
            "avg_return_tp2": 0.0,
            "avg_return_all": 0.0,
            "profit_factor": 0.0,
            "decisive_win_rate": 0.0,
            "shadow_labels_count": 0.0,
            "normal_labels_count": 0.0,
        }

    def get_signal_label_summary_since(self, since_iso: str) -> dict[str, float]:
        sql = """
            SELECT
                COUNT(*) AS total_labels,
                SUM(CASE WHEN sl.first_barrier_hit = 'TIME' THEN 1 ELSE 0 END) AS time_count,
                SUM(CASE WHEN sl.first_barrier_hit = 'SL' THEN 1 ELSE 0 END) AS sl_count,
                SUM(CASE WHEN sl.first_barrier_hit = 'TP1' THEN 1 ELSE 0 END) AS tp1_count,
                SUM(CASE WHEN sl.first_barrier_hit = 'TP2' THEN 1 ELSE 0 END) AS tp2_count,
                AVG(CASE WHEN sl.first_barrier_hit = 'TIME' THEN COALESCE(sl.realized_return_pct, 0) ELSE NULL END) AS avg_return_time,
                AVG(CASE WHEN sl.first_barrier_hit = 'SL' THEN COALESCE(sl.realized_return_pct, 0) ELSE NULL END) AS avg_return_sl,
                AVG(CASE WHEN sl.first_barrier_hit = 'TP1' THEN COALESCE(sl.realized_return_pct, 0) ELSE NULL END) AS avg_return_tp1,
                AVG(CASE WHEN sl.first_barrier_hit = 'TP2' THEN COALESCE(sl.realized_return_pct, 0) ELSE NULL END) AS avg_return_tp2,
                AVG(COALESCE(sl.realized_return_pct, 0)) AS avg_return_all,
                SUM(CASE WHEN COALESCE(sl.realized_return_pct, 0) > 0 THEN COALESCE(sl.realized_return_pct, 0) ELSE 0 END) AS gains,
                SUM(CASE WHEN COALESCE(sl.realized_return_pct, 0) < 0 THEN COALESCE(sl.realized_return_pct, 0) ELSE 0 END) AS losses,
                SUM(CASE WHEN sl.first_barrier_hit IN ('TP1', 'TP2', 'SL') THEN 1 ELSE 0 END) AS decisive_count,
                SUM(CASE WHEN sl.first_barrier_hit IN ('TP1', 'TP2', 'SL') AND COALESCE(sl.label, 0) = 1 THEN 1 ELSE 0 END) AS decisive_wins,
                SUM(CASE WHEN COALESCE(so.shadow_strategy, 0) = 1 THEN 1 ELSE 0 END) AS shadow_labels_count
            FROM signal_labels sl
            LEFT JOIN signal_observations so ON so.id = sl.observation_id
            WHERE sl.timestamp >= ?
        """
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        try:
            with self._connect() as conn:
                row = conn.execute(sql, (since_iso,)).fetchone()
                return self._signal_label_summary_from_row(row)
        except Exception:
            return self._empty_signal_label_summary()

    def get_high_score_label_summary_since(self, since_iso: str, min_score: int = 72) -> dict[str, float]:
        sql = """
            SELECT
                COUNT(*) AS total_labels,
                SUM(CASE WHEN sl.first_barrier_hit = 'TIME' THEN 1 ELSE 0 END) AS time_count,
                SUM(CASE WHEN sl.first_barrier_hit = 'SL' THEN 1 ELSE 0 END) AS sl_count,
                SUM(CASE WHEN sl.first_barrier_hit = 'TP1' THEN 1 ELSE 0 END) AS tp1_count,
                SUM(CASE WHEN sl.first_barrier_hit = 'TP2' THEN 1 ELSE 0 END) AS tp2_count,
                AVG(CASE WHEN sl.first_barrier_hit = 'TIME' THEN COALESCE(sl.realized_return_pct, 0) ELSE NULL END) AS avg_return_time,
                AVG(CASE WHEN sl.first_barrier_hit = 'SL' THEN COALESCE(sl.realized_return_pct, 0) ELSE NULL END) AS avg_return_sl,
                AVG(CASE WHEN sl.first_barrier_hit = 'TP1' THEN COALESCE(sl.realized_return_pct, 0) ELSE NULL END) AS avg_return_tp1,
                AVG(CASE WHEN sl.first_barrier_hit = 'TP2' THEN COALESCE(sl.realized_return_pct, 0) ELSE NULL END) AS avg_return_tp2,
                AVG(COALESCE(sl.realized_return_pct, 0)) AS avg_return_all,
                SUM(CASE WHEN COALESCE(sl.realized_return_pct, 0) > 0 THEN COALESCE(sl.realized_return_pct, 0) ELSE 0 END) AS gains,
                SUM(CASE WHEN COALESCE(sl.realized_return_pct, 0) < 0 THEN COALESCE(sl.realized_return_pct, 0) ELSE 0 END) AS losses,
                SUM(CASE WHEN sl.first_barrier_hit IN ('TP1', 'TP2', 'SL') THEN 1 ELSE 0 END) AS decisive_count,
                SUM(CASE WHEN sl.first_barrier_hit IN ('TP1', 'TP2', 'SL') AND COALESCE(sl.label, 0) = 1 THEN 1 ELSE 0 END) AS decisive_wins,
                SUM(CASE WHEN COALESCE(so.shadow_strategy, 0) = 1 THEN 1 ELSE 0 END) AS shadow_labels_count
            FROM signal_labels sl
            JOIN signal_observations so ON so.id = sl.observation_id
            WHERE sl.timestamp >= ?
              AND COALESCE(so.confidence_score, 0) >= ?
              AND so.side IN ('LONG', 'SHORT')
        """
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        try:
            with self._connect() as conn:
                row = conn.execute(sql, (since_iso, int(min_score))).fetchone()
                return self._signal_label_summary_from_row(row)
        except Exception:
            return self._empty_signal_label_summary()

    def get_shadow_opportunity_group_summaries_since(
        self,
        since_iso: str,
        *,
        min_score: int = 72,
        group_key: str = "symbol",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        score_bucket_expr = """
            CASE
                WHEN COALESCE(so.confidence_score, 0) >= 90 THEN '90-100'
                WHEN COALESCE(so.confidence_score, 0) >= 80 THEN '80-89'
                WHEN COALESCE(so.confidence_score, 0) >= 70 THEN '70-79'
                ELSE '<70'
            END
        """
        allowed = {
            "symbol": "COALESCE(so.symbol, 'NA')",
            "market_regime": "COALESCE(so.market_regime, 'NA')",
            "side": "COALESCE(so.side, 'NA')",
            "score_bucket": score_bucket_expr,
            "block_reason": "COALESCE(NULLIF(so.block_reason, ''), 'none')",
        }
        group_expr = allowed.get(group_key)
        if not group_expr:
            return []
        sql = f"""
            SELECT
                {group_expr} AS group_value,
                COUNT(*) AS total_labels,
                SUM(CASE WHEN sl.first_barrier_hit = 'TIME' THEN 1 ELSE 0 END) AS time_count,
                SUM(CASE WHEN sl.first_barrier_hit = 'SL' THEN 1 ELSE 0 END) AS sl_count,
                SUM(CASE WHEN sl.first_barrier_hit = 'TP1' THEN 1 ELSE 0 END) AS tp1_count,
                SUM(CASE WHEN sl.first_barrier_hit = 'TP2' THEN 1 ELSE 0 END) AS tp2_count,
                AVG(COALESCE(sl.realized_return_pct, 0)) AS avg_return,
                SUM(CASE WHEN COALESCE(sl.realized_return_pct, 0) > 0 THEN COALESCE(sl.realized_return_pct, 0) ELSE 0 END) AS gains,
                SUM(CASE WHEN COALESCE(sl.realized_return_pct, 0) < 0 THEN COALESCE(sl.realized_return_pct, 0) ELSE 0 END) AS losses
            FROM signal_labels sl
            JOIN signal_observations so ON so.id = sl.observation_id
            WHERE sl.timestamp >= ?
              AND COALESCE(so.confidence_score, 0) >= ?
              AND so.side IN ('LONG', 'SHORT')
            GROUP BY {group_expr}
            ORDER BY total_labels DESC
            LIMIT ?
        """
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        try:
            with self._connect() as conn:
                rows = self._fetchall_dicts(conn.execute(sql, (since_iso, int(min_score), int(limit))))
            return [_with_edge_metrics(row) for row in rows]
        except Exception:
            return []

    def get_missed_high_score_summary_since(self, since_iso: str, limit: int = 20) -> dict[str, Any]:
        count_sql = """
            SELECT COUNT(*) AS total
            FROM events
            WHERE timestamp >= ?
              AND event_type = ?
        """
        sql = """
            SELECT payload_json
            FROM events
            WHERE timestamp >= ?
              AND event_type = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """
        if self._use_postgres:
            count_sql = count_sql.replace("?", "%s")
            sql = sql.replace("?", "%s")
        reason_counts: dict[str, int] = {}
        total = 0
        try:
            with self._connect() as conn:
                total_row = conn.execute(count_sql, (since_iso, "training_high_score_missed")).fetchone()
                total = int(self._row_value(total_row, "total", 0, 0) or 0)
                rows = self._fetchall_dicts(conn.execute(sql, (since_iso, "training_high_score_missed", int(limit))))
            for row in rows:
                try:
                    payload = json.loads(row.get("payload_json") or "{}")
                except (TypeError, ValueError):
                    payload = {}
                reason = str(payload.get("reason") or "unknown")[:120]
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        except Exception:
            return {"total": 0, "by_reason": []}
        return {
            "total": total,
            "by_reason": [
                {"reason": reason, "count": count}
                for reason, count in sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)
            ],
        }

    def get_label_group_summaries(
        self,
        group_key: str,
        *,
        since_iso: str,
        min_labels: int = 100,
        limit: int = 5,
        best: bool = True,
    ) -> list[dict[str, Any]]:
        allowed = {
            "symbol": "so.symbol",
            "strategy_type": "so.strategy_type",
            "market_regime": "so.market_regime",
        }
        column = allowed.get(group_key)
        if not column:
            return []
        order = "DESC" if best else "ASC"
        sql = f"""
            SELECT
                COALESCE({column}, 'NA') AS group_value,
                COUNT(*) AS total_labels,
                SUM(CASE WHEN sl.first_barrier_hit IN ('TP1', 'TP2') THEN 1 ELSE 0 END) AS tp_count,
                SUM(CASE WHEN sl.first_barrier_hit = 'SL' THEN 1 ELSE 0 END) AS sl_count,
                SUM(CASE WHEN sl.first_barrier_hit = 'TIME' THEN 1 ELSE 0 END) AS time_count,
                AVG(COALESCE(sl.realized_return_pct, 0)) AS expectancy,
                SUM(CASE WHEN COALESCE(sl.realized_return_pct, 0) > 0 THEN COALESCE(sl.realized_return_pct, 0) ELSE 0 END) AS gains,
                SUM(CASE WHEN COALESCE(sl.realized_return_pct, 0) < 0 THEN COALESCE(sl.realized_return_pct, 0) ELSE 0 END) AS losses
            FROM signal_labels sl
            JOIN signal_observations so ON so.id = sl.observation_id
            WHERE sl.timestamp >= ?
            GROUP BY COALESCE({column}, 'NA')
            HAVING COUNT(*) >= ?
            ORDER BY expectancy {order}
        """
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            rows = self._fetchall_dicts(conn.execute(sql, (since_iso, int(min_labels))))
        for row in rows:
            gains = float(row.get("gains") or 0.0)
            losses = abs(float(row.get("losses") or 0.0))
            row["profit_factor"] = gains / losses if losses > 0 else 999.0 if gains > 0 else 0.0
            decisive = int(row.get("tp_count") or 0) + int(row.get("sl_count") or 0)
            row["decisive_win_rate"] = int(row.get("tp_count") or 0) / max(decisive, 1)
        rows.sort(
            key=lambda item: (float(item.get("profit_factor") or 0.0), float(item.get("expectancy") or 0.0)),
            reverse=best,
        )
        return rows[:limit]

    def get_event_type_counts_since(self, since_iso: str) -> dict[str, int]:
        sql = """
            SELECT event_type, COUNT(*) AS count
            FROM events
            WHERE timestamp >= ?
            GROUP BY event_type
        """
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        try:
            with self._connect() as conn:
                rows = self._fetchall_dicts(conn.execute(sql, (since_iso,)))
            return {str(row.get("event_type") or "NA"): int(row.get("count") or 0) for row in rows}
        except Exception:
            return {}

    def fetch_trades(self, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM trades ORDER BY timestamp ASC"
        params: tuple[Any, ...] = ()
        if limit:
            sql += " LIMIT ?"
            params = (limit,)
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql, params))

    def fetch_labeled_signal_rows(self, limit: int | None = None) -> list[dict[str, Any]]:
        sql = """
            SELECT so.*, sl.label, sl.first_barrier_hit, sl.bars_to_outcome,
                   sl.max_favorable_excursion, sl.max_adverse_excursion,
                   sl.realized_return_pct, sl.simulated_pnl, sl.would_have_won
            FROM signal_observations so
            JOIN signal_labels sl ON sl.observation_id = so.id
            ORDER BY so.timestamp ASC
        """
        params: tuple[Any, ...] = ()
        if limit:
            sql += " LIMIT ?"
            params = (limit,)
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            return self._fetchall_dicts(cur)

    def fetch_labeled_signal_rows_since(self, since_iso: str, limit: int = 50000) -> list[dict[str, Any]]:
        sql = """
            SELECT so.*, so.id AS observation_id,
                   sl.id AS label_id,
                   sl.timestamp AS label_timestamp,
                   sl.label,
                   sl.first_barrier_hit,
                   sl.bars_to_outcome,
                   sl.max_favorable_excursion,
                   sl.max_adverse_excursion,
                   sl.realized_return_pct,
                   sl.simulated_pnl,
                   sl.would_have_won
            FROM signal_labels sl
            JOIN signal_observations so ON sl.observation_id = so.id
            WHERE sl.timestamp >= ?
            ORDER BY sl.timestamp ASC, sl.id ASC
            LIMIT ?
        """
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql, (since_iso, max(1, int(limit or 50000)))))

    def fetch_signal_labels(self, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM signal_labels ORDER BY timestamp ASC"
        params: tuple[Any, ...] = ()
        if limit:
            sql += " LIMIT ?"
            params = (limit,)
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql, params))

    def get_paper_trade_summary(self) -> dict[str, int]:
        with self._connect() as conn:
            sql = """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status IN ('PAPER_OPEN', 'OPEN') THEN 1 ELSE 0 END) AS open_count,
                    SUM(
                        CASE
                            WHEN status NOT IN ('PAPER_OPEN', 'OPEN', 'PAPER_READY') THEN 1
                            ELSE 0
                        END
                    ) AS closed_count
                FROM trades
                WHERE mode = ?
            """
            if self._use_postgres:
                sql = sql.replace("?", "%s")
            row = conn.execute(sql, ("paper",)).fetchone()
            if row is None:
                return {"total": 0, "open": 0, "closed": 0}
            return {
                "total": int(self._row_value(row, "total", 0, 0) or 0),
                "open": int(self._row_value(row, "open_count", 1, 0) or 0),
                "closed": int(self._row_value(row, "closed_count", 2, 0) or 0),
            }

    def fetch_open_paper_trades(self) -> list[dict[str, Any]]:
        sql = """
            SELECT *
            FROM trades
            WHERE mode = ?
              AND status IN ('PAPER_OPEN', 'OPEN')
            ORDER BY timestamp ASC
        """
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql, ("paper",)))

    def get_open_paper_positions_summary(self, limit: int = 5) -> list[dict[str, Any]]:
        sql = """
            SELECT
                symbol,
                side,
                entry AS entry_price,
                timestamp AS opened_at,
                strategy_type AS strategy,
                confidence_score AS score,
                stop_loss,
                take_profit_1,
                take_profit_2,
                status,
                realized_pnl,
                unrealized_pnl,
                reason
            FROM trades
            WHERE mode = ?
              AND status IN ('PAPER_OPEN', 'OPEN')
            ORDER BY timestamp ASC
            LIMIT ?
        """
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql, ("paper", int(limit))))

    def fetch_stale_open_paper_trades(self, *, older_than_iso: str, limit: int = 10) -> list[dict[str, Any]]:
        sql = """
            SELECT *
            FROM trades
            WHERE mode = ?
              AND status IN ('PAPER_OPEN', 'OPEN')
              AND timestamp < ?
            ORDER BY timestamp ASC
            LIMIT ?
        """
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql, ("paper", older_than_iso, int(limit))))

    def find_label_for_paper_trade(self, trade: dict[str, Any]) -> dict[str, Any] | None:
        symbol = str(trade.get("symbol") or "").upper()
        side = str(trade.get("side") or "").upper()
        entry = safe_float(trade.get("entry"))
        tolerance = max(abs(entry) * 0.0005, 1e-9)
        if not symbol or side not in {"LONG", "SHORT"}:
            return None
        sql = """
            SELECT so.id AS observation_id,
                   sl.id AS label_id,
                   sl.timestamp AS label_timestamp,
                   sl.label,
                   sl.first_barrier_hit,
                   sl.bars_to_outcome,
                   sl.realized_return_pct,
                   sl.simulated_pnl,
                   sl.would_have_won
            FROM signal_observations so
            JOIN signal_labels sl ON sl.observation_id = so.id
            WHERE so.symbol = ?
              AND so.side = ?
              AND COALESCE(so.operated, 0) = 1
              AND (? <= 0 OR ABS(COALESCE(so.entry_price, 0) - ?) <= ?)
            ORDER BY sl.timestamp DESC
            LIMIT 1
        """
        params = (symbol, side, entry, entry, tolerance)
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return self._row_to_dict(row) if row else None

    def get_table_counts(self) -> dict[str, int]:
        tables = [
            "signal_observations",
            "signal_labels",
            "trades",
            "events",
            "bot_state",
            "strategy_variants",
            "strategy_variant_results",
            "signal_explanations",
            "signal_price_paths",
            "signal_counterfactuals",
            "stop_loss_failure_clusters",
            "win_clusters",
            "research_rules",
            "virtual_research_trades",
            "virtual_strategy_summary",
            "kronos_predictions",
            "market_context_events",
            "market_catalysts",
            "latency_metrics",
            "strategy_lab_candidates",
            "strategy_lab_walkforward",
            "strategy_lab_recommendations",
            "research_autopilot_runs",
        ]
        counts: dict[str, int] = {}
        with self._connect() as conn:
            for table in tables:
                try:
                    row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
                    counts[table] = int(self._row_value(row, "count", 0, 0) or 0)
                except Exception:
                    counts[table] = 0
        return counts

    def table_exists(self, table: str) -> bool:
        table = _safe_identifier(table)
        try:
            with self._connect() as conn:
                if self._use_postgres:
                    row = conn.execute("SELECT to_regclass(%s) AS name", (table,)).fetchone()
                    return bool(self._row_value(row, "name", 0, None))
                row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                return row is not None
        except Exception:
            return False

    def get_table_columns(self, table: str) -> list[str]:
        table = _safe_identifier(table)
        try:
            with self._connect() as conn:
                if self._use_postgres:
                    rows = conn.execute(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_name = %s
                        ORDER BY ordinal_position
                        """,
                        (table,),
                    ).fetchall()
                    return [self._row_value(row, "column_name", 0, "") for row in rows]
                rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
                return [self._row_value(row, "name", 1, "") for row in rows]
        except Exception:
            return []

    def fetch_table_rows(
        self,
        table: str,
        *,
        since_iso: str | None = None,
        timestamp_column: str | None = None,
        limit: int = 200000,
    ) -> list[dict[str, Any]]:
        table = _safe_identifier(table)
        columns = set(self.get_table_columns(table))
        if not columns:
            return []
        params: tuple[Any, ...] = ()
        where = ""
        if since_iso and timestamp_column and timestamp_column in columns:
            where = f" WHERE {timestamp_column} >= ?"
            params = (since_iso,)
        order_column = "id" if "id" in columns else (timestamp_column if timestamp_column in columns else sorted(columns)[0])
        sql = f"SELECT * FROM {table}{where} ORDER BY {order_column} ASC LIMIT ?"
        params = params + (max(1, int(limit or 200000)),)
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql, params))

    def fetch_table_rows_chunk(
        self,
        table: str,
        *,
        since_iso: str | None = None,
        timestamp_column: str | None = None,
        limit: int = 5000,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        table = _safe_identifier(table)
        columns = set(self.get_table_columns(table))
        if not columns:
            return []
        params: tuple[Any, ...] = ()
        where = ""
        if since_iso and timestamp_column and timestamp_column in columns:
            where = f" WHERE {timestamp_column} >= ?"
            params = (since_iso,)
        order_column = "id" if "id" in columns else (timestamp_column if timestamp_column in columns else sorted(columns)[0])
        sql = f"SELECT * FROM {table}{where} ORDER BY {order_column} ASC LIMIT ? OFFSET ?"
        params = params + (max(1, int(limit or 5000)), max(0, int(offset or 0)))
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql, params))

    def insert_table_row_if_missing(self, table: str, row: dict[str, Any]) -> str:
        table = _safe_identifier(table)
        if not row:
            return "skipped"
        columns = [col for col in self.get_table_columns(table) if col in row]
        if not columns:
            return "skipped"
        with self._connect() as conn:
            if "id" in columns:
                select_sql = f"SELECT 1 FROM {table} WHERE id=? LIMIT 1"
                if self._use_postgres:
                    select_sql = select_sql.replace("?", "%s")
                if conn.execute(select_sql, (row.get("id"),)).fetchone() is not None:
                    return "duplicate"
            elif table == "bot_state" and "key" in columns:
                select_sql = "SELECT 1 FROM bot_state WHERE key=? LIMIT 1"
                if self._use_postgres:
                    select_sql = select_sql.replace("?", "%s")
                if conn.execute(select_sql, (row.get("key"),)).fetchone() is not None:
                    return "duplicate"
            placeholders = ", ".join(["?"] * len(columns))
            sql = f"INSERT INTO {table}({', '.join(columns)}) VALUES ({placeholders})"
            if self._use_postgres:
                sql = sql.replace("?", "%s")
            try:
                conn.execute(sql, tuple(row.get(col) for col in columns))
                return "inserted"
            except Exception:
                return "skipped"

    def latest_trades(self, limit: int = 5) -> list[dict[str, Any]]:
        sql = "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?"
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql, (limit,)))

    def latest_operated_signal_observations(self, limit: int = 5) -> list[dict[str, Any]]:
        sql = "SELECT * FROM signal_observations WHERE operated=1 ORDER BY timestamp DESC LIMIT ?"
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql, (limit,)))

    def latest_signal_labels(self, limit: int = 5) -> list[dict[str, Any]]:
        sql = "SELECT * FROM signal_labels ORDER BY timestamp DESC LIMIT ?"
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql, (limit,)))

    def fetch_unlabeled_signal_observations(self, limit: int = 200) -> list[dict[str, Any]]:
        sql = """
            SELECT so.*
            FROM signal_observations so
            LEFT JOIN signal_labels sl ON sl.observation_id = so.id
            WHERE sl.id IS NULL AND so.side IN ('LONG', 'SHORT')
            ORDER BY so.timestamp ASC
            LIMIT ?
        """
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            cur = conn.execute(sql, (limit,))
            return self._fetchall_dicts(cur)

    def count_phase2_pending_labels(self) -> int:
        sql = f"""
            SELECT COUNT(*) AS count
            FROM signal_labels sl
            JOIN signal_observations so ON so.id = sl.observation_id
            WHERE {self._phase2_missing_where()}
        """
        with self._connect() as conn:
            row = conn.execute(sql).fetchone()
            return int(self._row_value(row, "count", 0, 0) or 0)

    def fetch_phase2_labeled_rows(self, limit: int = 250, offset: int = 0, missing_only: bool = True) -> list[dict[str, Any]]:
        where = f"WHERE {self._phase2_missing_where()}" if missing_only else ""
        sql = f"""
            SELECT so.*, so.id AS observation_id,
                   sl.id AS label_id, sl.label, sl.first_barrier_hit, sl.bars_to_outcome,
                   sl.max_favorable_excursion, sl.max_adverse_excursion,
                   sl.realized_return_pct, sl.simulated_pnl, sl.would_have_won
            FROM signal_labels sl
            JOIN signal_observations so ON so.id = sl.observation_id
            {where}
            ORDER BY sl.id ASC
            LIMIT ? OFFSET ?
        """
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql, (limit, offset)))

    def fetch_strategy_lab_rows(self, limit: int = 20000, offset: int = 0) -> list[dict[str, Any]]:
        limit = max(0, int(limit or 0))
        offset = max(0, int(offset or 0))
        if limit <= 0:
            return []
        sql = """
            SELECT so.*, so.id AS observation_id,
                   sl.id AS label_id,
                   sl.timestamp AS label_timestamp,
                   sl.label,
                   sl.first_barrier_hit,
                   sl.bars_to_outcome,
                   sl.max_favorable_excursion,
                   sl.max_adverse_excursion,
                   sl.realized_return_pct,
                   sl.simulated_pnl,
                   sl.would_have_won,
                   spp.max_favorable_excursion_pct AS path_max_favorable_excursion_pct,
                   spp.max_adverse_excursion_pct AS path_max_adverse_excursion_pct,
                   spp.candles_until_exit AS path_candles_until_exit,
                   spp.volatility_during_trade AS path_volatility_during_trade,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM signal_counterfactuals sc
                       WHERE sc.observation_id = so.id
                         AND sc.label_id = sl.id
                         AND sc.scenario_name = 'REVERSE_SIDE'
                         AND COALESCE(sc.improved_result, 0) = 1
                       LIMIT 1
                   ) THEN 1 ELSE 0 END AS counterfactual_reverse_helped,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM signal_counterfactuals sc
                       WHERE sc.observation_id = so.id
                         AND sc.label_id = sl.id
                         AND COALESCE(sc.avoided_loss, 0) = 1
                       LIMIT 1
                   ) THEN 1 ELSE 0 END AS counterfactual_avoided_loss,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM signal_counterfactuals sc
                       WHERE sc.observation_id = so.id
                         AND sc.label_id = sl.id
                         AND sc.scenario_name IN ('CLOSER_TP_0_5X', 'CLOSER_TP_0_75X')
                         AND COALESCE(sc.improved_result, 0) = 1
                       LIMIT 1
                   ) THEN 1 ELSE 0 END AS counterfactual_closer_tp_helped,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM signal_counterfactuals sc
                       WHERE sc.observation_id = so.id
                         AND sc.label_id = sl.id
                         AND sc.scenario_name IN ('WIDER_STOP_1_5X', 'WIDER_STOP_2X')
                         AND COALESCE(sc.improved_result, 0) = 1
                       LIMIT 1
                   ) THEN 1 ELSE 0 END AS counterfactual_wider_stop_helped,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM virtual_research_trades vrt
                       WHERE vrt.observation_id = so.id
                         AND vrt.label_id = sl.id
                         AND COALESCE(vrt.return_pct, 0) > 0
                       LIMIT 1
                   ) THEN 1 ELSE 0 END AS virtual_research_positive,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM stop_loss_failure_clusters slfc
                       WHERE COALESCE(slfc.symbol, so.symbol) = so.symbol
                         AND COALESCE(slfc.side, so.side) = so.side
                         AND COALESCE(slfc.strategy_type, so.strategy_type) = so.strategy_type
                         AND COALESCE(slfc.market_regime, so.market_regime) = so.market_regime
                         AND COALESCE(slfc.score_bucket, so.score_bucket) = so.score_bucket
                       LIMIT 1
                   ) THEN 1 ELSE 0 END AS in_stop_loss_failure_cluster,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM win_clusters wc
                       WHERE COALESCE(wc.symbol, so.symbol) = so.symbol
                         AND COALESCE(wc.side, so.side) = so.side
                         AND COALESCE(wc.strategy_type, so.strategy_type) = so.strategy_type
                         AND COALESCE(wc.market_regime, so.market_regime) = so.market_regime
                         AND COALESCE(wc.score_bucket, so.score_bucket) = so.score_bucket
                       LIMIT 1
                   ) THEN 1 ELSE 0 END AS in_win_cluster
            FROM signal_labels sl
            JOIN signal_observations so ON so.id = sl.observation_id
            LEFT JOIN signal_price_paths spp ON spp.observation_id = so.id AND spp.label_id = sl.id
            WHERE so.side IN ('LONG', 'SHORT')
            ORDER BY sl.timestamp ASC, sl.id ASC
            LIMIT ? OFFSET ?
        """
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql, (limit, offset)))

    @staticmethod
    def _phase2_missing_where() -> str:
        return """
            (
                NOT EXISTS (
                    SELECT 1 FROM signal_explanations se
                    WHERE se.observation_id = so.id AND se.label_id = sl.id
                )
                OR NOT EXISTS (
                    SELECT 1 FROM signal_price_paths spp
                    WHERE spp.observation_id = so.id AND spp.label_id = sl.id
                )
                OR NOT EXISTS (
                    SELECT 1 FROM signal_counterfactuals sc
                    WHERE sc.observation_id = so.id AND sc.label_id = sl.id
                )
            )
        """

    def upsert_signal_path_metric(self, payload: dict[str, Any]) -> int:
        payload = dict(payload)
        observation_id = int(payload.get("observation_id") or 0)
        if observation_id <= 0:
            return 0
        now = iso_utc()
        payload.setdefault("created_at", now)
        payload.setdefault("updated_at", now)
        allowed = {
            "observation_id",
            "symbol",
            "side",
            "score",
            "score_bucket",
            "market_regime",
            "source",
            "probe_key",
            "reject_reason",
            "priority",
            "entry_price",
            "current_price",
            "max_favorable_pct",
            "max_adverse_pct",
            "final_return_pct",
            "bars_tracked",
            "bars_to_mfe",
            "bars_to_mae",
            "first_barrier_hit",
            "would_hit_tp_025",
            "would_hit_tp_050",
            "would_hit_tp_075",
            "would_hit_tp_100",
            "would_hit_tp_150",
            "would_hit_sl_025",
            "would_hit_sl_050",
            "would_hit_sl_075",
            "would_hit_sl_100",
            "status",
            "created_at",
            "updated_at",
            "matured_at",
            "catalyst_active",
            "catalyst_id",
            "catalyst_category",
            "catalyst_direction",
            "catalyst_severity",
        }
        payload = {key: value for key, value in payload.items() if key in allowed}
        with self._connect() as conn:
            select_sql = "SELECT id FROM signal_path_metrics WHERE observation_id=?"
            if self._use_postgres:
                select_sql = select_sql.replace("?", "%s")
            row = conn.execute(select_sql, (observation_id,)).fetchone()
            existing_id = int(self._row_value(row, "id", 0, 0) or 0)
            if existing_id:
                updates = {key: value for key, value in payload.items() if key not in {"observation_id", "created_at"}}
                updates["updated_at"] = now
                assignments = ", ".join(f"{key}=?" for key in updates)
                sql = f"UPDATE signal_path_metrics SET {assignments} WHERE observation_id=?"
                params = tuple(updates.values()) + (observation_id,)
                if self._use_postgres:
                    sql = sql.replace("?", "%s")
                conn.execute(sql, params)
                return existing_id
            columns = list(payload.keys())
            placeholders = ", ".join(["?"] * len(columns))
            sql = f"INSERT INTO signal_path_metrics({', '.join(columns)}) VALUES ({placeholders})"
            if self._use_postgres:
                sql = sql.replace("?", "%s") + " RETURNING id"
            cur = conn.execute(sql, tuple(payload[col] for col in columns))
            return self._inserted_id(cur)

    def fetch_active_signal_path_metrics(self, limit: int = 250) -> list[dict[str, Any]]:
        sql = """
            SELECT *
            FROM signal_path_metrics
            WHERE status = 'active'
            ORDER BY created_at ASC, id ASC
            LIMIT ?
        """
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql, (max(1, int(limit or 250)),)))

    def signal_path_metric_exists(self, observation_id: int) -> bool:
        if not observation_id:
            return False
        sql = "SELECT 1 FROM signal_path_metrics WHERE observation_id=? LIMIT 1"
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return conn.execute(sql, (int(observation_id),)).fetchone() is not None

    def count_active_signal_path_metrics(self) -> int:
        sql = "SELECT COUNT(*) AS count FROM signal_path_metrics WHERE status = 'active'"
        with self._connect() as conn:
            row = conn.execute(sql).fetchone()
            return int(self._row_value(row, "count", 0, 0) or 0)

    def update_signal_path_metric(self, observation_id: int, **updates: Any) -> None:
        allowed = {
            "current_price",
            "max_favorable_pct",
            "max_adverse_pct",
            "final_return_pct",
            "bars_tracked",
            "bars_to_mfe",
            "bars_to_mae",
            "first_barrier_hit",
            "would_hit_tp_025",
            "would_hit_tp_050",
            "would_hit_tp_075",
            "would_hit_tp_100",
            "would_hit_tp_150",
            "would_hit_sl_025",
            "would_hit_sl_050",
            "would_hit_sl_075",
            "would_hit_sl_100",
            "status",
            "updated_at",
            "matured_at",
            "catalyst_active",
            "catalyst_id",
            "catalyst_category",
            "catalyst_direction",
            "catalyst_severity",
        }
        payload = {key: value for key, value in updates.items() if key in allowed}
        if not observation_id or not payload:
            return
        payload.setdefault("updated_at", iso_utc())
        assignments = ", ".join(f"{key}=?" for key in payload)
        sql = f"UPDATE signal_path_metrics SET {assignments} WHERE observation_id=?"
        params = tuple(payload.values()) + (int(observation_id),)
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            conn.execute(sql, params)

    def get_signal_path_metrics_summary_since(self, since_iso: str) -> dict[str, Any]:
        sql = """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active_count,
                SUM(CASE WHEN status = 'matured' THEN 1 ELSE 0 END) AS matured_count,
                SUM(CASE WHEN status IN ('insufficient_price', 'insufficient_price_path_data') THEN 1 ELSE 0 END) AS insufficient_count,
                AVG(COALESCE(max_favorable_pct, 0)) AS avg_mfe,
                AVG(COALESCE(max_adverse_pct, 0)) AS avg_mae,
                AVG(COALESCE(final_return_pct, 0)) AS avg_final_return,
                AVG(COALESCE(bars_tracked, 0)) AS avg_bars_tracked
            FROM signal_path_metrics
            WHERE created_at >= ?
        """
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        try:
            with self._connect() as conn:
                row = conn.execute(sql, (since_iso,)).fetchone()
            total = float(self._row_value(row, "total", 0, 0) or 0)
            insufficient = float(self._row_value(row, "insufficient_count", 3, 0) or 0)
            return {
                "total": total,
                "active_count": float(self._row_value(row, "active_count", 1, 0) or 0),
                "matured_count": float(self._row_value(row, "matured_count", 2, 0) or 0),
                "insufficient_count": insufficient,
                "avg_mfe": float(self._row_value(row, "avg_mfe", 4, 0.0) or 0.0),
                "avg_mae": float(self._row_value(row, "avg_mae", 5, 0.0) or 0.0),
                "avg_final_return": float(self._row_value(row, "avg_final_return", 6, 0.0) or 0.0),
                "avg_bars_tracked": float(self._row_value(row, "avg_bars_tracked", 7, 0.0) or 0.0),
                "coverage_pct": ((total - insufficient) / max(total, 1.0)) if total else 0.0,
            }
        except Exception:
            return {
                "total": 0.0,
                "active_count": 0.0,
                "matured_count": 0.0,
                "insufficient_count": 0.0,
                "avg_mfe": 0.0,
                "avg_mae": 0.0,
                "avg_final_return": 0.0,
                "avg_bars_tracked": 0.0,
                "coverage_pct": 0.0,
            }

    def get_signal_path_metrics_source_summary_since(self, since_iso: str) -> list[dict[str, Any]]:
        sql = """
            SELECT
                COALESCE(source, 'unknown') AS source,
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active_count,
                SUM(CASE WHEN status = 'matured' THEN 1 ELSE 0 END) AS matured_count,
                SUM(CASE WHEN status IN ('insufficient_price', 'insufficient_price_path_data') THEN 1 ELSE 0 END) AS insufficient_count,
                SUM(CASE WHEN status = 'expired' THEN 1 ELSE 0 END) AS expired_count,
                AVG(COALESCE(max_favorable_pct, 0)) AS avg_mfe,
                AVG(COALESCE(max_adverse_pct, 0)) AS avg_mae,
                AVG(COALESCE(final_return_pct, 0)) AS avg_final_return
            FROM signal_path_metrics
            WHERE created_at >= ?
            GROUP BY COALESCE(source, 'unknown')
            ORDER BY total DESC, source ASC
        """
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        try:
            with self._connect() as conn:
                rows = self._fetchall_dicts(conn.execute(sql, (since_iso,)))
            return rows
        except Exception:
            return []

    def fetch_signal_path_metrics_since(self, since_iso: str, limit: int = 50000) -> list[dict[str, Any]]:
        sql = """
            SELECT *
            FROM signal_path_metrics
            WHERE created_at >= ?
            ORDER BY created_at ASC, id ASC
            LIMIT ?
        """
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql, (since_iso, max(1, int(limit or 50000)))))

    def get_score_calibration_summaries_since(self, since_iso: str, min_score: int = 70) -> list[dict[str, Any]]:
        score_bucket_expr = """
            CASE
                WHEN COALESCE(so.confidence_score, 0) >= 95 THEN '95-100'
                WHEN COALESCE(so.confidence_score, 0) >= 90 THEN '90-94'
                WHEN COALESCE(so.confidence_score, 0) >= 80 THEN '80-89'
                WHEN COALESCE(so.confidence_score, 0) >= 70 THEN '70-79'
                ELSE '<70'
            END
        """
        sql = f"""
            SELECT
                {score_bucket_expr} AS group_value,
                COUNT(*) AS total_labels,
                SUM(CASE WHEN sl.first_barrier_hit = 'TIME' THEN 1 ELSE 0 END) AS time_count,
                SUM(CASE WHEN sl.first_barrier_hit = 'SL' THEN 1 ELSE 0 END) AS sl_count,
                SUM(CASE WHEN sl.first_barrier_hit = 'TP1' THEN 1 ELSE 0 END) AS tp1_count,
                SUM(CASE WHEN sl.first_barrier_hit = 'TP2' THEN 1 ELSE 0 END) AS tp2_count,
                AVG(COALESCE(sl.realized_return_pct, 0)) AS avg_return,
                SUM(CASE WHEN COALESCE(sl.realized_return_pct, 0) > 0 THEN COALESCE(sl.realized_return_pct, 0) ELSE 0 END) AS gains,
                SUM(CASE WHEN COALESCE(sl.realized_return_pct, 0) < 0 THEN COALESCE(sl.realized_return_pct, 0) ELSE 0 END) AS losses
            FROM signal_labels sl
            JOIN signal_observations so ON so.id = sl.observation_id
            WHERE sl.timestamp >= ?
              AND COALESCE(so.confidence_score, 0) >= ?
              AND so.side IN ('LONG', 'SHORT')
            GROUP BY {score_bucket_expr}
            ORDER BY group_value ASC
        """
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        try:
            with self._connect() as conn:
                rows = self._fetchall_dicts(conn.execute(sql, (since_iso, int(min_score))))
            return [_with_edge_metrics(row) for row in rows]
        except Exception:
            return []

    def update_trade_status(
        self,
        trade_id: int,
        status: str,
        realized_pnl: float = 0.0,
        unrealized_pnl: float = 0.0,
        error_message: str = "",
    ) -> None:
        with self._connect() as conn:
            self._execute(
                conn,
                "UPDATE trades SET status=?, realized_pnl=?, unrealized_pnl=?, error_message=? WHERE id=?",
                (status, realized_pnl, unrealized_pnl, error_message, trade_id),
            )

    def get_realized_pnl_since(self, since: datetime) -> float:
        with self._connect() as conn:
            sql = "SELECT COALESCE(SUM(realized_pnl), 0) AS pnl FROM trades WHERE timestamp >= ?"
            if self._use_postgres:
                sql = sql.replace("?", "%s")
            cur = conn.execute(sql, (since.astimezone(timezone.utc).isoformat(),))
            row = cur.fetchone()
            if row is None:
                return 0.0
            return float(self._row_value(row, "pnl", 0, 0.0) or 0.0)

    def get_daily_realized_pnl(self) -> float:
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.get_realized_pnl_since(start)

    def get_weekly_realized_pnl(self) -> float:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=now.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.get_realized_pnl_since(start)

    def set_state(self, key: str, value: Any) -> None:
        with self._connect() as conn:
            if self._use_postgres:
                conn.execute(
                    """
                    INSERT INTO bot_state(key, value, updated_at) VALUES (%s, %s, %s)
                    ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=EXCLUDED.updated_at
                    """,
                    (key, json_dumps(value), iso_utc()),
                )
            else:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO bot_state(key, value, updated_at) VALUES (?, ?, ?)
                    """,
                    (key, json_dumps(value), iso_utc()),
                )

    def get_state(self, key: str, default: Any | None = None) -> Any:
        with self._connect() as conn:
            sql = "SELECT value FROM bot_state WHERE key=? LIMIT 1"
            if self._use_postgres:
                sql = sql.replace("?", "%s")
            cur = conn.execute(sql, (key,))
            row = cur.fetchone()
            if row is None:
                return default
            raw = self._row_value(row, "value", 0, None)
            try:
                return json.loads(raw) if raw is not None else default
            except Exception:
                return default

    def delete_state(self, key: str) -> None:
        with self._connect() as conn:
            sql = "DELETE FROM bot_state WHERE key=?"
            if self._use_postgres:
                sql = sql.replace("?", "%s")
            conn.execute(sql, (key,))

    def list_open_trades(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM trades WHERE status IN ('OPEN', 'PAPER_OPEN', 'LIVE_OPEN')")
            return self._fetchall_dicts(cur)

    def _insert_payload(self, table: str, payload: dict[str, Any]) -> int:
        payload = dict(payload)
        payload.setdefault("created_at", iso_utc())
        columns = list(payload.keys())
        placeholders = ", ".join(["?"] * len(columns))
        sql = f"INSERT INTO {table}({', '.join(columns)}) VALUES ({placeholders})"
        if self._use_postgres:
            sql = sql.replace("?", "%s") + " RETURNING id"
        with self._connect() as conn:
            cur = conn.execute(sql, tuple(payload[col] for col in columns))
            return self._inserted_id(cur)

    def _fetch_table(self, table: str, limit: int | None = None) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM {table} ORDER BY id ASC"
        params: tuple[Any, ...] = ()
        if limit:
            sql += " LIMIT ?"
            params = (limit,)
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql, params))

    def _exists_by_fields(self, table: str, fields: dict[str, Any]) -> bool:
        conditions = " AND ".join(f"{field}=?" for field in fields)
        sql = f"SELECT 1 FROM {table} WHERE {conditions} LIMIT 1"
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return conn.execute(sql, tuple(fields.values())).fetchone() is not None

    def _delete_by_fields(self, table: str, fields: dict[str, Any]) -> None:
        conditions = " AND ".join(f"{field}=?" for field in fields)
        sql = f"DELETE FROM {table} WHERE {conditions}"
        self._execute_sql(sql, tuple(fields.values()))

    def _execute_sql(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            conn.execute(sql, params)

    def record_signal_explanation(self, payload: dict[str, Any]) -> int:
        return self._insert_payload("signal_explanations", payload)

    def record_signal_explanation_once(self, payload: dict[str, Any]) -> int:
        fields = {"observation_id": payload.get("observation_id"), "label_id": payload.get("label_id")}
        if self._exists_by_fields("signal_explanations", fields):
            return 0
        return self.record_signal_explanation(payload)

    def fetch_signal_explanations(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self._fetch_table("signal_explanations", limit)

    def record_signal_price_path(self, payload: dict[str, Any]) -> int:
        return self._insert_payload("signal_price_paths", payload)

    def record_signal_price_path_once(self, payload: dict[str, Any]) -> int:
        fields = {"observation_id": payload.get("observation_id"), "label_id": payload.get("label_id")}
        if self._exists_by_fields("signal_price_paths", fields):
            return 0
        return self.record_signal_price_path(payload)

    def fetch_signal_price_paths(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self._fetch_table("signal_price_paths", limit)

    def record_signal_counterfactual(self, payload: dict[str, Any]) -> int:
        return self._insert_payload("signal_counterfactuals", payload)

    def record_signal_counterfactual_once(self, payload: dict[str, Any]) -> int:
        fields = {
            "observation_id": payload.get("observation_id"),
            "label_id": payload.get("label_id"),
            "scenario_name": payload.get("scenario_name"),
        }
        if self._exists_by_fields("signal_counterfactuals", fields):
            return 0
        return self.record_signal_counterfactual(payload)

    def fetch_signal_counterfactuals(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self._fetch_table("signal_counterfactuals", limit)

    def record_stop_loss_failure_cluster(self, payload: dict[str, Any]) -> int:
        return self._insert_payload("stop_loss_failure_clusters", payload)

    def upsert_stop_loss_failure_cluster(self, payload: dict[str, Any]) -> int:
        self._delete_by_fields("stop_loss_failure_clusters", {"cluster_name": payload.get("cluster_name")})
        return self.record_stop_loss_failure_cluster(payload)

    def fetch_stop_loss_failure_clusters(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self._fetch_table("stop_loss_failure_clusters", limit)

    def record_win_cluster(self, payload: dict[str, Any]) -> int:
        return self._insert_payload("win_clusters", payload)

    def upsert_win_cluster(self, payload: dict[str, Any]) -> int:
        self._delete_by_fields("win_clusters", {"cluster_name": payload.get("cluster_name")})
        return self.record_win_cluster(payload)

    def fetch_win_clusters(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self._fetch_table("win_clusters", limit)

    def record_research_rule(self, payload: dict[str, Any]) -> int:
        return self._insert_payload("research_rules", payload)

    def upsert_research_rule(self, payload: dict[str, Any]) -> int:
        self._delete_by_fields("research_rules", {"rule_name": payload.get("rule_name")})
        return self.record_research_rule(payload)

    def fetch_research_rules(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self._fetch_table("research_rules", limit)

    def record_virtual_research_trade(self, payload: dict[str, Any]) -> int:
        return self._insert_payload("virtual_research_trades", payload)

    def record_virtual_research_trade_once(self, payload: dict[str, Any]) -> int:
        fields = {
            "variant_name": payload.get("variant_name"),
            "observation_id": payload.get("observation_id"),
            "label_id": payload.get("label_id"),
        }
        if self._exists_by_fields("virtual_research_trades", fields):
            return 0
        return self.record_virtual_research_trade(payload)

    def fetch_virtual_research_trades(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self._fetch_table("virtual_research_trades", limit)

    def upsert_virtual_strategy_summary(self, payload: dict[str, Any]) -> int:
        self._delete_by_fields("virtual_strategy_summary", {"variant_name": payload.get("variant_name")})
        return self._insert_payload("virtual_strategy_summary", payload)

    def fetch_virtual_strategy_summary(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self._fetch_table("virtual_strategy_summary", limit)

    def record_kronos_prediction(self, payload: dict[str, Any]) -> int:
        payload = dict(payload)
        payload.setdefault("timestamp", iso_utc())
        return self._insert_payload("kronos_predictions", payload)

    def fetch_kronos_predictions(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self._fetch_table("kronos_predictions", limit)

    def fetch_kronos_candidate_observations(self, limit: int = 100) -> list[dict[str, Any]]:
        sql = """
            SELECT so.*
            FROM signal_observations so
            WHERE so.side IN ('LONG', 'SHORT')
              AND (
                  so.kronos_prediction_id IS NULL
                  OR NOT EXISTS (
                      SELECT 1 FROM kronos_predictions kp
                      WHERE kp.observation_id = so.id
                  )
              )
            ORDER BY so.timestamp DESC
            LIMIT ?
        """
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql, (max(0, int(limit or 0)),)))

    def fetch_kronos_labeled_rows(self, limit: int | None = None) -> list[dict[str, Any]]:
        sql = """
            SELECT kp.id AS kronos_prediction_id,
                   kp.timestamp AS kronos_timestamp,
                   kp.symbol,
                   kp.observation_id,
                   kp.model_name,
                   kp.tokenizer_name,
                   kp.current_close,
                   kp.predicted_close,
                   kp.predicted_return_pct,
                   kp.predicted_range_pct,
                   kp.direction AS kronos_direction,
                   kp.confidence_score AS kronos_confidence_score,
                   kp.volatility_score AS kronos_volatility_score,
                   so.side,
                   so.strategy_type,
                   so.market_regime,
                   so.confidence_score,
                   so.shadow_strategy,
                   so.variant_params_json,
                   sl.id AS label_id,
                   sl.label,
                   sl.first_barrier_hit,
                   sl.realized_return_pct,
                   sl.simulated_pnl,
                   sl.bars_to_outcome
            FROM kronos_predictions kp
            JOIN signal_observations so ON so.id = kp.observation_id
            JOIN signal_labels sl ON sl.observation_id = so.id
            ORDER BY kp.timestamp ASC
        """
        params: tuple[Any, ...] = ()
        if limit:
            sql += " LIMIT ?"
            params = (limit,)
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql, params))

    def record_market_context_event(self, payload: dict[str, Any]) -> int:
        return self._insert_payload("market_context_events", payload)

    def fetch_market_context_events(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self._fetch_table("market_context_events", limit)

    def upsert_market_catalyst(self, payload: dict[str, Any]) -> int:
        payload = dict(payload)
        catalyst_id = str(payload.get("catalyst_id") or "").strip()
        if not catalyst_id:
            return 0
        now = iso_utc()
        payload.setdefault("created_at", now)
        payload["updated_at"] = now
        allowed = {
            "catalyst_id",
            "title",
            "category",
            "symbols",
            "regimes",
            "direction",
            "severity",
            "confidence",
            "source",
            "source_url_hash",
            "published_at",
            "start_at",
            "end_at",
            "summary",
            "raw_ref",
            "created_at",
            "updated_at",
        }
        clean = {key: sanitize(value) if isinstance(value, str) else value for key, value in payload.items() if key in allowed}
        clean["catalyst_id"] = catalyst_id
        with self._connect() as conn:
            select_sql = "SELECT id FROM market_catalysts WHERE catalyst_id=?"
            if self._use_postgres:
                select_sql = select_sql.replace("?", "%s")
            row = conn.execute(select_sql, (catalyst_id,)).fetchone()
            existing_id = int(self._row_value(row, "id", 0, 0) or 0)
            if existing_id:
                updates = {key: value for key, value in clean.items() if key not in {"catalyst_id", "created_at"}}
                assignments = ", ".join(f"{key}=?" for key in updates)
                sql = f"UPDATE market_catalysts SET {assignments} WHERE catalyst_id=?"
                params = tuple(updates.values()) + (catalyst_id,)
                if self._use_postgres:
                    sql = sql.replace("?", "%s")
                conn.execute(sql, params)
                return existing_id
            columns = list(clean.keys())
            placeholders = ", ".join(["?"] * len(columns))
            sql = f"INSERT INTO market_catalysts({', '.join(columns)}) VALUES ({placeholders})"
            if self._use_postgres:
                sql = sql.replace("?", "%s") + " RETURNING id"
            cur = conn.execute(sql, tuple(clean[col] for col in columns))
            return self._inserted_id(cur)

    def fetch_market_catalysts(self, *, since_iso: str | None = None, until_iso: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since_iso:
            clauses.append("COALESCE(end_at, start_at, published_at, created_at) >= ?")
            params.append(since_iso)
        if until_iso:
            clauses.append("COALESCE(start_at, published_at, created_at) <= ?")
            params.append(until_iso)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"""
            SELECT *
            FROM market_catalysts
            {where}
            ORDER BY COALESCE(published_at, start_at, created_at) DESC, id DESC
            LIMIT ?
        """
        params.append(max(1, int(limit or 500)))
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        try:
            with self._connect() as conn:
                return self._fetchall_dicts(conn.execute(sql, tuple(params)))
        except Exception:
            return []

    def fetch_active_market_catalysts(self, at_iso: str, *, limit: int = 500) -> list[dict[str, Any]]:
        sql = """
            SELECT *
            FROM market_catalysts
            WHERE COALESCE(start_at, published_at, created_at) <= ?
              AND COALESCE(end_at, start_at, published_at, created_at) >= ?
            ORDER BY severity DESC, confidence DESC, id DESC
            LIMIT ?
        """
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        try:
            with self._connect() as conn:
                return self._fetchall_dicts(conn.execute(sql, (at_iso, at_iso, max(1, int(limit or 500)))))
        except Exception:
            return []

    def record_strategy_lab_candidate(self, payload: dict[str, Any]) -> int:
        return self._insert_payload("strategy_lab_candidates", payload)

    def record_strategy_lab_walkforward(self, payload: dict[str, Any]) -> int:
        return self._insert_payload("strategy_lab_walkforward", payload)

    def record_strategy_lab_recommendation(self, payload: dict[str, Any]) -> int:
        return self._insert_payload("strategy_lab_recommendations", payload)

    def fetch_strategy_lab_candidates(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self._fetch_table("strategy_lab_candidates", limit)

    def fetch_strategy_lab_walkforward(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self._fetch_table("strategy_lab_walkforward", limit)

    def fetch_strategy_lab_recommendations(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self._fetch_table("strategy_lab_recommendations", limit)

    def record_research_autopilot_run_started(self, payload: dict[str, Any]) -> int:
        return self._insert_payload("research_autopilot_runs", payload)

    def update_research_autopilot_run(self, run_id: int, **updates: Any) -> None:
        allowed = {
            "ended_at",
            "status",
            "duration_seconds",
            "processed",
            "explanations_created",
            "counterfactuals_created",
            "clusters_updated",
            "rules_generated",
            "virtual_trades_simulated",
            "errors",
            "failure_reason",
        }
        payload = {key: value for key, value in updates.items() if key in allowed}
        if not run_id or not payload:
            return
        assignments = ", ".join(f"{key}=?" for key in payload)
        sql = f"UPDATE research_autopilot_runs SET {assignments} WHERE id=?"
        params = tuple(payload.values()) + (run_id,)
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            conn.execute(sql, params)

    def fetch_research_autopilot_runs(self, limit: int = 5) -> list[dict[str, Any]]:
        sql = "SELECT * FROM research_autopilot_runs ORDER BY started_at DESC, id DESC LIMIT ?"
        if self._use_postgres:
            sql = sql.replace("?", "%s")
        with self._connect() as conn:
            return self._fetchall_dicts(conn.execute(sql, (int(limit),)))


def _with_edge_metrics(row: dict[str, Any]) -> dict[str, Any]:
    total = float(row.get("total_labels") or 0.0)
    tp = float(row.get("tp1_count") or 0.0) + float(row.get("tp2_count") or 0.0)
    sl = float(row.get("sl_count") or 0.0)
    time_count = float(row.get("time_count") or 0.0)
    gains = float(row.get("gains") or 0.0)
    losses = abs(float(row.get("losses") or 0.0))
    row["profit_factor"] = gains / losses if losses > 0 else 999.0 if gains > 0 else 0.0
    row["time_ratio"] = time_count / max(total, 1.0)
    row["sl_ratio"] = sl / max(total, 1.0)
    row["tp_ratio"] = tp / max(total, 1.0)
    row["sample_warning"] = total < 50
    return row


def _safe_identifier(value: str) -> str:
    text = str(value or "").strip()
    if not text or any(not (char.isalnum() or char == "_") for char in text):
        raise ValueError(f"Identificador SQL no valido: {value!r}")
    return text
