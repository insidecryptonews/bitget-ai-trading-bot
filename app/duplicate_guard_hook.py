"""ResearchOps V7.5 — Hook research-safe que envuelve `FeatureLogger`.

Conecta el `duplicate_guard.fingerprint()` con el writer real de
`signal_observations` (en `app.feature_logger.FeatureLogger.record_observation`).

Modos:
  - `mode="audit"` (por defecto): NO bloquea inserciones. Solo mantiene un
    contador en memoria + registra `would_block_count` por símbolo/setup. Es
    seguro como primera línea para diagnosticar magnitud real en VPS.
  - `mode="enforce"`: bloquea inserciones duplicadas con la misma huella.
    Registra el bloqueo opcionalmente en la tabla aditiva `events` para
    auditoría, sin tocar `signal_observations`.

Hard rules:
  - flag `ENABLE_DUPLICATE_GUARD_HOOK=False` por defecto.
  - sin migraciones destructivas.
  - sin DELETE / UPDATE sobre datos reales.
  - sin llamadas a Bitget privado.
  - sin órdenes.
  - reporta `mode`, `would_block_count`, `actual_block_count`, top-N motivos.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from threading import Lock
from typing import Any

from .duplicate_guard import GuardVerdict, evaluate, fingerprint


FINAL_RECOMMENDATION = "NO LIVE"


@dataclass
class DuplicateGuardHookStats:
    """Estadísticas acumuladas del hook desde el arranque del worker."""
    mode: str
    enabled: bool
    seen_count: int = 0
    new_count: int = 0
    would_block_count: int = 0
    actual_block_count: int = 0
    market_probe_count: int = 0
    trade_signal_count: int = 0
    reasons_top: dict[str, int] = field(default_factory=dict)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HookDecision:
    """Decisión tomada para una observación concreta."""
    allow_write: bool
    fingerprint: str
    duplicate_class: str
    reason: str
    mode: str
    would_block: bool
    actual_block: bool
    is_market_probe: bool
    is_trade_signal: bool


class DuplicateGuardHook:
    """Hook que envuelve `FeatureLogger.record_observation`.

    Es un singleton por proceso. Mantiene un `dict[str, fingerprint_count]`
    en memoria para detectar inserciones repetidas dentro de la ventana del
    worker. NO persiste estado en la DB.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        mode: str = "audit",
        max_reasons_tracked: int = 20,
    ) -> None:
        self._enabled = bool(enabled)
        self._mode = "enforce" if str(mode).lower() == "enforce" else "audit"
        self._seen: dict[str, int] = {}
        self._reasons: dict[str, int] = {}
        self._stats = DuplicateGuardHookStats(mode=self._mode, enabled=self._enabled)
        self._lock = Lock()
        self._max_reasons_tracked = int(max_reasons_tracked)

    # -- Public API ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def mode(self) -> str:
        return self._mode

    def decide(self, observation: dict[str, Any]) -> HookDecision:
        """Decide si la observación debe escribirse. Es seguro llamar incluso
        con el hook deshabilitado: en ese caso siempre allow_write=True y
        `would_block=False` (cero side-effects)."""
        fp = fingerprint(observation)
        verdict: GuardVerdict = evaluate(observation, last_seen=None)
        # Detectamos duplicate basándonos en memoria del propio hook (no DB).
        already_seen = False
        with self._lock:
            count = self._seen.get(fp, 0)
            already_seen = count > 0
            if self._enabled:
                # Solo contabilizamos como visto si el hook está activo.
                self._seen[fp] = count + 1
                self._stats.seen_count += 1
                if verdict.is_market_probe:
                    self._stats.market_probe_count += 1
                elif verdict.is_trade_signal:
                    self._stats.trade_signal_count += 1
                if not already_seen:
                    self._stats.new_count += 1
        is_duplicate = already_seen
        would_block = is_duplicate and self._enabled
        actual_block = would_block and self._mode == "enforce"
        # Mensaje claro. Si el hook no está activo no contamos nada.
        if not self._enabled:
            return HookDecision(
                allow_write=True,
                fingerprint=fp,
                duplicate_class="HOOK_DISABLED",
                reason="hook_disabled_no_check_performed",
                mode=self._mode,
                would_block=False,
                actual_block=False,
                is_market_probe=verdict.is_market_probe,
                is_trade_signal=verdict.is_trade_signal,
            )
        if not is_duplicate:
            return HookDecision(
                allow_write=True,
                fingerprint=fp,
                duplicate_class="NEW",
                reason="first_occurrence_in_window",
                mode=self._mode,
                would_block=False,
                actual_block=False,
                is_market_probe=verdict.is_market_probe,
                is_trade_signal=verdict.is_trade_signal,
            )
        reason = "exact_duplicate_fingerprint_match_in_memory"
        with self._lock:
            self._stats.would_block_count += 1
            if actual_block:
                self._stats.actual_block_count += 1
            if len(self._reasons) < self._max_reasons_tracked or reason in self._reasons:
                self._reasons[reason] = self._reasons.get(reason, 0) + 1
            self._stats.reasons_top = dict(
                sorted(self._reasons.items(), key=lambda item: item[1], reverse=True)[:self._max_reasons_tracked]
            )
        return HookDecision(
            allow_write=not actual_block,
            fingerprint=fp,
            duplicate_class="EXACT_DUPLICATE",
            reason=reason,
            mode=self._mode,
            would_block=True,
            actual_block=actual_block,
            is_market_probe=verdict.is_market_probe,
            is_trade_signal=verdict.is_trade_signal,
        )

    def stats(self) -> DuplicateGuardHookStats:
        with self._lock:
            return DuplicateGuardHookStats(
                mode=self._stats.mode,
                enabled=self._stats.enabled,
                seen_count=self._stats.seen_count,
                new_count=self._stats.new_count,
                would_block_count=self._stats.would_block_count,
                actual_block_count=self._stats.actual_block_count,
                market_probe_count=self._stats.market_probe_count,
                trade_signal_count=self._stats.trade_signal_count,
                reasons_top=dict(self._stats.reasons_top),
            )

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()
            self._reasons.clear()
            self._stats = DuplicateGuardHookStats(mode=self._mode, enabled=self._enabled)


# -- Singleton helpers --------------------------------------------------------

_GLOBAL_HOOK: DuplicateGuardHook | None = None


def configure_global_hook(*, enabled: bool, mode: str) -> DuplicateGuardHook:
    """Configura el hook global. Pensado para usarse desde `main.py` al
    arrancar el worker."""
    global _GLOBAL_HOOK
    _GLOBAL_HOOK = DuplicateGuardHook(enabled=bool(enabled), mode=str(mode or "audit"))
    return _GLOBAL_HOOK


def get_global_hook() -> DuplicateGuardHook:
    """Devuelve el hook global. Si no se ha configurado, devuelve uno
    deshabilitado (modo audit)."""
    global _GLOBAL_HOOK
    if _GLOBAL_HOOK is None:
        _GLOBAL_HOOK = DuplicateGuardHook(enabled=False, mode="audit")
    return _GLOBAL_HOOK


def render_duplicate_guard_hook_stats_text(stats: DuplicateGuardHookStats) -> str:
    lines = [
        "DUPLICATE GUARD HOOK STATS START",
        f"enabled: {str(stats.enabled).lower()}",
        f"mode: {stats.mode}",
        f"seen_count: {stats.seen_count}",
        f"new_count: {stats.new_count}",
        f"would_block_count: {stats.would_block_count}",
        f"actual_block_count: {stats.actual_block_count}",
        f"trade_signal_count: {stats.trade_signal_count}",
        f"market_probe_count: {stats.market_probe_count}",
        "reasons_top:",
    ]
    for reason, count in stats.reasons_top.items():
        lines.append(f"- {reason}: {count}")
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "no_db_writes_from_hook: true",
        "final_recommendation: NO LIVE",
        "DUPLICATE GUARD HOOK STATS END",
    ])
    return "\n".join(lines)
