"""
Roostoo Trading Bot — Risk Manager
====================================
EVERY trade must pass through here before reaching the exchange.
Risk first, always.

Implements:
- Position sizing (equity-based, stop-adjusted)
- Maximum position count
- Cash reserve enforcement
- Gross exposure limits
- Drawdown protection (current, rolling 24h, strategy-level)
- Circuit breaker / kill switch
- Data safety checks
- Spread validation
"""

from __future__ import annotations

import logging
import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from config.trading_params import BotConfig, RiskConfig
from strategies.base import Signal, SignalDirection

logger = logging.getLogger("risk_manager")


class RiskDecision(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REDUCED = "REDUCED"   # Approved but with reduced size


@dataclass
class RiskCheckResult:
    """Result of a risk check."""
    decision: RiskDecision
    reason: str
    approved_quantity: float = 0.0
    approved_collateral: float = 0.0  # For shorts
    checks_passed: List[str] = field(default_factory=list)
    checks_failed: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "approved_quantity": self.approved_quantity,
            "approved_collateral": self.approved_collateral,
            "checks_passed": self.checks_passed,
            "checks_failed": self.checks_failed,
        }


class RiskManager:
    """
    Central risk management.
    Nothing trades without passing through here.
    """

    def __init__(self, config: BotConfig):
        self._config = config
        self._risk = config.risk
        self._lock = threading.Lock()

        # State tracking
        self._peak_equity: float = config.exchange.initial_portfolio_value
        self._current_equity: float = config.exchange.initial_portfolio_value
        self._current_drawdown: float = 0.0
        self._max_drawdown: float = 0.0
        self._rolling_24h_returns: List[tuple] = []  # (timestamp, equity)
        self._strategy_pnl: Dict[str, float] = {}
        self._strategy_peak: Dict[str, float] = {}

        # Circuit breaker state
        self._frozen: bool = False
        self._frozen_reason: str = ""
        self._frozen_time: float = 0.0
        self._permanent_kill: bool = False
        self._kill_reason: str = ""

        # Position tracking
        self._open_positions: int = 0
        self._gross_exposure: float = 0.0
        self._cash: float = config.exchange.initial_portfolio_value

        logger.info("RiskManager initialized — max_dd=%.1f%%, max_positions=%d, cash_reserve=%.0f%%",
                     self._risk.max_drawdown_pct * 100,
                     self._risk.max_positions,
                     self._risk.min_cash_reserve_pct * 100)

    # =======================================================================
    # Main risk check
    # =======================================================================

    def check_trade(
        self,
        signal: Signal,
        equity: float,
        cash: float,
        gross_exposure: float,
        open_positions: int,
        spread_bps: float,
        data_fresh: bool,
        api_healthy: bool,
    ) -> RiskCheckResult:
        """
        Run all risk checks on a proposed trade.
        Returns approval/rejection with reasoning.
        """
        with self._lock:
            self._current_equity = equity
            self._cash = cash
            self._gross_exposure = gross_exposure
            self._open_positions = open_positions

            checks_passed: list[str] = []
            checks_failed: list[str] = []

            # --- Kill switch ---
            if self._permanent_kill:
                return RiskCheckResult(
                    decision=RiskDecision.REJECTED,
                    reason=f"PERMANENT KILL: {self._kill_reason}",
                    checks_failed=["kill_switch"],
                )

            # --- Frozen ---
            if self._frozen:
                return RiskCheckResult(
                    decision=RiskDecision.REJECTED,
                    reason=f"RISK FROZEN: {self._frozen_reason}",
                    checks_failed=["risk_freeze"],
                )

            # --- Data safety ---
            if not data_fresh:
                checks_failed.append("data_freshness")
                return RiskCheckResult(
                    decision=RiskDecision.REJECTED,
                    reason="Market data is stale — refusing to trade",
                    checks_failed=checks_failed,
                )
            checks_passed.append("data_freshness")

            if not api_healthy:
                checks_failed.append("api_health")
                return RiskCheckResult(
                    decision=RiskDecision.REJECTED,
                    reason="API is unhealthy — refusing to trade",
                    checks_failed=checks_failed,
                )
            checks_passed.append("api_health")

            # --- Spread check ---
            if spread_bps > self._risk.max_spread_bps:
                checks_failed.append("spread")
                return RiskCheckResult(
                    decision=RiskDecision.REJECTED,
                    reason=f"Spread too wide: {spread_bps:.1f} bps > {self._risk.max_spread_bps} max",
                    checks_passed=checks_passed,
                    checks_failed=checks_failed,
                )
            checks_passed.append("spread")

            # --- Drawdown checks ---
            self._update_drawdown(equity)

            if self._current_drawdown >= self._risk.max_drawdown_pct:
                self._freeze(f"Max drawdown breached: {self._current_drawdown:.2%} >= {self._risk.max_drawdown_pct:.2%}")
                checks_failed.append("max_drawdown")
                return RiskCheckResult(
                    decision=RiskDecision.REJECTED,
                    reason=f"Max drawdown breached: {self._current_drawdown:.2%}",
                    checks_passed=checks_passed,
                    checks_failed=checks_failed,
                )
            checks_passed.append("max_drawdown")

            rolling_dd = self._compute_rolling_24h_drawdown(equity)
            if rolling_dd >= self._risk.rolling_24h_drawdown_pct:
                self._freeze(f"Rolling 24h drawdown: {rolling_dd:.2%} >= {self._risk.rolling_24h_drawdown_pct:.2%}")
                checks_failed.append("rolling_24h_drawdown")
                return RiskCheckResult(
                    decision=RiskDecision.REJECTED,
                    reason=f"Rolling 24h drawdown too high: {rolling_dd:.2%}",
                    checks_passed=checks_passed,
                    checks_failed=checks_failed,
                )
            checks_passed.append("rolling_24h_drawdown")

            # --- Strategy drawdown ---
            strat_name = signal.strategy
            strat_dd = self._get_strategy_drawdown(strat_name)
            if strat_dd >= self._risk.strategy_drawdown_pct:
                checks_failed.append("strategy_drawdown")
                return RiskCheckResult(
                    decision=RiskDecision.REJECTED,
                    reason=f"Strategy {strat_name} drawdown too high: {strat_dd:.2%}",
                    checks_passed=checks_passed,
                    checks_failed=checks_failed,
                )
            checks_passed.append("strategy_drawdown")

            # For DERISK signals, skip position-opening checks
            if signal.direction in (SignalDirection.DERISK, SignalDirection.SELL):
                # SELL / DERISK always need fewer checks
                if signal.direction == SignalDirection.DERISK:
                    return RiskCheckResult(
                        decision=RiskDecision.APPROVED,
                        reason="De-risk approved",
                        approved_quantity=0.0,  # Handled by executor
                        checks_passed=checks_passed,
                    )

            # --- Position count ---
            if signal.direction == SignalDirection.BUY and open_positions >= self._risk.max_positions:
                checks_failed.append("max_positions")
                return RiskCheckResult(
                    decision=RiskDecision.REJECTED,
                    reason=f"Max positions reached: {open_positions}/{self._risk.max_positions}",
                    checks_passed=checks_passed,
                    checks_failed=checks_failed,
                )
            checks_passed.append("max_positions")

            # --- Cash reserve ---
            min_cash = equity * self._risk.min_cash_reserve_pct
            available_cash = cash - min_cash
            if available_cash <= 0:
                checks_failed.append("cash_reserve")
                return RiskCheckResult(
                    decision=RiskDecision.REJECTED,
                    reason=f"Cash reserve violated: cash={cash:.2f}, min_reserve={min_cash:.2f}",
                    checks_passed=checks_passed,
                    checks_failed=checks_failed,
                )
            checks_passed.append("cash_reserve")

            # --- Gross exposure ---
            max_exposure = equity * self._risk.max_gross_exposure_pct
            remaining_exposure = max_exposure - gross_exposure
            if remaining_exposure <= 0:
                checks_failed.append("gross_exposure")
                return RiskCheckResult(
                    decision=RiskDecision.REJECTED,
                    reason=f"Gross exposure limit: {gross_exposure:.2f}/{max_exposure:.2f}",
                    checks_passed=checks_passed,
                    checks_failed=checks_failed,
                )
            checks_passed.append("gross_exposure")

            # --- Position sizing ---
            if signal.direction == SignalDirection.BUY:
                quantity = self._compute_position_size(
                    signal, equity, available_cash, remaining_exposure,
                )
                if quantity <= 0:
                    checks_failed.append("position_sizing")
                    return RiskCheckResult(
                        decision=RiskDecision.REJECTED,
                        reason="Computed position size is zero or negative",
                        checks_passed=checks_passed,
                        checks_failed=checks_failed,
                    )
                checks_passed.append("position_sizing")

                return RiskCheckResult(
                    decision=RiskDecision.APPROVED,
                    reason=f"Trade approved — qty={quantity:.6f}",
                    approved_quantity=quantity,
                    checks_passed=checks_passed,
                )

            elif signal.direction == SignalDirection.SELL:
                # Check if shorting is permitted (or spot-only competition mode)
                if not getattr(self._risk, "allow_shorting", True):
                    checks_failed.append("shorting_disabled")
                    return RiskCheckResult(
                        decision=RiskDecision.REJECTED,
                        reason="Shorting disabled (Spot-only competition mode)",
                        checks_passed=checks_passed,
                        checks_failed=checks_failed,
                    )

                # Short position sizing (collateral-based)
                collateral = self._compute_short_size(
                    signal, equity, available_cash, remaining_exposure,
                )
                if collateral <= 0:
                    checks_failed.append("position_sizing")
                    return RiskCheckResult(
                        decision=RiskDecision.REJECTED,
                        reason="Computed collateral is zero or negative",
                        checks_passed=checks_passed,
                        checks_failed=checks_failed,
                    )
                checks_passed.append("position_sizing")

                return RiskCheckResult(
                    decision=RiskDecision.APPROVED,
                    reason=f"Short approved — collateral={collateral:.2f}",
                    approved_collateral=collateral,
                    checks_passed=checks_passed,
                )

            # HOLD / NO_TRADE
            return RiskCheckResult(
                decision=RiskDecision.REJECTED,
                reason=f"Signal direction {signal.direction.value} does not require risk check",
                checks_passed=checks_passed,
            )

    # =======================================================================
    # Position sizing
    # =======================================================================

    def _compute_position_size(
        self,
        signal: Signal,
        equity: float,
        available_cash: float,
        remaining_exposure: float,
    ) -> float:
        """
        Compute position size based on:
        - Risk per trade (% of equity)
        - Stop distance (theoretical loss-at-stop cap)
        - Available cash (including buy-side taker fee)
        - Remaining exposure headroom
        """
        if signal.entry_price <= 0 or signal.stop_loss <= 0:
            return 0.0

        risk_amount = equity * self._risk.risk_per_trade_pct
        stop_distance = abs(signal.entry_price - signal.stop_loss)

        if stop_distance <= 0:
            return 0.0

        # Theoretical risk-at-stop quantity
        size_by_risk = risk_amount / stop_distance

        # Size based on available cash, factoring in taker commission so cash reserve is protected
        taker_fee = getattr(self._config.execution, "taker_fee", 0.0010)
        size_by_cash = available_cash / (signal.entry_price * (1.0 + taker_fee))

        # Size based on remaining gross exposure
        size_by_exposure = remaining_exposure / signal.entry_price

        # Constrain to the most restrictive boundary
        quantity = min(size_by_risk, size_by_cash, size_by_exposure)

        return max(0.0, quantity)

    def _compute_short_size(
        self,
        signal: Signal,
        equity: float,
        available_cash: float,
        remaining_exposure: float,
    ) -> float:
        """Compute short collateral based on risk parameters."""
        risk_amount = equity * self._risk.risk_per_trade_pct

        if signal.entry_price <= 0 or signal.stop_loss <= 0:
            return 0.0

        stop_distance_pct = abs(signal.stop_loss - signal.entry_price) / signal.entry_price
        if stop_distance_pct <= 0:
            return 0.0

        # Collateral = risk_amount / stop_distance_pct
        collateral_by_risk = risk_amount / stop_distance_pct

        # Limited by available cash (including fee allowance)
        taker_fee = getattr(self._config.execution, "taker_fee", 0.0010)
        max_collateral = available_cash / (1.0 + taker_fee)

        # Limited by remaining exposure
        collateral_by_exposure = remaining_exposure

        collateral = min(collateral_by_risk, max_collateral, collateral_by_exposure)

        return max(0.0, collateral)

    # =======================================================================
    # Drawdown tracking
    # =======================================================================

    def _update_drawdown(self, equity: float) -> None:
        """Update peak equity and drawdown metrics."""
        if equity > self._peak_equity:
            self._peak_equity = equity

        if self._peak_equity > 0:
            self._current_drawdown = (self._peak_equity - equity) / self._peak_equity
        else:
            self._current_drawdown = 0.0

        self._max_drawdown = max(self._max_drawdown, self._current_drawdown)

        # Track for rolling 24h
        now = time.time()
        self._rolling_24h_returns.append((now, equity))
        # Clean old entries
        cutoff = now - 86400
        self._rolling_24h_returns = [
            (t, e) for t, e in self._rolling_24h_returns if t >= cutoff
        ]

    def _compute_rolling_24h_drawdown(self, current_equity: float) -> float:
        """Compute drawdown from the peak within the last 24 hours."""
        if not self._rolling_24h_returns:
            return 0.0

        peak_24h = max(e for _, e in self._rolling_24h_returns)
        if peak_24h <= 0:
            return 0.0

        return max(0.0, (peak_24h - current_equity) / peak_24h)

    def _get_strategy_drawdown(self, strategy: str) -> float:
        """Get current drawdown for a specific strategy."""
        pnl = self._strategy_pnl.get(strategy, 0.0)
        peak = self._strategy_peak.get(strategy, 0.0)

        if peak <= 0:
            return 0.0

        if pnl < peak:
            return (peak - pnl) / abs(peak) if peak != 0 else 0.0
        return 0.0

    def update_strategy_pnl(self, strategy: str, pnl: float) -> None:
        """Update strategy-level PnL for drawdown tracking."""
        with self._lock:
            self._strategy_pnl[strategy] = pnl
            current_peak = self._strategy_peak.get(strategy, 0.0)
            if pnl > current_peak:
                self._strategy_peak[strategy] = pnl

    # =======================================================================
    # Circuit breaker / Kill switch
    # =======================================================================

    def _freeze(self, reason: str) -> None:
        """Activate temporary risk freeze."""
        if not self._frozen:
            self._frozen = True
            self._frozen_reason = reason
            self._frozen_time = time.time()
            logger.critical("RISK FREEZE: %s", reason)

    def activate_kill_switch(self, reason: str) -> None:
        """Activate permanent kill switch. No more trading."""
        with self._lock:
            self._permanent_kill = True
            self._kill_reason = reason
            logger.critical("KILL SWITCH ACTIVATED: %s", reason)

    def reset_freeze(self) -> bool:
        """
        Reset temporary freeze.
        Only allowed if drawdown has recovered below threshold.
        """
        with self._lock:
            if self._permanent_kill:
                logger.warning("Cannot reset — permanent kill is active")
                return False

            if self._current_drawdown < self._risk.max_drawdown_pct * 0.8:
                self._frozen = False
                self._frozen_reason = ""
                logger.info("Risk freeze reset — drawdown recovered to %.2f%%",
                           self._current_drawdown * 100)
                return True
            else:
                logger.warning("Cannot reset freeze — drawdown still %.2f%%",
                              self._current_drawdown * 100)
                return False

    def update_portfolio_state(
        self,
        equity: float,
        cash: float,
        gross_exposure: float,
        open_positions: int,
    ) -> None:
        """Update risk manager's view of portfolio state."""
        with self._lock:
            self._current_equity = equity
            self._cash = cash
            self._gross_exposure = gross_exposure
            self._open_positions = open_positions
            self._update_drawdown(equity)

    # =======================================================================
    # State / Dashboard
    # =======================================================================

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    @property
    def frozen_reason(self) -> str:
        return self._frozen_reason

    @property
    def is_killed(self) -> bool:
        return self._permanent_kill

    @property
    def kill_reason(self) -> str:
        return self._kill_reason

    def get_state(self) -> Dict[str, Any]:
        """Return complete risk state for dashboard."""
        return {
            "frozen": self._frozen,
            "frozen_reason": self._frozen_reason,
            "permanent_kill": self._permanent_kill,
            "kill_reason": self._kill_reason,
            "peak_equity": round(self._peak_equity, 2),
            "current_drawdown": round(self._current_drawdown, 4),
            "max_drawdown": round(self._max_drawdown, 4),
            "rolling_24h_drawdown": round(
                self._compute_rolling_24h_drawdown(self._current_equity), 4,
            ),
            "drawdown_limit": self._risk.max_drawdown_pct,
            "open_positions": self._open_positions,
            "max_positions": self._risk.max_positions,
            "gross_exposure": round(self._gross_exposure, 2),
            "exposure_limit": round(
                self._current_equity * self._risk.max_gross_exposure_pct, 2,
            ),
            "cash": round(self._cash, 2),
            "min_cash_reserve": round(
                self._current_equity * self._risk.min_cash_reserve_pct, 2,
            ),
            "strategy_pnl": {k: round(v, 2) for k, v in self._strategy_pnl.items()},
        }
