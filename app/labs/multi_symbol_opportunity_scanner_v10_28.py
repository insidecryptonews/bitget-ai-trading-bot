"""ResearchOps V10.28 - Multi-Symbol Shadow Opportunity Scanner (research only).

Scan a configurable universe of liquid USDT-perp symbols, quality-gate each one
(liquidity / volume / spread / volatility / data sufficiency / absurd moves),
score a candidate setup, RANK opportunities, and DECIDE disciplined SHADOW
entries (0, 1, or several) or STAY OUT -- never forcing a trade.

CRITICAL HONESTY: prior research (V10.13-23) found NO validated edge on public
OHLCV. So this scanner scores *candidate setups*; it NEVER claims a validated
edge, NEVER sends real/paper orders, and the disciplined gates make it usually
STAY OUT. Every decision carries edge_validated=false, shadow_only=true,
can_send_real_orders=false, final_recommendation=NO LIVE.

Pure/deterministic core (bars injected); no network, no DB, no orders here.
"""

from __future__ import annotations

import json
import math
import os
import statistics as st
from datetime import datetime, timezone
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import forward_shadow_regime_v10_21 as REG

TOOL_VERSION = "v10.28"
OUTPUT_ROOT = "reports/research/v10_28"
_FORBIDDEN_SEG = ("raw", "backup", "backups", "vault", "vaults", "prod", "production",
                  "live", "real", "private", "secret", "secrets", "credential",
                  "credentials", "db", "database", ".git")

DEFAULT_UNIVERSE = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "AVAXUSDT",
    "LINKUSDT", "DOGEUSDT", "LTCUSDT", "BCHUSDT", "DOTUSDT", "NEARUSDT", "APTUSDT",
    "ARBUSDT", "OPUSDT", "SUIUSDT", "INJUSDT", "ATOMUSDT",
]

DEFAULT_CONFIG = {
    "min_bars": 200,               # data sufficiency
    "max_single_bar_move": 0.35,   # >35% in one bar = bad data / absurd
    "max_spread_proxy": 0.02,      # median (high-low)/close must be <= 2%
    "min_atr_pct": 0.001,          # too flat = no opportunity
    "max_atr_pct": 0.12,           # too wild = bad conditions, stand aside
    "min_edge_score": 62,          # high bar -> usually STAY OUT
    "min_rr": 1.5,                 # minimum reward:risk
    "max_open_positions": 3,
    "max_correlation": 0.8,        # reject concurrent highly-correlated picks
    "risk_per_trade_pct": 0.5,     # % of (notional) equity risked per shadow trade
    "atr_stop_mult": 1.5,
    "atr_period": 14,
}


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "paper_candidate_future": False,
            "edge_validated": False, "makes_no_trades": True,
            "LIVE_TRADING": False, "DRY_RUN": True, "PAPER_TRADING": True,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


# --------------------------------------------------------------------------
# Indicators (bars = list of {ts,open,high,low,close,volume})
# --------------------------------------------------------------------------

def _closes(bars):
    return [float(b["close"]) for b in bars]


def _sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def _atr_pct(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = float(bars[i]["high"]), float(bars[i]["low"]), float(bars[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[-n:]) / n
    last = float(bars[-1]["close"])
    return (atr / last) if last > 0 else None


def _returns(bars):
    c = _closes(bars)
    return [(c[i] / c[i - 1] - 1) for i in range(1, len(c)) if c[i - 1] > 0]


# --------------------------------------------------------------------------
# Quality gate (liquidity / spread / volatility / data sufficiency / absurdity)
# --------------------------------------------------------------------------

def quality_gate(symbol: str, bars: list[dict], cfg: dict) -> dict[str, Any]:
    reasons = []
    n = len(bars)
    if n < cfg["min_bars"]:
        reasons.append(f"insufficient_data({n}<{cfg['min_bars']})")
    vols = [float(b.get("volume", 0) or 0) for b in bars]
    avg_vol = st.mean(vols) if vols else 0.0
    if avg_vol <= 0:
        reasons.append("no_volume")
    spreads = [(float(b["high"]) - float(b["low"])) / float(b["close"])
               for b in bars if float(b.get("close", 0) or 0) > 0]
    spread_proxy = st.median(spreads) if spreads else 1.0
    if spread_proxy > cfg["max_spread_proxy"]:
        reasons.append(f"spread_too_wide({spread_proxy:.4f})")
    rets = _returns(bars)
    max_move = max((abs(r) for r in rets), default=0.0)
    if max_move > cfg["max_single_bar_move"]:
        reasons.append(f"absurd_move({max_move:.2f})")
    atr_pct = _atr_pct(bars, cfg["atr_period"]) or 0.0
    if atr_pct < cfg["min_atr_pct"]:
        reasons.append("volatility_too_low")
    if atr_pct > cfg["max_atr_pct"]:
        reasons.append("volatility_too_high_bad_conditions")
    return {"symbol": symbol, "passed": not reasons, "reasons": reasons,
            "metrics": {"bars": n, "avg_volume": round(avg_vol, 4),
                        "spread_proxy": round(spread_proxy, 5),
                        "atr_pct": round(atr_pct, 5), "max_bar_move": round(max_move, 4)}}


# --------------------------------------------------------------------------
# Opportunity scoring (transparent 0-100; NOT a validated edge)
# --------------------------------------------------------------------------

def score_opportunity(symbol: str, bars: list[dict], cfg: dict) -> dict[str, Any]:
    closes = _closes(bars)
    c = closes[-1]
    sma20, sma50 = _sma(closes, 20), _sma(closes, 50)
    atr_pct = _atr_pct(bars, cfg["atr_period"]) or 0.0
    ret5 = (c / closes[-6] - 1) if len(closes) >= 6 else 0.0
    ret20 = (c / closes[-21] - 1) if len(closes) >= 21 else 0.0
    conf = []
    side = None
    score = 0
    up = sma20 is not None and sma50 is not None and c > sma20 > sma50
    down = sma20 is not None and sma50 is not None and c < sma20 < sma50
    if up:
        side = "long"; score += 30; conf.append("uptrend(price>sma20>sma50)")
    elif down:
        side = "short"; score += 30; conf.append("downtrend(price<sma20<sma50)")
    # pullback toward sma20 (continuation quality)
    if sma20:
        dist = abs(c / sma20 - 1)
        if dist < 0.02:
            score += 20; conf.append("near_sma20_pullback")
    # momentum consistency with side
    if side == "long" and ret5 > 0 and ret20 > 0:
        score += 20; conf.append("positive_momentum")
    elif side == "short" and ret5 < 0 and ret20 < 0:
        score += 20; conf.append("negative_momentum")
    # healthy volatility band
    if cfg["min_atr_pct"] * 3 <= atr_pct <= cfg["max_atr_pct"] * 0.6:
        score += 15; conf.append("healthy_volatility")
    # structure (higher highs for long / lower lows for short over last 20)
    if len(bars) >= 20:
        window = bars[-20:]
        hh = float(window[-1]["high"]) >= max(float(b["high"]) for b in window[:-1])
        ll = float(window[-1]["low"]) <= min(float(b["low"]) for b in window[:-1])
        if side == "long" and hh:
            score += 15; conf.append("breakout_structure")
        elif side == "short" and ll:
            score += 15; conf.append("breakdown_structure")
    # build the proposed setup (entry/stop/tp/rr/size) -- ALWAYS with a stop
    stop = tp = rr = None
    if side and atr_pct > 0:
        risk = cfg["atr_stop_mult"] * atr_pct * c
        if side == "long":
            stop = round(c - risk, 8); tp = round(c + cfg["min_rr"] * risk, 8)
        else:
            stop = round(c + risk, 8); tp = round(c - cfg["min_rr"] * risk, 8)
        rr = cfg["min_rr"]
    # size so that stop distance == risk_per_trade_pct of a notional unit (shadow)
    size_hint = None
    if stop is not None and c > 0:
        stop_dist = abs(c - stop) / c
        size_hint = round((cfg["risk_per_trade_pct"] / 100.0) / stop_dist, 6) if stop_dist > 0 else None
    return {"symbol": symbol, "edge_score": int(score), "side": side,
            "entry": round(c, 8), "stop": stop, "take_profit": tp, "rr": rr,
            "size_hint_units": size_hint, "atr_pct": round(atr_pct, 5),
            "ret5": round(ret5, 4), "ret20": round(ret20, 4),
            "confirmations": conf,
            "edge_validated": False, "note": "heuristic setup score, NOT a validated edge"}


# --------------------------------------------------------------------------
# Correlation + regime context
# --------------------------------------------------------------------------

def _corr(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 20:
        return 0.0
    a, b = a[-n:], b[-n:]
    try:
        return abs(st.correlation(a, b))   # abs: both directions add concentration
    except Exception:
        return 0.0


# --------------------------------------------------------------------------
# Scan + rank + decide
# --------------------------------------------------------------------------

def scan(bars_by_symbol: dict[str, list[dict]], config: dict | None = None) -> dict[str, Any]:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    analyzed, discarded, scored = [], [], []
    regimes = {}
    for sym, bars in bars_by_symbol.items():
        analyzed.append(sym)
        q = quality_gate(sym, bars, cfg)
        if not q["passed"]:
            discarded.append({"symbol": sym, "reasons": q["reasons"], "metrics": q["metrics"]})
            continue
        reg = REG.classify_symbol(bars, symbol=sym)
        regimes[sym] = reg.get("verdict")
        s = score_opportunity(sym, bars, cfg)
        s["regime"] = reg.get("verdict")
        scored.append(s)
    # rank by edge score (best first)
    board = sorted(scored, key=lambda x: x["edge_score"], reverse=True)

    decisions, stayed_out = [], []
    chosen_returns: list[list[float]] = []
    chosen_symbols: set[str] = set()
    for s in board:
        sym = s["symbol"]
        why_out = []
        if s["side"] is None:
            why_out.append("no_directional_setup")
        if s["edge_score"] < cfg["min_edge_score"]:
            why_out.append(f"edge_below_min({s['edge_score']}<{cfg['min_edge_score']})")
        if s["stop"] is None:
            why_out.append("no_stop_loss")
        if (s["rr"] or 0) < cfg["min_rr"]:
            why_out.append("rr_below_min")
        # rule: don't go long into a risk-off regime (bad conditions)
        if s["side"] == "long" and s.get("regime") == REG.R_RISK_OFF:
            why_out.append("long_blocked_risk_off")
        if sym in chosen_symbols:
            why_out.append("duplicate_symbol")
        if len(decisions) >= cfg["max_open_positions"]:
            why_out.append("max_open_positions_reached")
        # correlation cap vs already-chosen
        if not why_out:
            rets = _returns(bars_by_symbol[sym])
            if any(_corr(rets, cr) > cfg["max_correlation"] for cr in chosen_returns):
                why_out.append("too_correlated_with_open")
        if why_out:
            stayed_out.append({"symbol": sym, "edge_score": s["edge_score"], "reasons": why_out})
            continue
        decisions.append({
            "symbol": sym, "action": "SHADOW_ENTRY_CANDIDATE", "side": s["side"],
            "edge_score": s["edge_score"], "entry": s["entry"], "stop": s["stop"],
            "take_profit": s["take_profit"], "rr": s["rr"], "size_hint_units": s["size_hint_units"],
            "risk_per_trade_pct": cfg["risk_per_trade_pct"], "confirmations": s["confirmations"],
            "regime": s.get("regime"), "reason": "top-ranked setup clearing all discipline gates",
            "executed": False, "would_send_real_order": False, "edge_validated": False})
        chosen_symbols.add(sym)
        chosen_returns.append(_returns(bars_by_symbol[sym]))

    verdict = "SHADOW_CANDIDATES" if decisions else "STAY_OUT_NO_EDGE"
    return {"tool_version": TOOL_VERSION, "universe_size": len(bars_by_symbol),
            "analyzed": analyzed, "discarded": discarded,
            "opportunity_board": board, "regimes": regimes,
            "decisions": decisions, "stayed_out": stayed_out,
            "n_shadow_candidates": len(decisions), "verdict": verdict,
            "config": cfg, **_safety()}


# --------------------------------------------------------------------------
# Plan (no network / no writes) + hardened journal writer
# --------------------------------------------------------------------------

def plan(universe: list[str] | None = None, config: dict | None = None) -> dict[str, Any]:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    uni = list(universe) if universe else list(DEFAULT_UNIVERSE)
    return {
        "tool_version": TOOL_VERSION,
        "what": "Scan a universe of liquid USDT-perp symbols, quality-gate each, "
                "score + RANK candidate setups, and DECIDE disciplined SHADOW entries "
                "(0, 1, or several) or STAY OUT. Never forces a trade.",
        "universe": uni, "universe_size": len(uni),
        "quality_gates": ["insufficient_data", "no_volume", "spread_too_wide",
                          "absurd_move", "volatility_too_low", "volatility_too_high"],
        "discipline_rules": [
            "no entry unless edge_score >= min_edge_score",
            "every candidate carries an ATR stop-loss (no naked risk)",
            "reward:risk must be >= min_rr",
            "no long into a RISK_OFF regime",
            "no duplicate position on the same symbol",
            "respect max_open_positions",
            "reject picks too correlated with an already-chosen one",
            "no martingale, no absurd leverage (leverage not modelled at all here)",
        ],
        "writes_on_plan": False, "uses_api_keys": False, "uses_network": False,
        "reads_orders": False, "config": cfg,
        "honesty": "prior research (V10.13-23) found NO validated edge on public "
                   "OHLCV; scores are heuristic candidate quality, not a proven edge; "
                   "this module makes NO trades.",
        **_safety()}


def _repo_root():
    from pathlib import Path
    return Path(__file__).resolve().parents[2]


def safe_output_dir(rel: str = OUTPUT_ROOT):
    """Resolve an output dir under the repo, fail-closed (no traversal / symlink /
    forbidden segment). Journals are non-sensitive but we still contain writes."""
    from pathlib import Path
    rel_parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in rel_parts):
        raise ValueError(f"unsafe_output_dir(traversal): {rel}")
    if any(p.lower() in _FORBIDDEN_SEG for p in rel_parts):
        raise ValueError(f"unsafe_output_dir(forbidden_segment): {rel}")
    repo = _repo_root()
    logical = repo.joinpath(*rel_parts)
    # reject symlinked ancestor components that already exist
    cur = repo
    for part in rel_parts:
        cur = cur / part
        if cur.exists() and cur.is_symlink():
            raise ValueError(f"unsafe_output_dir(symlink_component): {rel}")
    real_repo = repo.resolve()
    real_target = logical.resolve()
    if real_repo != real_target and real_repo not in real_target.parents:
        raise ValueError(f"unsafe_output_dir(escapes_repo): {rel}")
    logical.mkdir(parents=True, exist_ok=True)
    return logical


def _now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(base, filename: str, text: str) -> str:
    if os.path.basename(filename) != filename or filename.startswith("."):
        raise ValueError(f"unsafe_filename: {filename}")
    path = base / filename
    tmp = base / (filename + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return str(path)


def write_journal(report: dict, output_dir: str = OUTPUT_ROOT, filename: str | None = None) -> str:
    """Autosave the latest scan report as JSON under the gitignored reports dir."""
    base = safe_output_dir(output_dir)
    payload = {"written_at": _now_z(), **report}
    return _atomic_write(base, filename or "scanner_state.json",
                         json.dumps(payload, ensure_ascii=False, indent=2))


def compact_scan(report: dict, scan_no: int = 0) -> dict[str, Any]:
    """Small, append-friendly summary of one scan (for the JSONL audit log)."""
    board = report.get("opportunity_board", [])
    return {
        "scan_no": scan_no, "ts": _now_z(),
        "analyzed": len(report.get("analyzed", [])),
        "discarded": [{"symbol": d["symbol"], "reasons": d["reasons"]}
                      for d in report.get("discarded", [])],
        "top": [{"symbol": s["symbol"], "edge_score": s["edge_score"],
                 "side": s["side"], "regime": s.get("regime")} for s in board[:5]],
        "decisions": [{"symbol": d["symbol"], "side": d["side"], "edge_score": d["edge_score"],
                       "entry": d["entry"], "stop": d["stop"], "take_profit": d["take_profit"],
                       "rr": d["rr"], "size_hint_units": d["size_hint_units"]}
                      for d in report.get("decisions", [])],
        "stayed_out": [{"symbol": s["symbol"], "reasons": s["reasons"]}
                       for s in report.get("stayed_out", [])],
        "verdict": report.get("verdict"),
        "n_shadow_candidates": report.get("n_shadow_candidates", 0),
        "edge_validated": False, "executed": False, "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }


def append_scan_log(report: dict, output_dir: str = OUTPUT_ROOT, scan_no: int = 0) -> str:
    """Append-only audit line per scan (periodic autosave, survives crash/power loss)."""
    base = safe_output_dir(output_dir)
    line = json.dumps(compact_scan(report, scan_no), ensure_ascii=False)
    path = base / "scanner_scans.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return str(path)


def write_shutdown(summary: dict, output_dir: str = OUTPUT_ROOT) -> str:
    base = safe_output_dir(output_dir)
    payload = {"shutdown_at": _now_z(), "clean_shutdown": True,
               "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE, **summary}
    return _atomic_write(base, "scanner_shutdown.json",
                         json.dumps(payload, ensure_ascii=False, indent=2))


def render_board(report: dict, scan_no: int, elapsed_s: float = 0.0) -> str:
    """Human-readable live board for the CMD window."""
    L = []
    L.append("=" * 68)
    L.append(f" SCAN #{scan_no}  {_now_z()}  (+{elapsed_s:.1f}s)  [SHADOW / NO LIVE]")
    L.append("=" * 68)
    board = report.get("opportunity_board", [])
    L.append(f" analyzed={len(report.get('analyzed', []))}  "
             f"scored={len(board)}  discarded={len(report.get('discarded', []))}  "
             f"candidates={report.get('n_shadow_candidates', 0)}")
    if board:
        L.append(" RANKING (best first):")
        L.append("   {:<10} {:>5} {:>6} {:>10} {:>12}".format("SYMBOL", "SCORE", "SIDE", "REGIME", "ATR%"))
        for s in board[:10]:
            L.append("   {:<10} {:>5} {:>6} {:>10} {:>11.2f}%".format(
                s["symbol"], s["edge_score"], (s["side"] or "-"),
                str(s.get("regime") or "-")[:10], (s.get("atr_pct") or 0.0) * 100))
    disc = report.get("discarded", [])
    if disc:
        L.append(" DISCARDED: " + ", ".join(f"{d['symbol']}({';'.join(d['reasons'])})" for d in disc[:8]))
    dec = report.get("decisions", [])
    if dec:
        L.append(" >>> SHADOW ENTRY CANDIDATES (simulated, NO real/paper order):")
        for d in dec:
            L.append(f"   {d['symbol']} {d['side'].upper()} score={d['edge_score']} "
                     f"entry={d['entry']} stop={d['stop']} tp={d['take_profit']} rr={d['rr']} "
                     f"size~{d['size_hint_units']}u  [{'/'.join(d['confirmations'][:3])}]")
    else:
        L.append(" >>> DECISION: STAY OUT (no setup cleared the discipline gates)")
    L.append(f" verdict={report.get('verdict')}   edge_validated=False   would_send_real_order=False")
    return "\n".join(L)


# --------------------------------------------------------------------------
# Live scan loop (network-free core; the CLI injects the real bars_provider)
# --------------------------------------------------------------------------

def run_loop(*, universe, bars_provider, config=None, max_scans: int = 1,
             interval_seconds: float = 60.0, output_dir: str = OUTPUT_ROOT,
             sleep_fn=None, should_stop=None, emit=None, time_fn=None) -> dict[str, Any]:
    """Core live loop. NO network here: `bars_provider(symbol)->list[bars]|None`.

    Discipline / safety are enforced by scan(); this only orchestrates fetch ->
    scan -> render -> AUTOSAVE (every scan) -> sleep, with a clean-shutdown check.
    max_scans<=0 => run until should_stop() (Ctrl+C or q/quit/exit/stop). Between
    scans the wait is chopped into small slices so a stop request is honoured fast.
    """
    import time as _time
    emit = emit or (lambda s: print(s, flush=True))
    sleep_fn = sleep_fn or _time.sleep
    should_stop = should_stop or (lambda: False)
    time_fn = time_fn or _time.monotonic
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    uni = [str(s).strip().upper() for s in universe if str(s).strip()]

    started = time_fn()
    scan_no = 0
    totals = {"scans": 0, "candidates": 0, "stay_out": 0, "errors": 0}
    last_report: dict[str, Any] = {}
    stop_reason = "completed"

    emit(f"[V10.28] Multi-Symbol Shadow Opportunity Scanner  universe={len(uni)}  "
         f"interval={interval_seconds}s  max_scans={max_scans or 'until-stop'}")
    emit("SAFE MODE: LIVE_TRADING=False DRY_RUN=True PAPER_TRADING=True - makes NO real/paper orders.")
    emit("Stop cleanly with Ctrl+C or by typing q / quit / exit / stop then Enter.")
    try:
        while True:
            if should_stop():
                stop_reason = "user_stop"
                break
            if max_scans and scan_no >= max_scans:
                stop_reason = "max_scans"
                break
            scan_no += 1
            bars_by_symbol: dict[str, list] = {}
            for sym in uni:
                try:
                    bars = bars_provider(sym)
                except Exception as exc:  # per-symbol isolation (bad data/latency)
                    totals["errors"] += 1
                    emit(f"   ! fetch error {sym}: {type(exc).__name__}")
                    continue
                if bars:
                    bars_by_symbol[sym] = bars
            report = scan(bars_by_symbol, cfg)
            last_report = report
            emit(render_board(report, scan_no, time_fn() - started))
            # --- periodic autosave: persist EVERY scan (survives crash/power loss) ---
            try:
                write_journal(report, output_dir)
                append_scan_log(report, output_dir, scan_no)
            except Exception as exc:
                emit(f"   ! autosave error: {type(exc).__name__}: {exc}")
            totals["scans"] += 1
            totals["candidates"] += report.get("n_shadow_candidates", 0)
            if report.get("verdict") == "STAY_OUT_NO_EDGE":
                totals["stay_out"] += 1
            if max_scans and scan_no >= max_scans:
                stop_reason = "max_scans"
                break
            # responsive interruptible wait
            waited = 0.0
            while waited < interval_seconds:
                if should_stop():
                    stop_reason = "user_stop"
                    break
                step = min(0.5, interval_seconds - waited)
                sleep_fn(step)
                waited += step
            if stop_reason == "user_stop":
                break
    except KeyboardInterrupt:
        stop_reason = "keyboard_interrupt"

    # --- clean shutdown: stop new work, flush state, confirm on screen ---
    emit("")
    emit(f"[V10.28] clean shutdown starting (reason={stop_reason}) ...")
    summary = {"tool_version": TOOL_VERSION, "stop_reason": stop_reason,
               "scans_completed": totals["scans"], "shadow_candidates_total": totals["candidates"],
               "stay_out_scans": totals["stay_out"], "fetch_errors": totals["errors"],
               "runtime_seconds": round(time_fn() - started, 2),
               "last_verdict": last_report.get("verdict"), **_safety()}
    try:
        sp = write_shutdown(summary, output_dir)
        emit(f"   state flushed, no new work started, journal saved -> {sp}")
    except Exception as exc:
        emit(f"   ! shutdown-save error: {type(exc).__name__}: {exc}")
    emit("[V10.28] CLEAN SHUTDOWN COMPLETE. No real/paper orders were ever sent.")
    return summary
