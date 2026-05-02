from __future__ import annotations

from dataclasses import dataclass, field

from .config import BotConfig
from .regime_detector import MarketRegime
from .signal_engine import Signal


@dataclass
class AllocationResult:
    selected_signals: list[Signal] = field(default_factory=list)
    rejected_signals: list[tuple[Signal, str]] = field(default_factory=list)
    reason: str = ""
    total_risk: float = 0.0
    correlation_warning: str = ""


class PortfolioAllocator:
    CORRELATION_GROUPS = {
        "BTCUSDT": "majors",
        "ETHUSDT": "majors",
        "SOLUSDT": "majors",
        "BNBUSDT": "majors",
        "DOGEUSDT": "alts_beta",
        "XRPUSDT": "alts_beta",
        "ADAUSDT": "alts_beta",
        "DOTUSDT": "alts_beta",
        "AVAXUSDT": "alts_beta",
        "LINKUSDT": "alts_beta",
    }

    def __init__(self, config: BotConfig) -> None:
        self.config = config

    def allocate(
        self,
        signals: list[Signal],
        *,
        balance: float,
        open_positions: list[dict] | None = None,
        regime: MarketRegime | None = None,
    ) -> AllocationResult:
        open_positions = open_positions or []
        result = AllocationResult()
        tradable = [s for s in signals if s.side != "NO_TRADE" and s.confidence_score >= self.config.min_score_to_trade]
        if not tradable:
            result.reason = "Todas las señales son mediocres o NO_TRADE"
            return result

        max_positions = self.config.max_open_positions
        if balance < 30:
            max_positions = 1
        elif balance <= 60:
            max_positions = self.config.small_account_max_open_positions
            if not self.config.allow_second_position_small_account:
                max_positions = 1
        if regime and regime.regime == "HIGH_VOLATILITY":
            max_positions = min(max_positions, 1)

        remaining_slots = max(0, max_positions - len(open_positions))
        if remaining_slots <= 0:
            result.reason = "Sin slots de posición disponibles"
            for signal in tradable:
                result.rejected_signals.append((signal, "sin slots"))
            return result

        ranked = sorted(tradable, key=self._risk_adjusted_score, reverse=True)
        best = ranked[0]
        if best.confidence_score >= 90:
            result.selected_signals = [best]
            result.reason = "Mejor señal >=90: concentrando en la oportunidad principal"
            for signal in ranked[1:]:
                result.rejected_signals.append((signal, "concentración en la mejor señal"))
            return result

        used_groups = {self.CORRELATION_GROUPS.get(p.get("symbol"), p.get("symbol")) for p in open_positions}
        for signal in ranked:
            if len(result.selected_signals) >= remaining_slots:
                result.rejected_signals.append((signal, "máximo de posiciones"))
                continue
            group = self.CORRELATION_GROUPS.get(signal.symbol, signal.symbol)
            same_direction_corr = any(
                self.CORRELATION_GROUPS.get(selected.symbol, selected.symbol) == group and selected.side == signal.side
                for selected in result.selected_signals
            )
            if group in used_groups or same_direction_corr:
                if signal.confidence_score < self.config.min_score_excellent:
                    result.rejected_signals.append((signal, "correlación alta"))
                    result.correlation_warning = "Se evitaron posiciones correlacionadas"
                    continue
            if len(result.selected_signals) == 1 and signal.confidence_score < self.config.min_score_excellent:
                result.rejected_signals.append((signal, "segunda señal no excelente"))
                continue
            result.selected_signals.append(signal)

        if result.selected_signals:
            result.reason = f"Seleccionadas {len(result.selected_signals)} señales por score ajustado y correlación"
        else:
            result.reason = "No se abrió nada tras filtro de correlación/riesgo"
        return result

    @staticmethod
    def _risk_adjusted_score(signal: Signal) -> float:
        rr_bonus = min(signal.risk_reward_ratio, 3.0) * 2.0
        warning_penalty = len(signal.warnings) * 4.0
        liquidity_bonus = 3.0 if signal.symbol in {"BTCUSDT", "ETHUSDT", "SOLUSDT"} else 0.0
        return signal.confidence_score + rr_bonus + liquidity_bonus - warning_penalty
