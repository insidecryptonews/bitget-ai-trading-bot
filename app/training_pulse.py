from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .utils import safe_float, safe_int


START_MARKER = "TRAINING PULSE START"
END_MARKER = "TRAINING PULSE END"


@dataclass
class TrainingPulse:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    window_started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_pulse_at: datetime | None = None
    cycle_count: int = 0
    cycles_ok: int = 0
    cycles_error: int = 0
    snapshots_ok: int = 0
    snapshots_empty: int = 0
    api_429_count: int = 0
    api_error_count: int = 0
    memory_mb_last: float = 0.0
    memory_mb_max: float = 0.0
    open_paper_positions_last: int = 0
    paper_reconcile_runs: int = 0
    paper_reconcile_closed_by_label: int = 0
    paper_reconcile_closed_by_time: int = 0
    paper_reconcile_left_open: int = 0
    slot_block_count: int = 0
    allocator_no_trade_count: int = 0
    allocator_selected_count: int = 0
    risk_block_count: int = 0
    paper_open_attempts: int = 0
    paper_open_success: int = 0
    paper_open_fail: int = 0
    labels_total: int = 0
    labels_time: int = 0
    labels_sl: int = 0
    labels_tp1: int = 0
    labels_tp2: int = 0
    signals_total: int = 0
    signals_long: int = 0
    signals_short: int = 0
    signals_no_trade: int = 0
    high_score_signals_total: int = 0
    missed_high_score_signals: int = 0
    market_regime_counts: Counter[str] = field(default_factory=Counter)
    no_trade_reason_counts: Counter[str] = field(default_factory=Counter)
    top_signal_scores: list[dict[str, Any]] = field(default_factory=list)
    top_block_reasons: Counter[str] = field(default_factory=Counter)

    def record_cycle_start(self) -> None:
        self.cycle_count += 1

    def record_cycle_ok(self) -> None:
        self.cycles_ok += 1

    def record_cycle_error(self, error_text: str) -> None:
        self.cycles_error += 1
        self.record_api_error(error_text)

    def record_snapshots(self, count: int) -> None:
        if count > 0:
            self.snapshots_ok += 1
        else:
            self.snapshots_empty += 1

    def record_regime(self, regime: str) -> None:
        if regime:
            self.market_regime_counts[str(regime).upper()] += 1

    def record_signals(self, signals: list[Any], min_score_to_trade: int) -> None:
        for signal in signals:
            side = str(getattr(signal, "side", "")).upper()
            score = safe_int(getattr(signal, "confidence_score", 0))
            self.signals_total += 1
            if side == "LONG":
                self.signals_long += 1
            elif side == "SHORT":
                self.signals_short += 1
            else:
                self.signals_no_trade += 1
                reason = str(getattr(signal, "reason", "") or "NO_TRADE")
                self.no_trade_reason_counts[_compact_reason(reason)] += 1
            if score >= min_score_to_trade and side in {"LONG", "SHORT"}:
                self.high_score_signals_total += 1
                self._add_top_signal(signal, score)

    def record_allocator(self, reason: str, selected_count: int) -> None:
        selected = max(0, int(selected_count or 0))
        self.allocator_selected_count += selected
        if selected == 0:
            self.allocator_no_trade_count += 1
        if _is_slot_reason(reason):
            self.record_slot_block(reason)

    def record_slot_block(self, reason: str) -> None:
        self.slot_block_count += 1
        self.top_block_reasons[_compact_reason(reason)] += 1

    def record_risk_block(self, reason: str) -> None:
        self.risk_block_count += 1
        self.top_block_reasons[_compact_reason(reason)] += 1

    def record_high_score_missed(self, reason: str) -> None:
        self.missed_high_score_signals += 1
        self.top_block_reasons[_compact_reason(reason)] += 1

    def record_paper_open_attempt(self, symbol: str, side: str, success: bool, reason: str = "") -> None:
        self.paper_open_attempts += 1
        if success:
            self.paper_open_success += 1
        else:
            self.paper_open_fail += 1
            self.top_block_reasons[_compact_reason(reason or f"paper_open_failed_{symbol}_{side}")] += 1

    def record_labels(self, label_counts: dict[str, Any]) -> None:
        total = safe_int(label_counts.get("total"))
        time_count = safe_int(label_counts.get("TIME") or label_counts.get("time"))
        sl_count = safe_int(label_counts.get("SL") or label_counts.get("sl"))
        tp1_count = safe_int(label_counts.get("TP1") or label_counts.get("tp1"))
        tp2_count = safe_int(label_counts.get("TP2") or label_counts.get("tp2"))
        self.labels_total += total
        self.labels_time += time_count
        self.labels_sl += sl_count
        self.labels_tp1 += tp1_count
        self.labels_tp2 += tp2_count

    def record_api_error(self, error_text: str) -> None:
        if not error_text:
            return
        self.api_error_count += 1
        if "429" in str(error_text) or "rate limit" in str(error_text).lower():
            self.api_429_count += 1

    def record_memory(self, mb: float) -> None:
        value = safe_float(mb)
        if value <= 0:
            return
        self.memory_mb_last = value
        self.memory_mb_max = max(self.memory_mb_max, value)

    def record_open_paper_positions(self, count: int) -> None:
        self.open_paper_positions_last = max(0, int(count or 0))

    def record_paper_reconcile(self, result: Any) -> None:
        if result is None:
            return
        self.paper_reconcile_runs += 1
        self.paper_reconcile_closed_by_label += safe_int(getattr(result, "paper_trades_closed_by_label", 0))
        self.paper_reconcile_closed_by_time += safe_int(getattr(result, "paper_trades_closed_by_time", 0))
        self.paper_reconcile_left_open = safe_int(getattr(result, "paper_trades_left_open", 0))

    def should_emit(self, now: datetime, interval_minutes: int) -> bool:
        if self.last_pulse_at is None:
            return True
        elapsed = (now - self.last_pulse_at).total_seconds()
        return elapsed >= max(1, int(interval_minutes or 10)) * 60

    def to_dict(self, config) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        uptime_min = (now - self.started_at).total_seconds() / 60.0
        window_min = (now - self.window_started_at).total_seconds() / 60.0
        diagnoses = self._diagnosis()
        recommendation = self._recommendation(diagnoses)
        tp_count = self.labels_tp1 + self.labels_tp2
        total_labels = max(self.labels_total, 1)
        return {
            "safety": {
                "paper_trading": bool(config.paper_trading),
                "live_trading": bool(config.live_trading),
                "dry_run": bool(config.dry_run),
                "worker_lightweight_mode": bool(config.worker_lightweight_mode),
            },
            "health": {
                "uptime_min": round(uptime_min, 2),
                "window_min": round(window_min, 2),
                "memory_mb_last": round(self.memory_mb_last, 2),
                "memory_mb_max": round(self.memory_mb_max, 2),
                "cycles_ok": self.cycles_ok,
                "cycles_error": self.cycles_error,
                "api_429_count": self.api_429_count,
                "api_error_count": self.api_error_count,
            },
            "paper": {
                "open_positions": self.open_paper_positions_last,
                "open_success": self.paper_open_success,
                "open_fail": self.paper_open_fail,
                "reconcile_runs": self.paper_reconcile_runs,
                "reconcile_closed_label": self.paper_reconcile_closed_by_label,
                "reconcile_closed_time": self.paper_reconcile_closed_by_time,
                "reconcile_left_open": self.paper_reconcile_left_open,
            },
            "allocator": {
                "selected": self.allocator_selected_count,
                "no_trade": self.allocator_no_trade_count,
                "slot_blocks": self.slot_block_count,
                "risk_blocks": self.risk_block_count,
            },
            "signals": {
                "long": self.signals_long,
                "short": self.signals_short,
                "no_trade": self.signals_no_trade,
                "high_score": self.high_score_signals_total,
                "missed_high_score": self.missed_high_score_signals,
            },
            "labels": {
                "total": self.labels_total,
                "time": self.labels_time,
                "sl": self.labels_sl,
                "tp1": self.labels_tp1,
                "tp2": self.labels_tp2,
                "time_ratio": self.labels_time / total_labels if self.labels_total else 0.0,
                "sl_ratio": self.labels_sl / total_labels if self.labels_total else 0.0,
                "tp_ratio": tp_count / total_labels if self.labels_total else 0.0,
            },
            "regimes": {
                "CHOPPY_MARKET": self.market_regime_counts.get("CHOPPY_MARKET", 0),
                "RANGE": self.market_regime_counts.get("RANGE", 0),
                "TREND_UP": self.market_regime_counts.get("TREND_UP", 0),
                "TREND_DOWN": self.market_regime_counts.get("TREND_DOWN", 0),
                "RISK_OFF": self.market_regime_counts.get("RISK_OFF", 0),
            },
            "top_signals": [dict(item) for item in self.top_signal_scores[: max(1, int(config.training_pulse_top_n or 5))]],
            "top_blocks": [
                {"reason": reason, "count": count}
                for reason, count in self.top_block_reasons.most_common(max(1, int(config.training_pulse_top_n or 5)))
            ],
            "diagnosis": diagnoses,
            "next_action": recommendation,
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, config, *, update_timestamp: bool = True) -> str:
        data = self.to_dict(config)
        lines = [
            START_MARKER,
            f"uptime_min: {data['health']['uptime_min']:.1f}",
            f"window_min: {data['health']['window_min']:.1f}",
            f"mode: {config.mode}",
            (
                "safety: "
                f"PAPER={data['safety']['paper_trading']} LIVE={data['safety']['live_trading']} "
                f"DRY={data['safety']['dry_run']} LIGHTWEIGHT={data['safety']['worker_lightweight_mode']}"
            ),
            f"memory_mb: last={data['health']['memory_mb_last']:.2f} max={data['health']['memory_mb_max']:.2f}",
            f"cycles: ok={data['health']['cycles_ok']} error={data['health']['cycles_error']}",
            f"api: 429={data['health']['api_429_count']} errors={data['health']['api_error_count']}",
            (
                "paper: "
                f"open_positions={data['paper']['open_positions']} "
                f"open_success={data['paper']['open_success']} open_fail={data['paper']['open_fail']}"
            ),
            (
                "paper_reconcile: "
                f"runs={data['paper']['reconcile_runs']} closed_label={data['paper']['reconcile_closed_label']} "
                f"closed_time={data['paper']['reconcile_closed_time']} left_open={data['paper']['reconcile_left_open']}"
            ),
            (
                "allocator: "
                f"selected={data['allocator']['selected']} no_trade={data['allocator']['no_trade']} "
                f"slot_blocks={data['allocator']['slot_blocks']}"
            ),
            (
                "signals: "
                f"LONG={data['signals']['long']} SHORT={data['signals']['short']} NO_TRADE={data['signals']['no_trade']} "
                f"high_score={data['signals']['high_score']} missed_high_score={data['signals']['missed_high_score']}"
            ),
            (
                "labels: "
                f"total={data['labels']['total']} TIME={data['labels']['time']} SL={data['labels']['sl']} "
                f"TP1={data['labels']['tp1']} TP2={data['labels']['tp2']}"
            ),
            "regimes: " + _counter_inline(self.market_regime_counts, config.training_pulse_top_n),
            "top_signals:",
            *_top_signal_lines(self.top_signal_scores, config.training_pulse_top_n),
            "top_blocks:",
            *_counter_lines(self.top_block_reasons, config.training_pulse_top_n),
            "diagnosis:",
            *[f"- {item}" for item in data["diagnosis"]],
            "next_action:",
            f"- {data['next_action']}",
            f"final_recommendation: {data['final_recommendation']}",
            END_MARKER,
        ]
        max_lines = max(10, int(config.training_pulse_max_lines or 80))
        if len(lines) > max_lines:
            lines = lines[: max_lines - 1] + [END_MARKER]
        if update_timestamp:
            self.last_pulse_at = datetime.now(timezone.utc)
        return "\n".join(lines)

    def reset_window(self) -> None:
        keep_started_at = self.started_at
        keep_last_pulse_at = self.last_pulse_at
        self.__dict__.update(TrainingPulse(started_at=keep_started_at).__dict__)
        self.last_pulse_at = keep_last_pulse_at

    def _add_top_signal(self, signal: Any, score: int) -> None:
        self.top_signal_scores.append(
            {
                "symbol": getattr(signal, "symbol", "NA"),
                "side": getattr(signal, "side", "NA"),
                "score": score,
                "reason": _compact_reason(str(getattr(signal, "reason", "") or "")),
            }
        )
        self.top_signal_scores.sort(key=lambda item: int(item.get("score") or 0), reverse=True)
        self.top_signal_scores = self.top_signal_scores[:20]

    def _diagnosis(self) -> list[str]:
        diagnosis: list[str] = []
        if self.api_429_count > 0:
            diagnosis.append("CHECK_RATE_LIMIT: rate limit Bitget; backoff activo")
        if self.slot_block_count > 0 and self.missed_high_score_signals > 0:
            diagnosis.append("CHECK_SLOT: senales buenas perdidas por slot")
        if self.labels_total > 0 and self.labels_time / max(self.labels_total, 1) > 0.80:
            diagnosis.append("NEED_RESEARCH: demasiadas TIME; revisar max_holding_bars/filtros/regimen")
        if self.labels_sl > (self.labels_tp1 + self.labels_tp2):
            diagnosis.append("NEED_RESEARCH: demasiadas SL; revisar stop/regimen/simbolos")
        choppy_range = self.market_regime_counts.get("CHOPPY_MARKET", 0) + self.market_regime_counts.get("RANGE", 0)
        if self.signals_total > 0 and self.signals_no_trade == self.signals_total and choppy_range > 0:
            diagnosis.append("PAPER ONLY: mercado lateral/choppy; no forzar trades")
        if self.memory_mb_max > 0 and self.memory_mb_last > self.memory_mb_max * 1.25:
            diagnosis.append("CHECK_MEMORY: revisar consumo de memoria")
        if not diagnosis:
            diagnosis.append("PAPER ONLY: worker estable; continuar observando")
        return diagnosis[:6]

    def _recommendation(self, diagnosis: list[str]) -> str:
        joined = " ".join(diagnosis)
        if "CHECK_RATE_LIMIT" in joined:
            return "CHECK_RATE_LIMIT"
        if "CHECK_SLOT" in joined:
            return "CHECK_SLOT"
        if "NEED_RESEARCH" in joined:
            return "NEED_RESEARCH"
        return "PAPER ONLY"


def _is_slot_reason(reason: str) -> bool:
    text = str(reason or "").lower()
    return "slot" in text or "posicion" in text or "posición" in text


def _compact_reason(reason: str, max_len: int = 90) -> str:
    text = " ".join(str(reason or "NA").split())
    return text[:max_len] if text else "NA"


def _counter_inline(counter: Counter[str], limit: int) -> str:
    if not counter:
        return "none"
    return " ".join(f"{key}={value}" for key, value in counter.most_common(max(1, limit)))


def _counter_lines(counter: Counter[str], limit: int) -> list[str]:
    if not counter:
        return ["- none"]
    return [f"- {key}={value}" for key, value in counter.most_common(max(1, limit))]


def _top_signal_lines(items: list[dict[str, Any]], limit: int) -> list[str]:
    if not items:
        return ["- none"]
    return [
        f"- {item.get('symbol')} {item.get('side')} score={item.get('score')} reason={item.get('reason')}"
        for item in items[: max(1, limit)]
    ]
