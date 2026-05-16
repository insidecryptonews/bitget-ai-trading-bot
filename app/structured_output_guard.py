from __future__ import annotations

import ast
import json
import math
import re
from typing import Any


START = "STRUCTURED OUTPUT GUARD SMOKE TEST START"
END = "STRUCTURED OUTPUT GUARD SMOKE TEST END"


class StructuredOutputGuard:
    """Small internal JSON guard for future LLM/news classifiers."""

    def parse(self, text: str, schema: dict[str, type] | None = None) -> dict[str, Any]:
        original = text or ""
        candidate = _extract_json(original)
        repaired = candidate != original.strip()
        errors: list[str] = []
        data: Any
        try:
            data = json.loads(candidate)
        except Exception:
            try:
                data = ast.literal_eval(_strip_trailing_commas(candidate))
                repaired = True
            except Exception as exc:
                return _result(False, repaired, [f"parse_error:{type(exc).__name__}"], None)
        if not isinstance(data, dict):
            return _result(False, repaired, ["not_object"], None)
        if _has_non_finite(data):
            return _result(False, repaired, ["non_finite_number"], None)
        sanitized = _sanitize(data)
        if schema:
            for key, expected in schema.items():
                if key not in sanitized:
                    errors.append(f"missing:{key}")
                elif not isinstance(sanitized[key], expected):
                    errors.append(f"type:{key}")
        valid = not errors
        final_decision = "ALLOW_PARSER_OUTPUT" if valid else "WATCH_ONLY_INVALID_OUTPUT"
        if valid and str(sanitized.get("decision") or "").upper().startswith("ALLOW"):
            final_decision = "WATCH_ONLY_REQUIRES_NON_LLM_CONFIRMATION"
        return {
            "valid": valid,
            "repaired": repaired,
            "errors": errors,
            "sanitized_data": sanitized if valid else {},
            "final_decision": final_decision,
        }


def smoke_test_text() -> str:
    guard = StructuredOutputGuard()
    valid = guard.parse("```json\n{'decision':'ALLOW','score': 1,}\n```", {"decision": str, "score": int})
    invalid = guard.parse('{"decision":"ALLOW","score": NaN}', {"decision": str, "score": float})
    result = bool(valid["valid"] and valid["repaired"] and not invalid["valid"] and "ALLOW" not in invalid["final_decision"])
    return "\n".join([
        START,
        f"valid_case_valid={str(valid['valid']).lower()}",
        f"valid_case_repaired={str(valid['repaired']).lower()}",
        f"invalid_case_valid={str(invalid['valid']).lower()}",
        f"invalid_cannot_allow={str('ALLOW' not in invalid['final_decision']).lower()}",
        "final_decision: NO LIVE",
        f"result: {'PASS' if result else 'FAIL'}",
        END,
    ])


def _extract_json(text: str) -> str:
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL | re.IGNORECASE)
    if fence:
        stripped = fence.group(1).strip()
    start_obj = stripped.find("{")
    end_obj = stripped.rfind("}")
    if start_obj >= 0 and end_obj >= start_obj:
        stripped = stripped[start_obj : end_obj + 1]
    return _strip_trailing_commas(stripped)


def _strip_trailing_commas(text: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", text.strip())


def _has_non_finite(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_has_non_finite(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_non_finite(item) for item in value)
    return False


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key)[:80]: _sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value[:50]]
    if isinstance(value, str):
        return re.sub(r"(?i)(api[_-]?key|secret|token|password|passphrase|private[_-]?key)\s*[:=]\s*[^\s,;]+", r"\1=***", value)[:1000]
    return value


def _result(valid: bool, repaired: bool, errors: list[str], data: Any) -> dict[str, Any]:
    return {
        "valid": valid,
        "repaired": repaired,
        "errors": errors,
        "sanitized_data": data or {},
        "final_decision": "WATCH_ONLY_INVALID_OUTPUT",
    }
