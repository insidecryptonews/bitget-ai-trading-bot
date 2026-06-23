"""ResearchOps V10.23 - Intraday Equity->Crypto Lead-Lag Study (research only).

Tests the user's hypothesis at INTRADAY resolution: "when NVDA/QQQ/tech sell off
hard during the US session, crypto reacts hours later -> detect risk-off early".
V10.20 found no LEADING signal at daily resolution; this is the intraday version.

Pipeline: public Yahoo intraday GET (allowlisted, staging-only) -> strict
no-lookahead alignment (equity bar usable only AFTER it closes; crypto label
window strictly in the future) -> explainable features -> event study ->
rule-based risk_off score -> baselines (BTC/QQQ/NVDA/VIX-only, random,
always-risk-off) -> IS/OOS split -> classification.

NO orders, NO live, NO paper, NO keys, NO private endpoints, NO raw/DB writes.
FINAL_RECOMMENDATION: NO LIVE.
"""

from __future__ import annotations

import csv
import json
import os
import random
import statistics as st
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from . import FINAL_RECOMMENDATION_NO_LIVE

TOOL_VERSION = "v10.23"
STAGING_MARKER = "intraday_leadlag_v10_23"
OUTPUT_ROOT = "reports/research/v10_23"
HOUR = 3600

# Yahoo public chart endpoint allowlist (GET only, no auth)
_ALLOWED_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
_ALLOWED_PATH_PREFIX = "/v8/finance/chart/"

_FORBIDDEN_SEG = ("raw", "backup", "backups", "vault", "vaults", "training_exports",
                  "secret", "secrets", "credential", "credentials", "db", "database",
                  "codex_result.md", "code_result.md")
_FORBIDDEN_SUF = (".env", ".db", ".sqlite", ".sqlite3", ".zip", ".tar", ".gz", ".pem", ".key")

# classification verdicts
C_REJECTED = "REJECTED_NO_PREDICTIVE_POWER"
C_BTC_BETTER = "REJECTED_BTC_ONLY_BETTER"
C_WEAK = "WEAK_INTRADAY_LEADLAG_SIGNAL"
C_CANDIDATE = "RESEARCH_CANDIDATE_NEEDS_MORE_INTRADAY_HISTORY"
C_DATA_LIMITED = "DATA_SOURCE_LIMITED_60D"
C_NO_LOOKAHEAD_FAIL = "NO_LOOKAHEAD_FAIL"


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "paper_candidate_future": False,
            "makes_no_trades": True, "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------
# Network safety (mirrors the V10.15/V10.7 public-GET-only pattern)
# --------------------------------------------------------------------------

def assert_safe_request(url: str, headers: dict[str, str] | None) -> None:
    p = urllib.parse.urlparse(url)
    if p.scheme != "https":
        raise ValueError(f"non-https blocked: {url}")
    if p.hostname not in _ALLOWED_HOSTS:
        raise ValueError(f"host not allowlisted: {p.hostname}")
    if not p.path.startswith(_ALLOWED_PATH_PREFIX):
        raise ValueError(f"path not allowlisted: {p.path}")
    for k in (headers or {}):
        if k.lower() in ("authorization", "cookie", "x-api-key", "apikey", "api-key", "token"):
            raise ValueError(f"auth header blocked: {k}")


def default_transport(url: str, headers: dict[str, str]) -> bytes:
    assert_safe_request(url, headers)
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


def _yahoo_url(symbol: str, interval: str, rng: str) -> str:
    return (f"https://query1.finance.yahoo.com{_ALLOWED_PATH_PREFIX}"
            f"{urllib.parse.quote(symbol)}?range={rng}&interval={interval}&includePrePost=false")


def parse_chart(raw: bytes) -> list[dict[str, float]]:
    o = json.loads(raw)
    res = (o.get("chart", {}).get("result") or [None])[0]
    if not res:
        return []
    tss = res.get("timestamp") or []
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    closes = q.get("close") or []
    out = []
    for i, t in enumerate(tss):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        out.append({"ts": int(t), "close": float(c)})
    out.sort(key=lambda x: x["ts"])
    return out


def fetch_series(symbol: str, interval: str, days: int,
                 transport: Callable[[str, dict[str, str]], bytes] | None = None) -> list[dict[str, float]]:
    rng = "60d" if days >= 45 else ("30d" if days >= 20 else "5d")
    if interval == "1h":
        rng = "60d" if days >= 45 else rng
    tr = transport or default_transport
    raw = tr(_yahoo_url(symbol, interval, rng), {"User-Agent": "researchops/1.0", "Accept": "application/json"})
    return parse_chart(raw)


# --------------------------------------------------------------------------
# Staging safety
# --------------------------------------------------------------------------

def safe_staging_dir(base: str | None = None) -> str:
    root = base or f"external_data/staging/{STAGING_MARKER}"
    segs = [s for s in root.replace("\\", "/").split("/") if s]
    if ".." in segs or (len(segs) and (segs[0].endswith(":") or root.startswith("/"))):
        raise ValueError("unsafe staging path")
    for s in (x.lower() for x in segs):
        if s in _FORBIDDEN_SEG or s.endswith(_FORBIDDEN_SUF) or ".env" in s:
            raise ValueError(f"forbidden staging segment: {s}")
    if STAGING_MARKER not in segs:
        raise ValueError("staging dir must live under the v10_23 marker")
    return root


def _safe_output_base(output_dir: str | None) -> str:
    base = output_dir or OUTPUT_ROOT
    segs = [s for s in base.replace("\\", "/").split("/") if s]
    if ".." in segs:
        return OUTPUT_ROOT
    for s in (x.lower() for x in segs):
        if s in _FORBIDDEN_SEG or s.endswith(_FORBIDDEN_SUF) or ".env" in s:
            return OUTPUT_ROOT
    return base


# --------------------------------------------------------------------------
# Plan / fetch
# --------------------------------------------------------------------------

def intraday_leadlag_plan(equities: list[str], cryptos: list[str], timeframes: list[str],
                          days: int) -> dict[str, Any]:
    return {
        "tool_version": TOOL_VERSION,
        "objective": "intraday equity->crypto lead-lag study; detect risk-off early; NO trades",
        "equities": equities, "cryptos": cryptos, "timeframes": timeframes, "days": days,
        "source": "Yahoo public chart GET (allowlisted host/path, no auth, staging-only)",
        "limits": "Yahoo intraday ~60d for equities -> expect DATA_SOURCE_LIMITED_60D / LOW_SAMPLE_WARNING",
        "no_lookahead_rules": ["equity bar usable only after close (open+interval<=decision_ts)",
                               "crypto label window strictly after decision_ts",
                               "features_max_ts<=decision_ts<label_start_ts"],
        "never": ["place_order", "create_order", "set_leverage", "private_get", "private_post",
                  "raw_write", "db_write", "paid_download", "APPROVED_FOR_PAPER", "APPROVED_FOR_LIVE"],
        "writes_network_on_plan": False, **_safety()}


def intraday_leadlag_fetch(equities: list[str], cryptos: list[str], timeframes: list[str],
                           days: int, apply: bool = False, output_dir: str | None = None,
                           transport: Callable[[str, dict[str, str]], bytes] | None = None) -> dict[str, Any]:
    rep: dict[str, Any] = {"tool_version": TOOL_VERSION, "apply": bool(apply), "days": days,
                           "timeframes": timeframes, "downloaded": [], "failed": [],
                           "run_id": _now_stamp(), **_safety()}
    if not apply:
        rep["mode"] = "DRY_RUN"
        rep["would_fetch"] = [f"{s}:{tf}" for s in (equities + cryptos) for tf in timeframes]
        rep["writes"] = False
        return rep
    rep["mode"] = "APPLY"
    staging = safe_staging_dir(None)
    run_dir = os.path.join(staging, rep["run_id"])
    os.makedirs(run_dir, exist_ok=True)
    rep["staging_dir"] = run_dir.replace("\\", "/")
    for kind, syms in (("equity", equities), ("crypto", cryptos)):
        for s in syms:
            for tf in timeframes:
                try:
                    rows = fetch_series(s, tf, days, transport=transport)
                    if not rows:
                        rep["failed"].append({"symbol": s, "tf": tf, "reason": "empty"})
                        continue
                    fn = f"{s.replace('^','_idx_')}_{tf}.csv"
                    with open(os.path.join(run_dir, fn), "w", newline="", encoding="utf-8") as f:
                        w = csv.writer(f); w.writerow(["ts", "close"])
                        for r in rows:
                            w.writerow([r["ts"], r["close"]])
                    rep["downloaded"].append({"symbol": s, "tf": tf, "kind": kind, "rows": len(rows),
                                              "first_ts": rows[0]["ts"], "last_ts": rows[-1]["ts"]})
                except Exception as e:
                    rep["failed"].append({"symbol": s, "tf": tf, "reason": f"{type(e).__name__}:{str(e)[:60]}"})
    return rep


# --------------------------------------------------------------------------
# No-lookahead alignment (the crux)
# --------------------------------------------------------------------------

def _ret_n_closed(series: dict[int, float], decision_ts: int, interval: int, n: int) -> tuple[float, int] | None:
    """Return (return over last n CLOSED bars before decision_ts, last_close_ts).
    A bar with open ts=o has closed at o+interval; it is usable iff o+interval<=decision_ts."""
    keys = sorted(series)
    closed = [k for k in keys if k + interval <= decision_ts]
    if len(closed) < n + 1:
        return None
    last_open = closed[-1]
    prev_open = closed[-1 - n]
    a, b = series[prev_open], series[last_open]
    if a <= 0:
        return None
    return (b / a - 1.0, last_open + interval)


def align_no_lookahead(crypto: dict[str, dict[int, float]], equity: dict[str, dict[int, float]],
                       interval: int, horizons: tuple[int, ...] = (1, 2, 4, 8),
                       max_staleness_h: int = 2) -> dict[str, Any]:
    """Build decision rows on the crypto hourly grid. Features come ONLY from
    equity bars closed at or before decision_ts; labels come ONLY from crypto
    bars strictly after decision_ts."""
    btc = crypto.get("BTC-USD") or {}
    grid = sorted(btc)
    rows: list[dict[str, Any]] = []
    feat_max_ts_global, label_min_start_global = [], []
    for t in grid:
        # need future for max horizon
        if (t + max(horizons) * interval) not in btc:
            continue
        # equity features: most-recent-closed bar; require freshness (during/just after session)
        eq_feats: dict[str, float] = {}
        eq_close_ts: list[int] = []
        fresh = False
        for sym, ser in equity.items():
            r1 = _ret_n_closed(ser, t, interval, 1)
            if r1 is None:
                continue
            eq_feats[f"{sym}_ret1"] = r1[0]
            eq_close_ts.append(r1[1])
            if (t - r1[1]) <= max_staleness_h * interval:
                fresh = True
            r2 = _ret_n_closed(ser, t, interval, 2)
            if r2 is not None:
                eq_feats[f"{sym}_ret2"] = r2[0]
        if not eq_feats or not fresh:
            continue
        # crypto labels (future only)
        labels: dict[str, float] = {}
        ok_future = True
        for cs, ser in crypto.items():
            base = ser.get(t)
            if base is None or base <= 0:
                ok_future = False
                break
            for h in horizons:
                fv = ser.get(t + h * interval)
                if fv is None:
                    ok_future = False
                    break
                labels[f"{cs}_ret{h}h"] = fv / base - 1.0
            if not ok_future:
                break
        if not ok_future:
            continue
        feat_max_ts = max(eq_close_ts)
        label_start = t + interval
        feat_max_ts_global.append(feat_max_ts)
        label_min_start_global.append(label_start)
        rows.append({"decision_ts": t, "feat_max_ts": feat_max_ts, "label_start_ts": label_start,
                     **eq_feats, **labels})
    # no-lookahead audit
    ok = all(r["feat_max_ts"] <= r["decision_ts"] < r["label_start_ts"] for r in rows)
    status = "OK" if (ok and rows) else ("NO_DATA" if not rows else "NO_LOOKAHEAD_FAIL")
    return {"rows": rows, "no_lookahead_status": status,
            "features_max_ts_le_decision": ok, "equity_bar_close_aligned": True,
            "n_rows": len(rows)}


# --------------------------------------------------------------------------
# Event study + score + baselines
# --------------------------------------------------------------------------

def _mean(xs: list[float]) -> float:
    return st.mean(xs) if xs else 0.0


def event_study(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """After an equity shock (NVDA -1% / QQQ -0.7% / SMH -0.8% over last 1h),
    what does crypto do next? Compare to non-shock baseline."""
    def shock(r):
        return (r.get("NVDA_ret1", 0) <= -0.01 or r.get("QQQ_ret1", 0) <= -0.007
                or r.get("SMH_ret1", 0) <= -0.008)
    sh = [r for r in rows if shock(r)]
    no = [r for r in rows if not shock(r)]
    out = {"n_shock": len(sh), "n_no_shock": len(no), "horizons": {}}
    for h in (1, 2, 4, 8):
        k = f"BTC-USD_ret{h}h"
        ek = f"ETH-USD_ret{h}h"
        out["horizons"][f"{h}h"] = {
            "btc_after_shock": round(_mean([r[k] for r in sh if k in r]), 5),
            "btc_no_shock": round(_mean([r[k] for r in no if k in r]), 5),
            "eth_after_shock": round(_mean([r[ek] for r in sh if ek in r]), 5),
            "eth_no_shock": round(_mean([r[ek] for r in no if ek in r]), 5)}
    return out


def risk_off_score(r: dict[str, Any]) -> int:
    s = 0
    s += 25 if r.get("NVDA_ret1", 0) <= -0.01 else 0
    s += 20 if r.get("QQQ_ret1", 0) <= -0.007 else 0
    s += 20 if r.get("SMH_ret1", 0) <= -0.008 else 0
    s += 15 if r.get("SPY_ret1", 0) <= -0.005 else 0
    s += 20 if r.get("_idx_VIX_ret1", 0) >= 0.02 else 0
    return s


def _label_drawdown(r: dict[str, Any], sym: str = "BTC-USD", h: int = 4, thr: float = -0.02) -> bool:
    return r.get(f"{sym}_ret{h}h", 0.0) <= thr


def _prec_recall(rows, pred, label):
    tp = sum(1 for r in rows if pred(r) and label(r))
    fp = sum(1 for r in rows if pred(r) and not label(r))
    fn = sum(1 for r in rows if (not pred(r)) and label(r))
    p = tp / (tp + fp) if (tp + fp) else 0.0
    rc = tp / (tp + fn) if (tp + fn) else 0.0
    return {"precision": round(p, 4), "recall": round(rc, 4), "flags": tp + fp, "tp": tp}


def evaluate(rows: list[dict[str, Any]], label_h: int = 4, label_thr: float = -0.02,
             score_thr: int = 45, seed: int = 7) -> dict[str, Any]:
    label = lambda r: _label_drawdown(r, "BTC-USD", label_h, label_thr)
    n = len(rows)
    cut = int(n * 0.7)
    rng = random.Random(seed)
    preds = {
        "risk_off_score>=thr": lambda r: risk_off_score(r) >= score_thr,
        "BTC-only(btc1<0)": lambda r: r.get("BTC-USD_ret1h", 0) < 0,
        "QQQ-only(qqq1<-0.7%)": lambda r: r.get("QQQ_ret1", 0) <= -0.007,
        "NVDA-only(nvda1<-1%)": lambda r: r.get("NVDA_ret1", 0) <= -0.01,
        "VIX-only(vix1>=2%)": lambda r: r.get("_idx_VIX_ret1", 0) >= 0.02,
        "always_riskoff_after_red_eq": lambda r: any(r.get(f"{s}_ret1", 0) < 0 for s in ("NVDA", "QQQ", "SPY", "SMH")),
        "random_same_freq": lambda r: rng.random() < 0.0,  # set below
    }
    # calibrate random to the score's flag frequency on IS
    is_rows, oos_rows = rows[:cut], rows[cut:]
    base_is = (sum(1 for r in is_rows if label(r)) / len(is_rows)) if is_rows else 0.0
    base_oos = (sum(1 for r in oos_rows if label(r)) / len(oos_rows)) if oos_rows else 0.0
    score_freq = (sum(1 for r in rows if risk_off_score(r) >= score_thr) / n) if n else 0.0
    rng2 = random.Random(seed + 1)
    preds["random_same_freq"] = lambda r, _rng=rng2, _f=score_freq: _rng.random() < _f
    out = {"n": n, "is_n": len(is_rows), "oos_n": len(oos_rows),
           "label": f"BTC-USD drawdown<= {label_thr*100:.0f}% next {label_h}h",
           "base_rate_is": round(base_is, 4), "base_rate_oos": round(base_oos, 4),
           "predictors": {}}
    for name, fn in preds.items():
        pis = _prec_recall(is_rows, fn, label)
        pos = _prec_recall(oos_rows, fn, label)
        pis["lift"] = round(pis["precision"] / base_is, 3) if base_is else 0.0
        pos["lift"] = round(pos["precision"] / base_oos, 3) if base_oos else 0.0
        out["predictors"][name] = {"IS": pis, "OOS": pos}
    return out


def _read_csv_series(path: str) -> dict[int, float]:
    out: dict[int, float] = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    out[int(float(r["ts"]))] = float(r["close"])
                except (TypeError, ValueError, KeyError):
                    continue
    except Exception:
        return {}
    return out


def load_staged(run_dir: str, tf: str, equities: list[str], cryptos: list[str]) -> tuple[dict, dict]:
    crypto, equity = {}, {}
    for s in cryptos:
        p = os.path.join(run_dir, f"{s.replace('^','_idx_')}_{tf}.csv")
        if os.path.isfile(p):
            crypto[s] = _read_csv_series(p)
    for s in equities:
        p = os.path.join(run_dir, f"{s.replace('^','_idx_')}_{tf}.csv")
        if os.path.isfile(p):
            equity[s.replace("^", "_idx_")] = _read_csv_series(p)
    return crypto, equity


def run_study(crypto: dict[str, dict[int, float]], equity: dict[str, dict[int, float]],
              interval: int, days: int) -> dict[str, Any]:
    aligned = align_no_lookahead(crypto, equity, interval)
    rows = aligned["rows"]
    rep: dict[str, Any] = {"tool_version": TOOL_VERSION, "generated_at": _now_stamp(),
                           "n_decision_rows": aligned["n_rows"],
                           "no_lookahead": {"status": aligned["no_lookahead_status"],
                                            "features_max_ts_le_decision": aligned["features_max_ts_le_decision"],
                                            "equity_bar_close_aligned": aligned["equity_bar_close_aligned"],
                                            "label_start_after_decision": True},
                           "equities": sorted(equity), "cryptos": sorted(crypto),
                           "interval_seconds": interval, "days": days, **_safety()}
    if aligned["no_lookahead_status"] != "OK" or not rows:
        rep["event_study"] = {}
        rep["evaluation"] = {}
        rep["classification"] = classify({"predictors": {}}, aligned["n_rows"],
                                         aligned["no_lookahead_status"], days)
        return rep
    rep["event_study"] = event_study(rows)
    rep["evaluation"] = evaluate(rows)
    rep["classification"] = classify(rep["evaluation"], aligned["n_rows"],
                                     aligned["no_lookahead_status"], days)
    return rep


def write_reports(rep: dict[str, Any], output_dir: str | None = None) -> dict[str, str]:
    base = _safe_output_base(output_dir)
    os.makedirs(base, exist_ok=True)
    paths = {}
    sc = os.path.join(base, "intraday_leadlag_scorecard.json").replace("\\", "/")
    with open(sc, "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2, default=str)
    paths["scorecard"] = sc
    ev = rep.get("event_study", {}).get("horizons", {})
    es = os.path.join(base, "intraday_leadlag_event_study.md").replace("\\", "/")
    lines = ["# V10.23 Intraday Lead-Lag Event Study (research only)", "",
             f"decision_rows: {rep.get('n_decision_rows')}  no_lookahead: {rep['no_lookahead']['status']}",
             "", "## BTC after equity shock vs no-shock (mean future return)"]
    for h, d in ev.items():
        lines.append(f"- {h}: BTC shock={d['btc_after_shock']} no_shock={d['btc_no_shock']} | "
                     f"ETH shock={d['eth_after_shock']} no_shock={d['eth_no_shock']}")
    lines += ["", "final_recommendation: NO LIVE"]
    with open(es, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    paths["event_study"] = es
    return paths


def classify(evald: dict[str, Any], n_rows: int, no_lookahead: str, days: int) -> dict[str, Any]:
    reasons = []
    if no_lookahead != "OK":
        return {"verdict": C_NO_LOOKAHEAD_FAIL, "reasons": ["alignment failed"], **_safety()}
    score = evald["predictors"].get("risk_off_score>=thr", {})
    btc = evald["predictors"].get("BTC-only(btc1<0)", {})
    s_oos = score.get("OOS", {})
    b_oos = btc.get("OOS", {})
    lift = s_oos.get("lift", 0.0)
    beats_btc = s_oos.get("precision", 0) > b_oos.get("precision", 0)
    low_sample = days <= 60 or n_rows < 400
    if lift > 1.3 and beats_btc and s_oos.get("flags", 0) >= 10:
        verdict = C_CANDIDATE if low_sample else C_WEAK
        reasons.append(f"OOS lift {lift} >1.3 and beats BTC-only")
    elif lift > 1.0 and beats_btc:
        verdict = C_WEAK
        reasons.append(f"OOS lift {lift} modest")
    elif not beats_btc:
        verdict = C_BTC_BETTER
        reasons.append("BTC-only OOS precision >= cross-asset score")
    else:
        verdict = C_REJECTED
        reasons.append(f"OOS lift {lift} <=1.0")
    if low_sample:
        reasons.append("LOW_SAMPLE_WARNING / DATA_SOURCE_LIMITED_60D")
    return {"verdict": verdict, "secondary": C_DATA_LIMITED if low_sample else None,
            "reasons": reasons, "low_sample_warning": low_sample, **_safety()}
