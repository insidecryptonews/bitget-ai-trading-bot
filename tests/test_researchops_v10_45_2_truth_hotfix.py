"""V10.45.2 truth hotfix: reproduces every Codex adversarial finding and proves
it fixed. Pagination, strict gaps, strict resampling, censoring out of EV,
fail-closed gate, path traversal, strict compiler, semantic dedup, Retry-After,
budget-per-attempt, provenance ledger. Research only, NO LIVE."""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

import pytest

from app.labs import ai_providers_v10_45_1 as P
from app.labs import edge_discovery_engine_v10_45_1 as ENG
from app.labs import public_data_backfill_v10_45_1 as BF

T0 = 1_700_000_100_000          # aligned to 5m/15m boundaries (mult of 300000)
BAR = 60_000


def _spec(**kw):
    s = {"strategy_id": "t", "origin": "test", "side": "LONG",
         "regime_filter": "ANY",
         "entry_conditions": [{"feature": "rsi_14", "op": "<", "value": 35.0}],
         "stop_policy": {"type": "fixed", "value": 0.006},
         "take_profit_policy": {"type": "fixed", "value": 0.006},
         "trailing_policy": {"type": "none", "value": 0.0},
         "time_exit": 30, "cooldown": 5}
    s.update(kw)
    return s


def _bars(n=600, seed=3, t0=T0):
    rng = random.Random(seed)
    price, out = 100.0, []
    for i in range(n):
        ch = rng.uniform(-0.002, 0.002)
        new = price * (1 + ch)
        out.append({"ts": t0 + i * BAR, "available_at": t0 + i * BAR + BAR,
                    "open": price, "high": max(price, new) * 1.001,
                    "low": min(price, new) * 0.999, "close": new,
                    "volume": 10.0, "turnover": 1000.0,
                    "symbol": "BTCUSDT", "venue": "bitget"})
        price = new
    return out


# ==========================================================================
# 1. PAGINATION: no candle lost, none duplicated, exact continuity
# ==========================================================================

def _synthetic_pages(n_bars: int, page_size: int, semantics: str):
    """Simulate a venue serving `n_bars` 1m candles via endTime pagination.
    semantics: 'open_inclusive' (open<=end), 'open_exclusive' (open<end) or
    'close_le_end' (close<=end — the REAL Bitget behaviour, probed live)."""
    all_ts = [T0 + i * BAR for i in range(n_bars)]

    def fetch_page(end_ms: int):
        if semantics == "open_inclusive":
            elig = [t for t in all_ts if t <= end_ms]
        elif semantics == "open_exclusive":
            elig = [t for t in all_ts if t < end_ms]
        else:                                    # close_le_end (Bitget real)
            elig = [t for t in all_ts if t + BAR <= end_ms]
        page = sorted(elig)[-page_size:]
        return [[t, 1.0, 2.0, 0.5, 1.5, 10.0, 100.0] for t in page]
    return fetch_page, all_ts


@pytest.mark.parametrize("semantics", ["open_inclusive", "open_exclusive",
                                       "close_le_end"])
@pytest.mark.parametrize("n_bars,page_size", [(1000, 200), (997, 200), (401, 100)])
def test_pagination_loses_no_candle(semantics, n_bars, page_size):
    fetch, all_ts = _synthetic_pages(n_bars, page_size, semantics)
    rows = BF.paginate_klines(fetch, end_ms=all_ts[-1] + BAR,
                              target_start_ms=all_ts[0] - 1,
                              max_requests=50, log=lambda *a: None)
    got = [r[0] for r in rows]
    assert len(got) == n_bars                    # exact expected count
    assert got == all_ts                         # chronological + continuous
    assert len(set(got)) == len(got)             # zero duplicates
    q = BF.strict_quality(got)
    assert q["gap_count"] == 0 and q["duplicates"] == 0
    assert q["quality_pass"] is True


def test_v10451_pagination_bug_is_reproduced_then_fixed():
    """The old `end = batch_min - BAR` dropped exactly one candle per page on
    the close<=endTime venue (real Bitget). Prove the fixed paginator does not."""
    fetch, all_ts = _synthetic_pages(600, 200, semantics="close_le_end")
    # old (buggy) walk
    rows_old: dict[int, bool] = {}
    end = all_ts[-1] + BAR
    for _ in range(10):
        page = fetch(end)
        if not page:
            break
        for r in page:
            rows_old[r[0]] = True
        end = min(r[0] for r in page) - BAR      # the V10.45.1 bug
    missing_old = len(all_ts) - len(rows_old)
    assert missing_old >= 2                      # bug visibly lost candles
    # fixed walk
    rows_new = BF.paginate_klines(fetch, all_ts[-1] + BAR, all_ts[0] - 1, 50,
                                  log=lambda *a: None)
    assert len(rows_new) == len(all_ts)          # fixed: nothing lost


# ==========================================================================
# 2. STRICT GAPS: only delta == T is continuous
# ==========================================================================

def test_two_minute_step_on_1m_data_is_a_gap():
    ts = [T0, T0 + BAR, T0 + 3 * BAR, T0 + 4 * BAR]      # 2-min jump inside
    q = BF.strict_quality(ts)
    assert q["gap_count"] == 1 and q["missing_bars"] == 1
    assert q["quality_pass"] is False


def test_duplicates_out_of_order_and_irregular_detected():
    q = BF.strict_quality([T0, T0, T0 + BAR])
    assert q["duplicates"] == 1 and not q["quality_pass"]
    q2 = BF.strict_quality([T0 + BAR, T0, T0 + 2 * BAR])
    assert q2["out_of_order"] == 1 and not q2["quality_pass"]
    q3 = BF.strict_quality([T0, T0 + BAR + 7, T0 + 2 * BAR])
    assert q3["irregular_deltas"] >= 1 and not q3["quality_pass"]


def test_dataset_quality_flags_invalid_ohlc():
    bars = _bars(50)
    bars[10]["low"] = bars[10]["high"] + 1        # impossible bar
    q = ENG.dataset_quality(bars, bar_ms=BAR)
    assert q["invalid_ohlc"] == 1 and q["quality_pass"] is False


# ==========================================================================
# 4. STRICT RESAMPLING: 4/5, 14/15, non-consecutive -> rejected
# ==========================================================================

def test_resample_rejects_incomplete_4_of_5():
    bars = _bars(25)
    del bars[7]                                   # bucket 1 now has 4/5 bars
    r5 = ENG.resample_bars(bars, 5)
    starts = [b["ts"] for b in r5]
    assert T0 + 5 * BAR not in starts             # incomplete bucket rejected
    assert T0 in starts                           # complete bucket kept


def test_resample_rejects_incomplete_14_of_15():
    bars = _bars(60)
    del bars[20]                                  # second 15m bucket -> 14/15
    r15 = ENG.resample_bars(bars, 15)
    starts = [b["ts"] for b in r15]
    assert T0 + 15 * BAR not in starts
    assert T0 in starts


def test_resample_rejects_non_consecutive_and_duplicate_buckets():
    bars = _bars(25)
    bars[6]["ts"] = bars[5]["ts"]                 # duplicate ts inside bucket 1
    r5 = ENG.resample_bars(bars, 5)
    assert T0 + 5 * BAR not in [b["ts"] for b in r5]


def test_resample_drops_open_trailing_bucket():
    bars = _bars(23)                              # last bucket only 3/5 bars
    r5 = ENG.resample_bars(bars, 5)
    assert len(r5) == 4                           # 4 complete buckets only
    assert r5[-1]["ts"] == T0 + 15 * BAR


# ==========================================================================
# 5. CENSORED / STALE never enter EV, PF, win-rate
# ==========================================================================

def test_censored_and_stale_excluded_from_metrics():
    trades = [
        {"net_return": 0.01, "exit_reason": "TP", "bars_held": 5,
         "entry_i": 1, "exit_i": 6, "censored": False},
        {"net_return": -0.005, "exit_reason": "SL", "bars_held": 3,
         "entry_i": 10, "exit_i": 13, "censored": False},
        {"net_return": 0.5, "exit_reason": "END_CENSORED", "bars_held": 2,
         "entry_i": 20, "exit_i": 22, "censored": True},   # huge fake win
        {"net_return": 0.4, "exit_reason": "STALE_EXIT", "bars_held": 2,
         "entry_i": 30, "exit_i": 32, "censored": False},  # unexecutable exit
    ]
    m = ENG.metrics(trades)
    assert m["n_trades"] == 2                     # only TP + SL count
    assert m["censored"] == 1 and m["invalid_execution"] == 1
    assert m["censored_ratio"] == 0.5
    assert abs(m["net_EV"] - 0.0025) < 1e-9       # (0.01 - 0.005) / 2
    assert m["win_rate"] == 0.5


def test_excessive_censoring_blocks_promotion():
    val = {"n_trades": 40, "n_eff": 40, "net_EV": 0.002,
           "net_EV_lower_bound": 0.001, "profit_factor": 2.0,
           "max_drawdown": -0.02, "censored_ratio": 0.5,   # 50% censored
           "outlier_dependence": 0.001, "stability_sign": 1}
    g = ENG.gate(val, None, True, data_quality_pass=True)
    assert g == "NEED_MORE_DATA"


# ==========================================================================
# 6. FAIL-CLOSED GATE: the exact Codex adversarial cases
# ==========================================================================

def _good_val():
    return {"n_trades": 50, "n_eff": 50, "net_EV": 0.002,
            "net_EV_lower_bound": 0.001, "profit_factor": 1.6,
            "max_drawdown": -0.03, "censored_ratio": 0.0,
            "outlier_dependence": 0.001, "stability_sign": 1}


def _good_hold():
    return {"n_trades": 20, "net_EV": 0.002, "net_EV_lower_bound": 0.0008,
            "profit_factor": 1.5, "censored_ratio": 0.0}


def test_gate_pf_02_dd_90pct_rejected():
    val = {**_good_val(), "profit_factor": 0.2, "max_drawdown": -0.90}
    assert ENG.gate(val, _good_hold(), True, data_quality_pass=True) == "REJECTED"


def test_gate_holdout_zero_trades_never_promotes():
    hold = {"n_trades": 0, "net_EV": None, "net_EV_lower_bound": None,
            "profit_factor": None, "censored_ratio": 0.0}
    g = ENG.gate(_good_val(), hold, True, data_quality_pass=True)
    assert g == "NEED_MORE_DATA"


def test_gate_baseline_superior_rejects():
    g = ENG.gate(_good_val(), _good_hold(), True, data_quality_pass=True,
                 baseline_best_lb=0.005)          # baseline beats candidate lb
    assert g == "REJECTED"


def test_gate_negative_stress_rejects():
    assert ENG.gate(_good_val(), _good_hold(), False,
                    data_quality_pass=True) == "REJECTED"


def test_gate_gappy_data_is_invalid_data():
    assert ENG.gate(_good_val(), _good_hold(), True,
                    data_quality_pass=False) == "INVALID_DATA"


def test_gate_all_pass_caps_at_shadow_while_proxies_exist():
    g = ENG.gate(_good_val(), _good_hold(), True, data_quality_pass=True,
                 baseline_best_lb=-0.001)
    assert g == "SHADOW_CANDIDATE_RESEARCH_ONLY"   # proxies cap below PAPER
    g2 = ENG.gate(_good_val(), _good_hold(), True, data_quality_pass=True,
                  baseline_best_lb=-0.001, execution_proxies=())
    assert g2 == "PAPER_CANDIDATE_RESEARCH_ONLY"


# ==========================================================================
# 7. PATH CONTAINMENT
# ==========================================================================

@pytest.mark.parametrize("bad", [
    "../evil", "..\\evil", "BTC/USDT", "BTC\\USDT", "C:ABSOLUTE",
    "%2E%2E", "%2e%2e", "..", "", "A" * 25, "btcusdt", "CON", "NUL",
    "LPT1", "BTC USDT", "BTC;USDT", "BTC..USDT"])
def test_symbol_whitelist_rejects_traversal(bad):
    with pytest.raises(ValueError):
        BF.validate_symbol(bad)


def test_contained_path_stays_inside_data_dir(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    p = BF._contained_path("bitget", "BTCUSDT", ".csv")
    base = tmp_path.resolve()
    assert base in p.parents
    with pytest.raises(ValueError):
        BF._contained_path("evil_venue", "BTCUSDT", ".csv")


# ==========================================================================
# 8. STRICT COMPILER
# ==========================================================================

def test_compiler_rejects_unknown_fields():
    seen: set[str] = set()
    st, _ = ENG.compile_strategy(_spec(surprise_field=1), seen)
    assert st == "INVALID"
    st2, _ = ENG.compile_strategy(_spec(entry_conditions=[
        {"feature": "rsi_14", "op": "<", "value": 30, "bonus": 1}]), seen)
    assert st2 == "INVALID"
    st3, _ = ENG.compile_strategy(_spec(stop_policy={"type": "fixed",
                                                     "value": 0.006,
                                                     "leverage_x": 10}), seen)
    assert st3 == "INVALID"


def test_compiler_rejects_nan_and_infinity():
    seen: set[str] = set()
    st, _ = ENG.compile_strategy(_spec(entry_conditions=[
        {"feature": "rsi_14", "op": "<", "value": float("nan")}]), seen)
    assert st == "INVALID"
    st2, _ = ENG.compile_strategy(_spec(stop_policy={"type": "fixed",
                                                     "value": float("inf")}), seen)
    assert st2 == "INVALID"


def test_cross_up_only_valid_for_macd_hist():
    seen: set[str] = set()
    st, spec = ENG.compile_strategy(_spec(entry_conditions=[
        {"feature": "macd_hist", "op": "cross_up", "value": 0}]), seen)
    assert st == "OK"
    assert ("macd_cross_up", ">", 0.5) in spec["conditions"]
    st2, _ = ENG.compile_strategy(_spec(entry_conditions=[
        {"feature": "rsi_14", "op": "cross_up", "value": 0}]), seen)
    assert st2 == "INVALID"                        # never silently remapped


# ==========================================================================
# 9. SEMANTIC DEDUP on the compiled spec
# ==========================================================================

def test_different_raw_json_same_compiled_is_duplicate():
    seen: set[str] = set()
    a = _spec(strategy_id="name_one", hypothesis="text A")
    b = _spec(strategy_id="totally_different", hypothesis="other words",
              economic_rationale="different rationale")
    st1, _ = ENG.compile_strategy(a, seen, symbol="BTCUSDT", timeframe="1m")
    st2, _ = ENG.compile_strategy(b, seen, symbol="BTCUSDT", timeframe="1m")
    assert st1 == "OK" and st2 == "DUPLICATE"      # semantics identical


def test_different_cooldown_is_not_duplicate():
    seen: set[str] = set()
    st1, _ = ENG.compile_strategy(_spec(cooldown=5), seen)
    st2, _ = ENG.compile_strategy(_spec(cooldown=10), seen)
    assert st1 == "OK" and st2 == "OK"


def test_different_timeframe_or_symbol_not_duplicate():
    seen: set[str] = set()
    st1, _ = ENG.compile_strategy(_spec(), seen, symbol="BTCUSDT", timeframe="1m")
    st2, _ = ENG.compile_strategy(_spec(), seen, symbol="BTCUSDT", timeframe="5m")
    st3, _ = ENG.compile_strategy(_spec(), seen, symbol="ETHUSDT", timeframe="1m")
    assert (st1, st2, st3) == ("OK", "OK", "OK")


# ==========================================================================
# 13. PROVIDERS: Retry-After variants + budget per attempt
# ==========================================================================

def test_retry_after_numeric_httpdate_invalid_missing():
    assert P.parse_retry_after("7") == 7.0
    assert P.parse_retry_after("0") == 0.0
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone as tz
    future = datetime.now(tz.utc) + timedelta(seconds=30)
    ra = P.parse_retry_after(format_datetime(future))
    assert ra is not None and 25 <= ra <= 31
    assert P.parse_retry_after("banana") is None
    assert P.parse_retry_after(None) is None
    assert P.parse_retry_after("inf") is None


def test_budget_checked_on_every_retry_attempt(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-real")
    calls = {"n": 0}

    def fake_http(url, payload=None, headers=None, timeout=60, method=None):
        calls["n"] += 1
        if url.endswith("/models"):
            return 200, {"data": [{"id": "llama-3.1-8b-instant"}]}, {}
        return 429, {}, {"retry-after": "0"}
    monkeypatch.setattr(P, "_http_json", fake_http)
    monkeypatch.setattr(P.time, "sleep", lambda s: None)
    g = P.GroqProvider(max_requests=2)             # reserve leaves 1 usable
    r = g.generate("x", use_cache=False)
    # first attempt consumes the budget; the retry must be stopped by the
    # budget gate, not fire another request
    assert r["error"] in ("BUDGET_EXHAUSTED", "RATE_LIMITED_429")
    assert g.requests_used <= 2


def test_groq_403_reports_cause_unknown(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-real")

    def fake_http(url, payload=None, headers=None, timeout=60, method=None):
        return 403, None, {}
    monkeypatch.setattr(P, "_http_json", fake_http)
    g = P.GroqProvider()
    assert g.available is False
    assert g.unavailable_reason() == "GROQ_FORBIDDEN_CAUSE_UNKNOWN"


# ==========================================================================
# 12. LEDGER provenance
# ==========================================================================

def test_ledger_entries_carry_run_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(ENG.CE, "_repo_root", lambda: tmp_path)
    ENG.set_run_context(run_id="test_run_1", repo_commit="abc123",
                        dataset_sha256="deadbeef", symbol="BTCUSDT",
                        timeframe="1m", cost_config=dict(ENG.DEFAULT_COSTS))
    ENG.ledger_append({"phase": "compile", "state": "INVALID",
                       "strategy_id": "x"})
    ledger = (tmp_path / "reports" / "research" / "v10_45_2_edge_discovery" /
              "experiment_ledger_v10_45_2.jsonl")
    entry = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    for k in ("run_id", "repo_commit", "dataset_sha256", "symbol",
              "timeframe", "cost_config", "at", "phase", "state"):
        assert k in entry, k
    ENG.set_run_context()                          # clean global state


# ==========================================================================
# 14. TRAIL is a separate exit reason from SL
# ==========================================================================

def test_trailing_exit_reported_as_trail_not_sl():
    bars = _bars(400, seed=15)
    i = 300
    e = bars[i + 1]["open"]
    for k, mult in ((2, 1.006), (3, 1.010), (4, 1.014)):
        bars[i + k]["high"] = e * mult
        bars[i + k]["low"] = e * (mult - 0.002)
        bars[i + k]["close"] = e * (mult - 0.001)
    bars[i + 5]["high"] = e * 1.012
    bars[i + 5]["low"] = e * 1.002                # falls through trail stop
    feats = ENG.build_features(bars)
    seen: set[str] = set()
    _, spec = ENG.compile_strategy(_spec(entry_conditions=[
        {"feature": "ret_1", "op": ">=", "value": -1.0}],
        stop_policy={"type": "fixed", "value": 0.03},
        take_profit_policy={"type": "fixed", "value": 0.05},
        trailing_policy={"type": "fixed", "value": 0.004},
        time_exit=50, cooldown=500), seen)
    r = ENG.replay(bars, feats, spec, i_start=i, i_end=i + 30)
    t = r["trades"][0]
    if t["exit_reason"] in ("SL",):
        pytest.fail("trailing-moved stop must exit as TRAIL, not SL")
    assert t["exit_reason"] in ("TRAIL", "TP", "TIME", "END_CENSORED")
