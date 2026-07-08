"""V10.43C persistent WS collector: long-session state machine, lock, safety."""

from __future__ import annotations

import csv

from app.labs import bybit_trades_ws_persistent_v10_43c as PWS

T0 = 1_700_000_000_000


def _frame(tid, ts=T0, price="60000", size="0.01", sym="BTCUSDT"):
    return {"topic": f"publicTrade.{sym}", "data": [
        {"T": ts, "s": sym, "S": "Buy", "v": size, "p": price, "i": tid}
    ]}


def test_stream_session_dedups_and_flushes_without_network():
    frames = iter([_frame("a", T0), _frame("a", T0), _frame("b", T0 + 1000)])
    flushed = []
    now = {"v": T0}

    def recv():
        now["v"] += 100
        return next(frames)

    st = PWS.new_state(["BTCUSDT"], T0, base=None)
    r = PWS.stream_session(recv, st, flushed.extend, now_ms_fn=lambda: now["v"],
                           flush_every_n=1)
    assert r["ended"] == "stream_end"
    assert st["trades_count"] == 2
    assert st["duplicates_skipped"] == 1
    assert [x["trade_id"] for x in flushed] == ["a", "b"]


def test_run_persistent_uses_writer_lock_and_idempotent_file_append(tmp_path):
    frames = [_frame("a", T0), _frame("a", T0), _frame("b", T0 + 1000)]

    def connect(_symbols):
        it = iter(frames)
        return lambda: next(it)

    r = PWS.run_persistent(connect, ["BTCUSDT"], base=tmp_path, pid=123,
                           max_sessions=1, flush_every_n=1,
                           now_ms_fn=lambda: T0, backoff_sleep_fn=lambda _s: None)
    assert r["can_send_real_orders"] is False
    f = tmp_path / "trades.csv"
    rows = list(csv.DictReader(open(f, newline="", encoding="utf-8")))
    assert [x["trade_id"] for x in rows] == ["a", "b"]
    assert not (tmp_path / "writer.lock").exists()


def test_fresh_writer_lock_blocks_second_writer(tmp_path):
    ok, _ = PWS.acquire_writer_lock(tmp_path, pid=1, now_ms=T0)
    assert ok is True
    ok2, holder = PWS.acquire_writer_lock(tmp_path, pid=2, now_ms=T0 + 1000)
    assert ok2 is False
    assert holder["pid"] == 1
    PWS.release_writer_lock(tmp_path, pid=1)


def test_health_marks_stale_stream():
    st = PWS.new_state(["BTCUSDT"], T0)
    st["connected"] = True
    st["last_msg_ts"] = T0
    h = PWS.compute_health(st, T0 + 25_000, stale_s=20)
    assert h["status"] == "STALE"
    assert h["uses_api_keys"] is False
    assert h["final_recommendation"] == "NO LIVE"


def test_status_is_research_only_public_no_orders():
    s = PWS.status()
    assert s["research_only"] is True
    assert s["uses_api_keys"] is False
    assert s["sends_orders"] is False
    assert "public" in s["endpoint"]
