"""ResearchOps V10.21 - Forward-Shadow Regime Overlay (read-only, NO ORDERS).

A live, zero-risk "what does the bot see right now" overlay. Given recent public
OHLCV it labels each symbol's CURRENT market regime (downtrend / uptrend / range /
high-vol / drawdown) and a research-only ACTION CONTEXT (e.g. LONG_BLOCKED,
RANGE_NO_TRADE, SHORT_BIAS, BOUNCE_CANDIDATE). It then journals snapshots over
time so you can watch its read evolve.

HONESTY (this is the whole point of the module):
- It is DESCRIPTIVE, not predictive. It classifies the regime that IS, it does
  NOT claim to forecast or to have a validated edge (V10.13-20 found none on
  public OHLCV). `descriptive_only=true`, `predicts_nothing=true`,
  `edge_validated=false`.
- It makes NO trades, sends NO orders, sets NO leverage, touches NO money.
- Its main practical value is RISK CONTEXT: telling you when conditions are
  unfavorable / noisy and the honest action is to NOT trade.

Pure/offline/deterministic. No .env, no DB, no raw writes, no private endpoints.
FINAL_RECOMMENDATION: NO LIVE.
"""

from __future__ import annotations

import csv
import json
import os
import statistics as st
from datetime import datetime, timezone
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE

TOOL_VERSION = "v10.21"
OUTPUT_ROOT = "reports/research/v10_21"
JOURNAL_ROOT = "reports/research/v10_21/regime_journal"
DAY_MS = 86_400_000

# regime verdict vocabulary (research-only action context; NOT trade signals)
R_RISK_OFF = "RISK_OFF_EARLY_WARNING"
R_SHORT_BIAS = "SHORT_BIAS"
R_LONG_BLOCKED = "LONG_BLOCKED"
R_BOUNCE = "BOUNCE_CANDIDATE"
R_RANGE = "RANGE_NO_TRADE"
R_RISK_ON = "RISK_ON_RECOVERY"
R_NO_EDGE = "NO_EDGE"

_FORBIDDEN_SEG = ("raw", "backup", "backups", "vault", "vaults", "training_exports",
                  "secret", "secrets", "credential", "credentials",
                  "codex_result.md", "code_result.md")
_FORBIDDEN_SUF = (".env", ".db", ".sqlite", ".sqlite3", ".zip", ".tar", ".gz", ".pem", ".key")


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "edge_validated": False,
            "paper_candidate_future": False,
            "descriptive_only": True, "predicts_nothing": True,
            "makes_no_trades": True, "is_trade_signal": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_output_base(output_dir: str | None, default: str) -> str:
    base = output_dir or default
    if not isinstance(base, str) or not base.strip() or "%" in base:
        return default
    segs = [s for s in base.replace("\\", "/").split("/") if s]
    if ".." in segs:
        return default
    for s in (x.lower() for x in segs):
        if s in _FORBIDDEN_SEG or s.endswith(_FORBIDDEN_SUF) or ".env" in s:
            return default
    return base


def _read_ohlcv(path: str) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    rows.append({"ts": int(float(r.get("timestamp") or r.get("ts"))),
                                 "open": float(r["open"]), "high": float(r["high"]),
                                 "low": float(r["low"]), "close": float(r["close"])})
                except (TypeError, ValueError, KeyError):
                    continue
    except Exception:
        return []
    rows.sort(key=lambda x: x["ts"])
    return rows


def _sma(vals: list[float], n: int) -> float | None:
    return sum(vals[-n:]) / n if len(vals) >= n else None


# --------------------------------------------------------------------------
# Descriptive regime classification (uses ONLY closed past bars -> no lookahead)
# --------------------------------------------------------------------------

def classify_symbol(bars: list[dict[str, float]], *, symbol: str = "", timeframe: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"symbol": symbol, "timeframe": timeframe, "bars": len(bars),
                           "regime": "INSUFFICIENT_DATA", "verdict": R_NO_EDGE,
                           "reasons": []}
    if len(bars) < 55:
        return out
    closes = [b["close"] for b in bars]
    c = closes[-1]
    sma20, sma50 = _sma(closes, 20), _sma(closes, 50)
    rets = [(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes)) if closes[i - 1] > 0]
    vol20 = st.pstdev(rets[-20:]) if len(rets) >= 20 else 0.0
    vol_hist = st.median([abs(x) for x in rets[-120:]]) if len(rets) >= 30 else (vol20 or 1e-9)
    ret5 = (c / closes[-6] - 1) if len(closes) >= 6 else 0.0
    ret20 = (c / closes[-21] - 1) if len(closes) >= 21 else 0.0
    high30 = max(b["high"] for b in bars[-30:])
    dd30 = (c / high30 - 1) if high30 > 0 else 0.0
    up = sma20 is not None and sma50 is not None and c > sma20 > sma50
    down = sma20 is not None and sma50 is not None and c < sma20 < sma50
    high_vol = vol20 > 1.6 * (vol_hist or 1e-9)
    low_range = abs(ret20) < 0.03 and vol20 < 0.9 * (vol_hist or 1e-9)
    reasons = []
    if down: reasons.append("price<SMA20<SMA50")
    if up: reasons.append("price>SMA20>SMA50")
    if high_vol: reasons.append(f"vol20={vol20:.4f}>1.6x_hist")
    if dd30 <= -0.10: reasons.append(f"drawdown30={dd30*100:.1f}%")
    # regime + research-only action context
    if down and (high_vol or dd30 <= -0.10):
        regime, verdict = "DOWNTREND_RISKOFF", R_RISK_OFF
        reasons.append("downtrend+stress -> risk_off; LONG context blocked, SHORT_BIAS context")
    elif down:
        regime, verdict = "DOWNTREND", R_LONG_BLOCKED
    elif dd30 <= -0.12 and ret5 > 0.03:
        regime, verdict = "RECOVERY_OFF_LOWS", R_BOUNCE
        reasons.append("deep drawdown + short-term reclaim -> bounce context (needs confirmation)")
    elif low_range:
        regime, verdict = "RANGE_LOWVOL", R_RANGE
        reasons.append("low range + low vol -> no-trade context (cost>move)")
    elif up:
        regime, verdict = "UPTREND", R_RISK_ON
        reasons.append("uptrend -> risk_on context (NOT a validated long signal)")
    else:
        regime, verdict = "MIXED_NOISE", R_NO_EDGE
    out.update({"regime": regime, "verdict": verdict, "close": c,
                "ret5": round(ret5, 4), "ret20": round(ret20, 4),
                "vol20": round(vol20, 5), "drawdown30": round(dd30, 4),
                "above_sma20": (sma20 is not None and c > sma20),
                "above_sma50": (sma50 is not None and c > sma50),
                "high_vol": high_vol, "reasons": reasons})
    return out


def _counts(per_symbol: list[dict[str, Any]]) -> dict[str, int]:
    v = [s.get("verdict") for s in per_symbol]
    return {"risk_off_count": v.count(R_RISK_OFF), "long_blocked_count": v.count(R_LONG_BLOCKED),
            "bounce_count": v.count(R_BOUNCE), "range_count": v.count(R_RANGE),
            "risk_on_count": v.count(R_RISK_ON), "no_edge_count": v.count(R_NO_EDGE)}


def _action_hint(basket_verdict: str) -> str:
    return {
        R_RISK_OFF: "RESEARCH CONTEXT: risk-off basket -> longs unfavorable / reduce-risk context (NO orders)",
        R_RANGE: "RESEARCH CONTEXT: range/low-vol -> no-trade context, cost>move (NO orders)",
        R_RISK_ON: "RESEARCH CONTEXT: broad uptrend -> risk-on context (NOT a validated long signal)",
    }.get(basket_verdict, "RESEARCH CONTEXT: no validated edge -> observe only (NO orders)")


def classify_basket(per_symbol: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [s for s in per_symbol if s["regime"] not in ("INSUFFICIENT_DATA",)]
    n = len(usable) or 1
    n_down = sum(1 for s in usable if s["regime"].startswith("DOWNTREND"))
    n_up = sum(1 for s in usable if s["regime"] == "UPTREND")
    n_range = sum(1 for s in usable if s["regime"] == "RANGE_LOWVOL")
    n_riskoff = sum(1 for s in usable if s["verdict"] == R_RISK_OFF)
    if n_riskoff >= 3 or n_down >= 4:
        basket = R_RISK_OFF
    elif n_range >= 3:
        basket = R_RANGE
    elif n_up >= 4:
        basket = R_RISK_ON
    else:
        basket = R_NO_EDGE
    return {"basket_verdict": basket, "n_symbols": len(usable),
            "n_downtrend": n_down, "n_uptrend": n_up, "n_range": n_range,
            "n_risk_off": n_riskoff,
            "note": "descriptive basket regime; NOT a trade signal; no validated edge"}


# --------------------------------------------------------------------------
# Run + journal
# --------------------------------------------------------------------------

def run_regime(sample_dir: str, symbols: list[str], timeframe: str = "1d") -> dict[str, Any]:
    rep: dict[str, Any] = {
        "tool_version": TOOL_VERSION, "generated_at": _now_iso(), "sample_dir": sample_dir,
        "timeframe": timeframe, "symbols": symbols, "per_symbol": [], "errors": [], **_safety()}
    if not (isinstance(sample_dir, str) and os.path.isdir(sample_dir)):
        rep["errors"].append("sample_dir_not_found")
        return rep
    per = []
    for s in symbols:
        path = os.path.join(sample_dir, f"{s}_{timeframe}_ohlcv.csv")
        if not os.path.isfile(path):
            rep["errors"].append(f"missing:{s}_{timeframe}")
            continue
        per.append(classify_symbol(_read_ohlcv(path), symbol=s, timeframe=timeframe))
    rep["per_symbol"] = per
    rep["basket"] = classify_basket(per)
    rep["counts"] = _counts(per)
    rep["action_hint"] = _action_hint(rep["basket"]["basket_verdict"])
    return rep


def write_journal(rep: dict[str, Any], output_dir: str | None = None) -> str:
    base = _safe_output_base(output_dir, JOURNAL_ROOT)
    os.makedirs(base, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(base, f"regime_{stamp}.json").replace("\\", "/")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2, default=str)
    # append a flat line to a rolling journal for easy time-series viewing
    counts = rep.get("counts") or _counts(rep.get("per_symbol", []))
    line = {"ts": rep["generated_at"], "sample_dir": rep.get("sample_dir"),
            "timeframe": rep.get("timeframe"),
            "symbols": [s["symbol"] for s in rep.get("per_symbol", [])],
            "basket": rep.get("basket", {}).get("basket_verdict"),
            "per_symbol": {s["symbol"]: s["verdict"] for s in rep.get("per_symbol", [])},
            "action_hint": rep.get("action_hint") or _action_hint(rep.get("basket", {}).get("basket_verdict", "")),
            "makes_no_trades": True, "can_send_real_orders": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE, **counts}
    with open(os.path.join(base, "regime_timeline.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(line, default=str) + "\n")
    return path


def summarize_timeline(rows: list[dict[str, Any]], last_n: int = 10) -> dict[str, Any]:
    """Read-only analysis of journaled snapshots: latest, evolution, regime
    changes, consecutive-snapshot streaks, weakest/recovering symbols."""
    out: dict[str, Any] = {"snapshots": len(rows), "latest": None, "previous": None,
                           "changes": [], "streaks": {}, "weakest": [], "recovering": [],
                           "evolution": []}
    rows = [r for r in rows if isinstance(r, dict) and r.get("per_symbol")]
    if not rows:
        return out
    latest = rows[-1]
    out["latest"] = latest
    out["previous"] = rows[-2] if len(rows) >= 2 else None
    out["evolution"] = [{"ts": r.get("ts"), "basket": r.get("basket"),
                         "per_symbol": r.get("per_symbol")} for r in rows[-last_n:]]
    # per-symbol verdict change vs previous snapshot + consecutive streak
    syms = list(latest.get("per_symbol", {}).keys())
    for sym in syms:
        cur = latest["per_symbol"].get(sym)
        prev = (out["previous"] or {}).get("per_symbol", {}).get(sym) if out["previous"] else None
        if prev is not None and prev != cur:
            tag = "ENTERED_RISK_OFF" if cur == R_RISK_OFF else (
                "EXITED_RISK_OFF" if prev == R_RISK_OFF else (
                    "NEW_BOUNCE_CANDIDATE" if cur == R_BOUNCE else "REGIME_CHANGE"))
            out["changes"].append({"symbol": sym, "from": prev, "to": cur, "event": tag})
        # streak: trailing snapshots with same verdict as current
        streak = 0
        for r in reversed(rows):
            if r.get("per_symbol", {}).get(sym) == cur:
                streak += 1
            else:
                break
        out["streaks"][sym] = {"verdict": cur, "consecutive_snapshots": streak}
    out["weakest"] = [s for s in syms if latest["per_symbol"][s] in (R_RISK_OFF, R_LONG_BLOCKED)]
    out["recovering"] = [s for s in syms if latest["per_symbol"][s] in (R_BOUNCE, R_RISK_ON)]
    return out


def forward_shadow_regime_plan() -> dict[str, Any]:
    return {
        "tool_version": TOOL_VERSION, "objective": (
            "read-only live overlay: classify the CURRENT market regime per symbol and a "
            "research-only action context; journal it over time; make NO trades"),
        "verdict_vocabulary": [R_RISK_OFF, R_SHORT_BIAS, R_LONG_BLOCKED, R_BOUNCE,
                               R_RANGE, R_RISK_ON, R_NO_EDGE],
        "inputs": "recent public OHLCV (reuse cross-exchange/bitget staged data, or fetch latest)",
        "honesty": ["descriptive_only", "predicts_nothing", "no_validated_edge",
                    "makes_no_trades", "main_value_is_risk_context_and_no_trade_filter"],
        "never": ["place_order", "create_order", "set_leverage", "ExecutionEngine.execute",
                  "PaperTrader.open_position", "APPROVED_FOR_PAPER", "APPROVED_FOR_LIVE"],
        **_safety()}
