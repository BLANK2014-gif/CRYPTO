"""
Roostoo Trading Bot — Strategy Base Class
==========================================
Abstract interface for all trading strategies.
Ensures uniform signal generation, extensibility, and
clean separation of strategy logic from execution.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from core.market_data import Candle, TickerSnapshot
from core.regime_engine import RegimeState
from core.cvd_engine import CVDState
from core.indicators import VolumeProfile


logger = logging.getLogger("strategy")


class SignalDirection(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    DERISK = "DERISK"
    HOLD = "HOLD"
    NO_TRADE = "NO_TRADE"


@dataclass
class Signal:
    """
    Unified signal object.
    All strategies produce signals in this format.
    """
    symbol: str
    direction: SignalDirection
    strategy: str
    confidence: float                # 0.0 to 1.0
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0
    expected_rr: float = 0.0         # Expected risk:reward ratio
    reason: str = ""
    timestamp: float = 0.0
    order_type: str = "LIMIT"        # LIMIT or MARKET
    scale_out_pct: float = 0.50      # Fraction to close at TP1
    urgency: str = "NORMAL"          # NORMAL, HIGH, EMERGENCY
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction.value,
            "strategy": self.strategy,
            "confidence": round(self.confidence, 3),
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit_1": self.take_profit_1,
            "take_profit_2": self.take_profit_2,
            "expected_rr": round(self.expected_rr, 2),
            "reason": self.reason,
            "order_type": self.order_type,
            "scale_out_pct": self.scale_out_pct,
            "urgency": self.urgency,
        }


class BaseStrategy(ABC):
    """Abstract base class for all trading strategies."""

    def __init__(self, name: str):
        self.name = name
        self._enabled = True
        self._last_signal: Optional[Signal] = None
        self.logger = logging.getLogger(f"strategy.{name}")

    @abstractmethod
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
        """
        Evaluate market conditions and optionally produce a signal.

        Args:
            symbol: Trading pair (e.g. "BTC/USD")
            candles_5m: Completed 5-minute candles
            candles_15m: Completed 15-minute candles
            ticker: Latest ticker snapshot
            regime: Current market regime
            cvd: Current CVD/order-flow state
            portfolio_context: Current positions, exposure, etc.

        Returns:
            Signal if conditions are met, None otherwise.
        """
        ...

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def last_signal(self) -> Optional[Signal]:
        return self._last_signal

    def _make_signal(self, **kwargs) -> Signal:
        """Helper to create a signal and update last_signal."""
        sig = Signal(**kwargs)
        self._last_signal = sig
        return sig
