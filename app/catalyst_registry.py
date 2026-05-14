from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from .catalyst_classifier import CatalystClassifier
from .config import BotConfig
from .database import Database
from .utils import safe_float, safe_int, sanitize


START = "CATALYST SUMMARY START"
END = "CATALYST SUMMARY END"


class CatalystRegistry:
    """Research-only catalyst storage and catalyst-aware label summaries."""

    def __init__(self, config: BotConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def add_manual(
        self,
        *,
        catalyst_id: str,
        title: str,
        symbols: list[str],
        category: str,
        direction: str,
        severity: str,
        confidence: float,
        hours_back: int = 0,
        hours_forward: int = 24,
        source: str = "manual",
        summary: str = "",
        regimes: list[str] | None = None,
    ) -> int:
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=max(0, int(hours_back or 0)))
        end = now + timedelta(hours=max(1, int(hours_forward or 24)))
        record = {
            "catalyst_id": sanitize(catalyst_id or self._manual_id(title, symbols, start.isoformat())),
            "title": sanitize(title)[:240],
            "category": sanitize(category or "other"),
            "symbols": ",".join(_normalize_symbols(symbols)),
            "regimes": ",".join(regimes or []),
            "direction": sanitize(direction or "unknown"),
            "severity": sanitize(severity or "low"),
            "confidence": max(0.0, min(1.0, float(confidence or 0.0))),
            "source": source,
            "source_url_hash": "",
            "published_at": start.isoformat(),
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "summary": sanitize(summary or title)[:700],
            "raw_ref": "",
        }
        return self.db.upsert_market_catalyst(record)

    def add_classified(self, *, title: str, summary: str = "", source: str = "manual", symbols_hint: list[str] | None = None, source_url: str = "") -> int:
        event = CatalystClassifier().classify(
            title=title,
            summary=summary,
            source=source,
            symbols_hint=symbols_hint,
            source_url=source_url,
        )
        return self.db.upsert_market_catalyst(event.to_record())

    def list(self, *, hours: int = 72) -> list[dict[str, Any]]:
        since = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours or 72)))).isoformat()
        return self.db.fetch_market_catalysts(since_iso=since, limit=500)

    def build_summary(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=hours)
        rows = self._fetch_labeled_rows(since.isoformat())
        catalysts = self.db.fetch_market_catalysts(since_iso=since.isoformat(), until_iso=now.isoformat(), limit=500)
        with_rows: list[dict[str, Any]] = []
        without_rows: list[dict[str, Any]] = []
        by_symbol: dict[str, dict[str, Any]] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            ts = str(row.get("label_timestamp") or row.get("timestamp") or "")
            active = match_catalysts(catalysts, symbol, ts)
            bucket = with_rows if active else without_rows
            bucket.append(row)
            item = by_symbol.setdefault(symbol or "NA", {"symbol": symbol or "NA", "with": [], "without": []})
            item["with" if active else "without"].append(row)
        by_symbol_rows = []
        for item in by_symbol.values():
            with_metrics = edge_metrics(item["with"])
            without_metrics = edge_metrics(item["without"])
            by_symbol_rows.append({
                "symbol": item["symbol"],
                "with_catalyst_samples": with_metrics["samples"],
                "with_catalyst_profit_factor": with_metrics["profit_factor"],
                "without_catalyst_samples": without_metrics["samples"],
                "without_catalyst_profit_factor": without_metrics["profit_factor"],
            })
        by_symbol_rows.sort(key=lambda item: safe_float(item.get("with_catalyst_profit_factor")), reverse=True)
        with_metrics = edge_metrics(with_rows)
        without_metrics = edge_metrics(without_rows)
        risk_flags = _risk_flags(catalysts, with_metrics, without_metrics)
        return {
            "hours": hours,
            "active_catalysts": catalysts,
            "with_catalyst": with_metrics,
            "without_catalyst": without_metrics,
            "by_symbol": by_symbol_rows[:12],
            "risk_flags": risk_flags,
            "recommendation": "do not treat catalyst edge as permanent",
            "final_recommendation": "NO LIVE",
        }

    def to_summary_text(self, *, hours: int = 24) -> str:
        payload = self.build_summary(hours=hours)
        lines = [
            START,
            f"hours: {payload['hours']}",
            "active_catalysts:",
            *_catalyst_lines(payload.get("active_catalysts", [])),
            "with_catalyst:",
            _metrics_line(payload["with_catalyst"]),
            "without_catalyst:",
            _metrics_line(payload["without_catalyst"]),
            "by_symbol:",
            *_symbol_lines(payload["by_symbol"]),
            "risk_flags:",
            *_risk_flag_lines(payload["risk_flags"]),
            f"recommendation: {payload['recommendation']}",
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)

    def _fetch_labeled_rows(self, since_iso: str) -> list[dict[str, Any]]:
        if hasattr(self.db, "fetch_labeled_signal_rows_since"):
            return self.db.fetch_labeled_signal_rows_since(since_iso, limit=50000)
        return [row for row in self.db.fetch_labeled_signal_rows(limit=50000) if str(row.get("timestamp") or "") >= since_iso]

    @staticmethod
    def _manual_id(title: str, symbols: list[str], start: str) -> str:
        text = f"{title}|{','.join(symbols)}|{start[:13]}"
        return "manual_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:18]


def match_catalysts(catalysts: list[dict[str, Any]], symbol: str, timestamp: str) -> list[dict[str, Any]]:
    symbol = str(symbol or "").upper()
    ts = _parse_dt(timestamp)
    matched = []
    for cat in catalysts:
        symbols = _csv_set(cat.get("symbols"))
        if symbols and "GLOBAL" not in symbols and symbol not in symbols:
            continue
        start = _parse_dt(cat.get("start_at") or cat.get("published_at") or cat.get("created_at"))
        end = _parse_dt(cat.get("end_at") or cat.get("start_at") or cat.get("created_at"))
        if start <= ts <= end:
            matched.append(cat)
    return matched


def edge_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    gains = sum(max(safe_float(row.get("realized_return_pct")), 0.0) for row in rows)
    losses = abs(sum(min(safe_float(row.get("realized_return_pct")), 0.0) for row in rows))
    total = len(rows)
    tp = sum(1 for row in rows if str(row.get("first_barrier_hit")) in {"TP1", "TP2"})
    sl = sum(1 for row in rows if str(row.get("first_barrier_hit")) == "SL")
    time_count = sum(1 for row in rows if str(row.get("first_barrier_hit")) == "TIME")
    return {
        "samples": total,
        "profit_factor": gains / losses if losses > 0 else 999.0 if gains > 0 else 0.0,
        "tp_count": tp,
        "sl_count": sl,
        "time_count": time_count,
        "tp_ratio": tp / max(total, 1),
        "sl_ratio": sl / max(total, 1),
        "time_ratio": time_count / max(total, 1),
        "expectancy": sum(safe_float(row.get("realized_return_pct")) for row in rows) / max(total, 1),
    }


def _csv_set(value: Any) -> set[str]:
    return {item.strip().upper() for item in str(value or "").split(",") if item.strip()}


def _normalize_symbols(symbols: list[str]) -> list[str]:
    clean = []
    for symbol in symbols:
        value = str(symbol or "").strip().upper()
        if value and value not in clean:
            clean.append(value)
    return clean or ["GLOBAL"]


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _risk_flags(catalysts: list[dict[str, Any]], with_metrics: dict[str, Any], without_metrics: dict[str, Any]) -> list[str]:
    flags = []
    if safe_int(with_metrics.get("samples")) >= 50 and safe_float(with_metrics.get("profit_factor")) > safe_float(without_metrics.get("profit_factor")) * 1.5:
        flags.append("catalyst_dependent_edge")
    if any(str(cat.get("direction")) == "bearish" and str(cat.get("severity")) in {"high", "critical"} for cat in catalysts):
        flags.append("bearish_macro_risk")
    if any(str(cat.get("category")) in {"regulation", "sec", "cftc"} and str(cat.get("direction")) == "bearish" for cat in catalysts):
        flags.append("regulatory_shock")
    return flags


def _catalyst_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- catalyst_id={row.get('catalyst_id')} category={row.get('category')} "
            f"direction={row.get('direction')} severity={row.get('severity')} symbols={row.get('symbols')}"
        )
        for row in rows[:10]
    ]


def _metrics_line(metrics: dict[str, Any]) -> str:
    return (
        f"- samples={safe_int(metrics.get('samples'))} PF={safe_float(metrics.get('profit_factor')):.2f} "
        f"TP%={safe_float(metrics.get('tp_ratio')) * 100:.1f} "
        f"SL%={safe_float(metrics.get('sl_ratio')) * 100:.1f} "
        f"TIME%={safe_float(metrics.get('time_ratio')) * 100:.1f}"
    )


def _symbol_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- {row.get('symbol')} with_catalyst_PF={safe_float(row.get('with_catalyst_profit_factor')):.2f} "
            f"without_catalyst_PF={safe_float(row.get('without_catalyst_profit_factor')):.2f} "
            f"with_samples={safe_int(row.get('with_catalyst_samples'))}"
        )
        for row in rows[:8]
    ]


def _risk_flag_lines(flags: list[str]) -> list[str]:
    return [f"- {flag}" for flag in flags] if flags else ["- none"]
