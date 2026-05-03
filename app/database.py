from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import BotConfig, PROJECT_ROOT
from .utils import iso_utc, json_dumps, sanitize


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
        self._ensure_research_columns(conn)

    def _ensure_research_columns(self, conn: Any) -> None:
        columns = {
            "shadow_strategy": "INTEGER DEFAULT 0",
            "strategy_variant_id": "INTEGER",
            "variant_params_json": "TEXT",
            "original_side": "TEXT",
            "original_strategy_type": "TEXT",
            "score_bucket": "TEXT",
        }
        if self._use_postgres:
            for name, spec in columns.items():
                self._execute(conn, f"ALTER TABLE signal_observations ADD COLUMN IF NOT EXISTS {name} {spec}")
            return
        cur = conn.execute("PRAGMA table_info(signal_observations)")
        existing = {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in cur.fetchall()}
        for name, spec in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE signal_observations ADD COLUMN {name} {spec}")

    def record_event(self, event_type: str, message: str, level: str = "INFO", payload: Any | None = None) -> None:
        with self._connect() as conn:
            self._execute(
                conn,
                "INSERT INTO events(timestamp, level, event_type, message, payload_json) VALUES (?, ?, ?, ?, ?)",
                (iso_utc(), level, event_type, message, json_dumps(payload or {})),
            )

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

    def get_table_counts(self) -> dict[str, int]:
        tables = [
            "signal_observations",
            "signal_labels",
            "trades",
            "events",
            "bot_state",
            "strategy_variants",
            "strategy_variant_results",
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

    def list_open_trades(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM trades WHERE status IN ('OPEN', 'PAPER_OPEN', 'LIVE_OPEN')")
            return self._fetchall_dicts(cur)
