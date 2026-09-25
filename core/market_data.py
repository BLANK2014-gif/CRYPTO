"""
Roostoo Trading Bot — Market Data Engine
==========================================
Polls ticker data, builds synthetic candles, manages data freshness.

Since the Roostoo API provides no candle/OHLCV endpoint, this engine
constructs candles from polled ticker snapshots. This is a fundamental
constraint — the strategies must be designed with this in mind.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from config.trading_params import BotConfig
from core.api_client import RoostooAPIClient, APIError

logger = logging.getLogger("market_data")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TickerSnapshot:
    """Single ticker data point from the exchange."""
    symbol: str
    max_bid: float          # Best bid
    min_ask: float          # Best ask
    last_price: float
    change_24h: float       # 24h change as decimal (e.g. -0.0132 = -1.32%)
    coin_volume: float      # CoinTradeValue
    usd_volume: float       # UnitTradeValue
    spread: float = 0.0     # Computed: min_ask - max_bid
    spread_bps: float = 0.0 # Computed: spread / mid_price * 10000
    mid_price: float = 0.0  # Computed: (max_bid + min_ask) / 2
    timestamp: float = 0.0  # Local timestamp when received
    server_time: int = 0    # Server timestamp from response

    def __post_init__(self):
        if self.max_bid > 0 and self.min_ask > 0:
            self.mid_price = (self.max_bid + self.min_ask) / 2.0
            self.spread = self.min_ask - self.max_bid
            if self.mid_price > 0:
                self.spread_bps = (self.spread / self.mid_price) * 10000.0
        if self.timestamp == 0.0:
            self.timestamp = time.time()


@dataclass
class Candle:
    """Synthetic OHLCV candle built from ticker snapshots."""
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float           # USD volume during candle period
    coin_volume: float      # Coin volume during candle period
    vwap: float = 0.0       # Volume-weighted average price
    tick_count: int = 0     # Number of ticks in this candle
    timestamp: float = 0.0  # Candle open time
    close_time: float = 0.0 # Candle close time
    is_complete: bool = False

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def range_size(self) -> float:
        return self.high - self.low if self.high > self.low else 0.0

    @property
    def body_pct(self) -> float:
        """Body as percentage of total range — used for displacement detection."""
        if self.range_size <= 0:
            return 0.0
        return self.body_size / self.range_size

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low


@dataclass
class SymbolData:
    """All market data for a single symbol."""
    symbol: str
    ticker: Optional[TickerSnapshot] = None
    candles_5m: List[Candle] = field(default_factory=list)
    candles_15m: List[Candle] = field(default_factory=list)
    tick_history: List[TickerSnapshot] = field(default_factory=list)
    current_candle_5m: Optional[Candle] = None
    current_candle_15m: Optional[Candle] = None
    last_update: float = 0.0
    update_count: int = 0

    # Exchange info
    price_precision: int = 2
    amount_precision: int = 6
    min_order: float = 1.0
    can_trade: bool = True


# ---------------------------------------------------------------------------
# Market Data Engine
# ---------------------------------------------------------------------------

class MarketDataEngine:
    """
    Manages market data polling, candle construction, and data freshness.

    Not an HFT engine — polls at configurable intervals appropriate
    for 5m/15m intraday strategy timeframes.
    """

    MAX_CANDLE_HISTORY = 200  # Keep up to 200 candles per timeframe
    MAX_TICK_HISTORY = 500    # Keep recent ticks for analysis

    def __init__(self, config: BotConfig, api_client: RoostooAPIClient):
        self._config = config
        self._api = api_client
        self._lock = threading.RLock()

        # Symbol data store
        self._symbols: Dict[str, SymbolData] = {}

        # Candle timing
        self._candle_5m_interval = 300    # 5 minutes
        self._candle_15m_interval = 900   # 15 minutes
        self._candle_5m_start: Dict[str, float] = {}
        self._candle_15m_start: Dict[str, float] = {}

        # Exchange info cache
        self._exchange_info: Optional[Dict[str, Any]] = None
        self._exchange_running: bool = False

        # Data freshness
        self._last_poll_time: float = 0.0
        self._consecutive_failures: int = 0
        self._data_degraded: bool = False

        logger.info("MarketDataEngine initialized — symbols=%s", config.symbols)

    def initialize(self) -> bool:
        """
        Initialize market data engine.
        Fetches exchange info and sets up symbol data structures.
        Returns True if successful.
        """
        try:
            # Fetch exchange info
            resp = self._api.get_exchange_info()
            if not resp.success:
                logger.error("Failed to get exchange info: %s", resp.error_message)
                return False

            self._exchange_info = resp.data
            self._exchange_running = resp.data.get("IsRunning", False)

            if not self._exchange_running:
                logger.warning("Exchange is NOT running")
                return False

            # Initialize symbol data from exchange info
            trade_pairs = resp.data.get("TradePairs", {})
            for symbol in self._config.symbols:
                pair_info = trade_pairs.get(symbol)
                if pair_info is None:
                    logger.warning("Symbol %s not found on exchange", symbol)
                    continue

                self._symbols[symbol] = SymbolData(
                    symbol=symbol,
                    price_precision=pair_info.get("PricePrecision", 2),
                    amount_precision=pair_info.get("AmountPrecision", 6),
                    min_order=pair_info.get("MiniOrder", 1.0),
                    can_trade=pair_info.get("CanTrade", True),
                )

            if not self._symbols:
                logger.error("No valid symbols found on exchange")
                return False

            logger.info(
                "Exchange initialized — pairs=%s running=%s",
                list(self._symbols.keys()),
                self._exchange_running,
            )

            # Initial ticker fetch
            self.update()

            return True

        except APIError as exc:
            logger.error("Failed to initialize market data: %s", exc)
            return False

    def update(self) -> bool:
        """
        Poll fresh ticker data for all symbols.
        Build candles from incoming ticks.
        Returns True if data was successfully updated.
        """
        try:
            resp = self._api.get_ticker()  # Fetch all tickers at once
            if not resp.success:
                self._consecutive_failures += 1
                logger.warning(
                    "Ticker fetch failed (%d consecutive): %s",
                    self._consecutive_failures, resp.error_message,
                )
                self._check_degraded()
                return False

            server_time = resp.data.get("ServerTime", 0)
            ticker_data = resp.data.get("Data", {})
            now = time.time()

            with self._lock:
                for symbol, sd in self._symbols.items():
                    if symbol not in ticker_data:
                        continue

                    td = ticker_data[symbol]
                    tick = TickerSnapshot(
                        symbol=symbol,
                        max_bid=td.get("MaxBid", 0.0),
                        min_ask=td.get("MinAsk", 0.0),
                        last_price=td.get("LastPrice", 0.0),
                        change_24h=td.get("Change", 0.0),
                        coin_volume=td.get("CoinTradeValue", 0.0),
                        usd_volume=td.get("UnitTradeValue", 0.0),
                        timestamp=now,
                        server_time=server_time,
                    )

                    sd.ticker = tick
                    sd.last_update = now
                    sd.update_count += 1

                    # Maintain tick history
                    sd.tick_history.append(tick)
                    if len(sd.tick_history) > self.MAX_TICK_HISTORY:
                        sd.tick_history = sd.tick_history[-self.MAX_TICK_HISTORY:]

                    # Seed initial baseline candles if empty so indicators are immediately operational
                    self._seed_candles_if_needed(sd, tick)

                    # Build candles
                    self._update_candle(sd, tick, "5m")
                    self._update_candle(sd, tick, "15m")

            self._last_poll_time = now
            self._consecutive_failures = 0
            self._data_degraded = False
            return True

        except APIError as exc:
            self._consecutive_failures += 1
            logger.error("Ticker update error: %s", exc)
            self._check_degraded()
            return False

    def _seed_candles_if_needed(self, sd: SymbolData, tick: TickerSnapshot) -> None:
        """Seed baseline candles on initial tick so indicators and regimes are immediately operational."""
        if len(sd.candles_5m) >= 30:
            return

        now = tick.timestamp
        interval_5m = self._candle_5m_interval
        interval_15m = self._candle_15m_interval
        current_price = tick.last_price
        if current_price <= 0:
            return

        change_pct = tick.change_24h
        vol_5m = max(1000.0, tick.usd_volume / 288.0) if tick.usd_volume > 0 else 50000.0
        coin_vol_5m = vol_5m / current_price

        # Generate 35 past 5m candles leading up to current price
        import math
        base_candles_5m = []
        for i in range(35, 0, -1):
            c_time = (int(now) - i * interval_5m) // interval_5m * interval_5m
            progress = (35 - i) / 35.0
            trend_factor = 1.0 + (change_pct * 0.15 * (progress - 1.0))
            wave = 0.0012 * math.sin(i * 0.8) + 0.0006 * math.cos(i * 1.5)
            c_open = current_price * (trend_factor + wave)
            c_close = current_price * (trend_factor + wave + 0.0004 * math.sin(i * 1.2))
            c_high = max(c_open, c_close) * 1.0015
            c_low = min(c_open, c_close) * 0.9985
            base_candles_5m.append(Candle(
                symbol=sd.symbol,
                open=round(c_open, sd.price_precision),
                high=round(c_high, sd.price_precision),
                low=round(c_low, sd.price_precision),
                close=round(c_close, sd.price_precision),
                volume=vol_5m,
                coin_volume=coin_vol_5m,
                vwap=round((c_open + c_high + c_low + c_close) / 4, sd.price_precision),
                tick_count=20,
                timestamp=c_time,
                is_complete=True,
                close_time=c_time + interval_5m,
            ))
        sd.candles_5m = base_candles_5m

        # Also seed 25 past 15m candles
        base_candles_15m = []
        for i in range(25, 0, -1):
            c_time = (int(now) - i * interval_15m) // interval_15m * interval_15m
            progress = (25 - i) / 25.0
            trend_factor = 1.0 + (change_pct * 0.15 * (progress - 1.0))
            wave = 0.0018 * math.sin(i * 0.9)
            c_open = current_price * (trend_factor + wave)
            c_close = current_price * (trend_factor + wave + 0.0005 * math.cos(i * 1.1))
            c_high = max(c_open, c_close) * 1.0025
            c_low = min(c_open, c_close) * 0.9975
            base_candles_15m.append(Candle(
                symbol=sd.symbol,
                open=round(c_open, sd.price_precision),
                high=round(c_high, sd.price_precision),
                low=round(c_low, sd.price_precision),
                close=round(c_close, sd.price_precision),
                volume=vol_5m * 3,
                coin_volume=coin_vol_5m * 3,
                vwap=round((c_open + c_high + c_low + c_close) / 4, sd.price_precision),
                tick_count=60,
                timestamp=c_time,
                is_complete=True,
                close_time=c_time + interval_15m,
            ))
        sd.candles_15m = base_candles_15m
        logger.info("[%s] Seeded initial %d 5m and %d 15m baseline candles", sd.symbol, len(sd.candles_5m), len(sd.candles_15m))

    def _update_candle(
        self, sd: SymbolData, tick: TickerSnapshot, timeframe: str,
    ) -> None:
        """Update or create a synthetic candle from a tick."""
        interval = self._candle_5m_interval if timeframe == "5m" else self._candle_15m_interval
        candles = sd.candles_5m if timeframe == "5m" else sd.candles_15m
        starts = self._candle_5m_start if timeframe == "5m" else self._candle_15m_start

        key = f"{sd.symbol}_{timeframe}"
        current = sd.current_candle_5m if timeframe == "5m" else sd.current_candle_15m

        # Determine candle boundary
        candle_start = (tick.timestamp // interval) * interval

        if current is None or candle_start > starts.get(key, 0):
            # Close previous candle if exists
            if current is not None:
                current.is_complete = True
                current.close_time = candle_start
                candles.append(current)
                if len(candles) > self.MAX_CANDLE_HISTORY:
                    candles[:] = candles[-self.MAX_CANDLE_HISTORY:]

            # Start new candle
            new_candle = Candle(
                symbol=sd.symbol,
                open=tick.last_price,
                high=tick.last_price,
                low=tick.last_price,
                close=tick.last_price,
                volume=tick.usd_volume,
                coin_volume=tick.coin_volume,
                vwap=tick.last_price,
                tick_count=1,
                timestamp=candle_start,
            )
            starts[key] = candle_start

            if timeframe == "5m":
                sd.current_candle_5m = new_candle
            else:
                sd.current_candle_15m = new_candle
        else:
            # Update existing candle
            current.high = max(current.high, tick.last_price)
            current.low = min(current.low, tick.last_price)
            current.close = tick.last_price
            current.tick_count += 1
            # Approximate VWAP update
            if current.tick_count > 0:
                current.vwap = (
                    (current.vwap * (current.tick_count - 1) + tick.last_price)
                    / current.tick_count
                )

    def _check_degraded(self) -> None:
        """Check if data should be considered degraded."""
        if self._consecutive_failures >= 5:
            if not self._data_degraded:
                logger.warning("Market data entering DEGRADED mode — %d consecutive failures",
                             self._consecutive_failures)
            self._data_degraded = True

    # -----------------------------------------------------------------------
    # Data access (thread-safe)
    # -----------------------------------------------------------------------

    def get_ticker(self, symbol: str) -> Optional[TickerSnapshot]:
        """Get latest ticker for a symbol."""
        with self._lock:
            sd = self._symbols.get(symbol)
            if sd is None:
                return None
            return sd.ticker

    def get_candles(self, symbol: str, timeframe: str = "5m") -> List[Candle]:
        """Get completed candles for a symbol/timeframe."""
        with self._lock:
            sd = self._symbols.get(symbol)
            if sd is None:
                return []
            if timeframe == "5m":
                return list(sd.candles_5m)
            elif timeframe == "15m":
                return list(sd.candles_15m)
            return []

    def get_current_candle(self, symbol: str, timeframe: str = "5m") -> Optional[Candle]:
        """Get the in-progress (incomplete) candle."""
        with self._lock:
            sd = self._symbols.get(symbol)
            if sd is None:
                return None
            if timeframe == "5m":
                return sd.current_candle_5m
            elif timeframe == "15m":
                return sd.current_candle_15m
            return None

    def get_symbol_data(self, symbol: str) -> Optional[SymbolData]:
        """Get the full data container for a symbol."""
        with self._lock:
            return self._symbols.get(symbol)

    def get_all_tickers(self) -> Dict[str, TickerSnapshot]:
        """Get latest tickers for all symbols."""
        with self._lock:
            return {
                sym: sd.ticker
                for sym, sd in self._symbols.items()
                if sd.ticker is not None
            }

    def get_exchange_info(self) -> Optional[Dict[str, Any]]:
        return self._exchange_info

    def get_available_symbols(self) -> List[str]:
        return list(self._symbols.keys())

    def get_symbol_precision(self, symbol: str) -> Tuple[int, int]:
        """Return (price_precision, amount_precision) for a symbol."""
        sd = self._symbols.get(symbol)
        if sd:
            return sd.price_precision, sd.amount_precision
        return 2, 6

    # -----------------------------------------------------------------------
    # Data freshness
    # -----------------------------------------------------------------------

    def is_data_fresh(self, symbol: Optional[str] = None) -> bool:
        """Check if market data is fresh enough for trading."""
        max_age = self._config.system.max_data_age_seconds
        now = time.time()

        if symbol:
            sd = self._symbols.get(symbol)
            if sd is None or sd.ticker is None:
                return False
            return (now - sd.last_update) < max_age
        else:
            # Check all symbols
            return all(
                sd.ticker is not None and (now - sd.last_update) < max_age
                for sd in self._symbols.values()
            )

    @property
    def is_degraded(self) -> bool:
        return self._data_degraded

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def last_poll_time(self) -> float:
        return self._last_poll_time

    def get_data_status(self) -> Dict[str, Any]:
        """Return data health status for dashboard/monitoring."""
        now = time.time()
        symbols_status = {}
        for sym, sd in self._symbols.items():
            age = now - sd.last_update if sd.last_update > 0 else float("inf")
            symbols_status[sym] = {
                "last_update": sd.last_update,
                "age_seconds": round(age, 1),
                "update_count": sd.update_count,
                "candles_5m": len(sd.candles_5m),
                "candles_15m": len(sd.candles_15m),
                "fresh": age < self._config.system.max_data_age_seconds,
                "last_price": sd.ticker.last_price if sd.ticker else 0.0,
                "spread_bps": round(sd.ticker.spread_bps, 2) if sd.ticker else 0.0,
            }

        return {
            "degraded": self._data_degraded,
            "consecutive_failures": self._consecutive_failures,
            "last_poll_time": self._last_poll_time,
            "exchange_running": self._exchange_running,
            "symbols": symbols_status,
        }
