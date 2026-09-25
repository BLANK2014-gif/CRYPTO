"""
Roostoo Trading Bot — Technical Indicators
============================================
ATR, VWAP, ADX, Volume Profile, and structure detection.
All calculations are deterministic and operate on Candle data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np

from core.market_data import Candle, TickerSnapshot


# ---------------------------------------------------------------------------
# ATR — Average True Range
# ---------------------------------------------------------------------------

def compute_atr(candles: List[Candle], period: int = 14) -> float:
    """
    Compute ATR from completed candles.
    Returns 0.0 if insufficient data.
    """
    if len(candles) < period + 1:
        return 0.0

    true_ranges: list[float] = []
    for i in range(1, len(candles)):
        prev_close = candles[i - 1].close
        c = candles[i]
        tr = max(
            c.high - c.low,
            abs(c.high - prev_close),
            abs(c.low - prev_close),
        )
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return 0.0

    # Wilder's smoothing (EMA-like)
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period

    return atr


def compute_atr_series(candles: List[Candle], period: int = 14) -> List[float]:
    """Compute ATR for each candle (returns list aligned with candles)."""
    if len(candles) < period + 1:
        return [0.0] * len(candles)

    true_ranges: list[float] = [0.0]  # First candle has no TR
    for i in range(1, len(candles)):
        prev_close = candles[i - 1].close
        c = candles[i]
        tr = max(
            c.high - c.low,
            abs(c.high - prev_close),
            abs(c.low - prev_close),
        )
        true_ranges.append(tr)

    atr_values: list[float] = [0.0] * len(candles)
    if len(true_ranges) > period:
        atr = sum(true_ranges[1:period + 1]) / period
        atr_values[period] = atr
        for i in range(period + 1, len(candles)):
            atr = (atr * (period - 1) + true_ranges[i]) / period
            atr_values[i] = atr

    return atr_values


# ---------------------------------------------------------------------------
# VWAP — Volume Weighted Average Price
# ---------------------------------------------------------------------------

def compute_vwap(candles: List[Candle]) -> float:
    """
    Compute session VWAP from candles.
    Uses typical price × volume weighting.
    """
    if not candles:
        return 0.0

    cum_tp_vol = 0.0
    cum_vol = 0.0

    for c in candles:
        typical_price = (c.high + c.low + c.close) / 3.0
        vol = c.volume if c.volume > 0 else c.coin_volume
        cum_tp_vol += typical_price * vol
        cum_vol += vol

    if cum_vol <= 0:
        return candles[-1].close if candles else 0.0

    return cum_tp_vol / cum_vol


def compute_vwap_series(candles: List[Candle]) -> List[float]:
    """Compute running VWAP for each candle."""
    result: list[float] = []
    cum_tp_vol = 0.0
    cum_vol = 0.0

    for c in candles:
        typical_price = (c.high + c.low + c.close) / 3.0
        vol = c.volume if c.volume > 0 else c.coin_volume
        cum_tp_vol += typical_price * vol
        cum_vol += vol
        vwap = cum_tp_vol / cum_vol if cum_vol > 0 else c.close
        result.append(vwap)

    return result


# ---------------------------------------------------------------------------
# ADX — Average Directional Index
# ---------------------------------------------------------------------------

def compute_adx(candles: List[Candle], period: int = 14) -> Tuple[float, float, float]:
    """
    Compute ADX, +DI, -DI.
    Returns (adx, plus_di, minus_di). All 0 if insufficient data.
    """
    if len(candles) < period * 2:
        return 0.0, 0.0, 0.0

    plus_dm_list: list[float] = []
    minus_dm_list: list[float] = []
    tr_list: list[float] = []

    for i in range(1, len(candles)):
        high = candles[i].high
        low = candles[i].low
        prev_high = candles[i - 1].high
        prev_low = candles[i - 1].low
        prev_close = candles[i - 1].close

        # True Range
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)

        # Directional Movement
        up_move = high - prev_high
        down_move = prev_low - low

        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

    if len(tr_list) < period:
        return 0.0, 0.0, 0.0

    # Smooth with Wilder's method
    def wilder_smooth(values: List[float], p: int) -> List[float]:
        smoothed: list[float] = [sum(values[:p])]
        for v in values[p:]:
            smoothed.append(smoothed[-1] - smoothed[-1] / p + v)
        return smoothed

    smooth_tr = wilder_smooth(tr_list, period)
    smooth_pdm = wilder_smooth(plus_dm_list, period)
    smooth_mdm = wilder_smooth(minus_dm_list, period)

    # DI values
    dx_list: list[float] = []
    plus_di_last = 0.0
    minus_di_last = 0.0

    for i in range(len(smooth_tr)):
        if smooth_tr[i] == 0:
            continue
        pdi = 100.0 * smooth_pdm[i] / smooth_tr[i]
        mdi = 100.0 * smooth_mdm[i] / smooth_tr[i]
        plus_di_last = pdi
        minus_di_last = mdi

        denom = pdi + mdi
        if denom > 0:
            dx = 100.0 * abs(pdi - mdi) / denom
            dx_list.append(dx)

    if len(dx_list) < period:
        return 0.0, plus_di_last, minus_di_last

    # Smooth DX to get ADX
    adx = sum(dx_list[:period]) / period
    for dx in dx_list[period:]:
        adx = (adx * (period - 1) + dx) / period

    return adx, plus_di_last, minus_di_last


# ---------------------------------------------------------------------------
# Volume Profile
# ---------------------------------------------------------------------------

@dataclass
class VolumeProfile:
    """Volume profile with Value Area, POC, VAH, VAL."""
    poc_price: float = 0.0      # Point of Control
    vah: float = 0.0            # Value Area High
    val: float = 0.0            # Value Area Low
    total_volume: float = 0.0
    value_area_volume: float = 0.0
    bins: List[Tuple[float, float]] = field(default_factory=list)  # (price, volume)
    num_bins: int = 0


def compute_volume_profile(
    candles: List[Candle],
    num_bins: int = 24,
    value_area_pct: float = 0.70,
) -> VolumeProfile:
    """
    Compute volume profile from candles.

    Args:
        candles: List of completed candles
        num_bins: Number of price bins
        value_area_pct: Fraction of total volume for value area (typically 70%)

    Returns:
        VolumeProfile with POC, VAH, VAL
    """
    if len(candles) < 3:
        return VolumeProfile()

    # Determine price range
    all_highs = [c.high for c in candles]
    all_lows = [c.low for c in candles]
    price_high = max(all_highs)
    price_low = min(all_lows)

    if price_high <= price_low:
        return VolumeProfile()

    bin_size = (price_high - price_low) / num_bins
    if bin_size <= 0:
        return VolumeProfile()

    # Distribute volume across bins
    volume_bins = [0.0] * num_bins
    bin_prices = [price_low + (i + 0.5) * bin_size for i in range(num_bins)]

    for c in candles:
        vol = c.volume if c.volume > 0 else c.coin_volume
        if vol <= 0:
            continue
        # Distribute candle volume proportionally across its range
        candle_low_bin = max(0, int((c.low - price_low) / bin_size))
        candle_high_bin = min(num_bins - 1, int((c.high - price_low) / bin_size))

        bins_covered = candle_high_bin - candle_low_bin + 1
        vol_per_bin = vol / bins_covered if bins_covered > 0 else vol

        for b in range(candle_low_bin, candle_high_bin + 1):
            if 0 <= b < num_bins:
                volume_bins[b] += vol_per_bin

    total_volume = sum(volume_bins)
    if total_volume <= 0:
        return VolumeProfile()

    # Find POC (bin with highest volume)
    poc_bin = int(np.argmax(volume_bins))
    poc_price = bin_prices[poc_bin]

    # Build value area (70% of volume, expanding outward from POC)
    va_volume = volume_bins[poc_bin]
    target_volume = total_volume * value_area_pct
    low_idx = poc_bin
    high_idx = poc_bin

    while va_volume < target_volume and (low_idx > 0 or high_idx < num_bins - 1):
        expand_low = volume_bins[low_idx - 1] if low_idx > 0 else 0
        expand_high = volume_bins[high_idx + 1] if high_idx < num_bins - 1 else 0

        if expand_low >= expand_high and low_idx > 0:
            low_idx -= 1
            va_volume += volume_bins[low_idx]
        elif high_idx < num_bins - 1:
            high_idx += 1
            va_volume += volume_bins[high_idx]
        elif low_idx > 0:
            low_idx -= 1
            va_volume += volume_bins[low_idx]
        else:
            break

    val = bin_prices[low_idx] - bin_size / 2.0
    vah = bin_prices[high_idx] + bin_size / 2.0

    bins = [(bin_prices[i], volume_bins[i]) for i in range(num_bins)]

    return VolumeProfile(
        poc_price=poc_price,
        vah=vah,
        val=val,
        total_volume=total_volume,
        value_area_volume=va_volume,
        bins=bins,
        num_bins=num_bins,
    )


# ---------------------------------------------------------------------------
# Swing High / Low Detection
# ---------------------------------------------------------------------------

@dataclass
class SwingPoint:
    """A detected swing high or low."""
    price: float
    index: int
    is_high: bool
    timestamp: float = 0.0


def detect_swing_points(
    candles: List[Candle], lookback: int = 5,
) -> Tuple[List[SwingPoint], List[SwingPoint]]:
    """
    Detect swing highs and swing lows.

    A swing high is a candle whose high is higher than `lookback` candles
    on each side. Similarly for swing lows.

    Returns: (swing_highs, swing_lows)
    """
    swing_highs: list[SwingPoint] = []
    swing_lows: list[SwingPoint] = []

    if len(candles) < lookback * 2 + 1:
        return swing_highs, swing_lows

    for i in range(lookback, len(candles) - lookback):
        c = candles[i]

        # Check swing high
        is_swing_high = all(
            c.high >= candles[i - j].high and c.high >= candles[i + j].high
            for j in range(1, lookback + 1)
        )
        if is_swing_high:
            swing_highs.append(SwingPoint(
                price=c.high,
                index=i,
                is_high=True,
                timestamp=c.timestamp,
            ))

        # Check swing low
        is_swing_low = all(
            c.low <= candles[i - j].low and c.low <= candles[i + j].low
            for j in range(1, lookback + 1)
        )
        if is_swing_low:
            swing_lows.append(SwingPoint(
                price=c.low,
                index=i,
                is_high=False,
                timestamp=c.timestamp,
            ))

    return swing_highs, swing_lows


# ---------------------------------------------------------------------------
# Equal Highs / Equal Lows
# ---------------------------------------------------------------------------

def detect_equal_levels(
    swing_points: List[SwingPoint],
    tolerance_pct: float = 0.001,
) -> List[Tuple[SwingPoint, SwingPoint]]:
    """
    Detect equal highs or equal lows (liquidity pools).

    Two swing points at nearly the same price suggest resting liquidity.
    """
    equal_pairs: list[Tuple[SwingPoint, SwingPoint]] = []

    for i in range(len(swing_points)):
        for j in range(i + 1, len(swing_points)):
            p1 = swing_points[i].price
            p2 = swing_points[j].price
            avg = (p1 + p2) / 2.0
            if avg > 0 and abs(p1 - p2) / avg < tolerance_pct:
                equal_pairs.append((swing_points[i], swing_points[j]))

    return equal_pairs


# ---------------------------------------------------------------------------
# Fair Value Gap (FVG) Detection
# ---------------------------------------------------------------------------

@dataclass
class FairValueGap:
    """A Fair Value Gap — three-candle imbalance."""
    top: float          # Upper boundary
    bottom: float       # Lower boundary
    is_bullish: bool    # True = bullish FVG (gap above)
    index: int          # Index of the middle candle
    timestamp: float = 0.0
    mitigated: bool = False

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def size(self) -> float:
        return self.top - self.bottom


def detect_fvg(
    candles: List[Candle],
    min_gap_pct: float = 0.001,
) -> List[FairValueGap]:
    """
    Detect Fair Value Gaps (imbalances).

    Bullish FVG: candle[i-1].high < candle[i+1].low
    Bearish FVG: candle[i-1].low > candle[i+1].high
    """
    fvgs: list[FairValueGap] = []

    if len(candles) < 3:
        return fvgs

    for i in range(1, len(candles) - 1):
        c_prev = candles[i - 1]
        c_mid = candles[i]
        c_next = candles[i + 1]

        # Bullish FVG
        if c_next.low > c_prev.high:
            gap = c_next.low - c_prev.high
            if c_prev.high > 0 and gap / c_prev.high >= min_gap_pct:
                fvgs.append(FairValueGap(
                    top=c_next.low,
                    bottom=c_prev.high,
                    is_bullish=True,
                    index=i,
                    timestamp=c_mid.timestamp,
                ))

        # Bearish FVG
        if c_prev.low > c_next.high:
            gap = c_prev.low - c_next.high
            if c_next.high > 0 and gap / c_next.high >= min_gap_pct:
                fvgs.append(FairValueGap(
                    top=c_prev.low,
                    bottom=c_next.high,
                    is_bullish=False,
                    index=i,
                    timestamp=c_mid.timestamp,
                ))

    return fvgs


# ---------------------------------------------------------------------------
# Order Block Detection
# ---------------------------------------------------------------------------

@dataclass
class OrderBlock:
    """A detected order block (last opposing candle before displacement)."""
    high: float
    low: float
    is_bullish: bool    # Bullish OB = last bearish candle before bullish move
    index: int
    timestamp: float = 0.0

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2.0


def detect_order_blocks(
    candles: List[Candle],
    displacement_min_body_pct: float = 0.60,
) -> List[OrderBlock]:
    """
    Detect order blocks.

    A bullish OB is the last bearish candle before a strong bullish move.
    A bearish OB is the last bullish candle before a strong bearish move.
    """
    obs: list[OrderBlock] = []

    if len(candles) < 3:
        return obs

    for i in range(1, len(candles)):
        c_prev = candles[i - 1]
        c_curr = candles[i]

        # Check for displacement (strong candle)
        if c_curr.body_pct < displacement_min_body_pct:
            continue

        # Bullish OB: previous candle bearish, current candle is strongly bullish
        if c_prev.is_bearish and c_curr.is_bullish:
            if c_curr.close > c_prev.high:  # Displacement
                obs.append(OrderBlock(
                    high=c_prev.high,
                    low=c_prev.low,
                    is_bullish=True,
                    index=i - 1,
                    timestamp=c_prev.timestamp,
                ))

        # Bearish OB: previous candle bullish, current candle strongly bearish
        elif c_prev.is_bullish and c_curr.is_bearish:
            if c_curr.close < c_prev.low:  # Displacement
                obs.append(OrderBlock(
                    high=c_prev.high,
                    low=c_prev.low,
                    is_bullish=False,
                    index=i - 1,
                    timestamp=c_prev.timestamp,
                ))

    return obs


# ---------------------------------------------------------------------------
# Volatility Percentile
# ---------------------------------------------------------------------------

def compute_volatility_percentile(
    candles: List[Candle], period: int = 50,
) -> float:
    """
    Compute current ATR percentile vs historical ATR values.
    Returns percentile 0-100.
    """
    if len(candles) < period + 14:
        return 50.0  # Default to middle

    atr_series = compute_atr_series(candles, period=14)
    valid_atrs = [a for a in atr_series[-period:] if a > 0]

    if len(valid_atrs) < 10:
        return 50.0

    current_atr = valid_atrs[-1]
    count_below = sum(1 for a in valid_atrs if a <= current_atr)
    return (count_below / len(valid_atrs)) * 100.0


# ---------------------------------------------------------------------------
# Momentum / RSI
# ---------------------------------------------------------------------------

def compute_rsi(candles: List[Candle], period: int = 14) -> float:
    """Compute RSI (Relative Strength Index)."""
    if len(candles) < period + 1:
        return 50.0

    gains: list[float] = []
    losses: list[float] = []

    for i in range(1, len(candles)):
        diff = candles[i].close - candles[i - 1].close
        if diff > 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(diff))

    if len(gains) < period:
        return 50.0

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------

def compute_ema(values: List[float], period: int) -> List[float]:
    """Compute Exponential Moving Average."""
    if len(values) < period:
        return [0.0] * len(values)

    multiplier = 2.0 / (period + 1)
    ema = [0.0] * len(values)
    ema[period - 1] = sum(values[:period]) / period

    for i in range(period, len(values)):
        ema[i] = (values[i] - ema[i - 1]) * multiplier + ema[i - 1]

    return ema
