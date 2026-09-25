"""
Roostoo Trading Bot — Strategy Engine
=======================================
Orchestrates strategy evaluation, signal aggregation, and
strategy selection based on market regime.

Does NOT execute trades — produces signals that flow through
risk manager → order executor.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from config.trading_params import BotConfig
from core.market_data import MarketDataEngine, TickerSnapshot
from core.regime_engine import RegimeEngine, RegimeState, MarketRegime
from core.cvd_engine import CVDEngine, CVDState
from strategies.base import BaseStrategy, Signal, SignalDirection
from strategies.value_area import ValueAreaStrategy
from strategies.liquidity_sweep import LiquiditySweepStrategy

logger = logging.getLogger("strategy_engine")


class StrategyEngine:
    """
    Multi-strategy engine with regime-aware strategy selection
    and signal aggregation.
    """

    def __init__(self, config: BotConfig, market_data: MarketDataEngine):
        self._config = config
        self._market = market_data
        self._regime_engine = RegimeEngine()
        self._cvd_engine = CVDEngine()

        # Initialize strategies
        self._strategies: Dict[str, BaseStrategy] = {}
        self._active_strategy: Optional[str] = None
        self._last_signal: Optional[Signal] = None
        self._last_regime: Optional[RegimeState] = None

        # Build strategies from config
        sc = config.strategies

        if sc.value_area.enabled:
            self._strategies["VALUE_AREA"] = ValueAreaStrategy(
                min_confidence=sc.value_area.min_confidence,
                vah_reclaim_candles=sc.value_area.vah_reclaim_candles,
                volume_profile_periods=sc.value_area.volume_profile_periods,
                tp1_scale_out_pct=sc.value_area.tp1_scale_out_pct,
                stop_alt_pct=config.risk.stop_alternative_pct,
            )

        if sc.liquidity_sweep.enabled:
            self._strategies["LIQUIDITY_SWEEP"] = LiquiditySweepStrategy(
                min_confidence=sc.liquidity_sweep.min_confidence,
                swing_lookback=sc.liquidity_sweep.swing_lookback,
                displacement_min_body_pct=sc.liquidity_sweep.displacement_min_body_pct,
                fvg_min_gap_pct=sc.liquidity_sweep.fvg_min_gap_pct,
                tp1_rr=sc.liquidity_sweep.tp1_rr,
            )

        # Strategy C placeholder — extensible without rewriting
        # if sc.strategy_c.enabled:
        #     self._strategies["STRATEGY_C"] = StrategyCImpl(...)

        logger.info(
            "StrategyEngine initialized — strategies=%s",
            list(self._strategies.keys()),
        )

    def evaluate(
        self,
        symbol: str,
        portfolio_context: Dict[str, Any],
    ) -> Optional[Signal]:
        """
        Evaluate all eligible strategies for a symbol.

        Flow:
        1. Get market data
        2. Classify regime
        3. Update CVD
        4. Evaluate eligible strategies
        5. Aggregate signals
        6. Return best signal or None
        """
        # 1. Get data
        candles_5m = self._market.get_candles(symbol, "5m")
        candles_15m = self._market.get_candles(symbol, "15m")
        ticker = self._market.get_ticker(symbol)

        if not ticker or not candles_5m:
            return None

        # 2. Classify regime
        cvd = self._cvd_engine.update(symbol, candles_5m, ticker)
        regime = self._regime_engine.classify(
            symbol, candles_5m, candles_15m, ticker, cvd,
        )
        self._last_regime = regime

        if regime.regime == MarketRegime.NO_TRADE:
            logger.debug("[%s] Regime = NO_TRADE: %s", symbol, regime.reason)
            self._active_strategy = None
            return None

        # 3. Evaluate eligible strategies
        candidate_signals: List[Signal] = []

        for strategy_name in regime.eligible_strategies:
            strategy = self._strategies.get(strategy_name)
            if strategy is None or not strategy.enabled:
                continue

            try:
                signal = strategy.evaluate(
                    symbol=symbol,
                    candles_5m=candles_5m,
                    candles_15m=candles_15m,
                    ticker=ticker,
                    regime=regime,
                    cvd=cvd,
                    portfolio_context=portfolio_context,
                )
                if signal is not None:
                    candidate_signals.append(signal)
            except Exception as exc:
                logger.error(
                    "Strategy %s evaluation error: %s", strategy_name, exc,
                    exc_info=True,
                )

        # 4. Aggregate signals
        best_signal = self._aggregate_signals(candidate_signals)

        if best_signal:
            self._active_strategy = best_signal.strategy
            self._last_signal = best_signal
            logger.info(
                "[%s] Signal: %s %s conf=%.2f entry=%.2f stop=%.2f tp1=%.2f | %s",
                symbol, best_signal.direction.value, best_signal.strategy,
                best_signal.confidence, best_signal.entry_price,
                best_signal.stop_loss, best_signal.take_profit_1,
                best_signal.reason,
            )
        else:
            self._active_strategy = None

        return best_signal

    def _aggregate_signals(self, signals: List[Signal]) -> Optional[Signal]:
        """
        Aggregate multiple strategy signals.

        Rules:
        - If signals conflict strongly, return NO_TRADE
        - Otherwise, return highest-confidence signal
        - Must meet minimum confidence threshold
        """
        if not signals:
            return None

        min_conf = self._config.signals.min_confidence_to_trade
        conflict_threshold = self._config.signals.conflict_threshold

        # Filter by minimum confidence
        valid = [s for s in signals if s.confidence >= min_conf]
        if not valid:
            return None

        # DERISK signals take priority over new positions
        derisk = [s for s in valid if s.direction == SignalDirection.DERISK]
        if derisk:
            return max(derisk, key=lambda s: s.confidence)

        # Check for conflicting signals
        buy_signals = [s for s in valid if s.direction == SignalDirection.BUY]
        sell_signals = [s for s in valid if s.direction == SignalDirection.SELL]

        if buy_signals and sell_signals:
            buy_conf = max(s.confidence for s in buy_signals)
            sell_conf = max(s.confidence for s in sell_signals)

            # If both are strong, conflict → NO_TRADE
            if buy_conf > conflict_threshold and sell_conf > conflict_threshold:
                logger.info(
                    "Signal conflict — BUY(%.2f) vs SELL(%.2f) → NO_TRADE",
                    buy_conf, sell_conf,
                )
                return None

        # Return highest confidence signal
        return max(valid, key=lambda s: s.confidence)

    # =======================================================================
    # State accessors
    # =======================================================================

    @property
    def active_strategy(self) -> Optional[str]:
        return self._active_strategy

    @property
    def last_signal(self) -> Optional[Signal]:
        return self._last_signal

    @property
    def last_regime(self) -> Optional[RegimeState]:
        return self._last_regime

    @property
    def regime_engine(self) -> RegimeEngine:
        return self._regime_engine

    @property
    def cvd_engine(self) -> CVDEngine:
        return self._cvd_engine

    def get_strategies_status(self) -> Dict[str, Any]:
        return {
            name: {
                "enabled": s.enabled,
                "last_signal": s.last_signal.to_dict() if s.last_signal else None,
            }
            for name, s in self._strategies.items()
        }
