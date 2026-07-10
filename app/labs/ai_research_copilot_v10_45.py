"""ResearchOps V10.45 - AI Research Co-Pilot (research/simulation ONLY, NO LIVE).

Lets a free/cheap LLM act as a research assistant: propose strategy hypotheses,
critique V10.44 candidates, suggest testable features and summarize reports.
The model NEVER touches execution:

  * default provider is a deterministic local MOCK (no network, no keys);
  * real providers (gemini/groq/openrouter/cloudflare/ollama) are fail-closed:
    missing env key -> MISSING_API_KEY, never reads or writes .env;
  * every model output must parse as JSON and pass a strict whitelist schema;
    anything else -> REJECTED_AI_OUTPUT;
  * outputs containing order-like/dangerous language (BUY NOW, SEND_ORDER,
    LIVE_READY, api key requests...) -> REJECTED_DANGEROUS_AI_OUTPUT;
  * context sent to the model is sanitized against prompt injection and never
    includes keys, balances, .env content or private endpoints;
  * request budget per run is hard-capped.

AI ideas are IDEAS ONLY (AI_IDEA_ONLY): they enter the normal Alpha Factory /
Incubator gates like any other hypothesis. Nothing here can promote to live.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import continuous_edge_factory_v10_38 as CE

TOOL_VERSION = "v10.45"
OUTPUT_SUBDIR = ("reports", "research", "v10_45_ai_copilot")
MAX_REQUESTS_PER_RUN = 20
PROVIDERS = ("mock", "gemini", "groq", "openrouter", "cloudflare", "ollama")
ENV_KEYS = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY",
            "openrouter": "OPENROUTER_API_KEY", "cloudflare": "CLOUDFLARE_AI_TOKEN",
            "ollama": None}          # local daemon, no key
MODES = ("provider-audit", "propose", "critique", "feature-ideas", "report-summary")

SIDES = ("LONG", "SHORT", "BOTH", "NO_TRADE")
DECISIONS = ("NO_TRADE", "LONG_SIM", "SHORT_SIM", "CLOSE_SIM")
CONFIDENCE = ("LOW", "MEDIUM", "HIGH")

DANGEROUS_TOKENS = (
    "buy now", "sell now", "buy_now", "sell_now", "send order", "send_order",
    "place order", "place_order", "execute order", "real order", "live ready",
    "live_ready", "go live", "enable live", "disable paper", "market order now",
    "api key", "api_key", "secret key", "private key", "credentials",
    "increase leverage", "increase sizing", "bypass", "skip the gate")

INJECTION_PATTERNS = (
    r"ignore (all |any )?previous instructions", r"disregard (the )?rules",
    r"you are now", r"system prompt", r"forget (your|the) instructions",
    r"send (real )?orders", r"reveal .*key", r"print .*env")


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "simulation_only": True,
            "can_send_real_orders": False, "paper_filter_enabled": False,
            "edge_validated": False, "not_actionable": True, "no_orders": True,
            "uses_api_keys_from_env_only": True, "never_touches_dotenv": True,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _out() -> Path:
    return CE._repo_root().joinpath(*OUTPUT_SUBDIR)


# ==========================================================================
# Prompt-injection sanitizer + danger detector
# ==========================================================================

def sanitize_context(text: str) -> str:
    """Strip instruction-like lines from DATA before it reaches the model.
    Defense in depth: even if something slips through, outputs only ever pass
    through the whitelist JSON schema below — free text is never executed."""
    clean_lines = []
    for line in str(text).splitlines():
        low = line.lower()
        if any(re.search(p, low) for p in INJECTION_PATTERNS):
            continue
        clean_lines.append(line)
    return "\n".join(clean_lines)


def contains_dangerous_language(obj: Any) -> str | None:
    """Return the offending token if any string in the payload asks for orders,
    live trading or credentials."""
    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                r = walk(v)
                if r:
                    return r
        elif isinstance(x, (list, tuple)):
            for v in x:
                r = walk(v)
                if r:
                    return r
        elif isinstance(x, str):
            low = x.lower()
            for tok in DANGEROUS_TOKENS:
                if tok in low:
                    return tok
        return None
    return walk(obj)


# ==========================================================================
# Strict output schemas (whitelist; anything else is rejected)
# ==========================================================================

def validate_idea(obj: Any) -> tuple[str, dict | None]:
    """AI idea -> ("OK", idea) | ("REJECTED_AI_OUTPUT"|"REJECTED_DANGEROUS_AI_OUTPUT", None)."""
    if not isinstance(obj, dict):
        return "REJECTED_AI_OUTPUT", None
    tok = contains_dangerous_language(obj)
    if tok:
        return "REJECTED_DANGEROUS_AI_OUTPUT", None
    required = {"hypothesis_name": str, "side": str, "regime": str,
                "features_needed": list, "entry_logic_plaintext": str,
                "exit_logic_plaintext": str, "expected_failure_modes": list,
                "required_data": list, "test_plan": str, "risk_flags": list,
                "no_live_confirmation": bool}
    for k, t in required.items():
        if k not in obj or not isinstance(obj[k], t):
            return "REJECTED_AI_OUTPUT", None
    if obj["side"].upper() not in SIDES:
        return "REJECTED_AI_OUTPUT", None
    if obj["no_live_confirmation"] is not True:
        return "REJECTED_AI_OUTPUT", None
    idea = {k: obj[k] for k in required}
    idea["side"] = idea["side"].upper()
    idea["status"] = "AI_IDEA_ONLY"
    return "OK", idea


def validate_decision(obj: Any) -> tuple[str, dict | None]:
    """AI simulated-trader decision -> ("OK", decision) | (reject_reason, None)."""
    if not isinstance(obj, dict):
        return "REJECTED_AI_OUTPUT", None
    tok = contains_dangerous_language(obj)
    if tok:
        return "REJECTED_DANGEROUS_AI_OUTPUT", None
    dec = str(obj.get("decision", "")).upper()
    if dec not in DECISIONS:
        return "REJECTED_AI_OUTPUT", None
    conf = str(obj.get("confidence_bucket", "LOW")).upper()
    if conf not in CONFIDENCE:
        return "REJECTED_AI_OUTPUT", None
    if obj.get("no_live_confirmation") is not True:
        return "REJECTED_AI_OUTPUT", None
    plan = obj.get("exit_plan") or {}
    if dec in ("LONG_SIM", "SHORT_SIM"):
        if not isinstance(plan, dict):
            return "REJECTED_AI_OUTPUT", None
        try:
            tp = float(plan.get("tp_bps", 0))
            sl = float(plan.get("sl_bps", 0))
            hold = int(plan.get("max_hold_bars", 0))
        except (TypeError, ValueError):
            return "REJECTED_AI_OUTPUT", None
        if tp <= 0 or sl <= 0 or hold <= 0:
            return "REJECTED_AI_OUTPUT", None
    return "OK", {"decision": dec, "confidence_bucket": conf,
                  "entry_reason": str(obj.get("entry_reason", ""))[:240],
                  "exit_plan": {"tp_bps": max(10.0, min(200.0, float(plan.get("tp_bps", 0) or 0))),
                                "sl_bps": max(10.0, min(200.0, float(plan.get("sl_bps", 0) or 0))),
                                "max_hold_bars": max(1, min(120, int(plan.get("max_hold_bars", 0) or 0))),
                                "trailing": bool(plan.get("trailing", False))},
                  "risk_flags": [str(r)[:80] for r in (obj.get("risk_flags") or [])][:8],
                  "no_live_confirmation": True}


# ==========================================================================
# Providers (mock = default, deterministic, no network)
# ==========================================================================

def provider_status(provider: str) -> dict[str, Any]:
    provider = (provider or "mock").lower()
    if provider not in PROVIDERS:
        return {"provider": provider, "status": "UNKNOWN_PROVIDER"}
    if provider == "mock":
        return {"provider": "mock", "status": "READY", "network": False, "cost": 0.0}
    if provider == "ollama":
        return {"provider": "ollama", "status": "LOCAL_DAEMON_REQUIRED",
                "network": "localhost only", "cost": 0.0,
                "note": "needs `ollama serve` + a pulled model; fully private"}
    env = ENV_KEYS[provider]
    if not os.environ.get(env or "", ""):
        return {"provider": provider, "status": "MISSING_API_KEY",
                "env_var": env, "note": "fail-closed: no key in environment; "
                "never reads .env; mock provider still works"}
    return {"provider": provider, "status": "KEY_PRESENT_UNTESTED", "env_var": env}


class MockProvider:
    """Deterministic local 'model' so the whole pipeline is testable offline.
    Its trading style is a transparent momentum/flow rule — the point is to
    exercise the sandbox honestly, not to pretend intelligence."""

    name = "mock"

    def propose(self, context: str) -> str:
        return json.dumps({
            "hypothesis_name": "ai_mock_flow_momentum_v1",
            "side": "LONG",
            "regime": "trend",
            "features_needed": ["flow_imbalance_10", "ret_5m_prefix", "volume_z"],
            "entry_logic_plaintext": "enter long when 10-bar flow imbalance is strongly positive while 5m return is positive and volume above average",
            "exit_logic_plaintext": "tp 60bps, sl 40bps, max hold 30 bars",
            "expected_failure_modes": ["chop regime whipsaw", "costs dominate small moves"],
            "required_data": ["continuous 1m bars with buy/sell volume"],
            "test_plan": "alpha-factory style train/validation/test with costs and stress",
            "risk_flags": ["small sample", "regime dependence"],
            "no_live_confirmation": True})

    def critique(self, context: str) -> str:
        return json.dumps({
            "critique": [
                "test window is short; treat any positive EV as provisional",
                "overlapping trades overstate effective sample size",
                "cost stress at 0.35% round-trip likely erases thin edges"],
            "overfit_risk": "HIGH_ON_SMALL_SAMPLE",
            "no_live_confirmation": True})

    def feature_ideas(self, context: str) -> str:
        return json.dumps({
            "features": [
                {"name": "burst_asymmetry", "definition": "buy_volume z-score minus sell_volume z-score over 15 bars", "ex_ante": True},
                {"name": "wick_pressure", "definition": "rolling mean of upper_wick_pct - lower_wick_pct over 10 bars", "ex_ante": True},
                {"name": "quiet_then_burst", "definition": "compression below q30 followed by volume_z above q90", "ex_ante": True}],
            "no_live_confirmation": True})

    def summarize(self, context: str) -> str:
        return json.dumps({
            "summary": "research-only status; no validated edge; data continuity is the binding constraint",
            "no_live_confirmation": True})

    def decide(self, context: dict) -> str:
        f = context.get("features") or {}
        flow = float(f.get("flow_imbalance_10", 0) or 0)
        r5 = float(f.get("ret_5m_prefix", 0) or 0)
        vz = float(f.get("volume_z", 0) or 0)
        pos = context.get("position_state", "FLAT")
        if pos != "FLAT":
            held = int(context.get("bars_held", 0) or 0)
            if held >= 25:
                return json.dumps({"decision": "CLOSE_SIM", "confidence_bucket": "LOW",
                                   "entry_reason": "max mock patience reached",
                                   "exit_plan": {"tp_bps": 60, "sl_bps": 40,
                                                 "max_hold_bars": 30, "trailing": False},
                                   "risk_flags": [], "no_live_confirmation": True})
            return json.dumps({"decision": "NO_TRADE", "confidence_bucket": "LOW",
                               "entry_reason": "holding", "exit_plan": {},
                               "risk_flags": [], "no_live_confirmation": True})
        if flow > 0.30 and r5 > 0 and vz > 0.5:
            return json.dumps({"decision": "LONG_SIM", "confidence_bucket": "MEDIUM",
                               "entry_reason": "positive flow + momentum + volume",
                               "exit_plan": {"tp_bps": 60, "sl_bps": 40,
                                             "max_hold_bars": 30, "trailing": False},
                               "risk_flags": ["chop risk"], "no_live_confirmation": True})
        if flow < -0.30 and r5 < 0 and vz > 0.5:
            return json.dumps({"decision": "SHORT_SIM", "confidence_bucket": "MEDIUM",
                               "entry_reason": "negative flow + momentum + volume",
                               "exit_plan": {"tp_bps": 60, "sl_bps": 40,
                                             "max_hold_bars": 30, "trailing": False},
                               "risk_flags": ["squeeze risk"], "no_live_confirmation": True})
        return json.dumps({"decision": "NO_TRADE", "confidence_bucket": "LOW",
                           "entry_reason": "no aligned signal", "exit_plan": {},
                           "risk_flags": [], "no_live_confirmation": True})


def get_provider(provider: str):
    """mock -> MockProvider; real providers -> None unless key present (and even
    then live HTTP is only attempted inside _call_real, which is never reached
    without an explicit provider flag + env key)."""
    provider = (provider or "mock").lower()
    if provider == "mock":
        return MockProvider()
    return None            # real network providers deliberately not auto-built


# ==========================================================================
# Copilot run (propose / critique / feature-ideas / report-summary)
# ==========================================================================

def _research_context(symbol: str) -> str:
    """Small, sanitized, non-sensitive research context for the model."""
    rd = CE._repo_root().joinpath("reports", "research")
    parts = [f"symbol={symbol}", "mode=RESEARCH_ONLY", "live=NEVER"]
    for rel in (("v10_44_alpha_sprint", "alpha_factory_v10_44.json"),
                ("v10_44_alpha_sprint", "candidate_incubator_v10_44.json"),
                ("ws_continuity_v10_43c", "ws_continuity_audit_v1043c.json")):
        try:
            obj = json.loads((rd.joinpath(*rel)).read_text(encoding="utf-8"))
            keep = {k: obj.get(k) for k in ("overall_verdict", "verdict",
                                            "strategies_tested", "state_counts",
                                            "max_contiguous_run", "forward_coverage",
                                            "candidate_status_counts") if k in obj}
            parts.append(f"{rel[-1]}: {json.dumps(keep, default=str)[:400]}")
        except Exception:
            continue
    return sanitize_context("\n".join(parts))


def run_copilot(mode: str = "propose", provider: str = "mock",
                symbol: str = "BTCUSDT", write_reports: bool = True) -> dict[str, Any]:
    mode = (mode or "propose").lower()
    if mode not in MODES:
        return {"status": "UNKNOWN_MODE", "mode": mode, **_safety()}
    pstat = provider_status(provider)
    summary: dict[str, Any] = {"tool_version": TOOL_VERSION, "ran_at": _now(),
                               "mode": mode, "provider": pstat["provider"],
                               "provider_status": pstat["status"],
                               "symbol": symbol, "requests_used": 0,
                               "max_requests": MAX_REQUESTS_PER_RUN, **_safety()}
    if mode == "provider-audit":
        summary["providers"] = provider_audit()["providers"]
        summary["status"] = "OK"
        if write_reports:
            _write_json("ai_copilot_last_run_v10_45.json", summary)
        return summary
    p = get_provider(provider)
    if p is None:
        summary["status"] = pstat["status"]        # MISSING_API_KEY / not built
        summary["note"] = ("real providers are fail-closed; run with "
                           "--provider mock (default) for the offline copilot")
        if write_reports:
            _write_json("ai_copilot_last_run_v10_45.json", summary)
        return summary
    ctx = _research_context(symbol)
    summary["requests_used"] = 1
    ideas: list[dict] = []
    rejected: list[dict] = []
    try:
        if mode == "propose":
            raw = p.propose(ctx)
        elif mode == "critique":
            raw = p.critique(ctx)
        elif mode == "feature-ideas":
            raw = p.feature_ideas(ctx)
        else:
            raw = p.summarize(ctx)
    except Exception as exc:
        summary["status"] = "PROVIDER_ERROR"
        summary["detail"] = str(exc)[:200]
        if write_reports:
            _write_json("ai_copilot_last_run_v10_45.json", summary)
        return summary
    try:
        obj = json.loads(raw)
    except Exception:
        summary["status"] = "REJECTED_AI_OUTPUT"
        summary["detail"] = "not valid JSON"
        if write_reports:
            _write_json("ai_copilot_last_run_v10_45.json", summary)
        return summary
    if mode == "propose":
        verdict, idea = validate_idea(obj)
        if verdict == "OK":
            ideas.append(idea)
            summary["status"] = "OK"
        else:
            rejected.append({"reason": verdict})
            summary["status"] = verdict
    else:
        tok = contains_dangerous_language(obj)
        if tok:
            summary["status"] = "REJECTED_DANGEROUS_AI_OUTPUT"
            summary["detail"] = f"dangerous token: {tok}"
        elif obj.get("no_live_confirmation") is not True:
            summary["status"] = "REJECTED_AI_OUTPUT"
        else:
            summary["status"] = "OK"
            summary["output"] = obj
    summary["ideas"] = ideas
    summary["ideas_generated"] = len(ideas)
    summary["ideas_rejected"] = len(rejected)
    if write_reports:
        _write_json("ai_copilot_last_run_v10_45.json", summary)
        if ideas:
            _write_json("ai_ideas_v10_45.json",
                        {"ran_at": _now(), "provider": pstat["provider"],
                         "ideas": ideas, **_safety()})
    return summary


# ==========================================================================
# Provider audit (free/cheap AI research options; static knowledge, no network)
# ==========================================================================

def provider_audit() -> dict[str, Any]:
    providers = [
        {"provider": "mock (local deterministic)", "cost": "0",
         "free_tier": "unlimited", "rate_limits": "none", "latency": "0ms",
         "privacy": "perfect (no network)", "json_strict": "yes",
         "quality": "rule-based only", "python": "built-in",
         "fit": "DEFAULT for tests + simulated-trader replay",
         "status": provider_status("mock")["status"]},
        {"provider": "Gemini API (Google AI Studio)", "cost": "free tier",
         "free_tier": "~250-1500 req/day (model-dependent)", "rate_limits": "~10-15 RPM free",
         "latency": "low-medium", "privacy": "free tier MAY use data for training — send only public research summaries",
         "json_strict": "good (JSON mode)", "quality": "high",
         "python": "REST or google-genai", "fit": "batch propose/critique (few calls/day)",
         "status": provider_status("gemini")["status"]},
        {"provider": "Groq API", "cost": "free tier",
         "free_tier": "~14k req/day on small models", "rate_limits": "~30 RPM free",
         "latency": "very low (fast inference)", "privacy": "states no training on API data",
         "json_strict": "good", "quality": "open-weights (Llama et al.) = medium-high",
         "python": "REST/openai-compatible", "fit": "cheap frequent critique",
         "status": provider_status("groq")["status"]},
        {"provider": "OpenRouter :free models", "cost": "free models available",
         "free_tier": "~50-1000 req/day", "rate_limits": "~20 RPM",
         "latency": "variable", "privacy": "routed to third parties — treat as public",
         "json_strict": "model-dependent", "quality": "variable",
         "python": "openai-compatible", "fit": "model shopping / fallback",
         "status": provider_status("openrouter")["status"]},
        {"provider": "Cloudflare Workers AI", "cost": "free 10k neurons/day",
         "free_tier": "yes", "rate_limits": "generous for small models",
         "latency": "low", "privacy": "reasonable", "json_strict": "model-dependent",
         "quality": "small open models", "python": "REST",
         "fit": "lightweight summaries", "status": provider_status("cloudflare")["status"]},
        {"provider": "Ollama (local open-weights)", "cost": "0 (own hardware)",
         "free_tier": "unlimited", "rate_limits": "hardware-bound",
         "latency": "seconds on CPU", "privacy": "perfect (fully local)",
         "json_strict": "needs prompting/retries", "quality": "7-8B = medium",
         "python": "REST localhost", "fit": "unlimited private experimentation; "
         "the only realistic option for bar-by-bar decisions at scale",
         "status": provider_status("ollama")["status"]},
    ]
    return {"tool_version": TOOL_VERSION, "ran_at": _now(),
            "providers": providers,
            "recommendation": (
                "mock stays the default. For a handful of daily propose/critique "
                "calls: Gemini or Groq free tier. For bar-by-bar simulated "
                "trading (hundreds of calls): only mock or a local Ollama model "
                "is realistic — cloud free tiers rate-limit far below replay "
                "volume. Never send keys/balances; only public research summaries."),
            "budget_controls": {"max_requests_per_run": MAX_REQUESTS_PER_RUN,
                                "default_provider": "mock",
                                "real_providers_fail_closed": True},
            **_safety()}


def write_provider_audit_report() -> str:
    a = provider_audit()
    out = _out()
    out.mkdir(parents=True, exist_ok=True)
    lines = ["# V10.45 AI Provider Audit (research only, NO LIVE)", "",
             f"- ran_at: {a['ran_at']}", ""]
    for p in a["providers"]:
        lines.append(f"## {p['provider']}")
        for k in ("cost", "free_tier", "rate_limits", "latency", "privacy",
                  "json_strict", "quality", "python", "fit", "status"):
            lines.append(f"- {k}: {p[k]}")
        lines.append("")
    lines += ["## Recommendation", "", a["recommendation"], "",
              "Keys only from environment variables; .env is never read or "
              "written; providers without keys fail closed. NO LIVE."]
    (out / "provider_audit_v10_45.md").write_text("\n".join(lines) + "\n",
                                                  encoding="utf-8")
    _write_json("provider_audit_v10_45.json", a)
    return str(out).replace("\\", "/")


def _write_json(name: str, obj: dict) -> None:
    out = _out()
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / (name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, out / name)


def render_cli(summary: dict[str, Any]) -> str:
    lines = ["AI RESEARCH COPILOT V10.45 START",
             f"mode: {summary.get('mode')}",
             f"provider: {summary.get('provider')}",
             f"provider_status: {summary.get('provider_status')}",
             f"status: {summary.get('status')}",
             f"ideas_generated: {summary.get('ideas_generated', 0)}",
             f"ideas_rejected: {summary.get('ideas_rejected', 0)}",
             f"requests_used: {summary.get('requests_used', 0)}/{summary.get('max_requests')}",
             "ai_role: research/simulation assistant only",
             "no_orders: true", "never_touches_dotenv: true",
             "can_send_real_orders: false",
             "final_recommendation: NO LIVE",
             "AI RESEARCH COPILOT V10.45 END"]
    if summary.get("detail"):
        lines.insert(5, f"detail: {summary['detail']}")
    return "\n".join(lines)
