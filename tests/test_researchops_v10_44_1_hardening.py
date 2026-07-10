"""V10.44.1 hardening: real stale detection, fresh-source recommendation,
honest multiple-testing penalty, watcher perf caches. Research only, NO LIVE."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.labs import alpha_factory_v10_44 as AF
from app.labs import research_dashboard_v10_43c as DASH
from app.labs import ws_continuity_v10_43c as PWS


# --------------------------------------------------------------------------
# Bug 1: heavy-metrics staleness must come from ran_at, not the dashboard JSON
# --------------------------------------------------------------------------

def _iso_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def test_stale_uses_real_ran_at_not_file_mtime(tmp_path: Path):
    # dashboard JSON freshly written (watcher just refreshed) but the heavy runs
    # are 12 hours old -> MUST be stale
    previous = {"strategy_hardening": {"ran_at": _iso_ago(12)},
                "exit_optimization": {"ran_at": _iso_ago(12)}}
    (tmp_path / "dashboard_data_v10_43c.json").write_text("{}", encoding="utf-8")
    meta = DASH._slow_metrics_meta(previous, tmp_path)
    assert meta["strategy_stale"] is True
    assert meta["exit_stale"] is True
    assert meta["strategy_age_seconds"] > 11 * 3600


def test_fresh_ran_at_is_not_stale(tmp_path: Path):
    previous = {"strategy_hardening": {"ran_at": _iso_ago(0.05)},
                "exit_optimization": {"ran_at": _iso_ago(0.05)}}
    meta = DASH._slow_metrics_meta(previous, tmp_path)
    assert meta["strategy_stale"] is False and meta["exit_stale"] is False


def test_missing_ran_at_is_stale_unknown(tmp_path: Path):
    meta = DASH._slow_metrics_meta({}, tmp_path)
    assert meta["strategy_stale"] is True and meta["exit_stale"] is True
    assert meta["strategy_last_updated_at"] == "STALE_UNKNOWN"
    assert meta["exit_last_updated_at"] == "STALE_UNKNOWN"


def test_strategy_and_exit_ages_are_independent(tmp_path: Path):
    previous = {"strategy_hardening": {"ran_at": _iso_ago(20)},
                "exit_optimization": {"ran_at": _iso_ago(0.01)}}
    meta = DASH._slow_metrics_meta(previous, tmp_path)
    assert meta["strategy_stale"] is True
    assert meta["exit_stale"] is False


# --------------------------------------------------------------------------
# Bug 2: a stale (dead-collector) dataset must never win the recommendation
# --------------------------------------------------------------------------

def _mk_bars(n, t0=1_700_000_000_000):
    out = []
    p = 100.0
    for i in range(n):
        out.append({"ts": t0 + i * 60_000, "bar_close_ts": t0 + i * 60_000,
                    "available_at": t0 + i * 60_000, "open": p, "high": p * 1.001,
                    "low": p * 0.999, "close": p, "volume": 10.0,
                    "buy_volume": 5.0, "sell_volume": 5.0, "n_trades": 10,
                    "max_trade": 1.0, "symbol": "BTCUSDT"})
    return out


def test_stale_source_not_recommended_over_fresh(monkeypatch, tmp_path: Path):
    # ws (v10.42) has the longest run but its dataset file is STALE (collector
    # dead); persistent is fresh with a shorter run -> recommend persistent.
    ages = {"rest": 3.0, "ws": 120.0, "ws_persistent": 0.5}
    monkeypatch.setattr(PWS, "_dataset_age_min",
                        lambda path: None)  # replaced below via ages map
    calls = iter(())

    def fake_age(path):
        s = str(path or "")
        if "bybit_microstructure" in s:
            return ages["rest"]
        if "ws_persistent" in s:
            return ages["ws_persistent"]
        return ages["ws"]
    monkeypatch.setattr(PWS, "_dataset_age_min", fake_age)
    c = PWS.dataset_source_compare_3way(
        "BTCUSDT",
        pers_bars=_mk_bars(120), ws_bars=_mk_bars(600), rest_bars=_mk_bars(30))
    assert c["recommended_source"] == "ws_persistent"
    assert c["ws"]["dataset_fresh"] is False
    assert c["ws_persistent"]["dataset_fresh"] is True


def test_recommended_stale_source_gets_blocker(monkeypatch):
    monkeypatch.setattr(PWS, "_dataset_age_min", lambda path: 999.0)  # all stale
    c = PWS.dataset_source_compare_3way(
        "BTCUSDT", pers_bars=_mk_bars(120), ws_bars=_mk_bars(60), rest_bars=_mk_bars(30))
    assert "RECOMMENDED_SOURCE_STALE" in c["blockers"]
    assert c["ready_for_shadow_forward"] is False


def test_preloaded_bars_are_used_not_reread(monkeypatch):
    # if preloaded bars are passed, the compare must NOT read any dataset
    def boom(*a, **k):
        raise AssertionError("should not re-read datasets when preloaded")
    monkeypatch.setattr(PWS, "load_persistent_bars", boom)
    monkeypatch.setattr(PWS.WS, "load_ws_bars", boom)
    monkeypatch.setattr(PWS.CE, "load_dataset", boom)
    monkeypatch.setattr(PWS, "_dataset_age_min", lambda path: 1.0)
    c = PWS.dataset_source_compare_3way(
        "BTCUSDT", pers_bars=_mk_bars(60), ws_bars=_mk_bars(30), rest_bars=_mk_bars(10))
    assert c["recommended_source"] == "ws_persistent"


# --------------------------------------------------------------------------
# Bug 3: the multiple-testing penalty must actually shrink the lower bound
# --------------------------------------------------------------------------

def test_lower_bound_shrinks_with_more_hypotheses():
    xs = [0.001, -0.0005, 0.002, 0.0008, -0.0002, 0.0015, 0.0003, -0.0001] * 6
    lb1 = AF._lower_bound(xs, tests=1)
    lb50 = AF._lower_bound(xs, tests=50)
    assert lb50 < lb1


def test_simulate_candidate_threads_n_tests(monkeypatch):
    captured = {}
    orig = AF._metrics_from_outcomes

    def spy(outs, n_tests):
        captured.setdefault("n_tests", []).append(n_tests)
        return orig(outs, n_tests)
    monkeypatch.setattr(AF, "_metrics_from_outcomes", spy)
    bars = _mk_bars(260)
    feats = AF.build_alpha_features(bars)
    q = AF._quantiles(feats, int(len(feats) * 0.6))
    rule = AF._rule_defs()[0]
    AF._simulate_candidate(rule, AF._exit_grid()[0], feats, bars, q, n_tests=50)
    assert captured["n_tests"] and all(t == 50 for t in captured["n_tests"])


# --------------------------------------------------------------------------
# Watcher perf: base-state TTL cache present and TTL-bounded (dead collector
# still detected); counters relabelled unambiguously in the panel
# --------------------------------------------------------------------------

def test_base_state_cache_expires(monkeypatch, tmp_path: Path):
    calls = {"n": 0}

    def fake_gather(symbol):
        calls["n"] += 1
        return {"symbol": symbol, "n": calls["n"]}
    monkeypatch.setattr(DASH.A, "gather_state", fake_gather)
    DASH._BASE_STATE_CACHE.clear()
    s1 = DASH._cached_base_state("BTCUSDT", tmp_path)
    s2 = DASH._cached_base_state("BTCUSDT", tmp_path)
    assert calls["n"] == 1 and s1["n"] == s2["n"]        # cached
    # expire the TTL -> recompute (a dead collector cannot hide forever)
    key = "BTCUSDT"
    ts, payload = DASH._BASE_STATE_CACHE[key]
    DASH._BASE_STATE_CACHE[key] = (ts - DASH.BASE_STATE_TTL_SECONDS - 1, payload)
    DASH._cached_base_state("BTCUSDT", tmp_path)
    assert calls["n"] == 2


def test_persistent_panel_labels_disambiguate_counters():
    d = {"persistent_health": {"status": "HEALTHY", "connected": True,
                               "messages_count": 10, "trades_count": 100,
                               "reconnect_count": 2},
         "persistent_continuity": {"trades": 900000, "bars": 800,
                                   "max_contiguous_run": 300,
                                   "forward_coverage": 0.45,
                                   "verdict": "WS_USABLE_FOR_EXPLORATORY_RESEARCH",
                                   "fit_for_shadow_forward": False}}
    html = DASH._panel_persistent_ws(d)
    assert "Session messages (this process)" in html
    assert "Session new trades (this process)" in html
    assert "Dataset trades (deduped, all time)" in html


# --------------------------------------------------------------------------
# Persistent collector root-cause fixes: incremental append (no full rewrite),
# tail-seeded dedup across restarts, consecutive-failure backoff reset
# --------------------------------------------------------------------------

from app.labs import bybit_trades_ws_persistent_v10_43c as PC


def _trade(ts, tid, sym="BTCUSDT"):
    return {"timestamp": ts, "symbol": sym, "price": 100.0, "size": 0.01,
            "aggressor_side": "buy", "trade_id": tid, "source_exchange": "bybit_linear"}


def test_append_incremental_does_not_rewrite_existing_bytes(tmp_path: Path):
    PC.append_rows_incremental([_trade(1000, "a"), _trade(2000, "b")], tmp_path)
    f = tmp_path / "trades.csv"
    first = f.read_text(encoding="utf-8")
    PC.append_rows_incremental([_trade(3000, "c")], tmp_path)
    second = f.read_text(encoding="utf-8")
    assert second.startswith(first)          # pure append: old bytes untouched
    assert second.count("trade_id") == 1     # single header
    assert "c" in second.splitlines()[-1]


def test_seed_seen_from_tail_dedups_across_restart(tmp_path: Path):
    PC.append_rows_incremental([_trade(1000, "a"), _trade(2000, "b")], tmp_path)
    seen = PC.seed_seen_from_tail(tmp_path)
    assert seen == {"a", "b"}
    # a "restarted" collector must skip the replayed ids
    from app.labs import bybit_trades_ws_collector_v10_41 as CORE
    uniq, seen2 = CORE.dedup_by_trade_id(
        [_trade(2000, "b"), _trade(3000, "c")], seen)
    assert [r["trade_id"] for r in uniq] == ["c"]


def test_seed_seen_missing_file_is_empty(tmp_path: Path):
    assert PC.seed_seen_from_tail(tmp_path / "nope") == set()


def test_backoff_resets_after_healthy_session(tmp_path: Path):
    """After many failures the ladder reaches 30s; ONE healthy session must
    reset it to the 1s rung (lifetime reconnect_count must not drive backoff)."""
    sleeps: list[int] = []
    clock = {"ms": 1_700_000_000_000}

    def now_ms():
        clock["ms"] += 50
        return clock["ms"]

    calls = {"n": 0}

    def connect(syms):
        calls["n"] += 1
        if calls["n"] <= 7:
            return None                     # 7 straight connect failures
        if calls["n"] == 8:                 # one healthy session with frames
            frames = iter([
                {"topic": "publicTrade.BTCUSDT", "type": "snapshot", "ts": 1,
                 "data": [{"T": clock["ms"], "s": "BTCUSDT", "S": "Buy",
                           "v": "0.01", "p": "100.0", "i": f"t{calls['n']}"}]},
            ])

            def recv():
                return next(frames)         # StopIteration -> clean stream_end
            return recv
        return None                         # next failure after the healthy one

    PC.run_persistent(
        connect, ["BTCUSDT"], now_ms_fn=now_ms, on_flush=lambda rows: None,
        base=tmp_path, pid=1234, max_sessions=9,
        backoff_sleep_fn=lambda s: sleeps.append(s),
        write_health_file=False, use_lock=False)
    # 7 failures walk the ladder 1,2,5,10,20,30,30 ...
    assert sleeps[:7] == [1, 2, 5, 10, 20, 30, 30]
    # session 8 was healthy (had msgs) then session 9 fails -> back to 1s rung
    assert sleeps[7] == 1
