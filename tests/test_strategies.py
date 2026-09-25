"""
Unit tests for Strategies and Strategy Engine.
Tests signal generation, confidence filtering, conflict resolution,
and priority aggregation.
"""

import pytest
from unittest.mock import MagicMock

from config.trading_params import (
    BotConfig,
    StrategiesConfig,
    ValueAreaStrategyConfig,
    LiquiditySweepStrategyConfig,
    SignalConfig,
)
from core.market_data import MarketDataEngine, Candle, TickerSnapshot
from core.strategy_engine import StrategyEngine
from core.regime_engine import RegimeState, MarketRegime
from core.cvd_engine import CVDState
from strategies.base import Signal, SignalDirection, BaseStrategy
from strategies.value_area import ValueAreaStrategy
from strategies.liquidity_sweep import LiquiditySweepStrategy


@pytest.fixture
def bot_config():
    return BotConfig(
        strategies=StrategiesConfig(
            value_area=ValueAreaStrategyConfig(enabled=True, min_confidence=0.70),
            liquidity_sweep=LiquiditySweepStrategyConfig(enabled=True, min_confidence=0.75),
        ),
        signals=SignalConfig(
            min_confidence_to_trade=0.70,
            conflict_threshold=0.60,
        ),
    )


@pytest.fixture
def strategy_engine(bot_config):
    mock_market = MagicMock(spec=MarketDataEngine)
    return StrategyEngine(bot_config, mock_market)


class TestSignalAggregation:
    def test_filter_low_confidence(self, strategy_engine):
        sig = Signal(
            symbol="BTC/USD",
            direction=SignalDirection.BUY,
            strategy="VALUE_AREA",
            entry_price=50000.0,
            stop_loss=49000.0,
            take_profit_1=52000.0,
            confidence=0.65,  # Below 0.70 threshold
        )
        assert strategy_engine._aggregate_signals([sig]) is None

    def test_conflict_resolution_both_strong(self, strategy_engine):
        sig_buy = Signal(
            symbol="BTC/USD",
            direction=SignalDirection.BUY,
            strategy="VALUE_AREA",
            entry_price=50000.0,
            stop_loss=49000.0,
            take_profit_1=52000.0,
            confidence=0.85,
        )
        sig_sell = Signal(
            symbol="BTC/USD",
            direction=SignalDirection.SELL,
            strategy="LIQUIDITY_SWEEP",
            entry_price=50000.0,
            stop_loss=51000.0,
            take_profit_1=48000.0,
            confidence=0.82,
        )
        # Both > conflict_threshold (0.60) -> Strong Conflict -> Return None
        assert strategy_engine._aggregate_signals([sig_buy, sig_sell]) is None

    def test_derisk_takes_priority(self, strategy_engine):
        sig_buy = Signal(
            symbol="BTC/USD",
            direction=SignalDirection.BUY,
            strategy="VALUE_AREA",
            entry_price=50000.0,
            stop_loss=49000.0,
            take_profit_1=52000.0,
            confidence=0.90,
        )
        sig_derisk = Signal(
            symbol="BTC/USD",
            direction=SignalDirection.DERISK,
            strategy="VALUE_AREA",
            entry_price=50000.0,
            stop_loss=0.0,
            take_profit_1=0.0,
            confidence=0.75,
        )
        selected = strategy_engine._aggregate_signals([sig_buy, sig_derisk])
        assert selected is not None
        assert selected.direction == SignalDirection.DERISK

    def test_highest_confidence_selected(self, strategy_engine):
        sig1 = Signal(
            symbol="BTC/USD",
            direction=SignalDirection.BUY,
            strategy="VALUE_AREA",
            entry_price=50000.0,
            stop_loss=49000.0,
            take_profit_1=52000.0,
            confidence=0.72,
        )
        sig2 = Signal(
            symbol="BTC/USD",
            direction=SignalDirection.BUY,
            strategy="LIQUIDITY_SWEEP",
            entry_price=50000.0,
            stop_loss=49500.0,
            take_profit_1=53000.0,
            confidence=0.88,
        )
        selected = strategy_engine._aggregate_signals([sig1, sig2])
        assert selected is not None
        assert selected.strategy == "LIQUIDITY_SWEEP"
        assert selected.confidence == 0.88
