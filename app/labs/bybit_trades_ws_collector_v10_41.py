"""ResearchOps V10.41 - Bybit public-trade WS collector CORE (research only).

The DATA_GAP problem in V10.40 comes from the current collector pulling ~1000
trades per REST cycle (clustered, multi-minute gaps). The real fix is a
CONTINUOUS websocket subscription to Bybit v5 public `publicTrade.<SYMBOL>`
(public, no keys, no orders). This module implements the PURE, testable core of
such a collector -- message parsing, trade_id dedup, gap detection, idempotent
merge/append -- WITHOUT any live network loop (that live loop is the documented
next step). Nothing here connects, sends orders, or uses keys.
"""

from __future__ import annotations

from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE

TOOL_VERSION = "v10.41"
SOURCE_EXCHANGE = "bybit_linear"
WS_PUBLIC_LINEAR = "wss://stream.bybit.com/v5/public/linear"   # reference only
STALE_MS = 15_000            # no frames for 15s -> stale stream
GAP_MS = 5_000               # inter-trade gap > 5s is notable for BTCUSDT


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "can_send_real_orders": False,
            "uses_api_keys": False, "subscribes_private_channels": False,
            "sends_orders": False, "not_actionable": True,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def parse_public_trade_message(msg: dict) -> list[dict]:
    """Bybit v5 `publicTrade.<SYMBOL>` frame -> canonical trade rows.

    Frame: {"topic":"publicTrade.BTCUSDT","type":"snapshot","ts":..,
            "data":[{"T":<ms>,"s":"BTCUSDT","S":"Buy"|"Sell","v":"0.01",
                     "p":"50000.5","i":"<tradeId>", ...}, ...]}
    Only the public trade stream is accepted; malformed items are skipped."""
    if not isinstance(msg, dict):
        return []
    topic = str(msg.get("topic", ""))
    if not topic.startswith("publicTrade."):
        return []
    rows: list[dict] = []
    for item in (msg.get("data") or []):
        try:
            ts = int(item["T"])
            price = float(item["p"])
            size = float(item["v"])
            side = str(item.get("S", "")).lower()          # "buy"/"sell"
            side = "buy" if side == "buy" else ("sell" if side == "sell" else "")
            tid = str(item["i"])
            sym = str(item.get("s") or topic.split(".", 1)[1])
        except (KeyError, TypeError, ValueError):
            continue
        if ts <= 0 or price <= 0 or size <= 0 or not tid:
            continue
        rows.append({"timestamp": ts, "symbol": sym, "price": price, "size": size,
                     "aggressor_side": side, "trade_id": tid,
                     "source_exchange": SOURCE_EXCHANGE})
    return rows


def dedup_by_trade_id(rows: list[dict], seen: set[str] | None = None
                      ) -> tuple[list[dict], set[str]]:
    """Drop rows whose trade_id was already seen (cross-frame + cross-restart
    idempotency). Returns (unique_rows, updated_seen)."""
    seen = set(seen) if seen else set()
    out = []
    for r in rows:
        tid = r.get("trade_id")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        out.append(r)
    return out, seen


def detect_gaps(rows: list[dict], gap_ms: int = GAP_MS) -> list[dict]:
    """Report inter-trade timestamp gaps > gap_ms in a time-sorted row list."""
    ts = sorted(int(r["timestamp"]) for r in rows if r.get("timestamp"))
    return [{"after_ts": ts[i - 1], "before_ts": ts[i], "gap_ms": ts[i] - ts[i - 1]}
            for i in range(1, len(ts)) if ts[i] - ts[i - 1] > gap_ms]


def merge_append(existing: list[dict], new: list[dict]) -> tuple[list[dict], int]:
    """Idempotent merge: dedup by trade_id across both sets, keep ascending
    timestamp order. Returns (merged_sorted, n_added)."""
    seen = {r["trade_id"] for r in existing if r.get("trade_id")}
    added = [r for r in new if r.get("trade_id") and r["trade_id"] not in seen
             and not seen.add(r["trade_id"])]
    merged = existing + added
    merged.sort(key=lambda r: int(r["timestamp"]))
    return merged, len(added)


def heartbeat(last_frame_ms: int, now_ms: int, stale_ms: int = STALE_MS) -> dict:
    """Stale-stream detector for a future live loop."""
    age = now_ms - last_frame_ms
    return {"age_ms": age, "stale": age > stale_ms,
            "status": "STALE_NO_FRAMES" if age > stale_ms else "ALIVE", **_safety()}


def plan() -> dict:
    """Design of the (not-yet-implemented) continuous live WS loop. Documents
    what a `scripts/collect_bybit_trades_ws_forever.ps1` + live runner would do.
    NO live loop is implemented today."""
    return {"tool_version": TOOL_VERSION, "core_implemented": True,
            "live_ws_loop_implemented": False,
            "reference_endpoint": WS_PUBLIC_LINEAR,
            "subscribe_topic": "publicTrade.<SYMBOL> (public, no auth)",
            "live_loop_design": [
                "connect wss v5 public linear; subscribe publicTrade.<symbols>",
                "on frame -> parse_public_trade_message -> dedup_by_trade_id",
                "buffer + periodic flush (append) to the V10.32 trades dataset",
                "heartbeat: STALE_NO_FRAMES if no frames > 15s -> reconnect backoff",
                "single-instance mutex; convive/replace the REST cluster collector",
                "write source_exchange=bybit_linear; ascending ts; sha256 manifest"],
            "why": ("continuous ticks remove the REST-cadence DATA_GAP that makes "
                    "V10.40 forward simulation mostly stale"),
            "safety": "public only, no keys, no orders, no .env, research-only",
            **_safety()}
