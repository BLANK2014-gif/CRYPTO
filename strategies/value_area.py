"""
Roostoo Trading Bot — Strategy A: Volume Profile & Value Area Rotation
========================================================================
VALUE_AREA strategy for consolidating / range-bound sessions.

Setup:
  LONG  — Price sweeps below VAL, fails to sustain, closes back above VAL.
           This is a failed auction / value acceptance pattern.
  SHORT — Price sweeps above VAH, shows volume exhaustion, closes below VAH.
           For spot: reduce/exit long exposure.

Key levels: VAH, VAL, POC, VWAP
TP1 = 50% scale-out at Session POC
TP2 = Remaining 50% at VAH (for longs)
Stop = ~1 ATR below lowest wick of VAL sweep (configurable 0.5% alt)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from core.market_data import Candle, TickerSnapshot
from core.regime_engine import RegimeState
from core.cvd_engine import CVDState
from core.indicators import (
    compute_atr,
    compute_vwap,
    compute_volume_profile,
    VolumeProfile,
)
from strategies.base import BaseStrategy, Signal, SignalDirection

logger = logging.getLogger("strategy.value_area")


class ValueAreaStrategy(BaseStrategy):
    """
    Strategy A — Volume Profile & Value Area Rotation.

    Trades mean reversion within the value area during
    range-bound / consolidating market conditions.
    """

    def __init__(
        self,
        min_confidence: float = 0.70,
        vah_reclaim_candles: int = 3,
        volume_profile_periods: int = 48,
        tp1_scale_out_pct: float = 0.50,
        stop_alt_pct: float = 0.005,
    ):
        super().__init__("VALUE_AREA")
        self._min_confidence = min_confidence
        self._reclaim_candles = vah_reclaim_candles
        self._vp_periods = volume_profile_periods
        self._tp1_scale_pct = tp1_scale_out_pct
        self._stop_alt_pct = stop_alt_pct

    def evaluate(
        self,
        symbol: str,
        candles_5m: List[Candle],
        candles_15m: List[Candle],
        ticker: TickerSnapshot,
        regime: RegimeState,
        cvd: CVDState,
        portfolio_context: Dict[str, Any],
    ) -> Optional[Signal]:
        """Evaluate value area rotation setup."""

        if not self._enabled:
            return None

        # Need enough candles for volume profile
        if len(candles_5m) < self._vp_periods:
            return None

        # Compute indicators
        vp_candles = candles_5m[-self._vp_periods:]
        vp = compute_volume_profile(vp_candles)
        atr = compute_atr(candles_5m)
        vwap = compute_vwap(candles_5m[-48:])

        if vp.poc_price <= 0 or vp.vah <= 0 or vp.val <= 0 or atr <= 0:
            return None

        current_price = ticker.last_price
        if current_price <= 0:
            return None

        # Check for existing position
        has_position = portfolio_context.get("has_position", False)
        position_side = portfolio_context.get("position_side", None)

        # --- LONG SETUP: VAL reclaim ---
        long_signal = self._check_val_reclaim(
            symbol, candles_5m, ticker, vp, atr, vwap, cvd, has_position,
        )
        if long_signal:
            return long_signal

        # --- SHORT / DE-RISK SETUP: VAH rejection ---
        derisk_signal = self._check_vah_rejection(
            symbol, candles_5m, ticker, vp, atr, vwap, cvd,
            has_position, position_side,
        )
        if derisk_signal:
            return derisk_signal

        return None

    def _check_val_reclaim(
        self,
        symbol: str,
        candles: List[Candle],
        ticker: TickerSnapshot,
        vp: VolumeProfile,
        atr: float,
        vwap: float,
        cvd: CVDState,
        has_position: bool,
    ) -> Optional[Signal]:
        """
        Long setup: Price breaches below VAL, selling fails to sustain,
        a candle closes back above VAL (failed auction / value acceptance).
        """
        if has_position:
            return None

        if len(candles) < self._reclaim_candles + 2:
            return None

        recent = candles[-(self._reclaim_candles + 2):]

        # 1. Find a candle that wicked below VAL
        sweep_found = False
        sweep_low = float("inf")
        for c in recent[:-1]:
            if c.low < vp.val:
                sweep_found = True
                sweep_low = min(sweep_low, c.low)

        if not sweep_found:
            return None

        # 2. Most recent candle must close ABOVE VAL (reclaim)
        last_candle = candles[-1]
        if last_candle.close < vp.val:
            return None

        # 3. The reclaim candle should be bullish or at least not strongly bearish
        if last_candle.is_bearish and last_candle.body_pct > 0.5:
            return None

        # 4. Confirmation: current tick price also above VAL
        if ticker.last_price < vp.val:
            return None

        # Calculate confidence
        confidence = 0.60

        # Boost: strong body on reclaim candle
        if last_candle.is_bullish and last_candle.body_pct > 0.50:
            confidence += 0.10

        # Boost: price near VWAP support
        if ticker.last_price < vwap * 1.01:
            confidence += 0.05

        # Boost: CVD bullish
        if cvd.trend == "BULLISH":
            confidence += 0.05

        # Boost: volume spike on reclaim
        if cvd.volume_spike:
            confidence += 0.05

        # Penalty: wide spread
        if ticker.spread_bps > 30:
            confidence -= 0.05

        confidence = max(0.0, min(1.0, confidence))

        if confidence < self._min_confidence:
            return None

        # Entry / Stop / TP
        entry_price = ticker.last_price

        # Stop: ~1 ATR below the sweep wick low, or 0.5% below
        stop_atr = sweep_low - atr
        stop_pct = entry_price * (1.0 - self._stop_alt_pct)
        stop_loss = max(stop_atr, stop_pct)

        # TP1: POC (50% scale-out)
        tp1 = vp.poc_price
        # TP2: VAH (remaining 50%)
        tp2 = vp.vah

        # Validate R:R
        risk = entry_price - stop_loss
        if risk <= 0:
            return None

        reward = tp1 - entry_price
        rr = reward / risk if risk > 0 else 0

        if rr < 1.0:
            return None

        return self._make_signal(
            symbol=symbol,
            direction=SignalDirection.BUY,
            strategy=self.name,
            confidence=confidence,
            entry_price=round(entry_price, 2),
            stop_loss=round(stop_loss, 2),
            take_profit_1=round(tp1, 2),
            take_profit_2=round(tp2, 2),
            expected_rr=round(rr, 2),
            reason=f"VAL reclaim at {vp.val:.2f}, sweep low={sweep_low:.2f}, POC={vp.poc_price:.2f}",
            timestamp=time.time(),
            order_type="LIMIT",
            scale_out_pct=self._tp1_scale_pct,
            metadata={
                "vah": vp.vah, "val": vp.val, "poc": vp.poc_price,
                "vwap": vwap, "atr": atr, "sweep_low": sweep_low,
            },
        )

    def _check_vah_rejection(
        self,
        symbol: str,
        candles: List[Candle],
        ticker: TickerSnapshot,
        vp: VolumeProfile,
        atr: float,
        vwap: float,
        cvd: CVDState,
        has_position: bool,
        position_side: Optional[str],
    ) -> Optional[Signal]:
        """
        Short / de-risk setup: Price sweeps above VAH, shows volume exhaustion,
        closes back below VAH. For spot: reduce/exit long exposure.
        """
        if len(candles) < self._reclaim_candles + 2:
            return None

        recent = candles[-(self._reclaim_candles + 2):]

        # 1. Find a candle that wicked above VAH
        sweep_found = False
        sweep_high = 0.0
        for c in recent[:-1]:
            if c.high > vp.vah:
                sweep_found = True
                sweep_high = max(sweep_high, c.high)

        if not sweep_found:
            return None

        # 2. Most recent candle must close BELOW VAH (rejection)
        last_candle = candles[-1]
        if last_candle.close > vp.vah:
            return None

        # 3. The rejection candle should be bearish or show exhaustion
        if last_candle.is_bullish and last_candle.body_pct > 0.5:
            return None

        # 4. Current tick also below VAH
        if ticker.last_price > vp.vah:
            return None

        # Calculate confidence
        confidence = 0.60

        if last_candle.is_bearish and last_candle.body_pct > 0.50:
            confidence += 0.10

        if cvd.trend == "BEARISH":
            confidence += 0.05

        if cvd.absorption_signal == "SELLING_ABSORPTION":
            confidence += 0.05

        if ticker.last_price > vwap:
            confidence += 0.03  # Still above VWAP = stronger rejection setup

        confidence = max(0.0, min(1.0, confidence))

        if confidence < self._min_confidence:
            return None

        # For spot: this is a de-risk / sell signal
        if has_position and position_side == "LONG":
            # De-risk existing long
            stop_loss = sweep_high + atr  # Stop above sweep high
            tp1 = vp.poc_price
            tp2 = vp.val

            risk = stop_loss - ticker.last_price
            reward = ticker.last_price - tp1
            rr = reward / risk if risk > 0 else 0

            return self._make_signal(
                symbol=symbol,
                direction=SignalDirection.DERISK,
                strategy=self.name,
                confidence=confidence,
                entry_price=round(ticker.last_price, 2),
                stop_loss=round(stop_loss, 2),
                take_profit_1=round(tp1, 2),
                take_profit_2=round(tp2, 2),
                expected_rr=round(rr, 2),
                reason=f"VAH rejection at {vp.vah:.2f}, de-risk long. sweep_high={sweep_high:.2f}",
                timestamp=time.time(),
                order_type="MARKET",
                urgency="HIGH",
                metadata={
                    "vah": vp.vah, "val": vp.val, "poc": vp.poc_price,
                    "vwap": vwap, "atr": atr, "sweep_high": sweep_high,
                },
            )
        elif not has_position:
            # Open short position via v6/short_open
            stop_loss = sweep_high + atr
            tp1 = vp.poc_price
            tp2 = vp.val

            risk = stop_loss - ticker.last_price
            reward = ticker.last_price - tp1
            rr = reward / risk if risk > 0 else 0

            if rr < 1.0:
                return None

            return self._make_signal(
                symbol=symbol,
                direction=SignalDirection.SELL,
                strategy=self.name,
                confidence=confidence,
                entry_price=round(ticker.last_price, 2),
                stop_loss=round(stop_loss, 2),
                take_profit_1=round(tp1, 2),
                take_profit_2=round(tp2, 2),
                expected_rr=round(rr, 2),
                reason=f"VAH rejection at {vp.vah:.2f}, short setup. sweep_high={sweep_high:.2f}",
                timestamp=time.time(),
                order_type="LIMIT",
                scale_out_pct=self._tp1_scale_pct,
                metadata={
                    "vah": vp.vah, "val": vp.val, "poc": vp.poc_price,
                    "vwap": vwap, "atr": atr, "sweep_high": sweep_high,
                },
            )

        return None
