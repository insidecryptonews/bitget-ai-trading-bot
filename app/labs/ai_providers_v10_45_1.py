"""ResearchOps V10.45.1 - REAL AI providers (research only, NO LIVE).

Real, separate adapters for Ollama (localhost), Groq and Gemini plus the
deterministic Mock baseline. Hard safety contract:

  * keys come ONLY from os.environ (GROQ_API_KEY / GEMINI_API_KEY); .env is
    never read or written; keys are never printed, logged, stored, cached,
    embedded in reports or sent to any model as content;
  * every error string is sanitized so a key can never leak through an
    exception message;
  * per-provider rate limiter + capped exponential backoff with jitter,
    Retry-After respected on 429; a provider that keeps failing is paused,
    never silently swapped for another;
  * disk cache keyed by (provider, model, prompt-hash, version) so repeated
    prompts cost zero quota; cache lives under external_data/staging
    (gitignored);
  * request budget per run with a 20% quota reserve.

These adapters generate/critique RESEARCH hypotheses only. Nothing here can
touch orders, exchanges, leverage or live flags.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import continuous_edge_factory_v10_38 as CE

TOOL_VERSION = "v10.45.3"
CACHE_SUBDIR = ("external_data", "staging", "ai_cache_v10_45_1")
OUTPUT_SUBDIR = ("reports", "research", "v10_45_3_edge_discovery")

OLLAMA_BASE = "http://localhost:11434"
GROQ_BASE = "https://api.groq.com/openai/v1"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

DEFAULT_TIMEOUT_S = 180
MAX_RETRIES = 3
BACKOFF_BASE_S = 2.0
QUOTA_RESERVE = 0.20          # keep 20% of the per-run budget unused

# preferred small/fast models first (CPU-friendly for ollama; cheap for cloud)
OLLAMA_PREFERRED = ("qwen2.5:7b", "qwen3:8b", "qwen2.5-coder:7b", "qwen3:14b")
GROQ_PREFERRED = ("llama-3.3-70b-versatile", "llama-3.1-8b-instant",
                  "openai/gpt-oss-20b", "gemma2-9b-it")
# 2026-07: gemini-2.5-flash returns 404 for new users and gemini-2.0-flash
# 429s on the free tier; the -latest aliases are the reliable entry points.
GEMINI_PREFERRED = ("gemini-flash-latest", "gemini-flash-lite-latest",
                    "gemini-2.5-flash", "gemini-2.0-flash")


def _safety() -> dict[str, Any]:
    return {"research_only": True, "simulation_only": True,
            "can_send_real_orders": False, "no_orders": True,
            "keys_env_only": True, "never_reads_dotenv": True,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


MAX_RETRY_AFTER_S = 120.0     # never sleep hours because of a malicious header

_SANITIZE_PATTERNS = (
    # JSON credential fields: "api_key": "...", "token": '...', "secret": ...
    (re.compile(r'("?(?:api[_-]?key|apikey|token|secret|password|passwd|pwd|'
                r'authorization|auth|credential[s]?|access[_-]?key)"?\s*[:=]\s*)'
                r'("[^"]*"|\'[^\']*\'|\S+)', re.I), r"\1<redacted>"),
    # Authorization / Bearer headers in any case
    (re.compile(r"(bearer|basic)\s+\S+", re.I), r"\1 <redacted>"),
    # query strings: ?key=...&token=... (any case)
    (re.compile(r"([?&](?:key|apikey|api_key|token|secret|signature|sign|"
                r"password)=)[^&\s\"']+", re.I), r"\1<redacted>"),
    # long opaque blobs (keys are long); keep after targeted rules
    (re.compile(r"[A-Za-z0-9_\-]{28,}"), "<redacted>"),
)


def sanitize_error(msg: str) -> str:
    """Credential-safe error text. Applies layered case-insensitive redaction
    (key=value, JSON fields, Bearer/Basic headers, query strings, long opaque
    blobs) and caps the length. Prefer building messages from the allowlist
    (provider, HTTP status, error class) and passing bodies through here."""
    s = str(msg)[:400]
    for pat, repl in _SANITIZE_PATTERNS:
        s = pat.sub(repl, s)
    return s


def parse_retry_after(value: str | None) -> float | None:
    """RFC-compliant Retry-After: numeric seconds OR an HTTP-date. Invalid or
    missing values return None (caller falls back to exponential backoff)."""
    if not value:
        return None
    v = str(value).strip()
    try:
        secs = float(v)
        return max(0.0, secs) if secs == secs and secs != float("inf") else None
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


def _cache_dir() -> Path:
    d = CE._repo_root().joinpath(*CACHE_SUBDIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_key(provider: str, model: str, prompt: str, version: str = TOOL_VERSION) -> str:
    h = hashlib.sha256(f"{provider}|{model}|{version}|{prompt}".encode("utf-8")).hexdigest()
    return h[:32]


def cache_get(provider: str, model: str, prompt: str) -> str | None:
    p = _cache_dir() / f"{provider}_{_cache_key(provider, model, prompt)}.json"
    if not p.is_file():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        return obj.get("response")
    except Exception:
        return None


def cache_put(provider: str, model: str, prompt: str, response: str) -> None:
    p = _cache_dir() / f"{provider}_{_cache_key(provider, model, prompt)}.json"
    try:
        p.write_text(json.dumps({"provider": provider, "model": model,
                                 "cached_at": datetime.now(timezone.utc).isoformat(),
                                 "prompt_sha": _cache_key(provider, model, prompt),
                                 "response": response}), encoding="utf-8")
    except Exception:
        pass


def _http_json(url: str, payload: dict | None = None, headers: dict | None = None,
               timeout: int = DEFAULT_TIMEOUT_S, method: str | None = None
               ) -> tuple[int, dict | None, dict]:
    """Minimal stdlib HTTP JSON call. Returns (status, body_json|None, resp_headers)."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            try:
                return resp.status, json.loads(body), hdrs
            except Exception:
                return resp.status, None, hdrs
    except urllib.error.HTTPError as e:
        hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
        try:
            body = json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:
            body = None
        return e.code, body, hdrs
    except Exception as e:
        return 0, {"_transport_error": sanitize_error(str(e))}, {}


class _RateLimiter:
    """Simple per-provider min-interval limiter + pause-until support."""

    def __init__(self, min_interval_s: float):
        self.min_interval_s = min_interval_s
        self._last = 0.0
        self.paused_until = 0.0

    def wait(self) -> bool:
        now = time.time()
        if now < self.paused_until:
            return False                      # provider paused (429 storm)
        delta = now - self._last
        if delta < self.min_interval_s:
            time.sleep(self.min_interval_s - delta)
        self._last = time.time()
        return True

    def pause(self, seconds: float) -> None:
        self.paused_until = time.time() + max(1.0, seconds)


class BaseProvider:
    name = "base"

    def __init__(self, max_requests: int = 20):
        self.max_requests = max_requests
        self.requests_used = 0
        self.fallback_events: list[str] = []

    def budget_left(self) -> bool:
        return self.requests_used < int(self.max_requests * (1 - QUOTA_RESERVE))

    def generate(self, prompt: str, temperature: float = 0.7,
                 use_cache: bool = True) -> dict[str, Any]:
        raise NotImplementedError


class MockProvider(BaseProvider):
    """Deterministic offline baseline; always available; zero cost."""
    name = "mock"
    model = "deterministic-rules"
    available = True

    def generate(self, prompt: str, temperature: float = 0.0,
                 use_cache: bool = True) -> dict[str, Any]:
        self.requests_used += 1
        return {"ok": True, "provider": self.name, "model": self.model,
                "text": "{}", "latency_s": 0.0, "cached": False}


class OllamaProvider(BaseProvider):
    """Local open-weights via the Ollama daemon. No internet, no keys."""
    name = "ollama"

    def __init__(self, model: str | None = None, max_requests: int = 60,
                 timeout_s: int = 480, num_predict: int = 1400):
        # CPU inference of a 7B model needs minutes for >1k tokens; 180s
        # timed out on every generation role in the first real run
        super().__init__(max_requests)
        self.timeout_s = timeout_s
        self.num_predict = num_predict
        self.models = self._discover()
        self.available = bool(self.models)
        self.model = model or self._pick(self.models)
        self.rl = _RateLimiter(0.5)

    @staticmethod
    def _discover() -> list[str]:
        status, body, _ = _http_json(f"{OLLAMA_BASE}/api/tags", timeout=5)
        if status != 200 or not body:
            return []
        return [m.get("name", "") for m in body.get("models", []) if m.get("name")]

    @staticmethod
    def model_digests() -> dict[str, str]:
        """Model -> content digest for provenance (best effort)."""
        status, body, _ = _http_json(f"{OLLAMA_BASE}/api/tags", timeout=5)
        if status != 200 or not body:
            return {}
        return {m.get("name", ""): str(m.get("digest", ""))[:19]
                for m in body.get("models", []) if m.get("name")}

    @staticmethod
    def _pick(models: list[str]) -> str | None:
        for pref in OLLAMA_PREFERRED:
            if pref in models:
                return pref
        return models[0] if models else None

    def generate(self, prompt: str, temperature: float = 0.7,
                 use_cache: bool = True) -> dict[str, Any]:
        if not self.available or not self.model:
            return {"ok": False, "provider": self.name, "error": "OLLAMA_NOT_RUNNING"}
        if use_cache:
            hit = cache_get(self.name, self.model, prompt)
            if hit is not None:
                return {"ok": True, "provider": self.name, "model": self.model,
                        "text": hit, "latency_s": 0.0, "cached": True}
        if not self.budget_left():
            return {"ok": False, "provider": self.name, "error": "BUDGET_EXHAUSTED"}
        self.rl.wait()
        t0 = time.time()
        payload = {"model": self.model, "prompt": prompt, "stream": False,
                   "format": "json",
                   "options": {"temperature": temperature,
                               "num_predict": self.num_predict},
                   "think": False}          # qwen3: disable slow thinking mode
        status, body, _ = _http_json(f"{OLLAMA_BASE}/api/generate", payload,
                                     timeout=self.timeout_s)
        self.requests_used += 1
        if status != 200 or not body or "response" not in body:
            err = sanitize_error(json.dumps((body or {}))[:200])
            return {"ok": False, "provider": self.name, "error": f"HTTP_{status}: {err}"}
        text = body["response"]
        if use_cache:
            cache_put(self.name, self.model, prompt, text)
        return {"ok": True, "provider": self.name, "model": self.model,
                "text": text, "latency_s": round(time.time() - t0, 2), "cached": False}


class GroqProvider(BaseProvider):
    """Groq cloud (openai-compatible). Key ONLY from env GROQ_API_KEY."""
    name = "groq"
    ENV = "GROQ_API_KEY"

    def __init__(self, model: str | None = None, max_requests: int = 20,
                 timeout_s: int = 60):
        super().__init__(max_requests)
        self.timeout_s = timeout_s
        self._key_present = bool(os.environ.get(self.ENV, ""))
        self.models: list[str] = []
        self.model = model
        self.available = False
        self.rl = _RateLimiter(2.5)          # ~24 RPM worst-case safe
        self.last_ratelimit: dict[str, str] = {}
        if self._key_present:
            self.models = self._discover()
            self.available = bool(self.models)
            if self.model is None:
                self.model = self._pick(self.models)

    @staticmethod
    def _headers() -> dict:
        return {"Authorization": f"Bearer {os.environ.get(GroqProvider.ENV, '')}"}

    def _discover(self) -> list[str]:
        status, body, _ = _http_json(f"{GROQ_BASE}/models", headers=self._headers(),
                                     timeout=15)
        self.discover_status = status
        if status != 200 or not body:
            return []
        return [m.get("id", "") for m in body.get("data", []) if m.get("id")]

    def unavailable_reason(self) -> str:
        """Never attributes a cause the server did not state."""
        if not self._key_present:
            return "MISSING_API_KEY"
        st = getattr(self, "discover_status", None)
        if st == 403:
            return "GROQ_FORBIDDEN_CAUSE_UNKNOWN"
        if st == 401:
            return "GROQ_UNAUTHORIZED_401"
        return f"NO_MODELS_DISCOVERED_HTTP_{st}"

    @staticmethod
    def _pick(models: list[str]) -> str | None:
        for pref in GROQ_PREFERRED:
            if pref in models:
                return pref
        chat = [m for m in models if "whisper" not in m and "tts" not in m
                and "guard" not in m]
        return chat[0] if chat else None

    def generate(self, prompt: str, temperature: float = 0.7,
                 use_cache: bool = True) -> dict[str, Any]:
        if not self._key_present:
            return {"ok": False, "provider": self.name, "error": "MISSING_API_KEY"}
        if not self.available or not self.model:
            return {"ok": False, "provider": self.name, "error": "NO_MODELS_DISCOVERED"}
        if use_cache:
            hit = cache_get(self.name, self.model, prompt)
            if hit is not None:
                return {"ok": True, "provider": self.name, "model": self.model,
                        "text": hit, "latency_s": 0.0, "cached": True}
        if not self.budget_left():
            return {"ok": False, "provider": self.name, "error": "BUDGET_EXHAUSTED"}
        payload = {"model": self.model, "temperature": temperature,
                   "response_format": {"type": "json_object"},
                   "messages": [
                       {"role": "system", "content":
                        "You are a quantitative RESEARCH assistant. Simulation only; "
                        "never suggest real orders. Reply with STRICT JSON only."},
                       {"role": "user", "content": prompt}]}
        for attempt in range(MAX_RETRIES):
            if not self.budget_left():           # budget gate on EVERY attempt
                return {"ok": False, "provider": self.name, "error": "BUDGET_EXHAUSTED"}
            if not self.rl.wait():
                return {"ok": False, "provider": self.name, "error": "PROVIDER_PAUSED_429"}
            t0 = time.time()
            status, body, hdrs = _http_json(f"{GROQ_BASE}/chat/completions", payload,
                                            headers=self._headers(),
                                            timeout=self.timeout_s)
            self.requests_used += 1
            self.last_ratelimit = {k: v for k, v in hdrs.items()
                                   if k.startswith("x-ratelimit")}
            if status == 200 and body:
                try:
                    text = body["choices"][0]["message"]["content"]
                except Exception:
                    return {"ok": False, "provider": self.name, "error": "BAD_RESPONSE_SHAPE"}
                if use_cache:
                    cache_put(self.name, self.model, prompt, text)
                return {"ok": True, "provider": self.name, "model": self.model,
                        "text": text, "latency_s": round(time.time() - t0, 2),
                        "cached": False, "ratelimit": self.last_ratelimit}
            if status == 429:
                raw_ra = hdrs.get("retry-after")
                retry_after = parse_retry_after(raw_ra)
                wait = retry_after if retry_after is not None else \
                    (BACKOFF_BASE_S * (2 ** attempt) + random.uniform(0, 1))
                capped = min(wait, MAX_RETRY_AFTER_S)
                self.fallback_events.append(
                    f"groq 429 attempt={attempt} raw_type="
                    f"{'numeric' if (raw_ra or '').strip().replace('.', '', 1).isdigit() else ('http-date' if retry_after is not None and raw_ra else 'absent/invalid')} "
                    f"parsed={None if retry_after is None else round(retry_after, 1)} "
                    f"capped={capped:.1f}s")
                if attempt == MAX_RETRIES - 1:
                    self.rl.pause(min(max(retry_after or 0, 60), MAX_RETRY_AFTER_S))
                    return {"ok": False, "provider": self.name, "error": "RATE_LIMITED_429"}
                time.sleep(capped)
                continue
            err = sanitize_error(json.dumps((body or {}))[:200])
            return {"ok": False, "provider": self.name, "error": f"HTTP_{status}: {err}"}
        return {"ok": False, "provider": self.name, "error": "RETRIES_EXHAUSTED"}


class GeminiProvider(BaseProvider):
    """Google Gemini. Key ONLY from env GEMINI_API_KEY (sent as header, never
    printed)."""
    name = "gemini"
    ENV = "GEMINI_API_KEY"

    def __init__(self, model: str | None = None, max_requests: int = 15,
                 timeout_s: int = 60):
        super().__init__(max_requests)
        self.timeout_s = timeout_s
        self._key_present = bool(os.environ.get(self.ENV, ""))
        self.models: list[str] = []
        self.model = model
        self.available = False
        self.rl = _RateLimiter(6.5)          # free tier ~10 RPM -> stay under
        if self._key_present:
            self.models = self._discover()
            self.available = bool(self.models)
            if self.model is None:
                self.model = self._pick(self.models)

    @staticmethod
    def _headers() -> dict:
        return {"x-goog-api-key": os.environ.get(GeminiProvider.ENV, "")}

    def _discover(self) -> list[str]:
        status, body, _ = _http_json(f"{GEMINI_BASE}/models", headers=self._headers(),
                                     timeout=15)
        if status != 200 or not body:
            return []
        out = []
        for m in body.get("models", []):
            name = str(m.get("name", "")).replace("models/", "")
            if "generateContent" in (m.get("supportedGenerationMethods") or []):
                out.append(name)
        return out

    @staticmethod
    def _pick(models: list[str]) -> str | None:
        for pref in GEMINI_PREFERRED:
            if pref in models:
                return pref
        flash = [m for m in models if "flash" in m and "image" not in m
                 and "live" not in m and "tts" not in m]
        return flash[0] if flash else (models[0] if models else None)

    def generate(self, prompt: str, temperature: float = 0.7,
                 use_cache: bool = True) -> dict[str, Any]:
        if not self._key_present:
            return {"ok": False, "provider": self.name, "error": "MISSING_API_KEY"}
        if not self.available or not self.model:
            return {"ok": False, "provider": self.name, "error": "NO_MODELS_DISCOVERED"}
        if use_cache:
            hit = cache_get(self.name, self.model, prompt)
            if hit is not None:
                return {"ok": True, "provider": self.name, "model": self.model,
                        "text": hit, "latency_s": 0.0, "cached": True}
        if not self.budget_left():
            return {"ok": False, "provider": self.name, "error": "BUDGET_EXHAUSTED"}
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature,
                                 "responseMimeType": "application/json"},
            "systemInstruction": {"parts": [{"text":
                "You are a quantitative RESEARCH assistant. Simulation only; never "
                "suggest real orders. Reply with STRICT JSON only."}]}}
        url = f"{GEMINI_BASE}/models/{self.model}:generateContent"
        for attempt in range(MAX_RETRIES):
            if not self.budget_left():           # budget gate on EVERY attempt
                return {"ok": False, "provider": self.name, "error": "BUDGET_EXHAUSTED"}
            if not self.rl.wait():
                return {"ok": False, "provider": self.name, "error": "PROVIDER_PAUSED_429"}
            t0 = time.time()
            status, body, hdrs = _http_json(url, payload, headers=self._headers(),
                                            timeout=self.timeout_s)
            self.requests_used += 1
            if status == 200 and body:
                try:
                    text = body["candidates"][0]["content"]["parts"][0]["text"]
                except Exception:
                    return {"ok": False, "provider": self.name, "error": "BAD_RESPONSE_SHAPE"}
                if use_cache:
                    cache_put(self.name, self.model, prompt, text)
                return {"ok": True, "provider": self.name, "model": self.model,
                        "text": text, "latency_s": round(time.time() - t0, 2),
                        "cached": False}
            if status == 429:
                raw_ra = hdrs.get("retry-after")
                retry_after = parse_retry_after(raw_ra)
                wait = retry_after if retry_after is not None else \
                    (BACKOFF_BASE_S * (2 ** attempt) + random.uniform(0, 1))
                capped = min(wait, MAX_RETRY_AFTER_S)
                self.fallback_events.append(
                    f"gemini 429 attempt={attempt} "
                    f"parsed={None if retry_after is None else round(retry_after, 1)} "
                    f"capped={capped:.1f}s")
                if attempt == MAX_RETRIES - 1:
                    self.rl.pause(min(max(retry_after or 0, 60), MAX_RETRY_AFTER_S))
                    return {"ok": False, "provider": self.name, "error": "RATE_LIMITED_429"}
                time.sleep(capped)
                continue
            err = sanitize_error(json.dumps((body or {}))[:200])
            return {"ok": False, "provider": self.name, "error": f"HTTP_{status}: {err}"}
        return {"ok": False, "provider": self.name, "error": "RETRIES_EXHAUSTED"}


# ==========================================================================
# Env detection (bool only) + connectivity test
# ==========================================================================

def env_key_status() -> dict[str, Any]:
    """Booleans ONLY — the values are never read into the report."""
    return {"GROQ_API_KEY_detected": bool(os.environ.get("GROQ_API_KEY", "")),
            "GEMINI_API_KEY_detected": bool(os.environ.get("GEMINI_API_KEY", "")),
            "env_not_inherited": not (bool(os.environ.get("GROQ_API_KEY", ""))
                                      or bool(os.environ.get("GEMINI_API_KEY", ""))),
            "note": "values never printed/stored; restart host process if False"}


def connectivity_test(write_reports: bool = True) -> dict[str, Any]:
    """Small REAL connectivity probe: one tiny call per available provider."""
    tiny = ('Reply with exactly this JSON: {"ping": "pong", "sim_only": true}')
    results = []
    oll = OllamaProvider(max_requests=3, timeout_s=120, num_predict=64)
    groq = GroqProvider(max_requests=3)
    gem = GeminiProvider(max_requests=3)
    for prov in (oll, groq, gem):
        if not getattr(prov, "available", False):
            if prov.name == "ollama":
                reason = "OLLAMA_NOT_RUNNING"
            elif hasattr(prov, "unavailable_reason"):
                reason = prov.unavailable_reason()
            elif not getattr(prov, "_key_present", True):
                reason = "MISSING_API_KEY"
            else:
                reason = "NO_MODELS_DISCOVERED"
            results.append({"provider": prov.name, "available": False,
                            "model": None, "latency_s": None, "error": reason})
            continue
        r = prov.generate(tiny, temperature=0.0, use_cache=False)
        ok = bool(r.get("ok"))
        parsed = None
        if ok:
            try:
                parsed = json.loads(r["text"])
            except Exception:
                parsed = None
        entry = {"provider": prov.name, "available": ok,
                 "model": getattr(prov, "model", None),
                 "models_discovered": getattr(prov, "models", [])[:25],
                 "latency_s": r.get("latency_s"),
                 "json_ok": isinstance(parsed, dict),
                 "ratelimit_headers": {k: v for k, v in
                                       (r.get("ratelimit") or {}).items()},
                 "error": sanitize_error(r.get("error", "")) if not ok else None}
        if prov.name == "ollama":
            entry["model_digests"] = OllamaProvider.model_digests()
        results.append(entry)
    report = {"tool_version": TOOL_VERSION,
              "ran_at": datetime.now(timezone.utc).isoformat(),
              "env": env_key_status(), "providers": results,
              "mock_always_available": True, **_safety()}
    if write_reports:
        out = CE._repo_root().joinpath(*OUTPUT_SUBDIR)
        out.mkdir(parents=True, exist_ok=True)
        tmp = out / "provider_connectivity_v10_45_1.json.tmp"
        tmp.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, out / "provider_connectivity_v10_45_1.json")
    return report


def build_providers(ollama_budget: int = 60, groq_budget: int = 20,
                    gemini_budget: int = 15) -> dict[str, BaseProvider]:
    """All providers that are actually available right now."""
    out: dict[str, BaseProvider] = {"mock": MockProvider()}
    oll = OllamaProvider(max_requests=ollama_budget)
    if oll.available:
        out["ollama"] = oll
    groq = GroqProvider(max_requests=groq_budget)
    if groq.available:
        out["groq"] = groq
    gem = GeminiProvider(max_requests=gemini_budget)
    if gem.available:
        out["gemini"] = gem
    return out
