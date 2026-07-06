"""ResearchOps V10.38 - Continuous Edge Factory (research only, fail-closed).

The machinery that discovers, validates, ranks, promotes and demotes strategy
CANDIDATES continuously -- without ever producing an actionable signal:

  bars -> point-in-time FEATURES -> future-only LABELS -> candidate DISCOVERY
  -> NET-EV after costs -> WALK-FORWARD vs baselines -> INCUBATOR states ->
  POLICY REGISTRY (audited) -> SHADOW decisions -> PAPER GATE (blocked) ->
  DRIFT detector -> reports.

HONESTY CONTRACT: every output is RESEARCH_ONLY / NOT_ACTIONABLE. Verdicts can
say PROMISING_RESEARCH_ONLY, never BUY/SELL. The paper gate always ends
BLOCKED (human approval is not encodable). LIVE states are structurally
rejected. Real money can only ever run a fixed, human-approved, audited
policy -- which does not exist. FINAL_RECOMMENDATION: NO LIVE.

Pure/deterministic core (bars injected in tests); dataset loading reads the
V10.32 forward dataset + V10.36 backfill days. No network in this module.
"""

from __future__ import annotations

import csv
import json
import math
import os
import random
import statistics as st
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE

TOOL_VERSION = "v10.38"
OUTPUT_SUBDIR = ("reports", "research", "v10_38")
REGISTRY_SUBDIR = ("reports", "research", "policy_registry")

DEFAULT_COSTS = {"fee_bps": 5.5, "spread_bps": 1.0, "slippage_bps": 3.0,
                 "turnover_penalty_bps": 1.0}
MIN_SAMPLE = 30
MIN_OOS_SAMPLE = 20
RANDOM_BASELINE_MARGIN = 1.3

CANDIDATE_STATES = frozenset({
    "DISCOVERED", "INCUBATING", "SHADOW_ELIGIBLE", "PAPER_ELIGIBLE_BLOCKED",
    "REJECTED", "EXPIRED", "PAUSED_DECAY", "PROMOTION_BLOCKED",
    "FUTURE_MICRO_LIVE_CANDIDATE_BLOCKED"})
FORBIDDEN_STATES = frozenset({"LIVE", "LIVE_READY", "CAN_SEND_REAL_ORDERS"})
REGISTRY_STATES = frozenset({"RESEARCH_ONLY", "SHADOW_ONLY",
                             "PAPER_CANDIDATE_BLOCKED", "REJECTED", "PAUSED",
                             "EXPIRED"})
CANDIDATE_VERDICTS = ("PROMISING_RESEARCH_ONLY", "NEEDS_MORE_DATA",
                      "REJECTED_NEGATIVE_EV", "REJECTED_OVERFIT_RISK",
                      "REJECTED_DATA_QUALITY", "NOT_ACTIONABLE")


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "edge_validated": False,
            "not_actionable": True, "no_orders": True,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ==========================================================================
# 0) Bars from the real datasets (V10.32 forward + V10.36 backfill days)
# ==========================================================================

def build_bars_from_trades(trades: list[dict], bar_seconds: int = 60,
                           symbol: str = "BTCUSDT") -> list[dict]:
    """trade rows (timestamp ms, price, size, aggressor_side) -> OHLCV bars
    with buy/sell volume split. Input may be unsorted; output is sorted.

    NO-LOOKAHEAD CONTRACT: a bar aggregates high/low/close of the WHOLE bucket,
    so it is NOT knowable at bar_start_ts. Each bar therefore carries an explicit
    bar_start_ts, bar_close_ts and last_trade_ts, and `available_at` is set to
    bar_close_ts (>= last_trade_ts). `ts` == bar_close_ts for downstream code, so
    features/labels anchor to when the candle is actually complete, never to its
    open."""
    rows = []
    for t in trades:
        try:
            rows.append((int(float(t["timestamp"])), float(t["price"]),
                         float(t["size"]), str(t.get("aggressor_side", "")),
                         str(t.get("symbol", symbol) or symbol)))
        except (KeyError, TypeError, ValueError):
            continue
    rows.sort(key=lambda r: r[0])
    bars: list[dict] = []
    width = bar_seconds * 1000
    cur = None
    for ts, price, size, side, sym in rows:
        bucket = (ts // width) * width
        if cur is None or cur["bar_start_ts"] != bucket:
            if cur is not None:
                bars.append(cur)
            cur = {"symbol": sym, "bar_start_ts": bucket,
                   "bar_close_ts": bucket + width, "ts": bucket + width,
                   "open": price, "high": price, "low": price, "close": price,
                   "volume": 0.0, "buy_volume": 0.0, "sell_volume": 0.0,
                   "n_trades": 0, "trade_count": 0, "max_trade": 0.0,
                   "first_trade_ts": ts, "last_trade_ts": ts,
                   "available_at": bucket + width}
        cur["high"] = max(cur["high"], price)
        cur["low"] = min(cur["low"], price)
        cur["close"] = price
        cur["volume"] += size
        cur["n_trades"] += 1
        cur["trade_count"] += 1
        cur["max_trade"] = max(cur["max_trade"], size)
        cur["last_trade_ts"] = ts
        # bar becomes fully known only once the bucket closes; never before
        cur["available_at"] = max(cur["bar_close_ts"], ts)
        if side == "buy":
            cur["buy_volume"] += size
        elif side == "sell":
            cur["sell_volume"] += size
    if cur is not None:
        bars.append(cur)
    return bars


def load_dataset(symbol: str = "BTCUSDT", bar_seconds: int = 60,
                 max_rows: int = 500_000) -> dict[str, Any]:
    """Read the real V10.32 forward dataset (+ any imported V10.36 backfill
    days) into bars + auxiliary series. Read-only."""
    repo = _repo_root()
    fwd = repo / "external_data" / "staging" / "bybit_microstructure_v10_32" / "dataset"
    bfl = repo / "external_data" / "staging" / "bybit_backfill_v10_36" / symbol

    def read_csv(path: Path, cap: int) -> list[dict]:
        if not path.is_file() or path.is_symlink():
            return []
        out = []
        with open(path, "r", newline="", encoding="utf-8") as f:
            for i, r in enumerate(csv.DictReader(f)):
                if i >= cap:
                    break
                out.append(r)
        return out

    trades = read_csv(fwd / "trades.csv", max_rows)
    if bfl.is_dir():
        for day_csv in sorted(bfl.glob("trades_*.csv")):
            trades.extend(read_csv(day_csv, max_rows))
    trades = [t for t in trades if str(t.get("symbol", "")).upper() == symbol]
    return {"symbol": symbol,
            "bars": build_bars_from_trades(trades, bar_seconds),
            "oi": read_csv(fwd / "open_interest.csv", max_rows),
            "funding": read_csv(fwd / "funding.csv", max_rows),
            "orderbook": read_csv(fwd / "orderbook_l2.csv", max_rows),
            "liquidations": read_csv(fwd / "liquidations.csv", max_rows)}


# ==========================================================================
# 1) FEATURES -- strictly point-in-time (only bars[:i+1] and aux <= ts)
# ==========================================================================

def _sma(vals, n):
    return sum(vals[-n:]) / min(n, len(vals)) if vals else 0.0


def _aux_before(series: list[tuple[int, float]], ts: int, n: int = 12) -> list[float]:
    return [v for t, v in series if t <= ts][-n:]


def build_features(bars: list[dict], oi: list[dict] | None = None,
                   funding: list[dict] | None = None,
                   orderbook: list[dict] | None = None,
                   liquidations: list[dict] | None = None,
                   lookback: int = 20) -> list[dict]:
    """One feature row per bar i, computed ONLY from data at or before
    bars[i]['ts']. available_at == bar close ts."""
    def aux(rows, key):
        out = []
        for r in (rows or []):
            try:
                out.append((int(float(r["timestamp"])), float(r[key])))
            except (KeyError, TypeError, ValueError):
                continue
        out.sort(key=lambda x: x[0])
        return out

    oi_s = aux(oi, "open_interest")
    fu_s = aux(funding, "funding_rate")
    ob_bid = aux(orderbook, "bid_price_1")
    ob_ask = aux(orderbook, "ask_price_1")
    ob_bsz = aux(orderbook, "bid_size_1")
    ob_asz = aux(orderbook, "ask_size_1")
    liq_ts = []
    for r in (liquidations or []):
        try:
            liq_ts.append((int(float(r["timestamp"])),
                           float(r.get("price", 0)) * float(r.get("size", 0)),
                           str(r.get("side", ""))))
        except (KeyError, TypeError, ValueError):
            continue
    liq_ts.sort(key=lambda x: x[0])

    feats: list[dict] = []
    for i in range(len(bars)):
        w = bars[max(0, i - lookback + 1):i + 1]
        ts = bars[i]["ts"]
        closes = [b["close"] for b in w]
        vols = [b["volume"] for b in w]
        rets = [(closes[j] / closes[j - 1] - 1) for j in range(1, len(closes))
                if closes[j - 1] > 0]
        c = closes[-1]
        vol_mean = _sma(vols, lookback) or 1e-12
        bs = sum(b["buy_volume"] for b in w)
        ss = sum(b["sell_volume"] for b in w)
        rv = st.pstdev(rets) if len(rets) > 1 else 0.0
        # a feature is available no earlier than its source bar is complete
        avail = bars[i].get("available_at", ts)
        f: dict[str, Any] = {"ts": ts, "available_at": avail, "close": c}
        # trades block
        f["trade_intensity"] = _sma([b["n_trades"] for b in w], lookback)
        f["buy_sell_imbalance"] = (bs - ss) / (bs + ss) if (bs + ss) > 0 else 0.0
        f["aggressive_flow_proxy"] = (bars[i]["buy_volume"] - bars[i]["sell_volume"]) / \
            (bars[i]["volume"] or 1e-12)
        f["volume_acceleration"] = bars[i]["volume"] / vol_mean - 1
        f["large_trade_proxy"] = bars[i]["max_trade"] / \
            (max((b["max_trade"] for b in w), default=1e-12) or 1e-12)
        f["burst_score"] = (bars[i]["n_trades"] - f["trade_intensity"]) / \
            (f["trade_intensity"] or 1e-12)
        f["trade_clustering"] = st.pstdev([b["n_trades"] for b in w]) / \
            (f["trade_intensity"] or 1e-12) if len(w) > 1 else 0.0
        f["short_term_price_impact"] = (rets[-1] / (bars[i]["volume"] / vol_mean)
                                        if rets and bars[i]["volume"] > 0 else 0.0)
        # orderbook block (last snapshot at or before ts)
        bid = _aux_before(ob_bid, ts, 1)
        ask = _aux_before(ob_ask, ts, 1)
        bsz = _aux_before(ob_bsz, ts, 1)
        asz = _aux_before(ob_asz, ts, 1)
        if bid and ask and bid[-1] > 0:
            f["spread"] = (ask[-1] - bid[-1]) / bid[-1]
            f["midprice"] = (ask[-1] + bid[-1]) / 2
            tot = (bsz[-1] + asz[-1]) if bsz and asz else 0.0
            f["top_imbalance"] = ((bsz[-1] - asz[-1]) / tot) if tot > 0 else 0.0
            f["depth_imbalance"] = f["top_imbalance"]          # L1 proxy
            f["liquidity_holes"] = 1.0 if f["spread"] > 0.001 else 0.0
            f["book_slope"] = f["spread"] / (tot or 1e-12)
            f["book_pressure"] = f["top_imbalance"] * (1 - min(f["spread"] * 100, 1))
        else:
            f.update({"spread": 0.0, "midprice": c, "top_imbalance": 0.0,
                      "depth_imbalance": 0.0, "liquidity_holes": 0.0,
                      "book_slope": 0.0, "book_pressure": 0.0})
        # oi block
        ois = _aux_before(oi_s, ts, 12)
        f["oi_change"] = (ois[-1] / ois[0] - 1) if len(ois) > 1 and ois[0] > 0 else 0.0
        f["oi_acceleration"] = ((ois[-1] - ois[-2]) - (ois[-2] - ois[-3])) / \
            (ois[-2] or 1e-12) if len(ois) > 2 else 0.0
        ret20 = (c / closes[0] - 1) if closes[0] > 0 else 0.0
        f["price_oi_divergence"] = (1.0 if (ret20 > 0) != (f["oi_change"] > 0)
                                    and abs(f["oi_change"]) > 1e-6 else 0.0)
        f["crowding_proxy"] = abs(f["oi_change"]) * (1 + abs(ret20) * 10)
        # funding block
        fus = _aux_before(fu_s, ts, 6)
        f["funding_level"] = fus[-1] if fus else 0.0
        f["funding_change"] = (fus[-1] - fus[0]) if len(fus) > 1 else 0.0
        f["funding_stress"] = abs(f["funding_level"]) / 0.0005
        f["crowdedness_proxy"] = f["funding_stress"] * (1 + f["crowding_proxy"])
        # liquidations block (window = bar span * lookback)
        lo = ts - lookback * 60_000
        win = [(t, n, s) for t, n, s in liq_ts if lo < t <= ts]
        f["liquidation_count"] = float(len(win))
        f["liquidation_notional_proxy"] = sum(n for _, n, _ in win)
        lb = sum(1 for _, _, s in win if s == "buy")
        ls_ = sum(1 for _, _, s in win if s == "sell")
        f["liquidation_side_imbalance"] = ((lb - ls_) / (lb + ls_)
                                           if (lb + ls_) > 0 else 0.0)
        recent = [(t, n, s) for t, n, s in win if t > ts - 3 * 60_000]
        f["cascade_score"] = float(len(recent)) / (f["liquidation_count"] or 1.0)
        f["aftershock_score"] = (abs(rets[-1]) * 100 if recent and rets else 0.0)
        # regime block
        f["realized_volatility"] = rv
        f["trend_score"] = (c / _sma(closes, lookback) - 1) if closes else 0.0
        f["chop_score"] = (sum(1 for r in rets if r > 0) / len(rets) - 0.5) * -2 \
            if rets else 0.0
        f["liquidity_regime"] = 1.0 if vol_mean > 0 and bars[i]["volume"] > vol_mean else 0.0
        f["session_bucket"] = float((ts // 3_600_000) % 24 // 8)
        f["stress_mode"] = 1.0 if (rv > 0.005 or f["liquidation_count"] > 5) else 0.0
        f["symbol_regime"] = ("trend" if abs(f["trend_score"]) > rv * 2 else "chop")
        feats.append(f)
    return feats


# ==========================================================================
# 2) LABELS -- strictly future-only, cost-adjusted, multi-horizon
# ==========================================================================

def build_labels(bars: list[dict], side: str = "long",
                 horizons: tuple[int, ...] = (5, 15, 60),
                 tp_pct: float = 0.004, sl_pct: float = 0.002,
                 time_bars: int = 60, costs: dict | None = None) -> list[dict]:
    """One label row per bar i, computed ONLY from bars[i+1:], with a REAL
    side-aware triple barrier -- NOT a costed inversion of the long outcome.

      long : TP when high >= entry*(1+tp), SL when low  <= entry*(1-sl)
      short: TP when low  <= entry*(1-tp), SL when high >= entry*(1+sl)

    MAE/MFE, gross/net return and cost_adjusted_outcome are all expressed in the
    side's own PnL space. Missing labels (end of data) are explicit, never
    silently dropped."""
    if side not in ("long", "short"):
        raise ValueError(f"side must be 'long' or 'short': {side}")
    c = {**DEFAULT_COSTS, **(costs or {})}
    round_trip_cost = 2 * (c["fee_bps"] + c["slippage_bps"]) / 10_000 \
        + c["spread_bps"] / 10_000
    labels: list[dict] = []
    for i, bar in enumerate(bars):
        entry = bar["close"]
        lab: dict[str, Any] = {"ts": bar["ts"], "side": side,
                               "side_label_method": "real_side_aware",
                               "label_available_at": None, "missing": False}
        if i + 1 >= len(bars) or entry <= 0:
            lab["missing"] = True
            labels.append(lab)
            continue
        future = bars[i + 1:i + 1 + time_bars]
        # forward returns per horizon, in the SIDE's PnL direction
        for h in horizons:
            if i + h < len(bars):
                r = bars[i + h]["close"] / entry - 1
                lab[f"forward_return_{h}"] = r if side == "long" else -r
            else:
                lab[f"forward_return_{h}"] = None
        if side == "long":
            tp, sl = entry * (1 + tp_pct), entry * (1 - sl_pct)
        else:
            tp, sl = entry * (1 - tp_pct), entry * (1 + sl_pct)
        outcome, hit_i = "TIME", len(future) - 1
        mae = mfe = 0.0
        for j, fb in enumerate(future):
            up, dn = fb["high"] / entry - 1, fb["low"] / entry - 1
            if side == "long":
                mfe, mae = max(mfe, up), min(mae, dn)
                hit_sl, hit_tp = fb["low"] <= sl, fb["high"] >= tp
            else:                                  # short: favorable = price down
                mfe, mae = max(mfe, -dn), min(mae, -up)
                hit_sl, hit_tp = fb["high"] >= sl, fb["low"] <= tp
            if hit_sl:                             # conservative: SL wins ties
                outcome, hit_i = "SL", j
                break
            if hit_tp:
                outcome, hit_i = "TP", j
                break
        if outcome == "TP":
            gross = tp_pct
        elif outcome == "SL":
            gross = -sl_pct
        else:
            close_ret = future[hit_i]["close"] / entry - 1
            gross = close_ret if side == "long" else -close_ret
        lab["triple_barrier"] = outcome
        lab["MAE"] = mae
        lab["MFE"] = mfe
        lab["time_to_hit"] = hit_i + 1
        lab["gross_return"] = gross
        lab["cost_estimate"] = round_trip_cost
        lab["net_return"] = gross - round_trip_cost
        lab["cost_adjusted_outcome"] = gross - round_trip_cost
        lab["stay_out_label"] = 1 if lab["cost_adjusted_outcome"] <= 0 else 0
        rv = st.pstdev([b["close"] for b in future[:15]]) / entry \
            if len(future) >= 2 else 0.0
        lab["volatility_adjusted_label"] = (lab["cost_adjusted_outcome"] / rv
                                            if rv > 1e-9 else 0.0)
        lab["label_available_at"] = future[hit_i]["ts"]
        labels.append(lab)
    return labels


def assert_no_lookahead(features: list[dict], labels: list[dict],
                        bars: list[dict] | None = None) -> bool:
    """Structural guard: every feature is available no earlier than its source
    bar is complete (never at bar open); every label only becomes available
    strictly AFTER the bar it belongs to. When `bars` is supplied, the feature's
    available_at must match the bar's own available_at exactly."""
    for idx, f in enumerate(features):
        av = f.get("available_at")
        if av is None or av < f["ts"]:
            raise ValueError(f"feature availability violates point-in-time at {f['ts']}")
        if bars is not None:
            src = bars[idx].get("available_at", bars[idx].get("ts"))
            if src is not None and av != src:
                raise ValueError(f"feature availability != source bar at {f['ts']}")
    for l in labels:
        if not l.get("missing") and l["label_available_at"] is not None \
                and l["label_available_at"] <= l["ts"]:
            raise ValueError(f"label available before its bar at {l['ts']}")
    return True


# ==========================================================================
# 3) NET-EV evaluation (costs first, abstention default)
# ==========================================================================

def evaluate_net_ev(outcomes: list[float], turnover: float = 1.0,
                    costs: dict | None = None,
                    min_edge_bps: float = 2.0) -> dict[str, Any]:
    """outcomes = cost-adjusted per-trade results (already net of round-trip
    costs from labels). Adds turnover + uncertainty penalties and decides."""
    c = {**DEFAULT_COSTS, **(costs or {})}
    n = len(outcomes)
    rep: dict[str, Any] = {"sample_size": n, "turnover": turnover, **_safety()}
    if n < MIN_SAMPLE:
        rep.update({"gross_EV": None, "net_EV": None, "net_EV_lower_bound": None,
                    "decision": "ABSTAIN", "reason": f"sample<{MIN_SAMPLE}"})
        return rep
    mean = st.mean(outcomes)
    sd = st.pstdev(outcomes) if n > 1 else 0.0
    turnover_cost = turnover * c["turnover_penalty_bps"] / 10_000
    uncertainty = 1.65 * sd / math.sqrt(n)
    net = mean - turnover_cost
    lower = net - uncertainty
    wins = [o for o in outcomes if o > 0]
    losses = [o for o in outcomes if o <= 0]
    rep.update({
        "gross_EV": round(mean, 8), "fees_included_in_outcomes": True,
        "turnover_cost": round(turnover_cost, 8),
        "uncertainty_penalty": round(uncertainty, 8),
        "net_EV": round(net, 8), "net_EV_lower_bound": round(lower, 8),
        "EV_confidence_interval": [round(net - uncertainty, 8),
                                   round(net + uncertainty, 8)],
        "win_rate": round(len(wins) / n, 4),
        "payoff_ratio": round((st.mean(wins) / abs(st.mean(losses)))
                              if wins and losses and st.mean(losses) != 0 else 0.0, 4)})
    if lower > min_edge_bps / 10_000:
        rep["decision"] = "TRADE_IN_SIMULATION_RESEARCH_ONLY"
    elif net > 0:
        rep["decision"] = "ABSTAIN"
        rep["reason"] = "net_EV positive but lower bound not above min edge"
    else:
        rep["decision"] = "REJECT"
        rep["reason"] = "net_EV_after_costs <= 0"
    return rep


# ==========================================================================
# 4) Candidate discovery (simple transparent threshold rules)
# ==========================================================================

DISCOVERY_FEATURES = ("buy_sell_imbalance", "burst_score", "oi_change",
                      "funding_level", "liquidation_side_imbalance",
                      "cascade_score", "trend_score", "book_pressure")


def _entries_for_rule(features, labels_side, feat, side, thr):
    """Collect the side's OWN cost-adjusted outcomes for the bars where the rule
    fires. `labels_side` must already be the side-aware label set (long labels
    for long rules, real short labels for short rules) -- no inversion here."""
    outs = []
    for f, l in zip(features, labels_side):
        if l.get("missing") or l.get("cost_adjusted_outcome") is None:
            continue
        v = f.get(feat)
        if v is None or isinstance(v, str):
            continue
        fired = v > thr if side == "long" else v < -thr
        if fired:
            outs.append(l["cost_adjusted_outcome"])
    return outs


def _approx_short_invert(long_outs):
    """Blocked fallback ONLY: approximate short outcomes by inverting costed long
    ones. Never promotable -- callers must tag SHORT_APPROXIMATE_LABELS."""
    load = 2 * (DEFAULT_COSTS["fee_bps"] + DEFAULT_COSTS["slippage_bps"]) / 10_000
    return [-o - load for o in long_outs]


def discover_candidates(features: list[dict], labels: list[dict],
                        symbol: str = "BTCUSDT", bars: list[dict] | None = None,
                        labels_short: list[dict] | None = None) -> list[dict]:
    """Grid of transparent one-feature threshold rules.

    P1 (no data snooping): the chronological split comes FIRST and every
    threshold is a quantile of the TRAIN features ONLY. OOS features never
    influence the threshold (only the evaluation). Each candidate records
    threshold_source='train_only'.

    P3 (real short): long rules use the long labels; short rules use REAL
    side-aware short labels (built from `bars` or passed in `labels_short`).
    If neither is available, short falls back to an approximate inversion that
    is flagged and can NEVER be PROMISING."""
    assert_no_lookahead(features, labels)
    n = len(features)
    split = int(n * 0.6)                          # chronological split FIRST
    train_feats = features[:split]
    train_ok = split >= MIN_SAMPLE
    approx_short = False
    if labels_short is None:
        labels_short = build_labels(bars, side="short") if bars is not None else None
        approx_short = labels_short is None
    labels_by_side = {"long": labels, "short": labels_short}
    candidates: list[dict] = []
    cid = 0
    for feat in DISCOVERY_FEATURES:
        # threshold derived from TRAIN ONLY -- OOS distribution is never seen here
        train_vals = sorted(f[feat] for f in train_feats
                            if isinstance(f.get(feat), (int, float)))
        insufficient_train = len(train_vals) < MIN_SAMPLE
        for q in (0.66, 0.9):
            thr = (train_vals[min(int(len(train_vals) * q), len(train_vals) - 1)]
                   if train_vals else 0.0)
            for side in ("long", "short"):
                cid += 1
                side_is_approx = side == "short" and approx_short
                side_labels = labels_by_side.get(side)
                if side_labels is None:            # blocked approximate short path
                    train_out = _approx_short_invert(
                        _entries_for_rule(features[:split], labels[:split], feat, side, thr))
                    oos_out = _approx_short_invert(
                        _entries_for_rule(features[split:], labels[split:], feat, side, thr))
                    side_method = "approx_inverse_long"
                else:
                    train_out = _entries_for_rule(features[:split], side_labels[:split],
                                                  feat, side, thr)
                    oos_out = _entries_for_rule(features[split:], side_labels[split:],
                                                feat, side, thr)
                    side_method = "real_side_aware"
                ev_tr = evaluate_net_ev(train_out)
                ev_oos = evaluate_net_ev(oos_out)
                blockers = ["SHORT_APPROXIMATE_LABELS"] if side_is_approx else []
                cand = {"candidate_id": f"{symbol}_{feat}_{side}_q{int(q*100)}_{cid}",
                        "symbol": symbol, "side": side,
                        "setup_name": f"{feat}>{'+' if side=='long' else '-'}p{int(q*100)}",
                        "regime": "all", "threshold": thr,
                        "threshold_source": "train_only",
                        "side_label_method": side_method,
                        "approximate_short_labels": bool(side_is_approx),
                        "promotion_blockers_extra": blockers,
                        "sample_size": ev_oos["sample_size"],
                        "win_rate": ev_oos.get("win_rate"),
                        "payoff_ratio": ev_oos.get("payoff_ratio"),
                        "gross_EV": ev_oos.get("gross_EV"),
                        "cost_estimate": DEFAULT_COSTS,
                        "net_EV": ev_oos.get("net_EV"),
                        "net_EV_lower_bound": ev_oos.get("net_EV_lower_bound"),
                        "max_drawdown": round(min(_cum_dd(oos_out), 0.0), 6),
                        "turnover": round(len(oos_out) / max(1, n - split), 4),
                        "confidence": ev_oos.get("win_rate") or 0.0,
                        "data_quality": "ok" if n >= 200 else "thin",
                        "train_sample_size": ev_tr["sample_size"],
                        "train_net_EV": ev_tr.get("net_EV"), **_safety()}
                if insufficient_train or not train_ok:
                    cand["verdict"] = "REJECTED_DATA_QUALITY"
                elif ev_oos["sample_size"] < MIN_OOS_SAMPLE:
                    cand["verdict"] = "NEEDS_MORE_DATA"
                elif n < 200:
                    cand["verdict"] = "REJECTED_DATA_QUALITY"
                elif (ev_oos.get("net_EV") or -1) <= 0:
                    cand["verdict"] = "REJECTED_NEGATIVE_EV"
                elif (ev_tr.get("net_EV") or 0) > 0 and \
                        (ev_oos.get("net_EV") or 0) < 0.3 * (ev_tr.get("net_EV") or 1):
                    cand["verdict"] = "REJECTED_OVERFIT_RISK"
                elif ev_oos["decision"] == "TRADE_IN_SIMULATION_RESEARCH_ONLY":
                    # approximate short can never be promoted to PROMISING
                    cand["verdict"] = ("NOT_ACTIONABLE" if side_is_approx
                                       else "PROMISING_RESEARCH_ONLY")
                else:
                    cand["verdict"] = "NOT_ACTIONABLE"
                candidates.append(cand)
    candidates.sort(key=lambda x: (x.get("net_EV_lower_bound") or -9), reverse=True)
    return candidates


def _cum_dd(outcomes: list[float]) -> float:
    peak = cum = 0.0
    dd = 0.0
    for o in outcomes:
        cum += o
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return dd


# ==========================================================================
# 5) Walk-forward vs baselines
# ==========================================================================

def walk_forward(features: list[dict], labels: list[dict], feat: str,
                 side: str, thr: float, n_windows: int = 4,
                 embargo: int = 5, seed: int = 7,
                 labels_short: list[dict] | None = None,
                 bars: list[dict] | None = None) -> dict[str, Any]:
    assert_no_lookahead(features, labels)
    n = len(features)
    # short walk-forward uses REAL side-aware labels (never a costed inversion)
    approx_short = False
    if side == "short":
        if labels_short is None and bars is not None:
            labels_short = build_labels(bars, side="short")
        side_labels = labels_short
        approx_short = side_labels is None
    else:
        side_labels = labels
    rep: dict[str, Any] = {"feature": feat, "side": side, "threshold": thr,
                           "side_label_method": ("approx_inverse_long" if approx_short
                                                 else "real_side_aware"),
                           "windows": [], **_safety()}
    if n < (n_windows + 1) * MIN_SAMPLE:
        rep["verdict"] = "NEEDS_MORE_DATA"
        return rep
    win = n // (n_windows + 1)
    rng = random.Random(seed)
    oos_evs, rand_evs = [], []
    for k in range(n_windows):
        te_lo = (k + 1) * win + embargo
        te_hi = min((k + 2) * win, n)
        if te_hi - te_lo < MIN_OOS_SAMPLE:
            continue
        f_te = features[te_lo:te_hi]
        if side_labels is None:                    # blocked approximate fallback
            outs = _approx_short_invert(
                _entries_for_rule(f_te, labels[te_lo:te_hi], feat, side, thr))
            l_te = labels[te_lo:te_hi]
        else:
            l_te = side_labels[te_lo:te_hi]
            outs = _entries_for_rule(f_te, l_te, feat, side, thr)
        ev = evaluate_net_ev(outs)
        # random baseline: same n entries, random bars in window
        pool = [l["cost_adjusted_outcome"] for l in l_te
                if not l.get("missing") and l.get("cost_adjusted_outcome") is not None]
        rnd = [rng.choice(pool) for _ in range(len(outs))] if pool and outs else []
        ev_r = evaluate_net_ev(rnd)
        rep["windows"].append({"window": k, "n": len(outs),
                               "net_EV": ev.get("net_EV"),
                               "random_net_EV": ev_r.get("net_EV")})
        if ev.get("net_EV") is not None:
            oos_evs.append(ev["net_EV"])
            rand_evs.append(ev_r.get("net_EV") or 0.0)
    rep["no_trade_baseline_EV"] = 0.0
    rep["shuffled_label_note"] = "random baseline draws outcomes uniformly (equivalent)"
    if not oos_evs:
        rep["verdict"] = "NEEDS_MORE_DATA"
        return rep
    mean_oos = st.mean(oos_evs)
    positive = sum(1 for e in oos_evs if e > 0)
    rep["net_EV_OOS"] = round(mean_oos, 8)
    rep["stability_score"] = round(positive / len(oos_evs), 4)
    rep["abstention_rate"] = None
    beats_random = mean_oos > RANDOM_BASELINE_MARGIN * max(st.mean(rand_evs), 0) \
        and mean_oos > 0
    if mean_oos <= 0:
        rep["verdict"] = "OOS_FAIL"
    elif not beats_random:
        rep["verdict"] = "OVERFIT_SUSPECTED"
    elif rep["stability_score"] < 0.75:
        rep["verdict"] = "UNSTABLE"
    else:
        rep["verdict"] = "OOS_PASS_RESEARCH_ONLY"
    return rep


# ==========================================================================
# 6) Incubator + policy registry (fail-closed states)
# ==========================================================================

class CandidateIncubator:
    def __init__(self):
        self.candidates: dict[str, dict] = {}

    def upsert(self, cand: dict, status: str = "DISCOVERED") -> dict:
        if status in FORBIDDEN_STATES:
            raise ValueError(f"forbidden state: {status}")
        if status not in CANDIDATE_STATES:
            raise ValueError(f"unknown state: {status}")
        entry = {**cand, "status": status,
                 "promotion_blockers": ["human_approval_required",
                                        "paper_filter_enabled=false",
                                        "edge_validated=false"]}
        self.candidates[cand["candidate_id"]] = entry
        return entry

    def transition(self, cid: str, new_status: str, reason: str) -> dict:
        if new_status in FORBIDDEN_STATES:
            raise ValueError(f"forbidden state: {new_status}")
        if new_status not in CANDIDATE_STATES:
            raise ValueError(f"unknown state: {new_status}")
        c = self.candidates[cid]
        c.setdefault("history", []).append(
            {"at": _now_iso(), "from": c["status"], "to": new_status,
             "reason": reason})
        c["status"] = new_status
        return c

    def rank(self) -> list[dict]:
        return sorted(self.candidates.values(),
                      key=lambda c: (c.get("net_EV_lower_bound") or -9),
                      reverse=True)


class PolicyRegistry:
    def __init__(self, base: Path | None = None):
        self.base = base or _repo_root().joinpath(*REGISTRY_SUBDIR)

    def register(self, policy: dict) -> str:
        status = policy.get("status", "RESEARCH_ONLY")
        if status in FORBIDDEN_STATES:
            raise ValueError(f"forbidden policy state: {status}")
        if status not in REGISTRY_STATES:
            raise ValueError(f"unknown policy state: {status}")
        self.base.mkdir(parents=True, exist_ok=True)
        pid = policy["policy_id"]
        path = self.base / f"{pid}.json"
        prev = {}
        if path.is_file():
            try:
                prev = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                prev = {}
        trail = prev.get("audit_trail", [])
        trail.append({"at": _now_iso(), "status": status,
                      "version": policy.get("version", 1)})
        doc = {**policy, "audit_trail": trail,
               "blocked_live_flags": {"actual_live_ready": False,
                                      "can_send_real_orders": False,
                                      "human_promotion_required": True},
               **_safety()}
        tmp = self.base / f"{pid}.json.tmp"
        tmp.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)
        return str(path)


# ==========================================================================
# 7) Shadow runner + paper gate (blocked) + drift detector
# ==========================================================================

def shadow_decide(candidate: dict, feature_row: dict) -> dict[str, Any]:
    feat = candidate["setup_name"].split(">")[0] if ">" in candidate.get(
        "setup_name", "") else None
    v = feature_row.get(feat) if feat else None
    thr = candidate.get("threshold", 0)
    side = candidate.get("side", "long")
    fired = isinstance(v, (int, float)) and (v > thr if side == "long" else v < -thr)
    abstain = None
    if not fired:
        abstain = "signal_not_fired"
    elif feature_row.get("spread", 0) > 0.001:
        fired, abstain = False, "spread_too_wide"
    elif feature_row.get("stress_mode") == 1.0 and side == "long":
        fired, abstain = False, "stress_regime"
    return {"timestamp": feature_row["ts"], "symbol": candidate["symbol"],
            "candidate_id": candidate["candidate_id"],
            "would_enter": bool(fired), "side": side,
            "confidence": candidate.get("confidence"),
            "expected_net_EV": candidate.get("net_EV"),
            "abstain_reason": abstain, "data_quality": candidate.get("data_quality"),
            "regime": feature_row.get("symbol_regime"),
            "shadow_outcome": None,
            "kind": "SHADOW_DECISION_ONLY_NOT_ACTIONABLE", **_safety()}


PAPER_GATE_CRITERIA = ("minimum_sample_size", "OOS_net_EV_positive",
                       "walk_forward_stable", "drawdown_acceptable",
                       "costs_included", "slippage_included", "no_overfit",
                       "regime_edge_clear", "not_one_day_dependency",
                       "shadow_period_sufficient", "human_approval_required")


def paper_promotion_gate(candidate: dict, wf: dict | None = None) -> dict[str, Any]:
    checks = {
        "minimum_sample_size": (candidate.get("sample_size") or 0) >= 100,
        "OOS_net_EV_positive": (candidate.get("net_EV") or 0) > 0,
        "walk_forward_stable": bool(wf and wf.get("verdict") == "OOS_PASS_RESEARCH_ONLY"),
        "drawdown_acceptable": (candidate.get("max_drawdown") or -1) > -0.05,
        "costs_included": True, "slippage_included": True,
        "no_overfit": candidate.get("verdict") != "REJECTED_OVERFIT_RISK",
        "regime_edge_clear": False, "not_one_day_dependency": False,
        "shadow_period_sufficient": False,
        "human_approval_required": False}      # not encodable: always unmet
    unmet = [k for k, v in checks.items() if not v]
    if len(unmet) <= 1 and unmet == ["human_approval_required"]:
        status = "HUMAN_REVIEW_REQUIRED"
    elif checks["OOS_net_EV_positive"] and checks["walk_forward_stable"]:
        status = "NEEDS_MORE_SHADOW"
    else:
        status = "PAPER_PROMOTION_REJECTED"
    return {"candidate_id": candidate.get("candidate_id"), "checks": checks,
            "unmet": unmet, "status": status,
            "paper_gate_blocked": True, "paper_filter_enabled": False,
            "note": ("gate ALWAYS ends blocked: human approval + sufficient "
                     "shadow time are not encodable"), **_safety()}


DRIFT_ACTIONS = ("PAUSE_CANDIDATE_SHADOW", "DEMOTE_TO_RESEARCH",
                 "REQUIRE_REVALIDATION", "BLOCK_PROMOTION", "ALERT_ONLY")


def drift_check(recent_outcomes: list[float], reference_outcomes: list[float],
                recent_features: list[float] | None = None,
                reference_features: list[float] | None = None) -> dict[str, Any]:
    rep: dict[str, Any] = {"signals": [], "action": "ALERT_ONLY", **_safety()}
    if len(recent_outcomes) >= 10 and len(reference_outcomes) >= 10:
        r_ev, ref_ev = st.mean(recent_outcomes), st.mean(reference_outcomes)
        rep["rolling_EV"] = round(r_ev, 8)
        rep["reference_EV"] = round(ref_ev, 8)
        if ref_ev > 0 and r_ev < 0:
            rep["signals"].append("EV_SIGN_FLIP")
        elif ref_ev > 0 and r_ev < 0.5 * ref_ev:
            rep["signals"].append("EV_DECAY")
        r_hit = sum(1 for o in recent_outcomes if o > 0) / len(recent_outcomes)
        ref_hit = sum(1 for o in reference_outcomes if o > 0) / len(reference_outcomes)
        if ref_hit - r_hit > 0.15:
            rep["signals"].append("HIT_RATE_DROP")
        if _cum_dd(recent_outcomes) < 2 * _cum_dd(reference_outcomes):
            rep["signals"].append("DRAWDOWN_EXPANSION")
    if recent_features and reference_features and len(reference_features) >= 10:
        med_ref = st.median(reference_features)
        spread = st.pstdev(reference_features) or 1e-12
        if abs(st.median(recent_features) - med_ref) > 2 * spread:
            rep["signals"].append("FEATURE_DISTRIBUTION_DRIFT")
    if "EV_SIGN_FLIP" in rep["signals"]:
        rep["action"] = "PAUSE_CANDIDATE_SHADOW"
    elif "EV_DECAY" in rep["signals"] or "HIT_RATE_DROP" in rep["signals"]:
        rep["action"] = "REQUIRE_REVALIDATION"
    elif "FEATURE_DISTRIBUTION_DRIFT" in rep["signals"]:
        rep["action"] = "BLOCK_PROMOTION"
    assert rep["action"] in DRIFT_ACTIONS
    rep["future_real_policy_note"] = "FUTURE_REAL_POLICY_PAUSE_REQUIRES_HUMAN_APPROVAL"
    return rep


# ==========================================================================
# 7b) Future micro-live scaffold -- DESIGN ONLY, structurally blocked
# ==========================================================================

FUTURE_MICRO_LIVE_SAFEGUARDS = (
    "approved_policy_only", "fixed_policy_version", "no_hot_self_modification",
    "max_loss", "max_orders", "max_exposure", "kill_switch", "stale_data_halt",
    "exchange_reconciliation", "duplicate_order_protection", "idempotency",
    "human_approval", "rollback", "audit_log", "shadow_learner_separate")


def future_micro_live_scaffold(approved_policy: dict | None = None) -> dict[str, Any]:
    """Describe -- but never enable -- the safeguards a future micro-live path
    would require. There is no approved, fixed, audited policy today, so this is
    ALWAYS blocked. It sends nothing, touches no exchange, holds no keys.

    The learner (this factory) is deliberately kept SEPARATE from any executor:
    a model/agent may NEVER flip a real policy automatically. Only a human can
    promote a fixed policy version, and that promotion is not encodable here."""
    # A policy is even a *candidate* for the (blocked) path only if it is an
    # explicitly RESEARCH_ONLY/SHADOW_ONLY registry entry with a pinned version.
    has_named_policy = bool(approved_policy and approved_policy.get("policy_id")
                            and approved_policy.get("version") is not None)
    policy_state = (approved_policy or {}).get("status")
    # even a named policy stays blocked: 'approved' is not encodable in software
    safeguards = {name: {"required": True, "implemented": False,
                         "status": "BLOCKED_DESIGN_ONLY"}
                  for name in FUTURE_MICRO_LIVE_SAFEGUARDS}
    blockers = ["no_human_approval", "no_fixed_approved_policy",
                "edge_not_validated", "paper_filter_disabled",
                "learner_must_stay_separate_from_executor"]
    if policy_state in FORBIDDEN_STATES:
        raise ValueError(f"forbidden policy state for scaffold: {policy_state}")
    return {"tool_version": TOOL_VERSION,
            "scaffold": "FUTURE_MICRO_LIVE_BLOCKED",
            "safeguards": safeguards,
            "has_named_policy_reference": has_named_policy,
            "candidate_state": "FUTURE_MICRO_LIVE_CANDIDATE_BLOCKED",
            "blockers": blockers,
            "actual_live_ready": False,
            "can_send_real_orders": False,
            "human_promotion_required": True,
            "shadow_learner_separate_from_executor": True,
            "note": ("scaffold is design-only; enabling any real order path "
                     "requires a human-approved fixed policy that does not exist"),
            **_safety()}


# ==========================================================================
# 8) Continuous edge cycle (manual, one pass, reports)
# ==========================================================================

def run_cycle(symbol: str = "BTCUSDT", bars: list[dict] | None = None,
              aux: dict | None = None, bar_seconds: int = 60,
              write_reports: bool = True) -> dict[str, Any]:
    data = ({"symbol": symbol, "bars": bars, **(aux or {})} if bars is not None
            else load_dataset(symbol, bar_seconds))
    bars_ = data.get("bars") or []
    summary: dict[str, Any] = {"tool_version": TOOL_VERSION, "symbol": symbol,
                               "ran_at": _now_iso(), "n_bars": len(bars_),
                               **_safety()}
    if len(bars_) < 3 * MIN_SAMPLE:
        summary["verdict"] = "NEEDS_MORE_DATA"
        summary["note"] = f"only {len(bars_)} bars; keep collecting"
        return summary
    features = build_features(bars_, data.get("oi"), data.get("funding"),
                              data.get("orderbook"), data.get("liquidations"))
    labels = build_labels(bars_, side="long")
    labels_short = build_labels(bars_, side="short")      # REAL side-aware short
    assert_no_lookahead(features, labels, bars_)
    missing = sum(1 for l in labels if l.get("missing"))
    summary["data_quality"] = {"bars": len(bars_), "missing_labels": missing}
    candidates = discover_candidates(features, labels, symbol, bars=bars_,
                                     labels_short=labels_short)
    inc = CandidateIncubator()
    wf_reports = {}
    shadow_log = []
    for cand in candidates[:20]:
        status = "DISCOVERED"
        if cand["verdict"] == "PROMISING_RESEARCH_ONLY":
            feat = cand["setup_name"].split(">")[0]
            wf = walk_forward(features, labels, feat, cand["side"], cand["threshold"],
                              labels_short=labels_short)
            wf_reports[cand["candidate_id"]] = wf
            status = ("SHADOW_ELIGIBLE" if wf.get("verdict") == "OOS_PASS_RESEARCH_ONLY"
                      else "INCUBATING")
        elif cand["verdict"].startswith("REJECTED"):
            status = "REJECTED"
        inc.upsert(cand, status)
        if status == "SHADOW_ELIGIBLE":
            shadow_log.append(shadow_decide(cand, features[-1]))
    gate_reports = [paper_promotion_gate(c, wf_reports.get(c["candidate_id"]))
                    for c in inc.rank()[:5]]
    # drift: recent vs reference outcomes (population level)
    outs = [l["cost_adjusted_outcome"] for l in labels
            if not l.get("missing") and l.get("cost_adjusted_outcome") is not None]
    drift = drift_check(outs[-50:], outs[:max(10, len(outs) - 50)])
    ranked = inc.rank()
    summary.update({
        "candidates_total": len(candidates),
        "promising": sum(1 for c in candidates
                         if c["verdict"] == "PROMISING_RESEARCH_ONLY"),
        "rejected": sum(1 for c in candidates if c["verdict"].startswith("REJECTED")),
        "shadow_eligible": sum(1 for c in ranked if c["status"] == "SHADOW_ELIGIBLE"),
        "top_candidates": [{k: c.get(k) for k in
                            ("candidate_id", "setup_name", "side", "net_EV",
                             "net_EV_lower_bound", "sample_size", "verdict",
                             "status")} for c in ranked[:5]],
        "drift": {"signals": drift["signals"], "action": drift["action"]},
        "paper_gate": "BLOCKED (human approval not encodable)",
        "future_micro_live": future_micro_live_scaffold()["scaffold"],
        "methodology": {
            "threshold_source": "train_only",
            "bar_time_semantics": "bar_close_available",
            "feature_available_at_contract": "after_source_available",
            "short_label_method": "real_side_aware",
            "guards_active": ["DATA_SNOOPING_GUARD_ACTIVE",
                              "BAR_AVAILABLE_AT_GUARD_ACTIVE"]},
        "blockers": ["edge_validated=false", "paper_filter_enabled=false",
                     "human_approval_required"],
        "next_data_needed": "more forward coverage (orderbook/liquidations days)",
        "verdict": ("CANDIDATES_UNDER_RESEARCH" if candidates
                    else "NO_CANDIDATES")})
    if write_reports:
        out_dir = _repo_root().joinpath(*OUTPUT_SUBDIR)
        out_dir.mkdir(parents=True, exist_ok=True)

        def wjson(name, obj):
            p = out_dir / name
            tmp = out_dir / (name + ".tmp")
            tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, p)

        wjson("continuous_edge_summary_v1038.json", summary)
        wjson("walk_forward_report_v1038.json", wf_reports)
        wjson("drift_report_v1038.json", drift)
        wjson("promotion_gate_report_v1038.json", gate_reports)
        with open(out_dir / "candidate_rankings_v1038.csv", "w", newline="",
                  encoding="utf-8") as f:
            cols = ["candidate_id", "setup_name", "side", "status", "verdict",
                    "sample_size", "net_EV", "net_EV_lower_bound", "max_drawdown",
                    "turnover"]
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for c in ranked:
                w.writerow({k: c.get(k) for k in cols})
        with open(out_dir / "shadow_policy_metrics_v1038.csv", "w", newline="",
                  encoding="utf-8") as f:
            cols = ["timestamp", "symbol", "candidate_id", "would_enter", "side",
                    "confidence", "expected_net_EV", "abstain_reason", "regime",
                    "kind"]
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for s in shadow_log:
                w.writerow({k: s.get(k) for k in cols})
        summary["reports_dir"] = str(out_dir).replace("\\", "/")
    return summary
