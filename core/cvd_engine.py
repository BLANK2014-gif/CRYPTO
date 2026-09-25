"""
Roostoo Trading Bot — CVD (Cumulative Volume Delta) Engine
============================================================
Order-flow analytics from available Roostoo market data.

CRITICAL CONSTRAINT: Roostoo provides no tick-level bid/ask volume
breakdown, only aggregate volume. This engine operates in DEGRADED MODE
by default, using heuristic approximations. It explicitly signals
degraded state rather than pretending real order-flow data exists.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from core.market_data import Candle, TickerSnapshot

logger = logging.getLogger("cvd_engine")


@dataclass
class CVDState:
    """Current CVD (Cumulative Volume Delta) state."""
    cvd_value: float = 0.0
    delta_volume: float = 0.0
    absorption_signal: str = "NONE"   # NONE, BUYING_ABSORPTION, SELLING_ABSORPTION
    degraded_mode: bool = True
    trend: str = "NEUTRAL"           # BULLISH, BEARISH, NEUTRAL
    volume_spike: bool = False
    last_update: float = 0.0

    def to_dict(self):
        return {
            "cvd_value": round(self.cvd_value, 4),
            "delta_volume": round(self.delta_volume, 4),
            "absorption_signal": self.absorption_signal,
            "degraded_mode": self.degraded_mode,
            "trend": self.trend,
            "volume_spike": self.volume_spike,
        }


class CVDEngine:
    """
    Heuristic order-flow engine.

    Since Roostoo doesn't provide tick-level trade data with
    aggressor-side tagging, we approximate using:
    - Price movement direction as a proxy for buy/sell pressure
    - Volume changes relative to moving averages
    - Spread changes as absorption indicators

    All outputs are explicitly marked as degraded-mode approximations.
    """

    def __init__(self, lookback: int = 50):
        self._lookback = lookback
        self._state: dict[str, CVDState] = {}
        self._volume_history: dict[str, list[float]] = {}
        self._delta_history: dict[str, list[float]] = {}

        logger.info("CVDEngine initialized — DEGRADED MODE (no real order-flow data)")

    def update(self, symbol: str, candles: List[Candle], ticker: Optional[TickerSnapshot] = None) -> CVDState:
        """
        Update CVD state for a symbol using available data.
        Always in degraded mode — heuristic approximation only.
        """
        if symbol not in self._state:
            self._state[symbol] = CVDState(degraded_mode=True)
            self._volume_history[symbol] = []
            self._delta_history[symbol] = []

        state = self._state[symbol]

        if len(candles) < 3:
            return state

        # Approximate delta using candle direction × volume
        # Bullish candle → positive delta, bearish → negative
        recent_candles = candles[-self._lookback:] if len(candles) >= self._lookback else candles

        cumulative_delta = 0.0
        for c in recent_candles:
            vol = c.volume if c.volume > 0 else c.coin_volume
            if c.is_bullish:
                # Approximate: bullish candle = more buying
                body_ratio = c.body_pct if c.range_size > 0 else 0.5
                delta = vol * body_ratio
            elif c.is_bearish:
                body_ratio = c.body_pct if c.range_size > 0 else 0.5
                delta = -vol * body_ratio
            else:
                delta = 0.0
            cumulative_delta += delta

        # Latest delta
        last_candle = candles[-1]
        last_vol = last_candle.volume if last_candle.volume > 0 else last_candle.coin_volume
        if last_candle.is_bullish:
            current_delta = last_vol * last_candle.body_pct
        elif last_candle.is_bearish:
            current_delta = -last_vol * last_candle.body_pct
        else:
            current_delta = 0.0

        # Track volume history for spike detection
        self._volume_history[symbol].append(last_vol)
        if len(self._volume_history[symbol]) > self._lookback:
            self._volume_history[symbol] = self._volume_history[symbol][-self._lookback:]

        self._delta_history[symbol].append(current_delta)
        if len(self._delta_history[symbol]) > self._lookback:
            self._delta_history[symbol] = self._delta_history[symbol][-self._lookback:]

        # Volume spike detection
        vol_history = self._volume_history[symbol]
        avg_vol = sum(vol_history) / len(vol_history) if vol_history else 0
        volume_spike = last_vol > avg_vol * 2.0 if avg_vol > 0 else False

        # Absorption detection (heuristic)
        # Absorption = high volume but price doesn't move much
        absorption_signal = "NONE"
        if volume_spike and last_candle.range_size > 0:
            if last_candle.body_pct < 0.30:
                # High volume but small body = absorption
                if last_candle.lower_wick > last_candle.upper_wick:
                    absorption_signal = "BUYING_ABSORPTION"
                else:
                    absorption_signal = "SELLING_ABSORPTION"

        # CVD trend
        delta_vals = self._delta_history[symbol]
        if len(delta_vals) >= 5:
            recent_sum = sum(delta_vals[-5:])
            if recent_sum > 0:
                trend = "BULLISH"
            elif recent_sum < 0:
                trend = "BEARISH"
            else:
                trend = "NEUTRAL"
        else:
            trend = "NEUTRAL"

        # Update state
        state.cvd_value = cumulative_delta
        state.delta_volume = current_delta
        state.absorption_signal = absorption_signal
        state.degraded_mode = True  # Always degraded without real order flow
        state.trend = trend
        state.volume_spike = volume_spike
        if ticker:
            state.last_update = ticker.timestamp

        return state

    def get_state(self, symbol: str) -> CVDState:
        """Get current CVD state for a symbol."""
        return self._state.get(symbol, CVDState(degraded_mode=True))

    def is_degraded(self) -> bool:
        """Always True — no real order-flow data available from Roostoo."""
        return True
