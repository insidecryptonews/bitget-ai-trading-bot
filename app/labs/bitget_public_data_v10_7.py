"""ResearchOps V10.7 — Bitget PUBLIC free data collector + local staging.

This is the FIRST ResearchOps module that performs network I/O — and it is
deliberately boxed in:

- ONLY HTTPS GET to ``api.bitget.com`` PUBLIC market-data endpoints;
- a strict allowlist (host + scheme + method + exact path) enforced BEFORE any
  socket is opened — private/order/account/position paths fail closed;
- NO auth headers are ever sent (no ACCESS-KEY / ACCESS-SIGN /
  ACCESS-PASSPHRASE / ACCESS-TIMESTAMP), so no API key / .env is involved;
- the fetcher is DRY-RUN by default; ``apply=True`` writes ONLY under
  ``external_data/staging/bitget_public_v10_7/<run_id>/`` (never raw, never DB);
- conservative rate limiting, timeouts, bounded retries and hard request/row
  guards; failures are accumulated, never fatal;
- nothing here can flip paper_ready/live_ready. FINAL: NO LIVE.

Network is injected as a ``transport`` callable so the whole pipeline is unit
tested with mocks and never touches the real internet in tests.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from . import FINAL_RECOMMENDATION_NO_LIVE

DATA_SOURCE = "bitget_public"
BITGET_HOST = "api.bitget.com"
BITGET_BASE = "https://api.bitget.com"
PRODUCT_TYPE = "usdt-futures"
TOOL_VERSION = "v10.7"

# PUBLIC market-data endpoints (GET only, no auth). Nothing else is reachable.
EP_CANDLES = "/api/v2/mix/market/candles"
EP_FUNDING = "/api/v2/mix/market/history-fund-rate"
EP_OI = "/api/v2/mix/market/open-interest"
ALLOWED_PATHS = frozenset({EP_CANDLES, EP_FUNDING, EP_OI})

# Defensive: private/trading fragments must never appear in a reachable path.
_FORBIDDEN_PATH_FRAGMENTS = ("/order", "/account", "/position", "/plan-order",
                             "/place", "/trace")
# Bitget private-auth headers that must NEVER be sent.
_FORBIDDEN_HEADERS = frozenset({"access-key", "access-sign", "access-passphrase",
                                "access-timestamp", "x-access-key"})

# Conservative network guards.
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_RATE_PER_S = 3.0          # well below Bitget's documented ~20 req/s/IP
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_S = 0.5
MAX_REQUESTS_PER_RUN = 400
MAX_ROWS_PER_FILE = 250_000
CANDLE_PAGE_LIMIT = 1000          # Bitget candles max per call
FUNDING_PAGE_SIZE = 100           # Bitget history-fund-rate max page size
# V10.7.2 — Bitget rejects a candles request whose [startTime,endTime] interval
# exceeds 90 days (code 00001). We chunk into windows safely under that cap.
MAX_CANDLE_WINDOW_DAYS = 80
MAX_CANDLE_WINDOW_MS = MAX_CANDLE_WINDOW_DAYS * 86_400_000
MAX_INNER_PAGES_PER_WINDOW = 50   # bound pages inside one window (no infinite loop)

STAGING_ROOT = "external_data/staging/bitget_public_v10_7"

# Supported data types and timeframe handling.
DATA_TYPES = ("candles", "funding", "oi_snapshot")
_TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
               "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720,
               "1d": 1440}
# Bitget v2 mix granularity casing (minutes lowercase m, hours/day uppercase).
_GRANULARITY = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
                "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H",
                "1d": "1D"}

# Honest, official-docs-derived queryable lookback notes (NOT a readiness promise).
COVERAGE_NOTES = {
    "1m": "~1 month (official_queryable_limit_note)",
    "3m": "~1 month (official_queryable_limit_note)",
    "5m": "~1 month (official_queryable_limit_note)",
    "15m": "~52 days (official_queryable_limit_note)",
    "30m": "~62 days (official_queryable_limit_note)",
    "1h": "~83 days (official_queryable_limit_note)",
    "2h": "~120 days (official_queryable_limit_note)",
    "4h": "~240 days (official_queryable_limit_note)",
    "6h": "~360 days (official_queryable_limit_note)",
    "12h": "NEEDS_MANUAL_VERIFICATION",
    "1d": "NEEDS_MANUAL_VERIFICATION",
}


class UnsafeRequestError(Exception):
    """Raised BEFORE any socket is opened when a request violates the allowlist."""


class BitgetApiError(Exception):
    """A Bitget HTTP/logical error carrying a sanitized status/code/msg so the
    run_report has an actionable reason (no headers, no secrets)."""

    def __init__(self, status: Any = "", code: Any = "", msg: Any = ""):
        self.status = str(status)
        self.code = str(code)
        self.msg = _sanitize_msg(msg)
        super().__init__(f"{self.status}:{self.code}:{self.msg}")


def _sanitize_msg(msg: Any) -> str:
    """Collapse whitespace, strip delimiters, truncate — public error text only."""
    try:
        text = " ".join(str(msg).split())
    except Exception:
        return ""
    text = text.replace(":", ";").replace("|", "/")
    return text[:120]


# --------------------------------------------------------------------------
# small pure helpers
# --------------------------------------------------------------------------

def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        f = float(value)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    f = _to_float(value)
    return int(f) if f is not None else None


def _norm_tf(token: Any) -> str | None:
    try:
        t = str(token).strip().lower()
    except Exception:
        return None
    return t if t in _TF_MINUTES else None


# --------------------------------------------------------------------------
# V10.7.1 (Codex fix) — single staging-path-safety gate. EVERY local write in
# this module (run_fetch staging_root, to-sample output, any sample conversion)
# must be under the bitget public staging marker. Fail-closed.
# --------------------------------------------------------------------------

# The marker that a path must contain (as consecutive segments) to be a legal
# write target. Tying allowance to this marker — not to an absolute repo root —
# lets temp-rooted runs work while still rejecting raw/outside/db/etc.
_STAGING_MARKER = ("external_data", "staging", "bitget_public_v10_7")
# Exact path segments that are never allowed anywhere in the path.
_FORBIDDEN_SEGMENTS = frozenset({
    "raw", "backup", "backups", "vault", "vaults", "training_exports",
    "secret", "secrets", "credential", "credentials",
    "codex_result.md", "code_result.md",
})
# Dangerous suffixes / substrings on any segment.
_FORBIDDEN_SUFFIXES = (".env", ".db", ".sqlite", ".sqlite3", ".zip", ".tar",
                       ".gz", ".tgz", ".pem", ".key")


def _contains_subsequence(seq: list[str], sub: list[str]) -> bool:
    if not sub or len(sub) > len(seq):
        return False
    return any(seq[i:i + len(sub)] == sub for i in range(len(seq) - len(sub) + 1))


def _forbidden_segment(segs: list[str]) -> str | None:
    for s in segs:
        if s in _FORBIDDEN_SEGMENTS:
            return "refuses_raw_directory" if s == "raw" else "unsafe_staging_dir"
        if ".env" in s or s.endswith(_FORBIDDEN_SUFFIXES):
            return "unsafe_staging_dir"
    return None


def validate_bitget_public_staging_dir_v107(staging_dir: Any, *,
                                            for_write: bool) -> str | None:
    """Return a blocker code if ``staging_dir`` is not a safe V10.7 staging
    path, else None. Pure, never raises.

    for_write=True  -> must be the bitget public staging marker dir or below it
                       (writes are staging-ONLY; raw/outside is fail-closed).
    for_write=False -> read-only audit: still blocks raw/db/.env/zip/backup/
                       vault/traversal/percent/symlink-escape, but does not
                       require the marker (so any otherwise-safe dir can be
                       inspected).
    Codes: staging_dir_outside_allowed_root / refuses_raw_directory /
    unsafe_staging_dir / percent_encoded_path_blocked /
    staging_symlink_escape_blocked.
    """
    if not isinstance(staging_dir, str) or not staging_dir.strip():
        return "unsafe_staging_dir"
    raw = staging_dir.strip()
    if "%" in raw:
        return "percent_encoded_path_blocked"
    if ".." in raw.replace("\\", "/").split("/"):
        return "unsafe_staging_dir"  # lexical traversal
    try:
        lexical = os.path.normpath(os.path.abspath(raw)).replace("\\", "/")
        real = os.path.realpath(raw).replace("\\", "/")
    except Exception:
        return "unsafe_staging_dir"
    lex_segs = [s.lower() for s in lexical.split("/") if s]
    real_segs = [s.lower() for s in real.split("/") if s]
    for segs in (lex_segs, real_segs):
        bad = _forbidden_segment(segs)
        if bad is not None:
            return bad
    if for_write:
        marker = [m.lower() for m in _STAGING_MARKER]
        lex_ok = _contains_subsequence(lex_segs, marker)
        real_ok = _contains_subsequence(real_segs, marker)
        if lex_ok and not real_ok:
            return "staging_symlink_escape_blocked"  # symlink leaves the root
        if not real_ok:
            return "staging_dir_outside_allowed_root"
    return None


# --------------------------------------------------------------------------
# A. Endpoint registry
# --------------------------------------------------------------------------

@dataclass
class PublicEndpointV107:
    name: str
    method: str
    path: str
    auth_required: bool
    params: list[str]
    notes: str
    implemented: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_endpoint_registry() -> list[PublicEndpointV107]:
    return [
        PublicEndpointV107(
            "candles", "GET", EP_CANDLES, False,
            ["symbol", "productType", "granularity", "startTime", "endTime", "limit"],
            "public USDT-futures candles; limit<=1000; productType=usdt-futures"),
        PublicEndpointV107(
            "history_funding", "GET", EP_FUNDING, False,
            ["symbol", "productType", "pageSize", "pageNo"],
            "public historical funding rate; pageSize<=100"),
        PublicEndpointV107(
            "open_interest", "GET", EP_OI, False,
            ["symbol", "productType"],
            "public CURRENT open interest snapshot (NOT long history)"),
        PublicEndpointV107(
            "mark_index_price_candles", "GET", "(planned)", False, [],
            "mark/index price candles — left PLANNED until a public endpoint is "
            "confirmed in docs", implemented=False),
    ]


def endpoint_registry_report() -> dict[str, Any]:
    return {
        "data_source": DATA_SOURCE,
        "base_url": BITGET_BASE,
        "product_type": PRODUCT_TYPE,
        "endpoints": [e.as_dict() for e in build_endpoint_registry()],
        "allowed_paths": sorted(ALLOWED_PATHS),
        "no_private_auth": True,
        "no_env": True,
        "public_get_only": True,
        "research_only": True,
        "paper_ready": False,
        "live_ready": False,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }


# --------------------------------------------------------------------------
# D. Network safety allowlist + transport
# --------------------------------------------------------------------------

def assert_safe_request(method: str, url: str,
                        headers: dict[str, Any] | None = None) -> bool:
    """Fail-closed BEFORE any socket. Only HTTPS GET to the exact public
    market-data paths on api.bitget.com, with no auth headers, is allowed."""
    if str(method).upper() != "GET":
        raise UnsafeRequestError(f"method_not_allowed:{method}")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise UnsafeRequestError(f"scheme_not_https:{parts.scheme}")
    if parts.netloc != BITGET_HOST:
        raise UnsafeRequestError(f"host_not_allowed:{parts.netloc}")
    if parts.path not in ALLOWED_PATHS:
        raise UnsafeRequestError(f"path_not_allowed:{parts.path}")
    for frag in _FORBIDDEN_PATH_FRAGMENTS:
        if frag in parts.path:
            raise UnsafeRequestError(f"forbidden_path_fragment:{frag}")
    for key in (headers or {}):
        if str(key).strip().lower() in _FORBIDDEN_HEADERS:
            raise UnsafeRequestError(f"forbidden_auth_header:{key}")
    return True


def _raw_http_get(url: str, timeout: float) -> dict[str, Any]:
    req = Request(url, method="GET",
                  headers={"User-Agent": "researchops-v10_7-public/1.0",
                           "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https+allowlisted)
            body = resp.read()
    except HTTPError as exc:  # capture Bitget's status + code/msg, no headers
        bcode, bmsg = "", ""
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            bcode, bmsg = payload.get("code", ""), payload.get("msg", "")
        except Exception:
            bmsg = getattr(exc, "reason", "") or ""
        raise BitgetApiError(status=exc.code, code=bcode, msg=bmsg) from None
    except URLError as exc:
        raise BitgetApiError(status="URLERR", code="",
                             msg=getattr(exc, "reason", "")) from None
    obj = json.loads(body.decode("utf-8"))
    return obj if isinstance(obj, dict) else {"data": obj}


def default_transport(path: str, params: dict[str, Any], *,
                      timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """The ONLY function that opens a real socket. Allowlist-checked first."""
    url = BITGET_BASE + path
    if params:
        url = url + "?" + urlencode({k: v for k, v in params.items() if v is not None})
    assert_safe_request("GET", url, headers={})
    return _raw_http_get(url, timeout=timeout)


Transport = Callable[..., dict[str, Any]]


# --------------------------------------------------------------------------
# C. Response parsers (tolerant; never raise)
# --------------------------------------------------------------------------

def parse_candles(payload: dict[str, Any], *, symbol: str, timeframe: str) -> list[dict[str, Any]]:
    data = (payload or {}).get("data") or []
    fetched = _now_iso()
    rows: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, (list, tuple)) or len(item) < 5:
            continue
        ts = _to_int(item[0])
        o, h, l, c = (_to_float(item[1]), _to_float(item[2]),
                      _to_float(item[3]), _to_float(item[4]))
        vb = _to_float(item[5]) if len(item) > 5 else None
        vq = _to_float(item[6]) if len(item) > 6 else None
        if ts is None or None in (o, h, l, c):
            continue
        rows.append({"timestamp_ms": ts, "symbol": symbol,
                     "product_type": PRODUCT_TYPE, "timeframe": timeframe,
                     "open": o, "high": h, "low": l, "close": c,
                     "volume_base": vb if vb is not None else "",
                     "volume_quote": vq if vq is not None else "",
                     "source": DATA_SOURCE, "fetched_at": fetched})
    return rows


def parse_funding(payload: dict[str, Any], *, symbol: str) -> list[dict[str, Any]]:
    data = (payload or {}).get("data") or []
    if isinstance(data, dict):  # some shapes wrap a list
        data = data.get("list") or data.get("fundingRateList") or []
    fetched = _now_iso()
    rows: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        ts = _to_int(item.get("fundingTime") or item.get("settleTime")
                     or item.get("ts"))
        rate = _to_float(item.get("fundingRate") or item.get("fundRate"))
        if ts is None or rate is None:
            continue
        rows.append({"funding_time_ms": ts,
                     "symbol": item.get("symbol") or symbol,
                     "product_type": PRODUCT_TYPE, "funding_rate": rate,
                     "source": DATA_SOURCE, "fetched_at": fetched})
    return rows


def parse_oi_snapshot(payload: dict[str, Any], *, symbol: str) -> list[dict[str, Any]]:
    data = (payload or {}).get("data") or {}
    fetched = _now_iso()
    ts = _to_int(data.get("ts") or data.get("timestamp")) or _now_ms()
    rows: list[dict[str, Any]] = []
    items = data.get("openInterestList") or data.get("list") or []
    if not items and (data.get("size") is not None or data.get("amount") is not None):
        items = [data]
    for item in items:
        if not isinstance(item, dict):
            continue
        size = _to_float(item.get("size") or item.get("amount")
                         or item.get("openInterest"))
        if size is None or size < 0:
            continue
        rows.append({"timestamp_ms": ts, "symbol": item.get("symbol") or symbol,
                     "product_type": PRODUCT_TYPE, "open_interest_size": size,
                     "source": DATA_SOURCE, "fetched_at": fetched})
    return rows


# --------------------------------------------------------------------------
# B. Coverage planner
# --------------------------------------------------------------------------

def build_plan_v107(symbols: list[str] | None = None,
                    timeframes: list[str] | None = None) -> dict[str, Any]:
    coverage = [{"timeframe": tf, "minutes": _TF_MINUTES[tf],
                 "official_queryable_limit_note": COVERAGE_NOTES[tf],
                 "bitget_granularity": _GRANULARITY[tf]}
                for tf in _TF_MINUTES]
    return {
        "data_source": DATA_SOURCE,
        "free": True,
        "api_key_required": False,
        "provider_verified": False,
        "coverage_matrix": coverage,
        "recommended_start_free": {
            "symbols": ["BTCUSDT", "ETHUSDT"], "timeframes": ["1h", "4h"],
            "days": 30},
        "recommended_wide_research": {
            "timeframes": {"1h": "max queryable (chunked)",
                           "4h": "up to ~180d via <=90d chunks (collector chunks automatically)"},
            "funding": "historical (paged)",
            "oi": "snapshots ACCUMULATED from today onward (no long history)"},
        "per_request_limit_note": (
            "Bitget /candles rejects any startTime-endTime interval > 90 days "
            "(code 00001); the collector chunks long ranges into <=80-day "
            "windows automatically. 6H is queryable but verify per symbol — not "
            "asserted as 180d-ready without a successful run."),
        "limitations": [
            "no long historical open interest (snapshot only)",
            "no complete public historical liquidations",
            "single candles request capped at a 90-day interval (auto-chunked)",
            "low timeframes capped ~1 month; not enough for 180/365d intraday",
            "no live readiness"],
        "paper_ready": False,
        "live_ready": False,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }


# --------------------------------------------------------------------------
# E. Rate-limited fetch loops + C. staging writer + run_report
# --------------------------------------------------------------------------

@dataclass
class _RunCtx:
    transport: Transport
    sleep_fn: Callable[[float], None]
    rate_per_s: float
    timeout: float
    max_retries: int
    requests_made: int = 0
    endpoints_called: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _last_t: float = 0.0

    def call(self, path: str, params: dict[str, Any], *,
             label: str = "") -> dict[str, Any] | None:
        if self.requests_made >= MAX_REQUESTS_PER_RUN:
            self.warnings.append("max_requests_guard_hit")
            return None
        # conservative rate limit
        min_interval = 1.0 / max(0.1, self.rate_per_s)
        wait = min_interval - (time.monotonic() - self._last_t)
        if wait > 0:
            self.sleep_fn(wait)
        attempt = 0
        while True:
            try:
                self.requests_made += 1
                payload = self.transport(path, params, timeout=self.timeout)
                self.endpoints_called.append(path)
                self._last_t = time.monotonic()
                # logical Bitget error (HTTP 200 but code != success)
                if isinstance(payload, dict) and str(payload.get("code", "00000")) not in ("00000", "0", "200"):
                    self.errors.append(
                        f"request_failed:{path}:{label}:200:"
                        f"{payload.get('code')}:{_sanitize_msg(payload.get('msg'))}")
                    return None
                return payload if isinstance(payload, dict) else None
            except UnsafeRequestError:
                raise  # never swallow a safety violation
            except BitgetApiError as exc:
                attempt += 1
                if attempt > self.max_retries:
                    # actionable detail: endpoint:symbol:tf:status:code:msg
                    self.errors.append(
                        f"request_failed:{path}:{label}:{exc.status}:{exc.code}:{exc.msg}")
                    self._last_t = time.monotonic()
                    return None
                self.sleep_fn(DEFAULT_BACKOFF_S * attempt)
            except Exception as exc:
                attempt += 1
                if attempt > self.max_retries:
                    self.errors.append(f"request_failed:{path}:{label}:{type(exc).__name__}")
                    self._last_t = time.monotonic()
                    return None
                self.sleep_fn(DEFAULT_BACKOFF_S * attempt)


def _fetch_candles(ctx: _RunCtx, symbol: str, tf: str, days: int) -> list[dict[str, Any]]:
    """Fetch candles over a long range by CHUNKING into windows whose
    [startTime,endTime] interval stays under Bitget's 90-day cap (code 00001),
    paging forward within each window. Dedups + sorts; never fills gaps."""
    gran = _GRANULARITY[tf]
    end_ms = _now_ms()
    start_ms = end_ms - int(days) * 86_400_000
    bar_ms = _TF_MINUTES[tf] * 60_000
    # window <= min(90-day cap, one page of `limit` bars)
    window_ms = max(bar_ms, min(MAX_CANDLE_WINDOW_MS, CANDLE_PAGE_LIMIT * bar_ms))
    label = f"{symbol}:{tf}"
    by_ts: dict[int, dict[str, Any]] = {}
    win_start = start_ms
    while win_start < end_ms:
        if ctx.requests_made >= MAX_REQUESTS_PER_RUN:
            ctx.warnings.append(f"max_requests_guard_hit:candles:{label}")
            break
        win_end = min(win_start + window_ms, end_ms)
        cursor = win_start
        inner = 0
        while cursor < win_end and inner < MAX_INNER_PAGES_PER_WINDOW:
            inner += 1
            payload = ctx.call(EP_CANDLES, {
                "symbol": symbol, "productType": PRODUCT_TYPE, "granularity": gran,
                "startTime": cursor, "endTime": win_end, "limit": CANDLE_PAGE_LIMIT},
                label=label)
            if payload is None:
                break  # error already recorded; stop this symbol/tf
            page = parse_candles(payload, symbol=symbol, timeframe=tf)
            if not page:
                break
            max_ts = cursor
            for r in page:
                by_ts[r["timestamp_ms"]] = r
                max_ts = max(max_ts, r["timestamp_ms"])
            if len(by_ts) >= MAX_ROWS_PER_FILE:
                ctx.warnings.append(f"max_rows_guard_hit:candles:{label}")
                return [by_ts[k] for k in sorted(by_ts)]
            if max_ts + bar_ms <= cursor:
                break  # no forward progress inside the window
            cursor = max_ts + bar_ms
            if len(page) < CANDLE_PAGE_LIMIT:
                break  # window exhausted in one (or few) pages
        win_start = win_end  # advance to the next <=window_ms chunk
    return [by_ts[k] for k in sorted(by_ts)]


def _fetch_funding(ctx: _RunCtx, symbol: str, days: int) -> list[dict[str, Any]]:
    cutoff = _now_ms() - int(days) * 86_400_000
    by_ts: dict[int, dict[str, Any]] = {}
    page_no = 1
    while page_no <= MAX_REQUESTS_PER_RUN:
        payload = ctx.call(EP_FUNDING, {
            "symbol": symbol, "productType": PRODUCT_TYPE,
            "pageSize": FUNDING_PAGE_SIZE, "pageNo": page_no}, label=symbol)
        if payload is None:
            break
        rows = parse_funding(payload, symbol=symbol)
        if not rows:
            break
        for r in rows:
            by_ts[r["funding_time_ms"]] = r
        if len(rows) < FUNDING_PAGE_SIZE or min(r["funding_time_ms"] for r in rows) < cutoff:
            break  # reached page end or far enough back
        page_no += 1
    return [by_ts[k] for k in sorted(by_ts) if k >= cutoff] or \
           [by_ts[k] for k in sorted(by_ts)]


def _fetch_oi_snapshot(ctx: _RunCtx, symbol: str) -> list[dict[str, Any]]:
    payload = ctx.call(EP_OI, {"symbol": symbol, "productType": PRODUCT_TYPE},
                       label=symbol)
    return parse_oi_snapshot(payload, symbol=symbol) if payload else []


_CSV_HEADERS = {
    "candles": ["timestamp_ms", "symbol", "product_type", "timeframe", "open",
                "high", "low", "close", "volume_base", "volume_quote", "source",
                "fetched_at"],
    "funding": ["funding_time_ms", "symbol", "product_type", "funding_rate",
                "source", "fetched_at"],
    "oi_snapshot": ["timestamp_ms", "symbol", "product_type",
                    "open_interest_size", "source", "fetched_at"],
}


def _write_csv(path: str, header: list[str], rows: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in header})


def run_fetch_v107(*, symbols: list[str], timeframes: list[str], days: int,
                   data_types: list[str], apply: bool = False,
                   transport: Transport | None = None,
                   staging_root: str = STAGING_ROOT,
                   sleep_fn: Callable[[float], None] | None = None,
                   rate_per_s: float = DEFAULT_RATE_PER_S,
                   timeout: float = DEFAULT_TIMEOUT_S,
                   max_retries: int = DEFAULT_MAX_RETRIES) -> dict[str, Any]:
    """Fetch PUBLIC Bitget market data. DRY-RUN by default (no network, no
    writes). With apply=True, calls the transport and writes ONLY under
    ``staging_root/<run_id>/``. Never writes raw/DB/.env."""
    started = _now_iso()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tfs = [tf for tf in (_norm_tf(t) for t in timeframes) if tf]
    bad_tfs = [t for t in timeframes if _norm_tf(t) is None]
    dts = [d for d in (str(x).strip().lower() for x in data_types) if d in DATA_TYPES]
    bad_dts = [d for d in data_types if str(d).strip().lower() not in DATA_TYPES]
    syms = [str(s).strip().upper() for s in symbols if str(s).strip()]

    report: dict[str, Any] = {
        "run_id": run_id, "tool_version": TOOL_VERSION,
        "data_source": DATA_SOURCE, "started_at": started, "ended_at": "",
        "dry_run": (not apply), "symbols": syms, "timeframes": tfs,
        "requested_days": int(days), "data_types": dts,
        "endpoints_called": [], "rows_written": {}, "files": [],
        "errors": [], "warnings": [], "staging_dir": "",
        "research_only": True, "paper_ready": False, "live_ready": False,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }
    for label, bad in (("unknown_timeframes", bad_tfs), ("unknown_data_types", bad_dts)):
        if bad:
            report["warnings"].append(f"{label}:{','.join(map(str, bad))}")
    if not syms or not tfs or not dts:
        report["errors"].append("nothing_to_fetch (need symbols+timeframes+data_types)")
        report["ended_at"] = _now_iso()
        return report

    if not apply:
        # Plan only — no network, no writes.
        planned = []
        for s in syms:
            for d in dts:
                if d == "candles":
                    planned += [f"candles:{s}:{tf}" for tf in tfs]
                else:
                    planned.append(f"{d}:{s}")
        report["planned_fetches"] = planned
        report["note"] = "dry-run: no network calls, no files written. Pass --apply to fetch."
        report["ended_at"] = _now_iso()
        return report

    # V10.7.1 (Codex fix) — staging-ONLY enforcement BEFORE any write/network.
    staging_block = validate_bitget_public_staging_dir_v107(staging_root, for_write=True)
    if staging_block is not None:
        report["errors"].append(f"staging_root_rejected:{staging_block}")
        report["blocked"] = True
        report["ended_at"] = _now_iso()
        return report  # fail-closed: never write outside the staging root

    ctx = _RunCtx(transport=transport or default_transport,
                  sleep_fn=sleep_fn or time.sleep, rate_per_s=rate_per_s,
                  timeout=timeout, max_retries=max_retries)
    run_dir = os.path.join(staging_root, run_id)
    rows_written: dict[str, int] = {}
    files: list[str] = []

    def _emit(dtype: str, sub: str, fname: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            report["warnings"].append(f"no_rows_written_for_{sub}_{dtype}")
            return
        rel = os.path.join(dtype, sub, fname)
        _write_csv(os.path.join(run_dir, rel), _CSV_HEADERS[dtype], rows)
        rows_written[rel.replace("\\", "/")] = len(rows)
        files.append(rel.replace("\\", "/"))

    for s in syms:
        if "candles" in dts:
            for tf in tfs:
                _emit("candles", s, f"{tf}.csv", _fetch_candles(ctx, s, tf, days))
        if "funding" in dts:
            _emit("funding", s, "funding.csv", _fetch_funding(ctx, s, days))
        if "oi_snapshot" in dts:
            _emit("oi_snapshot", s, "oi_snapshot.csv", _fetch_oi_snapshot(ctx, s))

    report["endpoints_called"] = sorted(set(ctx.endpoints_called))
    report["requests_made"] = ctx.requests_made
    report["errors"].extend(ctx.errors)
    report["warnings"].extend(ctx.warnings)
    report["rows_written"] = rows_written
    report["files"] = files
    report["staging_dir"] = run_dir.replace("\\", "/")
    if not rows_written:
        report["warnings"].append("no_rows_written")
    report["ended_at"] = _now_iso()

    try:
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "run_report.json"), "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
    except Exception as exc:
        report["warnings"].append(f"run_report_write_failed:{type(exc).__name__}")
    return report


# --------------------------------------------------------------------------
# F. Staging audit
# --------------------------------------------------------------------------

def _sha256_file(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _safe_rel(staging_dir: str, full: str) -> str:
    return os.path.relpath(full, staging_dir).replace("\\", "/")


def _audit_file(full: str, rel: str, dtype: str) -> dict[str, Any]:
    rep = {"path": rel, "data_type": dtype, "rows": 0, "sha256": "",
           "duplicates": 0, "gap_count": 0, "blockers": [], "warnings": []}
    if "%" in rel:
        rep["blockers"].append("percent_encoded_path")
        return rep
    if ".." in rel.split("/") or rel.startswith("/") or rel.startswith("~"):
        rep["blockers"].append("unsafe_path")
        return rep
    sha = _sha256_file(full)
    if sha is None:
        rep["blockers"].append("sha256_unreadable")
        return rep
    rep["sha256"] = sha
    try:
        with open(full, "r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except Exception:
        rep["blockers"].append("csv_unparseable")
        return rep
    rep["rows"] = len(rows)
    if not rows:
        rep["blockers"].append("zero_rows")
        return rep

    ts_field = {"candles": "timestamp_ms", "funding": "funding_time_ms",
                "oi_snapshot": "timestamp_ms"}.get(dtype, "timestamp_ms")
    tss: list[int] = []
    invalid = 0
    for r in rows:
        ts = _to_int(r.get(ts_field))
        if ts is None:
            invalid += 1
            continue
        tss.append(ts)
        if dtype == "candles":
            o, h, l, c = (_to_float(r.get("open")), _to_float(r.get("high")),
                          _to_float(r.get("low")), _to_float(r.get("close")))
            v = _to_float(r.get("volume_base"))
            if None in (o, h, l, c):
                invalid += 1
            elif h < l or h < max(o, c) or l > min(o, c) or (v is not None and v < 0):
                invalid += 1
        elif dtype == "funding":
            if _to_float(r.get("funding_rate")) is None:
                invalid += 1
        elif dtype == "oi_snapshot":
            oi = _to_float(r.get("open_interest_size"))
            if oi is None or oi < 0:
                invalid += 1
    if invalid:
        rep["blockers"].append(f"{dtype}_invalid_rows:{invalid}")
    if not tss:
        rep["blockers"].append("no_parseable_timestamps")
        return rep
    rep["duplicates"] = len(tss) - len(set(tss))
    if rep["duplicates"] > 0:
        rep["blockers"].append(f"duplicate_timestamps:{rep['duplicates']}")
    # gaps for candle series with a known timeframe
    if dtype == "candles":
        tf = _norm_tf((rows[0].get("timeframe") or "").strip())
        if tf and len(tss) > 1:
            bar = _TF_MINUTES[tf] * 60_000
            ordered = sorted(set(tss))
            gaps = sum(int((b - a) // bar) - 1 for a, b in zip(ordered, ordered[1:])
                       if b - a > bar)
            rep["gap_count"] = gaps
            if gaps > 0:
                rep["warnings"].append(f"gap_count:{gaps}")
    rep["start_ts"] = min(tss)
    rep["end_ts"] = max(tss)
    return rep


def audit_staging_v107(staging_dir: str) -> dict[str, Any]:
    report: dict[str, Any] = {
        "staging_dir": staging_dir, "tool_version": TOOL_VERSION,
        "audit_status": "STAGING_BLOCKED", "files": [], "rows_total": 0,
        "coverage": {}, "blockers": [], "warnings": [],
        "suggested_next": [
            "python -m app.research_lab bitget-public-to-sample-v107 --staging-dir <dir>",
            "python -m app.research_lab provider-sample-validate-v106 --sample-dir <sample_dir> --expected-days 30 --provider bitget_official",
            "python -m app.research_lab provider-sample-manifest-v106 --sample-dir <sample_dir> --expected-days 30 --provider bitget_official"],
        "paper_ready": False, "live_ready": False,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }
    if not (isinstance(staging_dir, str) and staging_dir and os.path.isdir(staging_dir)):
        report["blockers"].append("staging_dir_not_found")
        return report
    # V10.7.1 — centralised path safety (read-only: blocks raw/db/.env/zip/
    # backup/vault/traversal/percent/symlink-escape; no marker requirement).
    path_block = validate_bitget_public_staging_dir_v107(staging_dir, for_write=False)
    if path_block is not None:
        report["blockers"].append(path_block)
        return report

    file_reports: list[dict[str, Any]] = []
    for root, _dirs, files in os.walk(staging_dir):
        for fn in files:
            if not fn.lower().endswith(".csv"):
                continue
            full = os.path.join(root, fn)
            rel = _safe_rel(staging_dir, full)
            seg = rel.split("/")
            dtype = next((d for d in DATA_TYPES if d in seg), "")
            if not dtype:
                report["warnings"].append(f"unclassified_file:{rel}")
                continue
            file_reports.append(_audit_file(full, rel, dtype))

    if not file_reports:
        report["blockers"].append("no_classified_csv_files")
        return report

    report["files"] = file_reports
    report["rows_total"] = sum(fr["rows"] for fr in file_reports)
    all_blockers = [b for fr in file_reports for b in fr["blockers"]]
    all_warnings = [w for fr in file_reports for w in fr["warnings"]]
    report["blockers"].extend(all_blockers)
    report["warnings"].extend(all_warnings)
    starts = [fr["start_ts"] for fr in file_reports if fr.get("start_ts") is not None]
    ends = [fr["end_ts"] for fr in file_reports if fr.get("end_ts") is not None]
    if starts and ends:
        days = round((max(ends) - min(starts)) / 86_400_000.0, 2)
        report["coverage"] = {"start_ts": min(starts), "end_ts": max(ends),
                              "actual_days_covered": days}

    # V10.7.2 — EXPECTED-DATA audit: cross-check what the run_report requested
    # against what actually landed. A run that asked for candles but produced
    # none (or had request errors) must NOT read as a clean STAGING_OK.
    present_types = {fr["data_type"] for fr in file_reports if fr.get("data_type")}
    present_candle_keys = {
        (fr["path"].split("/")[1].upper(),
         os.path.splitext(fr["path"].split("/")[-1])[0].lower())
        for fr in file_reports
        if fr.get("data_type") == "candles" and len(fr["path"].split("/")) >= 3}
    report["expected_data"] = _audit_expected_data(
        staging_dir, present_types, present_candle_keys, report)

    if report["blockers"]:
        report["audit_status"] = "STAGING_BLOCKED"
    elif report["warnings"]:
        report["audit_status"] = "STAGING_HAS_WARNINGS"
    else:
        report["audit_status"] = "STAGING_OK"
    return report


def _audit_expected_data(staging_dir: str, present_types: set,
                         present_candle_keys: set,
                         report: dict[str, Any]) -> dict[str, Any]:
    """Read run_report.json (if any) and flag incompleteness vs what was
    requested. Mutates report[blockers]/[warnings]. Never raises."""
    summary: dict[str, Any] = {"run_report_found": False}
    rr_path = os.path.join(staging_dir, "run_report.json")
    if not os.path.isfile(rr_path):
        # V10.7.3 — without a run_report we cannot verify the staging contains
        # everything that was originally requested. Clean CSVs are not blocked,
        # but the audit must not read as a clean STAGING_OK either.
        report["warnings"].append("run_report_missing_expected_data_unverifiable")
        return summary
    try:
        with open(rr_path, "r", encoding="utf-8") as fh:
            rr = json.load(fh)
        if not isinstance(rr, dict):
            return summary
    except Exception:
        report["warnings"].append("run_report_unreadable")
        return summary

    summary["run_report_found"] = True
    requested = [d for d in (rr.get("data_types") or []) if d in DATA_TYPES]
    req_symbols = [str(s).upper() for s in (rr.get("symbols") or [])]
    req_tfs = [str(t).lower() for t in (rr.get("timeframes") or [])]
    summary["requested_data_types"] = requested
    summary["present_data_types"] = sorted(present_types)

    # 1) a requested data type that produced no file at all.
    missing_types = [d for d in requested if d not in present_types]
    for d in missing_types:
        report["blockers"].append(f"expected_data_type_missing:{d}")
    summary["missing_data_types"] = missing_types

    # 2) request-level errors recorded during the run.
    if rr.get("errors"):
        report["warnings"].append("run_report_errors_present")
        summary["run_report_error_count"] = len(rr["errors"])
    # 3) carry forward any no_rows warnings the run already recorded.
    for w in (rr.get("warnings") or []):
        if isinstance(w, str) and w.startswith("no_rows_written"):
            report["warnings"].append(w)

    # 4) per requested symbol/timeframe candle presence (warnings only — partial
    # coverage is recoverable; only a FULL candle miss is a blocker via (1)).
    if "candles" in requested and "candles" in present_types:
        missing_pairs = [f"requested_timeframe_missing:{s}:{tf}"
                         for s in req_symbols for tf in req_tfs
                         if (s, tf) not in present_candle_keys]
        for m in missing_pairs:
            report["warnings"].append(m)
        summary["missing_symbol_timeframe_count"] = len(missing_pairs)
    return summary


# --------------------------------------------------------------------------
# G. Convert staging -> a sample dir the V10.6 validator understands
# --------------------------------------------------------------------------

SAMPLE_SUBDIR = "_sample_v106"


def staging_to_sample_v107(staging_dir: str, *, apply: bool = True) -> dict[str, Any]:
    """Transform staging candles/funding into validator-friendly sample files
    (``<SYMBOL>_<tf>_ohlcv.csv`` / ``<SYMBOL>_funding.csv``) under the staging
    dir's ``_sample_v106`` subdir, so provider-sample-validate-v106 can read it
    WITHOUT any readiness bypass. OI snapshots are skipped (single-point).

    V10.7.1 (Codex fix) — writes here are staging-ONLY: ``staging_dir`` must be
    under the bitget public staging marker; raw/outside/traversal/percent/db/
    env/backup/vault/symlink-escape are fail-closed BEFORE any directory is
    created or any byte is written."""
    out_dir = os.path.join(staging_dir, SAMPLE_SUBDIR)
    report = {"staging_dir": staging_dir, "sample_dir": out_dir.replace("\\", "/"),
              "written": [], "skipped": [], "errors": [],
              "paper_ready": False, "live_ready": False,
              "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}
    # Fail-closed staging-only check BEFORE computing/creating any output path.
    path_block = validate_bitget_public_staging_dir_v107(staging_dir, for_write=True)
    if path_block is not None:
        report["blocked"] = True
        report["sample_dir"] = ""  # nothing will be written
        report["errors"].append(f"staging_dir_rejected:{path_block}")
        return report
    if not os.path.isdir(staging_dir):
        report["errors"].append("staging_dir_not_found")
        return report
    for root, _dirs, files in os.walk(staging_dir):
        if SAMPLE_SUBDIR in root.replace("\\", "/").split("/"):
            continue
        for fn in files:
            if not fn.lower().endswith(".csv"):
                continue
            rel = _safe_rel(staging_dir, os.path.join(root, fn)).split("/")
            full = os.path.join(root, fn)
            try:
                with open(full, "r", encoding="utf-8", newline="") as fh:
                    rows = list(csv.DictReader(fh))
            except Exception:
                report["errors"].append(f"unreadable:{'/'.join(rel)}")
                continue
            if "candles" in rel and rows:
                sym = (rows[0].get("symbol") or "SYMBOL").upper()
                tf = _norm_tf(rows[0].get("timeframe") or "") or "1h"
                out_rows = [{"timestamp": r.get("timestamp_ms"), "open": r.get("open"),
                             "high": r.get("high"), "low": r.get("low"),
                             "close": r.get("close"), "volume": r.get("volume_base")}
                            for r in rows]
                name = f"{sym}_{tf}_ohlcv.csv"
                if apply:
                    _write_csv(os.path.join(out_dir, name),
                               ["timestamp", "open", "high", "low", "close", "volume"],
                               out_rows)
                report["written"].append(name)
            elif "funding" in rel and rows:
                sym = (rows[0].get("symbol") or "SYMBOL").upper()
                out_rows = [{"timestamp": r.get("funding_time_ms"),
                             "funding_rate": r.get("funding_rate")} for r in rows]
                name = f"{sym}_funding.csv"
                if apply:
                    _write_csv(os.path.join(out_dir, name),
                               ["timestamp", "funding_rate"], out_rows)
                report["written"].append(name)
            elif "oi_snapshot" in rel:
                report["skipped"].append("oi_snapshot (single-point, not a series)")
    report["note"] = ("validate with: provider-sample-validate-v106 --sample-dir "
                      f"{report['sample_dir']} --expected-days 30 --provider bitget_official")
    return report


# --------------------------------------------------------------------------
# H. Collector status
# --------------------------------------------------------------------------

def _latest_staging_dir(staging_root: str = STAGING_ROOT) -> str:
    try:
        if not os.path.isdir(staging_root):
            return ""
        subs = [d for d in os.listdir(staging_root)
                if os.path.isdir(os.path.join(staging_root, d))]
        return os.path.join(staging_root, sorted(subs)[-1]).replace("\\", "/") if subs else ""
    except Exception:
        return ""


def collector_status_v107() -> dict[str, Any]:
    impl = [e.name for e in build_endpoint_registry() if e.implemented]
    planned = [e.name for e in build_endpoint_registry() if not e.implemented]
    return {
        "data_source": DATA_SOURCE,
        "implemented_endpoints": impl,
        "planned_endpoints": planned,
        "free_data_available": ["candles (capped by timeframe)",
                                "historical funding (paged)",
                                "open interest snapshot (point-in-time)"],
        "still_missing": ["long historical open interest",
                          "complete historical liquidations",
                          "180/365d on low timeframes"],
        "latest_staging_dir": _latest_staging_dir(),
        "no_private_auth": True, "no_env": True, "public_get_only": True,
        "paper_ready": False, "live_ready": False,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }
