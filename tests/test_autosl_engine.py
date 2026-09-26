"""
tests/test_autosl_engine.py
===========================
Comprehensive unit and scenario tests for the AutoSL Stop Loss Trailing
& Breakout Failed Exit Engine.
"""

from datetime import datetime, timezone, timedelta
import pytest
from core.autosl_engine import AutoSLExitEngine, CryptoPosition


@pytest.fixture
def autosl_engine():
    return AutoSLExitEngine({
        "validation_candles": 2,
        "candle_timeframe_seconds": 60,
        "min_expansion_percent": 1.0,
        "phase2_profit_activation": 2.0,
        "hard_sl_trailing_step": 10.0,
        "recalc_interval_seconds": 60,
        "min_tick_buffer": 0.5,
        "tick_size": 0.1,
        "momentum_extension_enabled": True,
        "fast_target_max_seconds": 180,
        "max_extensions": 3,
        "profit_lock_ratio": 0.50,
    })


def test_phase1_breakout_failed_exit_long(autosl_engine):
    """
    Test Phase 1 Failed Breakout Exit on Long:
    Price drops below entry, peak expansion < 1.0%, lost breakout level, volume drop detected.
    """
    entry_time = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
    pos = CryptoPosition(
        position_id="BTC_001",
        symbol="BTC/USD",
        side="BUY",
        quantity=1.0,
        entry_price=50000.0,
        entry_time=entry_time,
        entry_breakout_level=50000.0,
        initial_stop_loss=49000.0,  # -2% full SL
        stop_loss_price=49000.0,
        take_profit_price=52000.0,
        broker_sl_price=49000.0,
    )

    # 1. Right at entry (first 20s): peak is entry price
    reason, trigger = autosl_engine.on_tick(
        pos=pos,
        current_price=50100.0,  # +0.2% minor expansion
        current_candle_vol=500.0,
        prev_completed_vol=1000.0,
        prev_prev_completed_vol=1200.0,
        recent_market_high=50100.0,
        recent_market_low=49950.0,
        current_time=entry_time + timedelta(seconds=20),
    )
    assert reason is None
    assert pos.status == "OPEN"
    assert pos.peak_price == 50100.0
    assert pos.candles_since_entry == 0  # Entry bar not complete yet

    # 2. Candle 1 completes (65s elapsed) with volume drop detected
    t_c1 = entry_time + timedelta(seconds=65)
    reason, trigger = autosl_engine.on_tick(
        pos=pos,
        current_price=49900.0,  # Dropped below entry & breakout level (-0.2%)
        current_candle_vol=300.0,
        prev_completed_vol=800.0,        # Volume dropped vs previous
        prev_prev_completed_vol=1200.0,
        recent_market_high=50100.0,
        recent_market_low=49900.0,
        current_time=t_c1,
    )

    # All 4 conditions met:
    # 1. current_price (49900) < entry (50000)
    # 2. peak expansion (50100 -> +0.2%) < min_expansion (1.0%)
    # 3. current_price (49900) <= breakout level (50000)
    # 4. volume drop detected (800 < 1200)
    assert reason == "FAILED_BREAKOUT_EXIT"
    assert trigger == 49900.0
    assert pos.status == "EXITING"
    assert pos.exit_reason == "FAILED_BREAKOUT_EXIT"


def test_phase1_expansion_survives_invalidation(autosl_engine):
    """
    If price successfully expanded >= 1.0% above entry before pulling back,
    it should NOT trigger FAILED_BREAKOUT_EXIT.
    """
    entry_time = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
    pos = CryptoPosition(
        position_id="BTC_002",
        symbol="BTC/USD",
        side="BUY",
        quantity=1.0,
        entry_price=50000.0,
        entry_time=entry_time,
        entry_breakout_level=50000.0,
        initial_stop_loss=49000.0,
        stop_loss_price=49000.0,
        take_profit_price=52000.0,
        broker_sl_price=49000.0,
    )

    # First expands to 50600 (+1.2% > 1.0% min expansion)
    autosl_engine.on_tick(
        pos=pos,
        current_price=50600.0,
        current_candle_vol=1500.0,
        prev_completed_vol=1000.0,
        prev_prev_completed_vol=1000.0,
        recent_market_high=50600.0,
        recent_market_low=50000.0,
        current_time=entry_time + timedelta(seconds=30),
    )
    assert pos.peak_price == 50600.0

    # 70s elapsed, pull back below entry
    reason, _ = autosl_engine.on_tick(
        pos=pos,
        current_price=49950.0,
        current_candle_vol=500.0,
        prev_completed_vol=800.0,
        prev_prev_completed_vol=1000.0,
        recent_market_high=50600.0,
        recent_market_low=49950.0,
        current_time=entry_time + timedelta(seconds=70),
    )
    # Does not trigger FAILED_BREAKOUT_EXIT because peak achieved expansion requirement
    assert reason != "FAILED_BREAKOUT_EXIT"


def test_phase2_dynamic_trailing_sl_and_iron_rule(autosl_engine):
    """
    Test Phase 2 dynamic trailing stop tightening and the IRON RULE (never loosen).
    """
    entry_time = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
    pos = CryptoPosition(
        position_id="BTC_003",
        symbol="BTC/USD",
        side="BUY",
        quantity=1.0,
        entry_price=50000.0,
        entry_time=entry_time,
        entry_breakout_level=50000.0,
        initial_stop_loss=49250.0,  # 1.5% SL
        stop_loss_price=49250.0,
        take_profit_price=53000.0,
        broker_sl_price=49250.0,
        sl_percent=1.5,
    )

    # Advance beyond validation window (150s = > 2 candles)
    t_p2 = entry_time + timedelta(seconds=150)

    # Price moves to +3.5% profit ($51,750) -> triggers profit tier: >= +3.0% gives sl_percent = 0.9%
    autosl_engine.on_tick(
        pos=pos,
        current_price=51750.0,
        current_candle_vol=2000.0,
        prev_completed_vol=2000.0,
        prev_prev_completed_vol=1800.0,
        recent_market_high=51750.0,
        recent_market_low=50000.0,
        current_time=t_p2,
    )

    assert pos.validation_survived is True
    assert pos.sl_percent == 0.9  # Tightened from 1.5% to 0.9%
    assert pos.trailing_sl_active is True
    # Stop should trail peak: 51750 * (1 - 0.009) = 51284.25
    expected_stop = autosl_engine.round_to_tick(51750.0 * (1.0 - 0.009))
    assert pos.stop_loss_price == expected_stop
    assert pos.locked_profit > 0  # Profit locked in!

    # Test IRON RULE: Price pulls back to +1.6% profit ($50,800).
    # Even though profit is now only +1.6% (which ordinarily maps to 1.2% SL),
    # the stop percent must NEVER loosen back to 1.2%.
    pos.last_sl_recalc_time = 0.0  # Force recalculation
    autosl_engine.on_tick(
        pos=pos,
        current_price=50800.0,
        current_candle_vol=1000.0,
        prev_completed_vol=2000.0,
        prev_prev_completed_vol=1800.0,
        recent_market_high=51750.0,
        recent_market_low=50000.0,
        current_time=t_p2 + timedelta(seconds=70),
    )
    assert pos.sl_percent == 0.9  # Maintained tighter stop!
    assert pos.stop_loss_price == expected_stop  # Did not widen


def test_fast_momentum_extension_and_ratchet(autosl_engine):
    """
    Test Fast Momentum Extension:
    Price hits TP within 180s with active momentum near highs -> extends target and ratchets stop.
    """
    entry_time = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
    pos = CryptoPosition(
        position_id="BTC_004",
        symbol="BTC/USD",
        side="BUY",
        quantity=1.0,
        entry_price=50000.0,
        entry_time=entry_time,
        entry_breakout_level=50000.0,
        initial_stop_loss=49000.0,
        stop_loss_price=49000.0,
        take_profit_price=51000.0,  # $1,000 target distance
        original_target_distance=1000.0,
        broker_sl_price=49000.0,
    )

    # Reaches TP ($51,000) at 90s (< 180s max fast seconds) with strong momentum near high
    t_hit = entry_time + timedelta(seconds=90)
    reason, trigger = autosl_engine.on_tick(
        pos=pos,
        current_price=51050.0,
        current_candle_vol=3000.0,
        prev_completed_vol=2500.0,
        prev_prev_completed_vol=2000.0,
        recent_market_high=51100.0,  # within 0.2% of high
        recent_market_low=50000.0,
        current_time=t_hit,
    )

    # Did NOT exit on TP; instead extended momentum!
    assert reason is None
    assert pos.status == "OPEN"
    assert pos.extension_level == 1
    assert pos.momentum_status == "MOMENTUM_EXTENSION_1"
    # Target pushed further by 1000 -> 52000
    assert pos.take_profit_price == 52000.0
    # Stop ratcheted to lock 50% of original target: 50000 + (1000 * 1 * 0.5) = 50500
    assert pos.stop_loss_price >= 50500.0


def test_short_position_lifecycle(autosl_engine):
    """
    Test AutoSL mechanisms on SHORT positions:
    - Peak price tracks lowest price.
    - Invalidation on upside surge above entry & breakout level.
    """
    entry_time = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
    pos = CryptoPosition(
        position_id="ETH_001",
        symbol="ETH/USD",
        side="SELL",
        quantity=10.0,
        entry_price=3000.0,
        entry_time=entry_time,
        entry_breakout_level=3000.0,
        initial_stop_loss=3060.0,  # +2% SL
        stop_loss_price=3060.0,
        take_profit_price=2880.0,
        broker_sl_price=3060.0,
    )

    # Tick 1: Small drop to 2990
    autosl_engine.on_tick(
        pos=pos,
        current_price=2990.0,
        current_candle_vol=500.0,
        prev_completed_vol=1000.0,
        prev_prev_completed_vol=1200.0,
        recent_market_high=3005.0,
        recent_market_low=2990.0,
        current_time=entry_time + timedelta(seconds=20),
    )
    assert pos.peak_price == 2990.0

    # Candle 1 completes (65s): surges back above entry to 3010 with volume drop
    reason, trigger = autosl_engine.on_tick(
        pos=pos,
        current_price=3010.0,
        current_candle_vol=300.0,
        prev_completed_vol=600.0,
        prev_prev_completed_vol=1000.0,
        recent_market_high=3015.0,
        recent_market_low=2990.0,
        current_time=entry_time + timedelta(seconds=65),
    )
    assert reason == "FAILED_BREAKOUT_EXIT"
    assert trigger == 3010.0
    assert pos.status == "EXITING"
