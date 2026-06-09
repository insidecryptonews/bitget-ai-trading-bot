"""Tests for scripts/fetch_coinalyze_chunked_v102.py (V10.2.1).

No network, no API key, no real data. Mocked sessions only. Verifies:
chunk splitting, retry/backoff, fast-abort on 401, staging isolation (raw
never touched until a successful publish), publish modes, dedup, key
sanitization, report fields, --help, and the safety contract.
"""

from __future__ import annotations

import ast
import os
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
    # Don't write report files to external_data/reports during tests.
    monkeypatch.setattr(fc, "_write_report", lambda rep: "SKIPPED_IN_TEST")


class _Resp:
    def __init__(self, sc, payload=None, text="", reason=""):
        self.status_code = sc
        self._p = payload
        self.text = text
        self.reason = reason

    def json(self):
        return self._p


def _hist(url, params, *, fixed_ts=None):
    a = int(params["from"]); b = int(params["to"])
    syms = params["symbols"].split(",")
    ts = fixed_ts if fixed_ts is not None else list(range(a, b, 3600))[:50]
    if "/ohlcv" in url:
        mk = lambda t: {"t": t, "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 1500}
    elif "/open-interest" in url:
        mk = lambda t: {"t": t, "c": 1.8e9}
    elif "/funding" in url:
        mk = lambda t: {"t": t, "c": 0.0001}
    elif "/liquidation" in url:
        mk = lambda t: {"t": t, "l": 250000 if t % 4 == 0 else 0, "s": 0}
    else:
        mk = lambda t: {"t": t, "r": 1.05}
    return [{"symbol": s, "history": [mk(t) for t in ts]} for s in syms]


class OKSession:
    def get(self, url, params, headers, timeout):
        return _Resp(200, _hist(url, params))


class FixedTsSession:
    """Returns the SAME timestamps every chunk => forces cross-chunk dups."""
    def get(self, url, params, headers, timeout):
        return _Resp(200, _hist(url, params, fixed_ts=[NOW - 3600 * k for k in range(20)]))


class RetrySession:
    def __init__(self, fail_times=2, code=429):
        self.calls = {}
        self.fail_times = fail_times
        self.code = code

    def get(self, url, params, headers, timeout):
        self.calls[url] = self.calls.get(url, 0) + 1
        if self.calls[url] <= self.fail_times:
            return _Resp(self.code, text="rate", reason="Too Many Requests")
        return _Resp(200, _hist(url, params))


class AuthFailSession:
    def get(self, url, params, headers, timeout):
        return _Resp(401, text="invalid api_key=SECRETKEY123 denied", reason="Unauthorized")


class AlwaysFiveHundred:
    def get(self, url, params, headers, timeout):
        return _Resp(500, text="server error", reason="Internal Server Error")


# --- chunk math ---

def test_chunk_split_180():
    assert fc.n_chunks(180, 30) == 6
    assert len(fc.chunk_ranges(0, 180 * 86400, 30)) == 6


def test_chunk_split_365():
    assert fc.n_chunks(365, 30) == 13


# --- staging isolation ---

def test_staging_only_does_not_touch_raw(tmp_path):
    raw_m = tmp_path / "rm"; raw_l = tmp_path / "rl"
    raw_m.mkdir(); raw_l.mkdir()
    (raw_m / "old.csv").write_text("old")
    r = fc.run_chunked_fetch(SYM, key="K", session=OKSession(), days=180, chunk_days=30,
                             staging_dir=str(tmp_path / "stg"), publish_mode="staging-only",
                             sleep_fn=_noop, raw_market_dir=str(raw_m), raw_liq_dir=str(raw_l),
                             archive_base=str(tmp_path / "arch"), now_s=NOW)
    assert r.report_status == fc.ST_PARTIAL_STAGING
    assert r.chunks_ok == 6 and r.chunks_failed == 0
    assert r.old_data_touched is False
    assert (raw_m / "old.csv").exists()  # untouched
    assert not any(f.name.startswith("coinalyze") for f in raw_m.iterdir())
    assert (pathlib.Path(r.staging_dir) / "final" / "perp_market_state.csv").exists()


def test_chunk_failure_does_not_touch_raw(tmp_path):
    raw_m = tmp_path / "rm"; raw_m.mkdir()
    (raw_m / "old.csv").write_text("old")
    r = fc.run_chunked_fetch(SYM, key="K", session=AlwaysFiveHundred(), days=60, chunk_days=30,
                             staging_dir=str(tmp_path / "stg"), publish_mode="replace",
                             sleep_fn=_noop, max_retries=2, raw_market_dir=str(raw_m),
                             raw_liq_dir=str(tmp_path / "rl"), archive_base=str(tmp_path / "arch"), now_s=NOW)
    assert r.report_status == fc.ST_FAILED
    assert r.chunks_failed == 1
    assert r.old_data_touched is False
    assert (raw_m / "old.csv").exists()  # raw intact after failure


# --- retry / abort ---

def test_retry_429_then_success(tmp_path):
    r = fc.run_chunked_fetch(SYM, key="K", session=RetrySession(fail_times=2, code=429),
                             days=30, chunk_days=30, staging_dir=str(tmp_path / "s"),
                             publish_mode="staging-only", sleep_fn=_noop, max_retries=4,
                             raw_market_dir=str(tmp_path / "rm"), raw_liq_dir=str(tmp_path / "rl"),
                             archive_base=str(tmp_path / "a"), now_s=NOW)
    assert r.report_status == fc.ST_PARTIAL_STAGING
    assert r.chunks_ok == 1


def test_401_fast_abort_and_key_sanitized(tmp_path):
    raw_m = tmp_path / "rm"; raw_m.mkdir(); (raw_m / "old.csv").write_text("old")
    r = fc.run_chunked_fetch(SYM, key="SECRETKEY123", session=AuthFailSession(), days=60,
                             chunk_days=30, staging_dir=str(tmp_path / "s"), publish_mode="replace",
                             sleep_fn=_noop, raw_market_dir=str(raw_m), raw_liq_dir=str(tmp_path / "rl"),
                             archive_base=str(tmp_path / "a"), now_s=NOW)
    assert r.report_status == fc.ST_FAILED
    assert r.failure.get("status_code") == 401
    assert r.failure.get("kind") == "auth_or_bad_request_abort"
    assert "SECRETKEY123" not in r.failure.get("body", "")
    assert "REDACTED" in r.failure.get("body", "")
    assert r.old_data_touched is False
    assert (raw_m / "old.csv").exists()


def test_401_aborts_fast_single_attempt():
    # auth failures must NOT be retried many times
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
    assert s.calls == 1  # one attempt, no retries


# --- publish modes ---

def test_replace_archives_old_then_publishes(tmp_path):
    raw_m = tmp_path / "rm"; raw_l = tmp_path / "rl"
    raw_m.mkdir(); raw_l.mkdir()
    (raw_m / "old.csv").write_text("old")
    r = fc.run_chunked_fetch(SYM, key="K", session=OKSession(), days=30, chunk_days=30,
                             staging_dir=str(tmp_path / "s"), publish_mode="replace", sleep_fn=_noop,
                             raw_market_dir=str(raw_m), raw_liq_dir=str(raw_l),
                             archive_base=str(tmp_path / "arch"), now_s=NOW)
    assert r.report_status == fc.ST_OK
    assert r.old_data_touched is True
    assert len(r.published_files) == 2
    assert not (raw_m / "old.csv").exists()  # archived away
    assert any(f.name.startswith("coinalyze") for f in raw_m.iterdir())  # published
    # old file archived somewhere under archive_base
    arch_files = list((tmp_path / "arch").rglob("old.csv"))
    assert arch_files


def test_append_keeps_old(tmp_path):
    raw_m = tmp_path / "rm"; raw_l = tmp_path / "rl"
    raw_m.mkdir(); raw_l.mkdir()
    (raw_m / "old.csv").write_text("old")
    r = fc.run_chunked_fetch(SYM, key="K", session=OKSession(), days=30, chunk_days=30,
                             staging_dir=str(tmp_path / "s"), publish_mode="append", sleep_fn=_noop,
                             raw_market_dir=str(raw_m), raw_liq_dir=str(raw_l),
                             archive_base=str(tmp_path / "arch"), now_s=NOW)
    assert r.report_status == fc.ST_OK
    assert r.old_data_touched is False
    assert (raw_m / "old.csv").exists()  # kept
    assert any(f.name.startswith("coinalyze") for f in raw_m.iterdir())  # appended


# --- dedup ---

def test_dedup_across_chunks(tmp_path):
    r = fc.run_chunked_fetch(SYM, key="K", session=FixedTsSession(), days=90, chunk_days=30,
                             staging_dir=str(tmp_path / "s"), publish_mode="staging-only",
                             sleep_fn=_noop, raw_market_dir=str(tmp_path / "rm"),
                             raw_liq_dir=str(tmp_path / "rl"), archive_base=str(tmp_path / "a"), now_s=NOW)
    # 3 chunks returning the same timestamps => duplicates across chunks > 0
    assert r.duplicates_removed > 0
    assert r.report_status == fc.ST_PARTIAL_STAGING


# --- report fields ---

def test_report_has_minimum_fields(tmp_path):
    r = fc.run_chunked_fetch(SYM, key="K", session=OKSession(), days=30, chunk_days=30,
                             staging_dir=str(tmp_path / "s"), publish_mode="staging-only",
                             sleep_fn=_noop, raw_market_dir=str(tmp_path / "rm"),
                             raw_liq_dir=str(tmp_path / "rl"), archive_base=str(tmp_path / "a"), now_s=NOW)
    d = r.as_dict()
    for k in ("report_status", "symbols", "days", "interval", "chunk_days", "chunks_total",
              "chunks_ok", "chunks_failed", "rows_market_state", "rows_liquidations",
              "min_timestamp", "max_timestamp", "duplicates_removed", "staging_dir",
              "publish_mode", "published_files", "old_data_touched", "api_key_printed",
              "db_writes", "research_only", "final_recommendation"):
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
    assert "key never printed" in out


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
        assert tok not in src, f"contains {tok}"
    assert 'os.environ.get("COINALYZE_API_KEY")' in src
    assert "print(key" not in src
