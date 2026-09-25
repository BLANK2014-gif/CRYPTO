"""
Roostoo Trading Bot — Strategy B: Liquidity Sweep & Market Structure Shift
============================================================================
LIQUIDITY_SWEEP strategy for high-volatility session opens and stop cascades.

Setup:
  LONG  — Price wicks below swing low / EQL, liquidity is swept, subsequent
           candle aggressively breaks above lower-high structure with bullish
           displacement. Entry on FVG or OB retest.
  SHORT — Price spikes through swing high / EQH, fails to hold, bearish
           displacement. For spot: reduce/exit long exposure.

Entry: Limit on FVG / Order Block retest
Stop: Below absolute sweep wick low
TP1: 1:2 R:R (move stop to breakeven after TP1)
TP2: Opposing unmitigated buyside liquidity / structural high
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
    detect_swing_points,
    detect_equal_levels,
    detect_fvg,
    detect_order_blocks,
    FairValueGap,
    OrderBlock,
    SwingPoint,
)
from strategies.base import BaseStrategy, Signal, SignalDirection

logger = logging.getLogger("strategy.liquidity_sweep")


class LiquiditySweepStrategy(BaseStrategy):
    """
    Strategy B — Liquidity Sweep & Market Structure Shift.

    Trades liquidity grab reversals during high-volatility conditions.
    """

    def __init__(
        self,
        min_confidence: float = 0.75,
        swing_lookback: int = 5,
        displacement_min_body_pct: float = 0.60,
        fvg_min_gap_pct: float = 0.001,
        tp1_rr: float = 2.0,
    ):
        super().__init__("LIQUIDITY_SWEEP")
        self._min_confidence = min_confidence
        self._swing_lookback = swing_lookback
        self._displacement_body_pct = displacement_min_body_pct
        self._fvg_min_gap = fvg_min_gap_pct
        self._tp1_rr = tp1_rr

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
        """Evaluate liquidity sweep + MSS setup."""

        if not self._enabled:
            return None

        if len(candles_5m) < 30:
            return None

        current_price = ticker.last_price
        if current_price <= 0:
            return None

        atr = compute_atr(candles_5m)
        if atr <= 0:
            return None

        # Detect structure
        swing_highs, swing_lows = detect_swing_points(
            candles_5m, lookback=self._swing_lookback,
        )

        has_position = portfolio_context.get("has_position", False)
        position_side = portfolio_context.get("position_side", None)

        # --- LONG: Bullish liquidity sweep ---
        long_signal = self._check_bullish_sweep(
            symbol, candles_5m, ticker, atr, swing_highs, swing_lows,
            cvd, has_position,
        )
        if long_signal:
            return long_signal

        # --- SHORT / DE-RISK: Bearish liquidity sweep ---
        derisk_signal = self._check_bearish_sweep(
            symbol, candles_5m, ticker, atr, swing_highs, swing_lows,
            cvd, has_position, position_side,
        )
        if derisk_signal:
            return derisk_signal

        return None

    def _check_bullish_sweep(
        self,
        symbol: str,
        candles: List[Candle],
        ticker: TickerSnapshot,
        atr: float,
        swing_highs: List[SwingPoint],
        swing_lows: List[SwingPoint],
        cvd: CVDState,
        has_position: bool,
    ) -> Optional[Signal]:
        """
        Bullish setup:
        1. Price wicks below an important swing low or EQL
        2. Liquidity is swept
        3. Subsequent candle aggressively breaks above structure (MSS)
        4. Bullish displacement candle
        """
        if has_position:
            return None

        if not swing_lows:
            return None

        # Get recent swing lows (liquidity pools)
        recent_swing_lows = [sl for sl in swing_lows if sl.index > len(candles) - 30]
        if not recent_swing_lows:
            # Use older swing lows
            recent_swing_lows = swing_lows[-3:] if swing_lows else []

        if not recent_swing_lows:
            return None

        # Equal lows are extra juicy liquidity
        eql_pairs = detect_equal_levels(
            [s for s in swing_lows if not s.is_high], tolerance_pct=0.002,
        )

        # Find the most relevant liquidity level
        target_levels = [sl.price for sl in recent_swing_lows]
        for pair in eql_pairs:
            avg = (pair[0].price + pair[1].price) / 2
            target_levels.append(avg)

        if not target_levels:
            return None

        # Look for sweep + reclaim pattern in recent candles
        lookback = min(8, len(candles) - 1)
        recent = candles[-lookback:]

        sweep_detected = False
        sweep_low_price = float("inf")
        sweep_level = 0.0

        for candle in recent[:-1]:  # Exclude most recent
            for level in target_levels:
                if candle.low < level and candle.close >= level * 0.998:
                    # Wicked below but closed near or above
                    sweep_detected = True
                    sweep_low_price = min(sweep_low_price, candle.low)
                    sweep_level = level

        if not sweep_detected:
            return None

        # Check for MSS (Market Structure Shift) - bullish displacement after sweep
        last_candle = candles[-1]

        # The most recent candle should show bullish displacement
        if not last_candle.is_bullish:
            return None

        if last_candle.body_pct < self._displacement_body_pct:
            return None

        # MSS confirmation: candle should close above a recent lower high
        # Find the most recent lower high
        recent_highs = [c.high for c in candles[-10:-1]]
        if recent_highs:
            lower_high = min(recent_highs[-3:]) if len(recent_highs) >= 3 else min(recent_highs)
            if last_candle.close <= lower_high:
                return None  # Didn't break structure

        # Look for entry zone (FVG or OB retest)
        fvgs = detect_fvg(candles[-15:], min_gap_pct=self._fvg_min_gap)
        obs = detect_order_blocks(candles[-15:], self._displacement_body_pct)

        # Find bullish FVG or OB for entry
        entry_zone = None
        for fvg in reversed(fvgs):
            if fvg.is_bullish and fvg.bottom < ticker.last_price:
                entry_zone = ("FVG", fvg.midpoint)
                break

        if entry_zone is None:
            for ob in reversed(obs):
                if ob.is_bullish and ob.high < ticker.last_price:
                    entry_zone = ("OB", ob.midpoint)
                    break

        # Entry price: prefer limit at FVG/OB, fallback to current price
        if entry_zone:
            entry_price = entry_zone[1]
            entry_type = "LIMIT"
            entry_reason = f"Limit at {entry_zone[0]} retest"
        else:
            entry_price = ticker.last_price
            entry_type = "LIMIT"  # Still prefer limit near current price
            entry_reason = "Entry near displacement close"

        # Stop: immediately below the absolute sweep wick low
        stop_loss = sweep_low_price - (atr * 0.1)  # Tiny buffer

        # TP1: 1:2 R:R
        risk = entry_price - stop_loss
        if risk <= 0:
            return None

        tp1 = entry_price + (risk * self._tp1_rr)

        # TP2: opposing unmitigated buyside liquidity or structural high
        tp2 = tp1  # Default
        if swing_highs:
            upper_targets = [sh.price for sh in swing_highs if sh.price > tp1]
            if upper_targets:
                tp2 = min(upper_targets)

        rr = (tp1 - entry_price) / risk

        # Confidence calculation
        confidence = 0.65

        # Boost: strong displacement
        if last_candle.body_pct > 0.70:
            confidence += 0.08

        # Boost: equal lows swept (stronger liquidity grab)
        if eql_pairs:
            confidence += 0.07

        # Boost: FVG/OB entry available
        if entry_zone:
            confidence += 0.05

        # Boost: CVD confirms
        if cvd.trend == "BULLISH":
            confidence += 0.05

        # Boost: volume spike on sweep
        if cvd.volume_spike:
            confidence += 0.05

        # Penalty: wide spread
        if ticker.spread_bps > 30:
            confidence -= 0.05

        confidence = max(0.0, min(1.0, confidence))

        if confidence < self._min_confidence:
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
            reason=f"Bullish liquidity sweep below {sweep_level:.2f}, {entry_reason}",
            timestamp=time.time(),
            order_type=entry_type,
            scale_out_pct=0.50,
            metadata={
                "sweep_low": sweep_low_price,
                "sweep_level": sweep_level,
                "entry_zone": entry_zone,
                "displacement_body_pct": last_candle.body_pct,
                "atr": atr,
            },
        )

    def _check_bearish_sweep(
        self,
        symbol: str,
        candles: List[Candle],
        ticker: TickerSnapshot,
        atr: float,
        swing_highs: List[SwingPoint],
        swing_lows: List[SwingPoint],
        cvd: CVDState,
        has_position: bool,
        position_side: Optional[str],
    ) -> Optional[Signal]:
        """
        Bearish / de-risk setup:
        1. Price spikes through established swing high / EQH
        2. Fails to hold above
        3. Bearish displacement occurs
        4. Local micro-support breaks
        """
        if not swing_highs:
            return None

        recent_swing_highs = [sh for sh in swing_highs if sh.index > len(candles) - 30]
        if not recent_swing_highs:
            recent_swing_highs = swing_highs[-3:] if swing_highs else []

        if not recent_swing_highs:
            return None

        eqh_pairs = detect_equal_levels(
            [s for s in swing_highs if s.is_high], tolerance_pct=0.002,
        )

        target_levels = [sh.price for sh in recent_swing_highs]
        for pair in eqh_pairs:
            avg = (pair[0].price + pair[1].price) / 2
            target_levels.append(avg)

        # Look for sweep above + rejection
        lookback = min(8, len(candles) - 1)
        recent = candles[-lookback:]

        sweep_detected = False
        sweep_high_price = 0.0
        sweep_level = 0.0

        for candle in recent[:-1]:
            for level in target_levels:
                if candle.high > level and candle.close <= level * 1.002:
                    sweep_detected = True
                    sweep_high_price = max(sweep_high_price, candle.high)
                    sweep_level = level

        if not sweep_detected:
            return None

        # Bearish displacement
        last_candle = candles[-1]
        if not last_candle.is_bearish:
            return None

        if last_candle.body_pct < self._displacement_body_pct:
            return None

        # Confidence
        confidence = 0.65

        if last_candle.body_pct > 0.70:
            confidence += 0.08
        if eqh_pairs:
            confidence += 0.07
        if cvd.trend == "BEARISH":
            confidence += 0.05
        if cvd.volume_spike:
            confidence += 0.05
        if ticker.spread_bps > 30:
            confidence -= 0.05

        confidence = max(0.0, min(1.0, confidence))

        if confidence < self._min_confidence:
            return None

        # Stop: immediately above the sweep wick high
        stop_loss = sweep_high_price + (atr * 0.1)

        # Entry
        entry_price = ticker.last_price

        risk = stop_loss - entry_price
        if risk <= 0:
            return None

        tp1 = entry_price - (risk * self._tp1_rr)
        tp2 = tp1  # Default
        if swing_lows:
            lower_targets = [sl.price for sl in swing_lows if sl.price < tp1]
            if lower_targets:
                tp2 = max(lower_targets)  # Nearest lower target

        rr = (entry_price - tp1) / risk

        # For spot with existing position: de-risk
        if has_position and position_side == "LONG":
            return self._make_signal(
                symbol=symbol,
                direction=SignalDirection.DERISK,
                strategy=self.name,
                confidence=confidence,
                entry_price=round(entry_price, 2),
                stop_loss=round(stop_loss, 2),
                take_profit_1=round(tp1, 2),
                take_profit_2=round(tp2, 2),
                expected_rr=round(rr, 2),
                reason=f"Bearish sweep above {sweep_level:.2f}, de-risk long",
                timestamp=time.time(),
                order_type="MARKET",
                urgency="HIGH",
                metadata={
                    "sweep_high": sweep_high_price,
                    "sweep_level": sweep_level,
                    "atr": atr,
                },
            )
        elif not has_position:
            # Open short via v6/short_open
            return self._make_signal(
                symbol=symbol,
                direction=SignalDirection.SELL,
                strategy=self.name,
                confidence=confidence,
                entry_price=round(entry_price, 2),
                stop_loss=round(stop_loss, 2),
                take_profit_1=round(tp1, 2),
                take_profit_2=round(tp2, 2),
                expected_rr=round(rr, 2),
                reason=f"Bearish liquidity sweep above {sweep_level:.2f}, short setup",
                timestamp=time.time(),
                order_type="LIMIT",
                scale_out_pct=0.50,
                metadata={
                    "sweep_high": sweep_high_price,
                    "sweep_level": sweep_level,
                    "atr": atr,
                },
            )

        return None
