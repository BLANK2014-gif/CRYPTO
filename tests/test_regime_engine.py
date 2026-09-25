"""
Unit tests for Market Regime Engine.
Tests regime classification (TREND, RANGE, HIGH_VOLATILITY, LOW_VOLATILITY, NO_TRADE),
eligible strategies filtering, and trend direction.
"""

import pytest
from core.market_data import Candle
from core.regime_engine import RegimeEngine, MarketRegime, TrendDirection


def make_candle_list(count: int, start_price: float, trend_step: float = 0.0, noise: float = 1.0) -> list[Candle]:
    candles = []
    base_ts = 1700000000.0
    for i in range(count):
        mid = start_price + i * trend_step
        o = mid - 0.5
        c = mid + 0.5
        h = max(o, c) + noise
        l = min(o, c) - noise
        v = 1000.0
        candles.append(Candle(
            symbol="BTC/USD",
            open=o,
            high=h,
            low=l,
            close=c,
            volume=v,
            coin_volume=v / c,
            timestamp=base_ts + i * 300,
            close_time=base_ts + (i + 1) * 300,
            is_complete=True,
        ))
    return candles


class TestRegimeEngine:
    @pytest.fixture
    def engine(self):
        return RegimeEngine()

    def test_insufficient_candles_yields_no_trade(self, engine):
        candles_5m = make_candle_list(15, 50000.0)
        state = engine.classify("BTC/USD", candles_5m, [])
        assert state.regime == MarketRegime.NO_TRADE
        assert "Insufficient candle data" in state.reason

    def test_strong_trend_classification(self, engine):
        # 60 candles with strong upward steps -> high ADX
        candles_5m = make_candle_list(60, 50000.0, trend_step=50.0, noise=5.0)
        state = engine.classify("BTC/USD", candles_5m, [])
        assert state.regime in (MarketRegime.TREND, MarketRegime.HIGH_VOLATILITY)
        assert state.trend_direction == TrendDirection.BULLISH
        assert "LIQUIDITY_SWEEP" in state.eligible_strategies

    def test_ranging_classification(self, engine):
        # 60 candles oscillating in a tight range -> low ADX
        candles = []
        base_ts = 1700000000.0
        for i in range(60):
            offset = 2.0 if i % 2 == 0 else -2.0
            mid = 50000.0 + offset
            candles.append(Candle(
                symbol="BTC/USD",
                open=mid - 1.0,
                high=mid + 3.0,
                low=mid - 3.0,
                close=mid + 1.0,
                volume=1000.0,
                coin_volume=1000.0 / mid,
                timestamp=base_ts + i * 300,
                close_time=base_ts + (i + 1) * 300,
                is_complete=True,
            ))
        state = engine.classify("BTC/USD", candles, [])
        assert state.regime in (MarketRegime.RANGE, MarketRegime.LOW_VOLATILITY, MarketRegime.TRANSITION)

    def test_strategy_config_filtering(self, engine):
        # Even if trend triggers LIQUIDITY_SWEEP, config can disable it
        candles_5m = make_candle_list(60, 50000.0, trend_step=50.0, noise=5.0)
        config = {
            "value_area": {"enabled": False},
            "liquidity_sweep": {"enabled": False},
        }
        state = engine.classify("BTC/USD", candles_5m, [], strategy_config=config)
        assert "VALUE_AREA" not in state.eligible_strategies
        assert "LIQUIDITY_SWEEP" not in state.eligible_strategies
