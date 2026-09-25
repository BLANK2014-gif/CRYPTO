"""
Roostoo Trading Bot — Market Regime Engine
============================================
Classifies the current market state to determine which strategies
are eligible to trade. The regime engine prevents blindly applying
one strategy across all conditions.

Regimes:
  TREND          — Directional market (ADX > threshold)
  RANGE          — Mean-reverting market (low ADX, price in VA)
  HIGH_VOLATILITY — Elevated ATR / vol percentile
  LOW_VOLATILITY  — Compressed ATR / vol percentile
  TRANSITION     — Changing regime, uncertain direction
  NO_TRADE       — Insufficient or unreliable data
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

from core.market_data import Candle, TickerSnapshot
from core.indicators import (
    compute_adx,
    compute_atr,
    compute_volatility_percentile,
    compute_vwap,
    compute_volume_profile,
    VolumeProfile,
)
from core.cvd_engine import CVDState

logger = logging.getLogger("regime_engine")


class MarketRegime(str, Enum):
    TREND = "TREND"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    TRANSITION = "TRANSITION"
    NO_TRADE = "NO_TRADE"


class TrendDirection(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


@dataclass
class RegimeState:
    """Complete regime classification for a symbol."""
    regime: MarketRegime = MarketRegime.NO_TRADE
    trend_direction: TrendDirection = TrendDirection.NEUTRAL
    adx: float = 0.0
    atr: float = 0.0
    atr_pct: float = 0.0         # ATR as % of price
    volatility_percentile: float = 50.0
    vwap: float = 0.0
    price_vs_vwap: str = "AT"     # ABOVE, BELOW, AT
    volume_profile: Optional[VolumeProfile] = None
    price_location: str = "NONE"  # IN_VALUE, ABOVE_VALUE, BELOW_VALUE
    cvd_trend: str = "NEUTRAL"
    confidence: float = 0.0
    eligible_strategies: List[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> Dict:
        return {
            "regime": self.regime.value,
            "trend_direction": self.trend_direction.value,
            "adx": round(self.adx, 2),
            "atr": round(self.atr, 4),
            "atr_pct": round(self.atr_pct, 4),
            "volatility_percentile": round(self.volatility_percentile, 1),
            "vwap": round(self.vwap, 2),
            "price_vs_vwap": self.price_vs_vwap,
            "price_location": self.price_location,
            "cvd_trend": self.cvd_trend,
            "confidence": round(self.confidence, 2),
            "eligible_strategies": self.eligible_strategies,
            "reason": self.reason,
            "vah": round(self.volume_profile.vah, 2) if self.volume_profile else 0.0,
            "poc": round(self.volume_profile.poc_price, 2) if self.volume_profile else 0.0,
            "val": round(self.volume_profile.val, 2) if self.volume_profile else 0.0,
        }


class RegimeEngine:
    """
    Market regime classifier.

    Evaluates ADX, ATR, volatility, VWAP, volume profile, and
    order-flow signals to classify current market conditions.
    """

    # Configurable thresholds
    ADX_TREND_THRESHOLD = 25.0     # ADX > 25 = trending
    ADX_STRONG_TREND = 40.0        # ADX > 40 = strong trend
    ADX_RANGE_THRESHOLD = 20.0     # ADX < 20 = ranging
    VOL_HIGH_PERCENTILE = 80.0     # > 80th pctl = high volatility
    VOL_LOW_PERCENTILE = 20.0      # < 20th pctl = low volatility
    VOL_EXTREME_PERCENTILE = 95.0  # > 95th = extreme

    def __init__(self):
        self._last_regime: Dict[str, RegimeState] = {}
        logger.info("RegimeEngine initialized")

    def classify(
        self,
        symbol: str,
        candles_5m: List[Candle],
        candles_15m: List[Candle],
        ticker: Optional[TickerSnapshot] = None,
        cvd: Optional[CVDState] = None,
        strategy_config: Optional[Dict] = None,
    ) -> RegimeState:
        """
        Classify market regime for a symbol.

        Uses multiple timeframe analysis (5m candles for detail,
        15m for confirmation).
        """
        state = RegimeState()

        # Insufficient data check
        if len(candles_5m) < 30:
            state.regime = MarketRegime.NO_TRADE
            state.reason = f"Insufficient candle data ({len(candles_5m)}/30 required)"
            self._last_regime[symbol] = state
            return state

        # Compute indicators
        adx, plus_di, minus_di = compute_adx(candles_5m)
        atr = compute_atr(candles_5m)
        vol_pctl = compute_volatility_percentile(candles_5m)
        vwap = compute_vwap(candles_5m)
        vp = compute_volume_profile(candles_5m)

        state.adx = adx
        state.atr = atr
        state.volatility_percentile = vol_pctl
        state.vwap = vwap
        state.volume_profile = vp

        # ATR as percentage of price
        current_price = candles_5m[-1].close if candles_5m else 0
        if current_price > 0:
            state.atr_pct = atr / current_price

        # Price vs VWAP
        if current_price > vwap * 1.002:
            state.price_vs_vwap = "ABOVE"
        elif current_price < vwap * 0.998:
            state.price_vs_vwap = "BELOW"
        else:
            state.price_vs_vwap = "AT"

        # Price location relative to value area
        if vp.vah > 0 and vp.val > 0:
            if current_price > vp.vah:
                state.price_location = "ABOVE_VALUE"
            elif current_price < vp.val:
                state.price_location = "BELOW_VALUE"
            else:
                state.price_location = "IN_VALUE"

        # Trend direction
        if plus_di > minus_di and adx > self.ADX_RANGE_THRESHOLD:
            state.trend_direction = TrendDirection.BULLISH
        elif minus_di > plus_di and adx > self.ADX_RANGE_THRESHOLD:
            state.trend_direction = TrendDirection.BEARISH
        else:
            state.trend_direction = TrendDirection.NEUTRAL

        # CVD integration
        if cvd:
            state.cvd_trend = cvd.trend

        # --- Regime classification logic ---

        # Extreme volatility → no trade
        if vol_pctl > self.VOL_EXTREME_PERCENTILE:
            state.regime = MarketRegime.NO_TRADE
            state.confidence = 0.8
            state.reason = f"Extreme volatility (pctl={vol_pctl:.0f})"
            self._last_regime[symbol] = state
            return state

        # Strong trend
        if adx > self.ADX_TREND_THRESHOLD:
            if vol_pctl > self.VOL_HIGH_PERCENTILE:
                state.regime = MarketRegime.HIGH_VOLATILITY
                state.eligible_strategies = ["LIQUIDITY_SWEEP"]
                state.confidence = 0.75
                state.reason = f"Trending with high volatility (ADX={adx:.1f}, vol_pctl={vol_pctl:.0f})"
            else:
                state.regime = MarketRegime.TREND
                state.eligible_strategies = ["LIQUIDITY_SWEEP"]
                state.confidence = min(0.9, adx / 50.0)
                state.reason = f"Trending market (ADX={adx:.1f})"

        # Range-bound
        elif adx < self.ADX_RANGE_THRESHOLD:
            if vol_pctl < self.VOL_LOW_PERCENTILE:
                state.regime = MarketRegime.LOW_VOLATILITY
                state.eligible_strategies = ["VALUE_AREA"]
                state.confidence = 0.7
                state.reason = f"Range with low volatility (ADX={adx:.1f}, vol_pctl={vol_pctl:.0f})"
            else:
                state.regime = MarketRegime.RANGE
                state.eligible_strategies = ["VALUE_AREA"]
                state.confidence = max(0.6, 1.0 - adx / 30.0)
                state.reason = f"Range-bound market (ADX={adx:.1f})"

        # Transition zone
        else:
            # Check if transitioning from previous regime
            prev = self._last_regime.get(symbol)
            if prev and prev.regime != MarketRegime.NO_TRADE:
                state.regime = MarketRegime.TRANSITION
                state.eligible_strategies = ["VALUE_AREA", "LIQUIDITY_SWEEP"]
                state.confidence = 0.5
                state.reason = f"Regime transition (ADX={adx:.1f}, was {prev.regime.value})"
            else:
                state.regime = MarketRegime.TRANSITION
                state.eligible_strategies = []
                state.confidence = 0.4
                state.reason = f"Unclear regime (ADX={adx:.1f})"

        # High volatility events can override to favor LIQUIDITY_SWEEP
        if vol_pctl > self.VOL_HIGH_PERCENTILE and "LIQUIDITY_SWEEP" not in state.eligible_strategies:
            state.eligible_strategies.append("LIQUIDITY_SWEEP")

        # Filter by strategy config
        if strategy_config:
            if not strategy_config.get("value_area", {}).get("enabled", True):
                state.eligible_strategies = [s for s in state.eligible_strategies if s != "VALUE_AREA"]
            if not strategy_config.get("liquidity_sweep", {}).get("enabled", True):
                state.eligible_strategies = [s for s in state.eligible_strategies if s != "LIQUIDITY_SWEEP"]

        self._last_regime[symbol] = state
        return state

    def get_regime(self, symbol: str) -> RegimeState:
        """Get last computed regime for a symbol."""
        return self._last_regime.get(symbol, RegimeState())

    def get_all_regimes(self) -> Dict[str, RegimeState]:
        return dict(self._last_regime)
