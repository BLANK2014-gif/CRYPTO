"""
core/autosl_engine.py
=====================
Production implementation of the AutoSL Stop Loss Trailing & Breakout Failed Exit Engine
for Autonomous Crypto Trading (Spot & Perpetual Futures).

Key Architecture:
1. Phase 1: Validation Window (Breakout Failed Exit)
   - Multi-condition confluence check during first N candles post-entry.
   - If breakout fails to expand, falls back below breakout level, and volume dries up,
     triggers immediate FAILED_BREAKOUT_EXIT market order (cutting loss at -0.2% to -0.5%
     instead of full -2.0% SL).
2. Phase 2: Validated Trend & Dual-Layer Trailing Stop
   - Layer A (Exchange Hard Stop): Ratchets forward in discrete steps.
   - Layer B (Internal Soft Dynamic SL): High-water mark trailing stop that dynamically
     tightens based on ATR volatility and profit protection tiers (never widens).
3. Fast Momentum Extension & Profit Ratchet
   - If price reaches initial TP unusually fast (< 180s) with active momentum,
     extends TP target and ratchets stop loss to lock in 50% of target distance per level.
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple, List

logger = logging.getLogger("AutoSLExitEngine")


@dataclass
class CryptoPosition:
    """AutoSL Managed Position State."""
    position_id: str
    symbol: str
    side: str                          # "BUY" / "LONG" or "SELL" / "SHORT"
    quantity: float
    entry_price: float
    entry_time: datetime

    # Structural Breakout Context
    entry_breakout_level: float        # Resistance level broken (BUY) or Support (SELL)
    entry_candle_volume: float = 0.0
    prev_candle_volume: float = 0.0

    # Stop Loss & Take Profit
    initial_stop_loss: float = 0.0
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0
    broker_sl_price: float = 0.0       # Hard SL on exchange
    sl_percent: float = 1.5            # Current dynamic SL %

    # State Tracking
    status: str = "OPEN"               # "OPEN", "EXITING", "CLOSED"
    exit_reason: str = ""
    current_price: float = 0.0
    peak_price: float = 0.0
    peak_profit_points: float = 0.0
    locked_profit: float = 0.0

    # Phase 1 vs Phase 2 Validation
    candles_since_entry: int = 0
    last_candle_time: Optional[datetime] = None
    validation_survived: bool = False
    volume_drop_detected: bool = False
    trailing_sl_active: bool = False
    is_exit_initiated: bool = False

    # Momentum Extension State
    momentum_status: str = "NORMAL"    # "NORMAL", "MOMENTUM_EXTENSION_1", ..., "FINAL_TRAILING"
    original_target_distance: float = 0.0
    extension_level: int = 0
    last_sl_recalc_time: float = 0.0
    strategy: str = ""

    def is_long(self) -> bool:
        return self.side.upper() in ("BUY", "LONG")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price if not math.isinf(self.take_profit_price) else 999999.0,
            "broker_sl_price": self.broker_sl_price,
            "sl_percent": round(self.sl_percent, 2),
            "peak_price": self.peak_price,
            "locked_profit": round(self.locked_profit, 2),
            "validation_survived": self.validation_survived,
            "trailing_sl_active": self.trailing_sl_active,
            "momentum_status": self.momentum_status,
            "extension_level": self.extension_level,
            "candles_since_entry": self.candles_since_entry,
            "status": self.status,
            "exit_reason": self.exit_reason,
            "strategy": self.strategy,
        }


class AutoSLExitEngine:
    """
    AutoSL Exit & Trailing Stop Loss Management Engine.
    Evaluates tick-by-tick and candle updates to manage early invalidation,
    dual-layer trailing stops, and momentum extensions.
    """

    DEFAULT_CONFIG: Dict[str, Any] = {
        # Validation Window (Phase 1)
        "validation_candles": 2,
        "candle_timeframe_seconds": 60,       # 1-minute candles or 300 for 5m
        "min_expansion_percent": 1.0,         # Must expand >= 1.0% to confirm breakout
        "phase2_profit_activation": 2.0,       # +2.0% immediately graduates to Phase 2

        # Trailing SL Configuration
        "hard_sl_trailing_step": 10.0,        # Hard exchange stop step
        "recalc_interval_seconds": 60,         # Dynamic SL recalculation interval
        "min_tick_buffer": 0.5,               # Minimum price movement to adjust SL
        "tick_size": 0.1,                     # Symbol tick size

        # Profit Protection Tiers (Unrealized Profit % -> Max Allowed SL Distance %)
        "profit_protection_tiers": [
            {"profit_pct": 10.0, "sl_percent": 0.4},
            {"profit_pct": 5.0, "sl_percent": 0.6},
            {"profit_pct": 3.0, "sl_percent": 0.9},
            {"profit_pct": 1.5, "sl_percent": 1.2},
        ],

        # Momentum Extension
        "momentum_extension_enabled": True,
        "fast_target_max_seconds": 180,
        "max_extensions": 3,
        "profit_lock_ratio": 0.50,            # Lock 50% of target distance per level
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config: Dict[str, Any] = {**self.DEFAULT_CONFIG, **(config or {})}
        logger.info("AutoSLExitEngine initialized with %d profit tiers", len(self.config["profit_protection_tiers"]))

    def round_to_tick(self, price: float) -> float:
        tick = self.config.get("tick_size", 0.1)
        if tick <= 0:
            return round(price, 4)
        return round(round(price / tick) * tick, 8)

    # ─────────────────────────────────────────────────────────────
    # Main Hook: Call on every market tick or price update
    # ─────────────────────────────────────────────────────────────
    def on_tick(
        self,
        pos: CryptoPosition,
        current_price: float,
        current_candle_vol: float,
        prev_completed_vol: float,
        prev_prev_completed_vol: float,
        recent_market_high: float,
        recent_market_low: float,
        current_time: Optional[datetime] = None,
    ) -> Tuple[Optional[str], Optional[float]]:
        """
        Processes a new tick for a position.
        Returns: (exit_signal_reason, trigger_price) or (None, None) if position remains open.
        """
        if pos.status != "OPEN" or pos.is_exit_initiated:
            return None, None

        current_time = current_time or datetime.now(timezone.utc)
        pos.current_price = current_price

        # Initialize peak price if not set
        if pos.peak_price <= 0:
            pos.peak_price = pos.entry_price

        is_long = pos.is_long()

        # 1. Update High-Water Mark (Peak Price)
        if is_long:
            pos.peak_price = max(pos.peak_price, current_price)
            profit_points = pos.peak_price - pos.entry_price
            profit_pct = ((current_price / pos.entry_price) - 1.0) * 100.0 if pos.entry_price > 0 else 0.0
        else:
            pos.peak_price = min(pos.peak_price, current_price)
            profit_points = pos.entry_price - pos.peak_price
            profit_pct = ((pos.entry_price / current_price) - 1.0) * 100.0 if current_price > 0 else 0.0

        # 2. Check Absolute Stop Loss Hit First
        if is_long and pos.stop_loss_price > 0 and current_price <= pos.stop_loss_price:
            reason = "TRAILING_SL_HIT" if pos.trailing_sl_active else "SL_HIT"
            return self._initiate_exit(pos, reason, pos.stop_loss_price)
        if not is_long and pos.stop_loss_price > 0 and current_price >= pos.stop_loss_price:
            reason = "TRAILING_SL_HIT" if pos.trailing_sl_active else "SL_HIT"
            return self._initiate_exit(pos, reason, pos.stop_loss_price)

        # 3. Check Take Profit / Fast Momentum Extension
        if self._is_tp_reached(pos, current_price):
            if self._can_extend_momentum(pos, current_time, recent_market_high, recent_market_low):
                self._apply_momentum_extension(pos)
            else:
                return self._initiate_exit(pos, "TP_HIT", pos.take_profit_price)

        # 4. Update Candle Progression & Volume Drop Detection
        self._update_candle_tracking(pos, current_time, prev_completed_vol, prev_prev_completed_vol)

        # 5. Evaluate Phase 1 vs Phase 2
        validation_candles = self.config["validation_candles"]
        min_expansion = self.config["min_expansion_percent"]
        phase2_activation_pct = self.config["phase2_profit_activation"]

        # Phase 1: Breakout Validation Check
        if pos.candles_since_entry <= validation_candles and not pos.validation_survived:
            # Check Initial SL breach during Phase 1
            if is_long and pos.initial_stop_loss > 0 and current_price <= pos.initial_stop_loss:
                return self._initiate_exit(pos, "FAILED_BREAKOUT_EXIT", current_price)
            if not is_long and pos.initial_stop_loss > 0 and current_price >= pos.initial_stop_loss:
                return self._initiate_exit(pos, "FAILED_BREAKOUT_EXIT", current_price)

            # Check Confluence Invalidation (only after entry candle has closed, > 0)
            if pos.candles_since_entry > 0:
                is_failed = self._check_breakout_failure(pos, current_price, min_expansion)
                if is_failed:
                    logger.warning(
                        "[FAILED_BREAKOUT] Invalidation triggered for %s (%s). "
                        "Entry=%.2f, Current=%.2f, Peak=%.2f, VolDrop=%s. Aborting before full SL hit!",
                        pos.symbol, pos.side, pos.entry_price, current_price,
                        pos.peak_price, pos.volume_drop_detected,
                    )
                    return self._initiate_exit(pos, "FAILED_BREAKOUT_EXIT", current_price)

            # Even in Phase 1, trail SL if the trade is in green
            if profit_pct > 0.5:
                self._update_soft_trailing_sl(pos)

            # Graduate to Phase 2 early if price exploded into strong profit
            if profit_pct >= phase2_activation_pct:
                pos.validation_survived = True
                logger.info(
                    "[PHASE2_GRADUATION] %s reached +%.2f%% profit. Validated early!",
                    pos.symbol, profit_pct,
                )

        # Phase 2: Post-Validation Active Trailing
        if pos.candles_since_entry > validation_candles or profit_pct >= phase2_activation_pct:
            pos.validation_survived = True

            # Recalculate dynamic SL % periodically (tighter only)
            self._recalculate_dynamic_sl(pos, current_time, profit_pct)

            # Execute Soft Trailing SL calculation
            self._update_soft_trailing_sl(pos)

            # Check Hard Broker Stop Trailing Step
            self._update_hard_broker_sl(pos, profit_points)

        return None, None

    # ─────────────────────────────────────────────────────────────
    # Internal Evaluation Helpers
    # ─────────────────────────────────────────────────────────────
    def _check_breakout_failure(self, pos: CryptoPosition, current_price: float, min_expansion: float) -> bool:
        """
        AutoSL 4-Condition Confluence Check for Failed Breakout:
        1. Price fell below entry (or rose above entry for Short)
        2. Peak price never achieved minimum breakout expansion %
        3. Price surrendered the breakout structural level
        4. Follow-through volume dried up
        """
        is_long = pos.is_long()
        if is_long:
            price_dropped = current_price < pos.entry_price
            peak_expansion_pct = ((pos.peak_price / pos.entry_price) - 1.0) * 100.0 if pos.entry_price > 0 else 0.0
            failed_expansion = peak_expansion_pct < min_expansion
            # If breakout level not specified, fallback to entry_price
            breakout_level = pos.entry_breakout_level if pos.entry_breakout_level > 0 else pos.entry_price
            lost_breakout = current_price <= breakout_level
            weak_volume = pos.volume_drop_detected
            return price_dropped and failed_expansion and lost_breakout and weak_volume
        else:
            price_dropped = current_price > pos.entry_price
            peak_expansion_pct = ((pos.entry_price / pos.peak_price) - 1.0) * 100.0 if pos.peak_price > 0 else 0.0
            failed_expansion = peak_expansion_pct < min_expansion
            breakout_level = pos.entry_breakout_level if pos.entry_breakout_level > 0 else pos.entry_price
            lost_breakout = current_price >= breakout_level
            weak_volume = pos.volume_drop_detected
            return price_dropped and failed_expansion and lost_breakout and weak_volume

    def _update_candle_tracking(
        self,
        pos: CryptoPosition,
        current_time: datetime,
        prev_completed_vol: float,
        prev_prev_completed_vol: float,
    ) -> None:
        """
        Tracks candle progression avoiding the 0-second trap.
        Evaluates whether follow-through volume dropped between the latest completed candles.
        """
        candle_seconds = self.config.get("candle_timeframe_seconds", 60)
        if pos.last_candle_time is None:
            pos.last_candle_time = pos.entry_time

        elapsed = (current_time - pos.last_candle_time).total_seconds()
        if elapsed >= candle_seconds:
            new_candles = max(1, int(elapsed // candle_seconds))
            pos.candles_since_entry += new_candles
            pos.last_candle_time = current_time

            # Completed volume comparison: completed candle vs predecessor
            if prev_prev_completed_vol > 0 and prev_completed_vol < prev_prev_completed_vol:
                pos.volume_drop_detected = True
            else:
                pos.volume_drop_detected = False

    def _is_tp_reached(self, pos: CryptoPosition, current_price: float) -> bool:
        """Check whether current price reached take profit price."""
        if pos.take_profit_price <= 0 or math.isinf(pos.take_profit_price):
            return False
        if pos.is_long():
            return current_price >= pos.take_profit_price
        else:
            return current_price <= pos.take_profit_price

    def _can_extend_momentum(
        self,
        pos: CryptoPosition,
        current_time: datetime,
        recent_market_high: float,
        recent_market_low: float,
    ) -> bool:
        """
        Check if position is eligible for Fast Momentum Extension:
        1. Momentum extension enabled and under max limit.
        2. Hit target fast (< fast_target_max_seconds).
        3. Price holding near peak/trough with active follow-through.
        """
        if not self.config.get("momentum_extension_enabled", True):
            return False
        if pos.extension_level >= self.config.get("max_extensions", 3):
            return False

        # Fast target arrival check
        elapsed = (current_time - pos.entry_time).total_seconds()
        fast_target_max = self.config.get("fast_target_max_seconds", 180)
        if elapsed > fast_target_max:
            return False

        # Momentum confirmation near extreme without volume collapse
        if pos.is_long():
            near_high = recent_market_high > 0 and pos.current_price >= recent_market_high * 0.998
            return near_high and not pos.volume_drop_detected
        else:
            near_low = recent_market_low > 0 and pos.current_price <= recent_market_low * 1.002
            return near_low and not pos.volume_drop_detected

    def _apply_momentum_extension(self, pos: CryptoPosition) -> None:
        """Extend target and ratchet stop loss up to lock in profit."""
        pos.extension_level += 1
        max_ext = self.config.get("max_extensions", 3)
        lock_ratio = self.config.get("profit_lock_ratio", 0.50)

        # Ensure original target distance is preserved
        if pos.original_target_distance <= 0:
            pos.original_target_distance = abs(pos.take_profit_price - pos.entry_price)

        dist = pos.original_target_distance

        # Push Take Profit further
        if pos.is_long():
            pos.take_profit_price = self.round_to_tick(pos.take_profit_price + dist)
            new_stop = pos.entry_price + (dist * pos.extension_level * lock_ratio)
            pos.stop_loss_price = max(pos.stop_loss_price, self.round_to_tick(new_stop))
        else:
            pos.take_profit_price = self.round_to_tick(pos.take_profit_price - dist)
            new_stop = pos.entry_price - (dist * pos.extension_level * lock_ratio)
            pos.stop_loss_price = min(pos.stop_loss_price, self.round_to_tick(new_stop))

        pos.trailing_sl_active = True

        if pos.extension_level >= max_ext:
            pos.momentum_status = "FINAL_TRAILING"
            pos.take_profit_price = float("inf")
            logger.info(
                "[MOMENTUM_EXTENSION] %s reached FINAL_TRAILING level %d. "
                "Uncapped TP, Ratchet Stop=%.2f",
                pos.symbol, pos.extension_level, pos.stop_loss_price,
            )
        else:
            pos.momentum_status = f"MOMENTUM_EXTENSION_{pos.extension_level}"
            logger.info(
                "[MOMENTUM_EXTENSION] %s extended to Level %d. "
                "New TP=%.2f, Ratchet Stop=%.2f",
                pos.symbol, pos.extension_level, pos.take_profit_price, pos.stop_loss_price,
            )

    def _recalculate_dynamic_sl(
        self,
        pos: CryptoPosition,
        current_time: datetime,
        profit_pct: float,
    ) -> None:
        """
        Recalculates dynamic SL % based on multi-tier profit protection.
        IRON RULE: Never widen (only tighten).
        """
        recalc_interval = self.config.get("recalc_interval_seconds", 60)
        now_ts = current_time.timestamp()
        if pos.last_sl_recalc_time > 0 and (now_ts - pos.last_sl_recalc_time) < recalc_interval:
            return

        pos.last_sl_recalc_time = now_ts
        tiers = self.config.get("profit_protection_tiers", [])

        new_sl_percent = None
        for tier in sorted(tiers, key=lambda x: x["profit_pct"], reverse=True):
            if profit_pct >= tier["profit_pct"]:
                new_sl_percent = tier["sl_percent"]
                break

        if new_sl_percent is not None:
            # IRON RULE: Never loosen stop loss
            if new_sl_percent < pos.sl_percent:
                logger.debug(
                    "Tightening SL %% for %s from %.2f%% to %.2f%% (+%.2f%% profit)",
                    pos.symbol, pos.sl_percent, new_sl_percent, profit_pct,
                )
                pos.sl_percent = new_sl_percent

    def _update_soft_trailing_sl(self, pos: CryptoPosition) -> None:
        """Layer B: Dynamic high-water mark trailing stop calculation."""
        min_tick = self.config.get("min_tick_buffer", 0.5)
        if pos.is_long():
            potential_sl = pos.peak_price * (1.0 - pos.sl_percent / 100.0)
            if potential_sl > pos.stop_loss_price + min_tick:
                pos.stop_loss_price = self.round_to_tick(potential_sl)
                pos.trailing_sl_active = True
                pos.locked_profit = (pos.stop_loss_price - pos.entry_price) * pos.quantity
        else:
            potential_sl = pos.peak_price * (1.0 + pos.sl_percent / 100.0)
            if pos.stop_loss_price <= 0 or potential_sl < pos.stop_loss_price - min_tick:
                pos.stop_loss_price = self.round_to_tick(potential_sl)
                pos.trailing_sl_active = True
                pos.locked_profit = (pos.entry_price - pos.stop_loss_price) * pos.quantity

    def _update_hard_broker_sl(self, pos: CryptoPosition, profit_points: float) -> None:
        """Layer A: Discrete step-ratchet trailing stop for exchange hard orders."""
        trailing_step = self.config.get("hard_sl_trailing_step", 10.0)
        if profit_points >= pos.peak_profit_points + trailing_step:
            steps_moved = int((profit_points - pos.peak_profit_points) / trailing_step)
            total_points = steps_moved * trailing_step
            if pos.is_long():
                new_broker_sl = pos.broker_sl_price + total_points
            else:
                new_broker_sl = pos.broker_sl_price - total_points

            new_broker_sl = self.round_to_tick(new_broker_sl)
            pos.peak_profit_points += total_points
            pos.broker_sl_price = new_broker_sl

    def _initiate_exit(self, pos: CryptoPosition, reason: str, trigger_price: float) -> Tuple[str, float]:
        """Initiate position exit and mark state."""
        pos.status = "EXITING"
        pos.exit_reason = reason
        pos.is_exit_initiated = True
        return reason, trigger_price
