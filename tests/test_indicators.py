"""
Unit tests for Technical Indicators.
Covers ATR, VWAP, ADX, Volume Profile (POC/VAH/VAL), Swing Points,
Equal Levels, Fair Value Gaps (FVG), Order Blocks, Volatility Percentile, RSI, EMA.
"""

import time
import pytest
from core.market_data import Candle
from core.indicators import (
    compute_atr,
    compute_atr_series,
    compute_vwap,
    compute_vwap_series,
    compute_adx,
    compute_volume_profile,
    detect_swing_points,
    detect_equal_levels,
    detect_fvg,
    detect_order_blocks,
    compute_volatility_percentile,
    compute_rsi,
    compute_ema,
    VolumeProfile,
    SwingPoint,
)


def create_candles(prices_hlcv: list[tuple[float, float, float, float, float]]) -> list[Candle]:
    """Helper to create list of candles: (open, high, low, close, volume)."""
    candles = []
    base_ts = 1700000000.0
    for i, (o, h, l, c, v) in enumerate(prices_hlcv):
        candles.append(Candle(
            symbol="BTC/USD",
            open=o,
            high=h,
            low=l,
            close=c,
            volume=v,
            coin_volume=v / c if c > 0 else 0,
            timestamp=base_ts + i * 300,
            close_time=base_ts + (i + 1) * 300,
            is_complete=True,
        ))
    return candles


class TestATR:
    def test_insufficient_data(self):
        candles = create_candles([(100, 105, 95, 102, 10)] * 5)
        assert compute_atr(candles, period=14) == 0.0

    def test_atr_computation(self):
        # 20 candles with 10-point ranges
        raw = [(100 + i, 110 + i, 90 + i, 100 + i, 50) for i in range(25)]
        candles = create_candles(raw)
        atr = compute_atr(candles, period=14)
        assert atr > 0.0
        # True range is ~20 each step
        assert 18.0 <= atr <= 22.0

    def test_atr_series_length(self):
        raw = [(100 + i, 105 + i, 95 + i, 100 + i, 50) for i in range(20)]
        candles = create_candles(raw)
        series = compute_atr_series(candles, period=14)
        assert len(series) == len(candles)


class TestVWAP:
    def test_vwap_empty(self):
        assert compute_vwap([]) == 0.0

    def test_vwap_calculation(self):
        raw = [
            (100, 110, 90, 100, 100),  # typical price = 100, vol = 100 -> tp*v = 10000
            (100, 120, 100, 110, 200), # typical price = 110, vol = 200 -> tp*v = 22000
        ]
        candles = create_candles(raw)
        vwap = compute_vwap(candles)
        # Expected: (10000 + 22000) / (100 + 200) = 32000 / 300 = 106.666...
        assert pytest.approx(vwap, rel=1e-3) == 106.6667

    def test_vwap_series(self):
        raw = [
            (100, 105, 95, 100, 50),
            (100, 115, 95, 110, 50),
        ]
        candles = create_candles(raw)
        series = compute_vwap_series(candles)
        assert len(series) == 2
        assert series[0] == 100.0


class TestADX:
    def test_adx_insufficient_data(self):
        candles = create_candles([(100, 105, 95, 100, 50)] * 10)
        adx, pdi, mdi = compute_adx(candles, period=14)
        assert adx == 0.0

    def test_adx_strong_trend(self):
        # Monotonically increasing prices
        raw = [(100 + i * 2, 103 + i * 2, 99 + i * 2, 102 + i * 2, 100) for i in range(35)]
        candles = create_candles(raw)
        adx, pdi, mdi = compute_adx(candles, period=14)
        assert adx > 20.0  # Strong directional trend
        assert pdi > mdi   # Bullish DI greater than Bearish DI


class TestVolumeProfile:
    def test_volume_profile_poc_vah_val(self):
        raw = [
            (100, 102, 98, 100, 10),
            (101, 105, 99, 103, 100),  # Most volume at ~102-104
            (103, 106, 102, 104, 80),
            (104, 110, 103, 108, 10),
        ]
        candles = create_candles(raw)
        vp = compute_volume_profile(candles, num_bins=10, value_area_pct=0.70)
        assert vp.total_volume > 0
        assert vp.poc_price > 0
        assert vp.val <= vp.poc_price <= vp.vah
        assert vp.value_area_volume <= vp.total_volume


class TestSwingPointsAndStructure:
    def test_detect_swing_high_and_low(self):
        # Create a peak at index 5 and trough at index 10
        raw = [
            (100, 101, 99, 100, 10),
            (101, 102, 100, 101, 10),
            (102, 103, 101, 102, 10),
            (103, 105, 102, 104, 10),
            (104, 107, 103, 106, 10),
            (106, 120, 105, 110, 10),  # SWING HIGH at index 5
            (108, 109, 106, 107, 10),
            (106, 107, 104, 105, 10),
            (104, 105, 102, 103, 10),
            (102, 103, 100, 101, 10),
            (99, 100, 80, 95, 10),    # SWING LOW at index 10
            (95, 98, 94, 97, 10),
            (97, 101, 96, 100, 10),
            (100, 102, 99, 101, 10),
        ]
        candles = create_candles(raw)
        highs, lows = detect_swing_points(candles, lookback=2)
        assert any(h.price == 120 for h in highs)
        assert any(l.price == 80 for l in lows)

    def test_detect_equal_levels(self):
        pts = [
            SwingPoint(price=100.0, index=2, is_high=True),
            SwingPoint(price=100.05, index=8, is_high=True),  # within 0.1%
            SwingPoint(price=120.0, index=15, is_high=True),
        ]
        eq = detect_equal_levels(pts, tolerance_pct=0.001)
        assert len(eq) == 1
        assert eq[0][0].price == 100.0
        assert eq[0][1].price == 100.05

    def test_detect_fvg(self):
        raw = [
            (100, 102, 98, 101, 10),    # c0: high=102
            (102, 112, 101, 111, 50),   # c1: big bullish displacement
            (111, 115, 106, 114, 20),   # c2: low=106 -> Gap between 102 and 106!
        ]
        candles = create_candles(raw)
        fvgs = detect_fvg(candles, min_gap_pct=0.001)
        assert len(fvgs) == 1
        assert fvgs[0].is_bullish is True
        assert fvgs[0].bottom == 102.0
        assert fvgs[0].top == 106.0

    def test_detect_order_blocks(self):
        raw = [
            (100, 102, 98, 99, 10),     # Bearish candle (c0)
            (99, 110, 99, 109, 100),    # Strongly bullish displacement candle (c1)
            (109, 112, 108, 111, 20),
        ]
        candles = create_candles(raw)
        obs = detect_order_blocks(candles, displacement_min_body_pct=0.60)
        assert len(obs) >= 1
        assert obs[0].is_bullish is True


class TestRSIAndEMA:
    def test_rsi_bounds_and_trend(self):
        # Monotonically increasing -> RSI should be high
        raw = [(100 + i, 101 + i, 99 + i, 100.5 + i, 50) for i in range(25)]
        candles = create_candles(raw)
        rsi = compute_rsi(candles, period=14)
        assert 70.0 <= rsi <= 100.0

    def test_ema_computation(self):
        values = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
        ema = compute_ema(values, period=3)
        assert len(ema) == len(values)
        assert ema[0] == 0.0
        assert ema[1] == 0.0
        assert ema[2] == 11.0  # SMA of first 3 is (10+11+12)/3 = 11.0
        assert ema[-1] > ema[2]
