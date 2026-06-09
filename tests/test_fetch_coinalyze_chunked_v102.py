"""Tests for scripts/fetch_coinalyze_chunked_v102.py (V10.2.1 + V10.2.2).

No network, no API key, no real data. Mocked sessions only. Verifies:
chunk splitting, retry/backoff, fast-abort on 401, staging isolation,
publish modes, dedup, key sanitization, report fields, --help, the safety
contract, AND the V10.2.2 undercoverage detector + marker audit + overlap.
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys

import pytest

_SCRIPTS = str(pathlib.Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import fetch_coinalyze_chunked_v102 as fc  # noqa: E402

SYM = {"BTCUSDT": "BTCUSDT_PERP.A", "ETHUSDT": "ETHUSDT_PERP.A"}
NOW = 1_780_000_000


def _noop(_x):
    return None


@pytest.fixture(autouse=True)
def _no_report_files(monkeypatch):
    monkeypatch.setattr(fc, "_write_report", lambda rep: "SKIPPED_IN_TEST")


class _Resp:
    def __init__(self, sc, payload=None, text="", reason=""):
        self.status_code = sc
        self._p = payload
        self.text = text
        self.reason = reason

    def json(self):
        return self._p


def _mk(url):
    if "/ohlcv" in url:
        return lambda t: {"t": t, "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 1500}
    if "/open-interest" in url:
        return lambda t: {"t": t, "c": 1.8e9}
    if "/funding" in url:
        return lambda t: {"t": t, "c": 0.0001}
    if "/liquidation" in url:
        return lambda t: {"t": t, "l": 250000 if t % 4 == 0 else 0, "s": 0}
    return lambda t: {"t": t, "r": 1.05}


def _resp(url, params, ts):
    f = _mk(url)
    syms = params["symbols"].split(",")
    return _Resp(200, [{"symbol": s, "history": [f(t) for t in ts]} for s in syms])


class FullSession:
    """Full hourly coverage within [from,to] => sufficient coverage."""
    def get(self, url, params, headers, timeout):
        a = int(params["from"]); b = int(params["to"])
        return _resp(url, params, list(range(a, b, 3600)))


class CappedRecentSession:
    """API ignores 'from' and only returns the last ~84 days => undercoverage."""
    def get(self, url, params, headers, timeout):
        a = int(params["from"]); b = int(params["to"]); cutoff = NOW - 84 * 86400
        return _resp(url, params, [t for t in range(a, b, 3600) if t >= cutoff])


class FixedTsSession:
    """Same recent timestamps every chunk => heavy overlap (range cap)."""
    def get(self, url, params, headers, timeout):
        return _resp(url, params, [NOW - 3600 * k for k in range(20)])


class RetrySession:
    def __init__(self, fail_times=2, code=429):
        self.calls = {}
        self.fail_times = fail_times
        self.code = code

    def get(self, url, params, headers, timeout):
        self.calls[url] = self.calls.get(url, 0) + 1
        if self.calls[url] <= self.fail_times:
            return _Resp(self.code, text="rate", reason="Too Many Requests")
        a = int(params["from"]); b = int(params["to"])
        return _resp(url, params, list(range(a, b, 3600)))


class AuthFailSession:
    def get(self, url, params, headers, timeout):
        return _Resp(401, text="invalid api_key=SECRETKEY123 denied", reason="Unauthorized")


class AlwaysFiveHundred:
    def get(self, url, params, headers, timeout):
        return _Resp(500, text="server error", reason="Internal Server Error")


def _run(session, *, days, chunk_days, publish_mode, tmp, **kw):
    return fc.run_chunked_fetch(
        SYM, key=kw.pop("key", "K"), session=session, days=days, chunk_days=chunk_days,
        staging_dir=str(tmp / "stg"), publish_mode=publish_mode, sleep_fn=_noop,
        raw_market_dir=str(tmp / "rm"), raw_liq_dir=str(tmp / "rl"),
        archive_base=str(tmp / "arch"), now_s=NOW, **kw)


# --- chunk math ---

def test_chunk_split_180():
    assert fc.n_chunks(180, 30) == 6


def test_chunk_split_365():
    assert fc.n_chunks(365, 30) == 13


# --- V10.2.2 undercoverage ---

def test_180_requested_84_received_is_undercoverage(tmp_path):
    raw_m = tmp_path / "rm"; raw_m.mkdir(); (raw_m / "old.csv").write_text("old")
    r = fc.run_chunked_fetch(SYM, key="K", session=CappedRecentSession(), days=180, chunk_days=30,
                             staging_dir=str(tmp_path / "stg"), publish_mode="replace", sleep_fn=_noop,
                             raw_market_dir=str(raw_m), raw_liq_dir=str(tmp_path / "rl"),
                             archive_base=str(tmp_path / "arch"), now_s=NOW)
    assert r.report_status == fc.ST_UNDERCOVERAGE
    assert r.undercoverage is True
    assert r.coverage_ratio_by_days < 0.80
    assert r.publish_allowed is False
    assert r.publish_blocker == "insufficient_history_coverage"
    assert r.do_not_replace_raw is True
    # replace + undercoverage MUST NOT touch or archive raw
    assert r.old_data_touched is False
    assert r.published_files == []
    assert (raw_m / "old.csv").exists()
    assert not any(f.name.startswith("coinalyze") for f in raw_m.iterdir())


def test_staging_only_undercoverage_does_not_publish(tmp_path):
    raw_m = tmp_path / "rm"; raw_m.mkdir()
    r = fc.run_chunked_fetch(SYM, key="K", session=CappedRecentSession(), days=180, chunk_days=30,
                             staging_dir=str(tmp_path / "stg"), publish_mode="staging-only", sleep_fn=_noop,
                             raw_market_dir=str(raw_m), raw_liq_dir=str(tmp_path / "rl"),
                             archive_base=str(tmp_path / "arch"), now_s=NOW)
    assert r.report_status == fc.ST_UNDERCOVERAGE
    assert r.publish_allowed is False
    assert not any(f.name.startswith("coinalyze") for f in raw_m.iterdir())


def test_sufficient_coverage_is_partial_staging(tmp_path):
    raw_m = tmp_path / "rm"; raw_m.mkdir(); (raw_m / "old.csv").write_text("old")
    r = _run(FullSession(), days=2, chunk_days=1, publish_mode="staging-only", tmp=tmp_path)
    assert r.report_status == fc.ST_PARTIAL_STAGING
    assert r.undercoverage is False
    assert r.coverage_ratio_by_rows >= 0.80
    assert r.publish_allowed is True
    assert (raw_m / "old.csv").exists()  # staging-only never touches raw


def test_report_contains_publish_allowed_false_on_undercoverage(tmp_path):
    r = _run(CappedRecentSession(), days=180, chunk_days=30, publish_mode="staging-only", tmp=tmp_path)
    d = r.as_dict()
    assert d["publish_allowed"] is False
    assert d["undercoverage"] is True
    assert d["do_not_replace_raw"] is True


# --- markers ---

def test_chunk_marker_audit_fields(tmp_path):
    _run(FullSession(), days=2, chunk_days=1, publish_mode="staging-only", tmp=tmp_path)
    mk = json.loads((tmp_path / "stg" / "chunk_000.done.json").read_text())
    for k in ("chunk_index", "chunk_start", "chunk_end", "chunk_start_iso", "chunk_end_iso",
              "symbols", "endpoint_rows", "market_state_rows_built", "liquidation_rows_built",
              "min_timestamp", "max_timestamp", "min_timestamp_iso", "max_timestamp_iso",
              "empty_endpoints", "chunk_status"):
        assert k in mk, f"marker missing {k}"
    assert mk["endpoint_rows"]["ohlcv"] > 0
    assert "open_interest" in mk["endpoint_rows"]
    assert mk["chunk_status"] == "OK"
    assert mk["market_state_rows_built"] > 0


# --- overlap / range-cap ---

def test_overlap_detected_when_chunks_repeat(tmp_path):
    r = _run(FixedTsSession(), days=90, chunk_days=30, publish_mode="staging-only", tmp=tmp_path)
    assert r.chunk_overlap_detected is True
    assert r.overlap_ratio > 0
    assert r.duplicates_removed > 0
    assert r.possible_api_range_cap_or_ignored_from_to is True
    assert r.raw_chunk_rows_before_dedup > r.unique_timestamps


# --- staging isolation / failure ---

def test_chunk_failure_does_not_touch_raw(tmp_path):
    raw_m = tmp_path / "rm"; raw_m.mkdir(); (raw_m / "old.csv").write_text("old")
    r = fc.run_chunked_fetch(SYM, key="K", session=AlwaysFiveHundred(), days=60, chunk_days=30,
                             staging_dir=str(tmp_path / "stg"), publish_mode="replace", sleep_fn=_noop,
                             max_retries=2, raw_market_dir=str(raw_m), raw_liq_dir=str(tmp_path / "rl"),
                             archive_base=str(tmp_path / "arch"), now_s=NOW)
    assert r.report_status == fc.ST_FAILED
    assert r.chunks_failed == 1
    assert r.old_data_touched is False
    assert r.do_not_replace_raw is True
    assert (raw_m / "old.csv").exists()


# --- retry / abort ---

def test_retry_429_then_success(tmp_path):
    r = fc.run_chunked_fetch(SYM, key="K", session=RetrySession(fail_times=2, code=429),
                             days=2, chunk_days=1, staging_dir=str(tmp_path / "s"),
                             publish_mode="staging-only", sleep_fn=_noop, max_retries=4,
                             raw_market_dir=str(tmp_path / "rm"), raw_liq_dir=str(tmp_path / "rl"),
                             archive_base=str(tmp_path / "a"), now_s=NOW)
    assert r.report_status == fc.ST_PARTIAL_STAGING
    assert r.chunks_ok == 2


def test_401_fast_abort_and_key_sanitized(tmp_path):
    raw_m = tmp_path / "rm"; raw_m.mkdir(); (raw_m / "old.csv").write_text("old")
    r = fc.run_chunked_fetch(SYM, key="SECRETKEY123", session=AuthFailSession(), days=60,
                             chunk_days=30, staging_dir=str(tmp_path / "s"), publish_mode="replace",
                             sleep_fn=_noop, raw_market_dir=str(raw_m), raw_liq_dir=str(tmp_path / "rl"),
                             archive_base=str(tmp_path / "a"), now_s=NOW)
    assert r.report_status == fc.ST_FAILED
    assert r.failure.get("status_code") == 401
    assert "SECRETKEY123" not in r.failure.get("body", "")
    assert "REDACTED" in r.failure.get("body", "")
    assert r.old_data_touched is False
    assert (raw_m / "old.csv").exists()


def test_401_aborts_fast_single_attempt():
    s = AuthFailSession()
    s.calls = 0
    orig = s.get
    def counting(url, params, headers, timeout):
        s.calls += 1
        return orig(url, params, headers, timeout)
    s.get = counting
    data, err = fc._http_get_json(s, "/ohlcv-history", {"from": 0, "to": 1, "symbols": "X"},
                                  "SECRETKEY123", max_retries=5, retry_sleep=0.0, sleep_fn=_noop)
    assert err is not None and err.kind == "auth_or_bad_request_abort"
    assert s.calls == 1


# --- publish modes (sufficient coverage) ---

def test_replace_archives_old_then_publishes(tmp_path):
    raw_m = tmp_path / "rm"; raw_l = tmp_path / "rl"
    raw_m.mkdir(); raw_l.mkdir()
    (raw_m / "old.csv").write_text("old")
    r = _run(FullSession(), days=2, chunk_days=1, publish_mode="replace", tmp=tmp_path)
    assert r.report_status == fc.ST_OK
    assert r.old_data_touched is True
    assert len(r.published_files) == 2
    assert not (raw_m / "old.csv").exists()
    assert any(f.name.startswith("coinalyze") for f in raw_m.iterdir())
    assert list((tmp_path / "arch").rglob("old.csv"))


def test_append_keeps_old(tmp_path):
    raw_m = tmp_path / "rm"; raw_l = tmp_path / "rl"
    raw_m.mkdir(); raw_l.mkdir()
    (raw_m / "old.csv").write_text("old")
    r = _run(FullSession(), days=2, chunk_days=1, publish_mode="append", tmp=tmp_path)
    assert r.report_status == fc.ST_OK
    assert r.old_data_touched is False
    assert (raw_m / "old.csv").exists()
    assert any(f.name.startswith("coinalyze") for f in raw_m.iterdir())


# --- dedup ---

def test_dedup_across_chunks(tmp_path):
    r = _run(FixedTsSession(), days=90, chunk_days=30, publish_mode="staging-only", tmp=tmp_path)
    assert r.duplicates_removed > 0
    # fixed-ts => few rows => undercoverage (and overlap)
    assert r.report_status == fc.ST_UNDERCOVERAGE


# --- report fields ---

def test_report_has_minimum_fields(tmp_path):
    r = _run(FullSession(), days=2, chunk_days=1, publish_mode="staging-only", tmp=tmp_path)
    d = r.as_dict()
    for k in ("report_status", "symbols", "days", "interval", "chunk_days", "chunks_total",
              "chunks_ok", "chunks_failed", "rows_market_state", "rows_liquidations",
              "min_timestamp", "max_timestamp", "duplicates_removed", "requested_days",
              "actual_days_covered", "expected_market_rows", "actual_market_rows",
              "coverage_ratio_by_days", "coverage_ratio_by_rows", "undercoverage",
              "publish_allowed", "publish_blocker", "do_not_replace_raw",
              "chunk_overlap_detected", "overlap_ratio", "unique_timestamps",
              "raw_chunk_rows_before_dedup", "possible_api_range_cap_or_ignored_from_to",
              "staging_dir", "publish_mode", "published_files", "old_data_touched",
              "api_key_printed", "db_writes", "research_only", "final_recommendation"):
        assert k in d, f"missing {k}"
    assert d["api_key_printed"] is False
    assert d["db_writes"] == 0
    assert d["final_recommendation"] == "NO LIVE"


# --- CLI / safety ---

def test_no_key_need_key(monkeypatch, capsys):
    monkeypatch.delenv("COINALYZE_API_KEY", raising=False)
    rc = fc.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "NEED_KEY" in out


def test_help_works():
    with pytest.raises(SystemExit) as e:
        fc.main(["--help"])
    assert e.value.code == 0


def test_safety_scan_no_forbidden():
    src = pathlib.Path(fc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden = {"place_order", "set_leverage", "set_margin_mode",
                 "private_get", "private_post", "execute", "open_position"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            assert name not in forbidden, f"calls {name}"
    for tok in ('open(".env', "load_dotenv", "LIVE_TRADING = True",
                "can_send_real_orders = True", "ENABLE_PAPER_POLICY_FILTER = True",
                "import ExecutionEngine", "PaperTrader("):
        assert tok not in src
    assert 'os.environ.get("COINALYZE_API_KEY")' in src
    assert "print(key" not in src
