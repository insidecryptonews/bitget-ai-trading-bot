from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from .config import BotConfig
from .order_manager import InstrumentRules, OrderManager
from .signal_engine import Signal


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    position_size: float = 0.0
    notional: float = 0.0
    margin_required: float = 0.0
    risk_amount: float = 0.0
    real_risk: float = 0.0
    leverage: int = 0
    risk_based_notional: float = 0.0
    margin_based_notional: float = 0.0
    exposure_based_notional: float = 0.0
    selected_notional: float = 0.0
    available_balance: float = 0.0
    stop_distance_pct: float = 0.0
    used_margin: float = 0.0
    max_margin_per_trade: float = 0.0
    remaining_total_margin: float = 0.0
    margin_mode: str = "isolated"
    force_isolated_margin: bool = True
    auto_margin: bool = False
    trade_margin_usdt: float = 0.0
    max_trade_margin_usdt: float = 0.0
    selected_margin_usdt: float = 0.0
    entry_price: float = 0.0
    target_notional: float = 0.0
    raw_quantity: float = 0.0
    rounded_quantity: float = 0.0
    calculated_notional_after_rounding: float = 0.0
    notional_deviation_pct: float = 0.0
    notional_deviation_side: str = ""
    max_allowed_deviation_for_side: float = 0.0
    quantity_rounding_mode: str = ""
    quantity: float = 0.0
    new_required_margin: float = 0.0
    total_margin_after_trade: float = 0.0
    max_total_margin_allowed_after_buffer: float = 0.0
    open_positions_count: int = 0
    max_open_positions_allowed: int = 0
    risk_amount_estimated: float = 0.0
    risk_pct_estimated: float = 0.0
    block_reason: str = ""
    warnings: list[str] | None = None
    signal: Signal | None = None


class RiskManager:
    def __init__(self, config: BotConfig, order_manager: OrderManager | None = None, logger=None) -> None:
        self.config = config
        self.order_manager = order_manager
        self.logger = logger
        self.consecutive_losses = 0
        self.cooldown_until: datetime | None = None
        self.api_failure_count = 0

    def set_consecutive_losses(self, count: int) -> None:
        self.consecutive_losses = count
        if count >= self.config.max_consecutive_losses:
            self.cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=self.config.cooldown_after_losses_minutes)

    def register_api_failure(self) -> None:
        self.api_failure_count += 1

    def reset_api_failures(self) -> None:
        self.api_failure_count = 0

    def validate_signal(
        self,
        signal: Signal,
        *,
        balance: float,
        available_balance: float | None = None,
        open_positions: list[dict] | None = None,
        daily_pnl: float = 0.0,
        weekly_pnl: float = 0.0,
        rules: InstrumentRules | None = None,
        isolated_verified: bool = True,
        auto_margin_off_verified: bool = True,
    ) -> RiskDecision:
        open_positions = open_positions or []
        available_balance = balance if available_balance is None else available_balance
        warnings: list[str] = []

        early_block = self._preflight_block(
            signal=signal,
            balance=balance,
            open_positions=open_positions,
            daily_pnl=daily_pnl,
            weekly_pnl=weekly_pnl,
            isolated_verified=isolated_verified,
            auto_margin_off_verified=auto_margin_off_verified,
        )
        if early_block:
            return early_block

        if rules is None:
            return RiskDecision(False, "Reglas reales de instrumento no disponibles", block_reason="missing_instrument_rules")
        if not rules.is_active:
            return RiskDecision(False, f"Instrumento no activo: {rules.symbol_status}", block_reason="instrument_not_active")

        leverage = self._initial_leverage(signal, rules)
        sizing = self.calculate_position_size(
            balance=balance,
            available_balance=available_balance,
            signal=signal,
            rules=rules,
            leverage=leverage,
            open_positions=open_positions,
        )
        if not sizing.approved:
            self._log_sizing(signal.symbol, signal.side, sizing)
            return sizing

        if self.order_manager:
            ok, order_warnings = self.order_manager.validate_order(
                signal.symbol, sizing.position_size, signal.entry_price, sizing.leverage, "market"
            )
            warnings.extend(order_warnings)
            if not ok:
                blocked = replace(
                    sizing,
                    approved=False,
                    reason="Orden no cumple reglas Bitget: " + "; ".join(order_warnings),
                    block_reason="exchange_rules",
                )
                self._log_sizing(signal.symbol, signal.side, blocked)
                return blocked

        adjusted_signal = replace(
            signal,
            position_size=sizing.position_size,
            leverage_recommendation=sizing.leverage,
            estimated_fees=sizing.notional * rules.taker_fee_rate * 2,
            estimated_slippage=sizing.notional * 0.0003,
        )
        sizing.warnings = warnings
        sizing.signal = adjusted_signal
        self._log_sizing(signal.symbol, signal.side, sizing)
        return sizing

    def calculate_position_size(
        self,
        *,
        balance: float,
        available_balance: float,
        signal: Signal,
        rules: InstrumentRules,
        leverage: int,
        open_positions: list[dict] | None = None,
    ) -> RiskDecision:
        open_positions = open_positions or []
        risk_amount = balance * self.config.max_risk_per_trade
        stop_distance = abs(signal.entry_price - signal.stop_loss)
        stop_distance_pct = stop_distance / signal.entry_price if signal.entry_price > 0 else 0.0
        if stop_distance <= 0:
            return RiskDecision(False, "Distancia al stop invalida", block_reason="invalid_stop_distance")

        round_trip_fee_rate = rules.taker_fee_rate * 2
        slippage_rate = 0.0003
        cost_rate = round_trip_fee_rate + slippage_rate
        risk_based_notional = risk_amount / (stop_distance_pct + cost_rate)

        used_margin = self._used_margin(open_positions)
        max_positions_allowed = self._max_positions_for_balance(balance)
        max_total_margin_allowed_after_buffer = max(
            0.0,
            balance * self.config.max_total_margin_usage - float(self.config.margin_safety_buffer_usdt),
        )
        remaining_total_margin = max(0.0, max_total_margin_allowed_after_buffer - used_margin)
        max_margin_per_trade = min(
            float(self.config.max_trade_margin_usdt),
            available_balance * self.config.max_margin_usage_per_trade,
            remaining_total_margin,
        )
        free_margin_floor = self._min_free_margin_required(balance)
        max_margin_by_free_balance = max(0.0, available_balance - free_margin_floor)

        selected_margin = self._select_margin(max_margin_per_trade, max_margin_by_free_balance)
        base_kwargs = self._decision_base(
            balance=balance,
            available_balance=available_balance,
            open_positions=open_positions,
            risk_amount=risk_amount,
            risk_based_notional=risk_based_notional,
            margin_based_notional=max_margin_per_trade * leverage,
            exposure_based_notional=float(self.config.max_notional_per_trade_small_account),
            selected_notional=selected_margin * leverage,
            stop_distance_pct=stop_distance_pct,
            used_margin=used_margin,
            max_margin_per_trade=max_margin_per_trade,
            remaining_total_margin=remaining_total_margin,
            leverage=leverage,
            selected_margin=selected_margin,
            total_margin_after_trade=used_margin + selected_margin,
            max_total_margin_allowed_after_buffer=max_total_margin_allowed_after_buffer,
            open_positions_count=len(open_positions),
            max_open_positions_allowed=max_positions_allowed,
        )

        if self.config.use_fixed_trade_margin and float(self.config.trade_margin_usdt) > float(self.config.max_trade_margin_usdt):
            return RiskDecision(
                False,
                "TRADE_MARGIN_USDT supera MAX_TRADE_MARGIN_USDT",
                block_reason="trade_margin_above_max",
                **base_kwargs,
            )
        if selected_margin < float(self.config.min_trade_margin_usdt):
            return RiskDecision(
                False,
                "selected_margin_usdt menor que MIN_TRADE_MARGIN_USDT",
                block_reason="trade_margin_below_min",
                **base_kwargs,
            )
        if selected_margin > max_margin_per_trade + 1e-9:
            return RiskDecision(
                False,
                "selected_margin_usdt supera limite por trade",
                block_reason="trade_margin_cap",
                **base_kwargs,
            )
        if used_margin + selected_margin >= max_total_margin_allowed_after_buffer + 1e-9:
            return RiskDecision(
                False,
                "total_margin_after_trade supera max_total_margin_allowed_after_buffer",
                block_reason="total_margin_cap",
                **base_kwargs,
            )
        if available_balance - selected_margin < free_margin_floor - 1e-9:
            return RiskDecision(
                False,
                "free margin tras trade queda por debajo del minimo",
                block_reason="free_margin_floor",
                **base_kwargs,
            )
        if stop_distance_pct + 1e-12 < self.config.min_stop_distance_pct:
            return RiskDecision(
                False,
                (
                    "Stop demasiado estrecho para small account: "
                    f"stop_distance_pct={stop_distance_pct:.4%} < "
                    f"MIN_STOP_DISTANCE_PCT={self.config.min_stop_distance_pct:.4%}"
                ),
                block_reason="stop_too_tight",
                **base_kwargs,
            )

        leverage_attempts = self._leverage_attempts(leverage, rules)
        last_decision: RiskDecision | None = None
        for candidate_leverage in leverage_attempts:
            decision = self._size_for_leverage(
                signal=signal,
                rules=rules,
                leverage=candidate_leverage,
                selected_margin=selected_margin,
                risk_amount=risk_amount,
                stop_distance=stop_distance,
                cost_rate=cost_rate,
                base_kwargs={**base_kwargs, "leverage": candidate_leverage},
            )
            if decision.approved:
                return decision
            if decision.block_reason != "risk_above_limit":
                return decision
            last_decision = decision

        return last_decision or RiskDecision(False, "No se pudo calcular tamaño seguro", **base_kwargs)

    def _size_for_leverage(
        self,
        *,
        signal: Signal,
        rules: InstrumentRules,
        leverage: int,
        selected_margin: float,
        risk_amount: float,
        stop_distance: float,
        cost_rate: float,
        base_kwargs: dict,
    ) -> RiskDecision:
        target_notional = selected_margin * leverage
        if self._is_small_account_from_margin(selected_margin) and target_notional > self.config.max_notional_per_trade_small_account:
            target_notional = self.config.max_notional_per_trade_small_account

        raw_quantity = target_notional / signal.entry_price
        candidate_decisions = [
            self._evaluate_quantity_candidate(
                signal=signal,
                rules=rules,
                leverage=leverage,
                target_notional=target_notional,
                raw_quantity=raw_quantity,
                quantity=quantity,
                mode=mode,
                selected_margin=selected_margin,
                risk_amount=risk_amount,
                stop_distance=stop_distance,
                cost_rate=cost_rate,
                base_kwargs=base_kwargs,
            )
            for mode, quantity in self._quantity_candidates(raw_quantity, rules)
        ]

        approved = [decision for decision in candidate_decisions if decision.approved]
        floor_decision = next((decision for decision in candidate_decisions if decision.quantity_rounding_mode == "floor"), None)
        if floor_decision and floor_decision.approved and floor_decision.notional_deviation_side == "under":
            return floor_decision
        if approved:
            return min(approved, key=lambda decision: decision.notional_deviation_pct)
        return min(candidate_decisions, key=lambda decision: decision.notional_deviation_pct)

    def _evaluate_quantity_candidate(
        self,
        *,
        signal: Signal,
        rules: InstrumentRules,
        leverage: int,
        target_notional: float,
        raw_quantity: float,
        quantity: float,
        mode: str,
        selected_margin: float,
        risk_amount: float,
        stop_distance: float,
        cost_rate: float,
        base_kwargs: dict,
    ) -> RiskDecision:
        rounded_quantity = quantity
        notional = rounded_quantity * signal.entry_price
        if notional > target_notional:
            notional_deviation_side = "over"
            max_allowed_deviation = self.config.max_over_notional_deviation_pct
        else:
            notional_deviation_side = "under"
            max_allowed_deviation = self.config.max_under_notional_deviation_pct
        notional_deviation_pct = abs(notional - target_notional) / target_notional if target_notional else 1.0
        margin_required = notional / leverage
        real_risk = rounded_quantity * stop_distance + notional * cost_rate
        risk_pct = real_risk / max(float(base_kwargs["available_balance"]), 1e-9)
        decision_kwargs = {
            **base_kwargs,
            "position_size": rounded_quantity,
            "quantity": rounded_quantity,
            "entry_price": signal.entry_price,
            "target_notional": target_notional,
            "raw_quantity": raw_quantity,
            "rounded_quantity": rounded_quantity,
            "calculated_notional_after_rounding": notional,
            "notional_deviation_pct": notional_deviation_pct,
            "notional_deviation_side": notional_deviation_side,
            "max_allowed_deviation_for_side": max_allowed_deviation,
            "quantity_rounding_mode": mode,
            "notional": notional,
            "selected_notional": notional,
            "margin_required": margin_required,
            "new_required_margin": margin_required,
            "real_risk": real_risk,
            "risk_amount_estimated": real_risk,
            "risk_pct_estimated": risk_pct,
            "selected_margin_usdt": selected_margin,
            "total_margin_after_trade": base_kwargs["used_margin"] + margin_required,
            "margin_based_notional": base_kwargs["max_margin_per_trade"] * leverage,
        }

        if rounded_quantity <= 0:
            return RiskDecision(False, "Tamano calculado cero", block_reason="zero_size", **decision_kwargs)
        if rounded_quantity < rules.min_trade_num:
            return RiskDecision(
                False,
                "rounded_quantity inferior al minTradeNum de Bitget",
                block_reason="quantity_below_min",
                **decision_kwargs,
            )
        if notional < rules.min_trade_usdt:
            return RiskDecision(False, "Notional inferior al minimo de Bitget", block_reason="notional_below_min", **decision_kwargs)
        if notional_deviation_pct > max_allowed_deviation:
            return RiskDecision(
                False,
                (
                    "calculated_notional_after_rounding supera la desviacion permitida "
                    f"para {notional_deviation_side}: {notional_deviation_pct:.4%} > {max_allowed_deviation:.4%}"
                ),
                block_reason="notional_deviation",
                **decision_kwargs,
            )
        if margin_required > base_kwargs["max_margin_per_trade"] + 1e-9:
            return RiskDecision(
                False,
                "Redondeo/minimo de Bitget exige mas margen que el maximo permitido por trade",
                block_reason="exchange_min_breaks_margin",
                **decision_kwargs,
            )
        if base_kwargs["used_margin"] + margin_required >= base_kwargs["max_total_margin_allowed_after_buffer"] + 1e-9:
            return RiskDecision(
                False,
                "Redondeo/minimo de Bitget supera margen total permitido",
                block_reason="exchange_min_breaks_total_margin",
                **decision_kwargs,
            )
        if real_risk > risk_amount + 1e-9:
            return RiskDecision(
                False,
                f"Riesgo real supera MAX_RISK_PER_TRADE: real_risk={real_risk:.4f} > risk_amount={risk_amount:.4f}",
                block_reason="risk_above_limit",
                **decision_kwargs,
            )
        if notional > self.config.max_notional_per_trade_small_account + 1e-9 and self.config.is_small_account_config:
            return RiskDecision(
                False,
                "Notional supera MAX_NOTIONAL_PER_TRADE_SMALL_ACCOUNT",
                block_reason="notional_above_small_account_cap",
                **decision_kwargs,
            )

        return RiskDecision(True, "Riesgo aprobado", **decision_kwargs)

    def _quantity_candidates(self, raw_quantity: float, rules: InstrumentRules) -> list[tuple[str, float]]:
        floor_qty = OrderManager.round_size(raw_quantity, rules, "down")
        ceil_qty = OrderManager.round_size(raw_quantity, rules, "up")
        nearest_qty = floor_qty if abs(raw_quantity - floor_qty) <= abs(ceil_qty - raw_quantity) else ceil_qty
        ordered = [("floor", floor_qty), ("nearest", nearest_qty), ("ceil", ceil_qty)]
        seen: set[float] = set()
        unique: list[tuple[str, float]] = []
        for mode, quantity in ordered:
            key = round(quantity, max(rules.volume_place + 4, 8))
            if key in seen:
                continue
            seen.add(key)
            unique.append((mode, quantity))
        return unique

    def _preflight_block(
        self,
        *,
        signal: Signal,
        balance: float,
        open_positions: list[dict],
        daily_pnl: float,
        weekly_pnl: float,
        isolated_verified: bool,
        auto_margin_off_verified: bool,
    ) -> RiskDecision | None:
        if signal.side == "NO_TRADE":
            return RiskDecision(False, signal.reason, block_reason="no_trade")
        if self.config.margin_mode != "isolated":
            return RiskDecision(False, "margin_mode != isolated", block_reason="margin_mode_not_isolated")
        if self.config.force_isolated_margin and not isolated_verified:
            return RiskDecision(False, "FORCE_ISOLATED_MARGIN=true y no se verifico isolated", block_reason="isolated_not_verified")
        if self.config.auto_margin:
            return RiskDecision(False, "AUTO_MARGIN debe estar false/off", block_reason="auto_margin_enabled")
        if not auto_margin_off_verified:
            return RiskDecision(False, "AUTO_MARGIN=false pero no se pudo verificar off", block_reason="auto_margin_off_not_verified")
        if balance < self.config.stop_trading_below_balance_usdt:
            return RiskDecision(False, f"Balance {balance:.2f} < {self.config.stop_trading_below_balance_usdt:.2f} USDT", block_reason="low_balance")
        if self.cooldown_until and datetime.now(timezone.utc) < self.cooldown_until:
            return RiskDecision(False, f"Cooldown activo hasta {self.cooldown_until.isoformat()}", block_reason="cooldown")
        if self.api_failure_count >= 3:
            return RiskDecision(False, "API fallando repetidamente; trading pausado", block_reason="api_failures")
        if self.config.require_stop_loss and signal.stop_loss <= 0:
            return RiskDecision(False, "Bloqueado: toda operacion necesita stop loss", block_reason="missing_stop_loss")
        if self.config.require_take_profit and (signal.take_profit_1 <= 0 or signal.take_profit_2 <= 0):
            return RiskDecision(False, "Bloqueado: toda operacion necesita take profit", block_reason="missing_take_profit")
        if signal.risk_reward_ratio < self.config.min_risk_reward:
            return RiskDecision(False, f"R:R {signal.risk_reward_ratio:.2f} < {self.config.min_risk_reward:.2f}", block_reason="low_rr")
        if daily_pnl <= -(balance * self.config.max_daily_loss):
            return RiskDecision(False, "Circuit breaker: perdida diaria maxima alcanzada", block_reason="daily_loss")
        if weekly_pnl <= -(balance * self.config.max_weekly_loss):
            return RiskDecision(False, "Circuit breaker: perdida semanal maxima alcanzada", block_reason="weekly_loss")
        if len(open_positions) >= self._max_positions_for_balance(balance):
            return RiskDecision(False, "Maximo de posiciones abiertas alcanzado", block_reason="max_positions")
        if any(p.get("symbol") == signal.symbol for p in open_positions):
            return RiskDecision(False, f"Ya existe una posicion abierta en {signal.symbol}", block_reason="symbol_position_exists")
        return None

    def _decision_base(
        self,
        *,
        balance: float,
        available_balance: float,
        open_positions: list[dict],
        risk_amount: float,
        risk_based_notional: float,
        margin_based_notional: float,
        exposure_based_notional: float,
        selected_notional: float,
        stop_distance_pct: float,
        used_margin: float,
        max_margin_per_trade: float,
        remaining_total_margin: float,
        leverage: int,
        selected_margin: float,
        total_margin_after_trade: float,
        max_total_margin_allowed_after_buffer: float,
        open_positions_count: int,
        max_open_positions_allowed: int,
    ) -> dict:
        return {
            "risk_amount": risk_amount,
            "risk_based_notional": risk_based_notional,
            "margin_based_notional": margin_based_notional,
            "exposure_based_notional": exposure_based_notional,
            "selected_notional": selected_notional,
            "available_balance": available_balance,
            "stop_distance_pct": stop_distance_pct,
            "used_margin": used_margin,
            "max_margin_per_trade": max_margin_per_trade,
            "remaining_total_margin": remaining_total_margin,
            "leverage": leverage,
            "margin_mode": self.config.margin_mode,
            "force_isolated_margin": self.config.force_isolated_margin,
            "auto_margin": self.config.auto_margin,
            "trade_margin_usdt": float(self.config.trade_margin_usdt),
            "max_trade_margin_usdt": float(self.config.max_trade_margin_usdt),
            "selected_margin_usdt": selected_margin,
            "new_required_margin": selected_margin,
            "total_margin_after_trade": total_margin_after_trade,
            "max_total_margin_allowed_after_buffer": max_total_margin_allowed_after_buffer,
            "open_positions_count": open_positions_count,
            "max_open_positions_allowed": max_open_positions_allowed,
            "risk_amount_estimated": risk_amount,
            "risk_pct_estimated": risk_amount / max(balance, 1e-9),
        }

    def _select_margin(self, max_margin_per_trade: float, max_margin_by_free_balance: float) -> float:
        if self.config.use_fixed_trade_margin:
            return min(float(self.config.trade_margin_usdt), max_margin_per_trade, max_margin_by_free_balance)
        return min(max_margin_per_trade, max_margin_by_free_balance)

    def _initial_leverage(self, signal: Signal, rules: InstrumentRules) -> int:
        requested = signal.leverage_recommendation or self.config.default_leverage
        if signal.confidence_score < 90:
            requested = min(requested, self.config.default_leverage)
        requested = min(requested, self.config.max_leverage, rules.max_leverage)
        return max(requested, rules.min_leverage)

    def _leverage_attempts(self, leverage: int, rules: InstrumentRules) -> list[int]:
        return list(range(leverage, max(rules.min_leverage, 1) - 1, -1))

    def _max_positions_for_balance(self, balance: float) -> int:
        if balance < 60:
            return self.config.small_account_max_open_positions
        if balance <= 60 and not self.config.allow_second_position_small_account:
            return self.config.small_account_max_open_positions
        return min(self.config.max_open_positions, self.config.small_account_max_open_positions if balance <= 60 else self.config.max_open_positions)

    def _is_small_account_from_margin(self, selected_margin: float) -> bool:
        return self.config.is_small_account_config or selected_margin <= float(self.config.max_trade_margin_usdt)

    def _min_free_margin_required(self, balance: float) -> float:
        configured = float(self.config.min_free_margin_after_trade)
        ratio_amount = balance * configured if 0 <= configured <= 1 else configured
        return max(float(self.config.margin_safety_buffer_usdt), ratio_amount)

    @staticmethod
    def _used_margin(open_positions: list[dict]) -> float:
        total = 0.0
        for position in open_positions:
            for key in ("margin_used", "margin_required", "marginRequired", "marginSize", "margin"):
                value = position.get(key)
                if value in (None, ""):
                    continue
                try:
                    total += float(value)
                    break
                except (TypeError, ValueError):
                    continue
        return total

    def _log_sizing(self, symbol: str, side: str, decision: RiskDecision) -> None:
        if not self.logger:
            return
        details = (
            f"symbol={symbol}, side={side}, margin_mode={decision.margin_mode}, "
            f"force_isolated_margin={decision.force_isolated_margin}, auto_margin={decision.auto_margin}, "
            f"trade_margin_usdt={decision.trade_margin_usdt:.2f}, "
            f"max_trade_margin_usdt={decision.max_trade_margin_usdt:.2f}, "
            f"selected_margin_usdt={decision.selected_margin_usdt:.2f}, "
            f"entry_price={decision.entry_price:.8f}, leverage={decision.leverage}, "
            f"target_notional={decision.target_notional:.4f}, raw_quantity={decision.raw_quantity:.10f}, "
            f"rounded_quantity={decision.rounded_quantity:.10f}, "
            f"calculated_notional_after_rounding={decision.calculated_notional_after_rounding:.4f}, "
            f"notional_deviation_pct={decision.notional_deviation_pct:.4%}, "
            f"notional_deviation_side={decision.notional_deviation_side}, "
            f"max_allowed_deviation_for_side={decision.max_allowed_deviation_for_side:.4%}, "
            f"quantity_rounding_mode={decision.quantity_rounding_mode}, "
            f"notional={decision.notional:.4f}, quantity={decision.quantity:.8f}, "
            f"used_margin_before={decision.used_margin:.4f}, new_required_margin={decision.new_required_margin:.4f}, "
            f"total_margin_after_trade={decision.total_margin_after_trade:.4f}, "
            f"max_total_margin_allowed_after_buffer={decision.max_total_margin_allowed_after_buffer:.4f}, "
            f"open_positions_count={decision.open_positions_count}, "
            f"max_open_positions_allowed={decision.max_open_positions_allowed}, "
            f"risk_amount_estimated={decision.risk_amount_estimated:.4f}, "
            f"risk_pct_estimated={decision.risk_pct_estimated:.4%}, "
            f"block_reason={decision.block_reason}, reason={decision.reason}"
        )
        if decision.approved:
            self.logger.info("Position sizing %s: %s", symbol, details)
        else:
            self.logger.info("Position sizing bloqueado %s: %s", symbol, details)
