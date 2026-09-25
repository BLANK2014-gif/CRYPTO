"""
Roostoo Trading Bot — Portfolio Tracker
========================================
Local ledger maintaining cash, balances, positions, PnL, fees,
equity curve, and performance metrics.

Continuously reconciles against the Roostoo exchange.
An order is confirmed only after verified exchange state.
"""

from __future__ import annotations

import json
import logging
import math
import pathlib
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from config.trading_params import BotConfig
from core.api_client import RoostooAPIClient, APIError

logger = logging.getLogger("portfolio_tracker")


@dataclass
class Position:
    """A tracked position (long or short)."""
    symbol: str
    side: str               # LONG or SHORT
    quantity: float
    avg_entry: float
    current_price: float = 0.0
    notional: float = 0.0
    unrealized_pnl: float = 0.0
    strategy: str = ""
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0
    highest_price: float = 0.0     # For trailing stop
    tp1_hit: bool = False
    entry_time: float = 0.0

    # Short-specific
    collateral: float = 0.0
    short_position_id: Optional[int] = None

    def update_price(self, price: float) -> None:
        self.current_price = price
        self.notional = self.quantity * price
        if self.side == "LONG":
            self.unrealized_pnl = (price - self.avg_entry) * self.quantity
        else:
            self.unrealized_pnl = (self.avg_entry - price) * self.quantity
        self.highest_price = max(self.highest_price, price)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "avg_entry": self.avg_entry,
            "current_price": self.current_price,
            "notional": round(self.notional, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "strategy": self.strategy,
            "stop_loss": self.stop_loss,
            "take_profit_1": self.take_profit_1,
            "take_profit_2": self.take_profit_2,
            "highest_price": self.highest_price,
            "tp1_hit": self.tp1_hit,
            "collateral": self.collateral,
        }


@dataclass
class EquitySnapshot:
    """A timestamped equity reading."""
    timestamp: int        # milliseconds
    equity: float
    cash: float
    gross_exposure: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "equity": round(self.equity, 2),
            "cash": round(self.cash, 2),
            "gross_exposure": round(self.gross_exposure, 2),
        }


class PortfolioTracker:
    """
    Local portfolio ledger with exchange reconciliation.

    Tracks:
    - Cash (USD free balance)
    - Coin balances
    - Long positions (from spot buys)
    - Short positions (from v6/short_open)
    - Realized PnL, unrealized PnL
    - Fees paid
    - Equity curve
    - Performance metrics
    """

    def __init__(self, config: BotConfig, api_client: RoostooAPIClient):
        self._config = config
        self._api = api_client
        self._lock = threading.RLock()

        # Balances
        self._cash: float = config.exchange.initial_portfolio_value
        self._locked_cash: float = 0.0
        self._balances: Dict[str, float] = {}  # symbol -> free qty

        # Positions
        self._positions: Dict[str, Position] = {}  # symbol -> position

        # PnL tracking
        self._realized_pnl: float = 0.0
        self._total_fees: float = 0.0
        self._trade_count: int = 0
        self._wins: int = 0
        self._losses: int = 0
        self._total_win_amount: float = 0.0
        self._total_loss_amount: float = 0.0

        # Equity
        self._initial_equity: float = config.exchange.initial_portfolio_value
        self._equity: float = self._initial_equity
        self._peak_equity: float = self._initial_equity
        self._equity_curve: List[EquitySnapshot] = []
        self._daily_returns: List[float] = []

        # State
        self._last_reconcile: float = 0.0
        self._reconcile_errors: int = 0

        logger.info("PortfolioTracker initialized — initial=%s", self._initial_equity)

    # =======================================================================
    # Exchange reconciliation
    # =======================================================================

    def reconcile(self) -> bool:
        """
        Synchronize local state with Roostoo exchange.
        This is the source of truth — local state must match.
        """
        try:
            # Get balances
            bal_resp = self._api.get_balance()
            if not bal_resp.success:
                self._reconcile_errors += 1
                logger.error("Balance reconciliation failed: %s", bal_resp.error_message)
                return False

            wallet = bal_resp.data.get("Wallet", {})

            with self._lock:
                self._cash = wallet.get("USD", {}).get("Free", 0.0)
                self._locked_cash = wallet.get("USD", {}).get("Lock", 0.0)

                # Update coin balances
                for coin, bal_data in wallet.items():
                    if coin == "USD":
                        continue
                    free = bal_data.get("Free", 0.0)
                    self._balances[coin] = free

            # Get short positions
            short_resp = self._api.get_short_positions()
            if short_resp.success:
                positions = short_resp.data.get("Positions", [])
                with self._lock:
                    # Update/create short positions
                    active_short_symbols = set()
                    for pos in positions:
                        pair = pos.get("Pair", "")
                        active_short_symbols.add(pair)
                        self._update_short_position(pos)

                    # Remove closed shorts
                    to_remove = [
                        sym for sym, p in self._positions.items()
                        if p.side == "SHORT" and sym not in active_short_symbols
                    ]
                    for sym in to_remove:
                        del self._positions[sym]

            self._last_reconcile = time.time()
            self._reconcile_errors = 0
            self._update_equity()

            logger.info(
                "Reconciled — cash=%.2f equity=%.2f positions=%d",
                self._cash, self._equity, len(self._positions),
            )
            return True

        except APIError as exc:
            self._reconcile_errors += 1
            logger.error("Reconciliation error: %s", exc)
            return False

    def _update_short_position(self, pos_data: Dict[str, Any]) -> None:
        """Update a short position from exchange data."""
        pair = pos_data.get("Pair", "")
        existing = self._positions.get(pair)

        if existing and existing.side == "SHORT":
            existing.quantity = pos_data.get("ShortQty", existing.quantity)
            existing.avg_entry = pos_data.get("EntryPrice", existing.avg_entry)
            existing.current_price = pos_data.get("CurrentPrice", existing.current_price)
            existing.unrealized_pnl = pos_data.get("UnrealizedPNL", 0.0)
            existing.collateral = pos_data.get("Collateral", existing.collateral)
            existing.notional = existing.quantity * existing.current_price
        else:
            self._positions[pair] = Position(
                symbol=pair,
                side="SHORT",
                quantity=pos_data.get("ShortQty", 0.0),
                avg_entry=pos_data.get("EntryPrice", 0.0),
                current_price=pos_data.get("CurrentPrice", 0.0),
                unrealized_pnl=pos_data.get("UnrealizedPNL", 0.0),
                collateral=pos_data.get("Collateral", 0.0),
                short_position_id=pos_data.get("ID"),
                entry_time=pos_data.get("CreateTimestamp", 0) / 1000,
            )

    # =======================================================================
    # Position management
    # =======================================================================

    def record_fill(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        commission: float,
        strategy: str = "",
        stop_loss: float = 0.0,
        tp1: float = 0.0,
        tp2: float = 0.0,
    ) -> None:
        """Record a filled order in the portfolio."""
        with self._lock:
            self._total_fees += commission
            self._trade_count += 1

            if side == "BUY":
                coin = symbol.split("/")[0]
                existing = self._positions.get(symbol)
                if existing and existing.side == "LONG":
                    # Average in
                    total_qty = existing.quantity + quantity
                    if total_qty > 0:
                        existing.avg_entry = (
                            (existing.avg_entry * existing.quantity + price * quantity)
                            / total_qty
                        )
                    existing.quantity = total_qty
                else:
                    self._positions[symbol] = Position(
                        symbol=symbol,
                        side="LONG",
                        quantity=quantity,
                        avg_entry=price,
                        current_price=price,
                        strategy=strategy,
                        stop_loss=stop_loss,
                        take_profit_1=tp1,
                        take_profit_2=tp2,
                        entry_time=time.time(),
                    )

            elif side == "SELL":
                existing = self._positions.get(symbol)
                if existing and existing.side == "LONG":
                    # Calculate realized PnL
                    pnl = (price - existing.avg_entry) * quantity - commission
                    self._realized_pnl += pnl

                    if pnl > 0:
                        self._wins += 1
                        self._total_win_amount += pnl
                    else:
                        self._losses += 1
                        self._total_loss_amount += abs(pnl)

                    existing.quantity -= quantity
                    if existing.quantity <= 0.0001:  # Effectively closed
                        del self._positions[symbol]

            self._update_equity()

    def record_short_fill(
        self,
        symbol: str,
        action: str,   # "OPEN" or "CLOSE"
        quantity: float,
        price: float,
        commission: float,
        collateral: float = 0.0,
        realized_pnl: float = 0.0,
        strategy: str = "",
        stop_loss: float = 0.0,
        tp1: float = 0.0,
        tp2: float = 0.0,
    ) -> None:
        """Record a short position open/close."""
        with self._lock:
            self._total_fees += commission
            self._trade_count += 1

            if action == "OPEN":
                self._positions[symbol] = Position(
                    symbol=symbol,
                    side="SHORT",
                    quantity=quantity,
                    avg_entry=price,
                    current_price=price,
                    collateral=collateral,
                    strategy=strategy,
                    stop_loss=stop_loss,
                    take_profit_1=tp1,
                    take_profit_2=tp2,
                    entry_time=time.time(),
                )
            elif action == "CLOSE":
                self._realized_pnl += realized_pnl
                if realized_pnl > 0:
                    self._wins += 1
                    self._total_win_amount += realized_pnl
                else:
                    self._losses += 1
                    self._total_loss_amount += abs(realized_pnl)

                existing = self._positions.get(symbol)
                if existing and existing.side == "SHORT":
                    existing.quantity -= quantity
                    if existing.quantity <= 0.0001:
                        del self._positions[symbol]

            self._update_equity()

    def update_prices(self, prices: Dict[str, float]) -> None:
        """Update current prices for all positions."""
        with self._lock:
            for symbol, pos in self._positions.items():
                if symbol in prices:
                    pos.update_price(prices[symbol])
            self._update_equity()

    # =======================================================================
    # Equity / Performance
    # =======================================================================

    def _update_equity(self) -> None:
        """Recalculate total equity."""
        total_unrealized = sum(p.unrealized_pnl for p in self._positions.values())
        position_value = sum(
            p.notional for p in self._positions.values() if p.side == "LONG"
        )
        short_collateral = sum(
            p.collateral for p in self._positions.values() if p.side == "SHORT"
        )

        self._equity = self._cash + self._locked_cash + position_value + total_unrealized + short_collateral
        self._peak_equity = max(self._peak_equity, self._equity)

        # Record equity snapshot (limit frequency)
        now_ms = int(time.time() * 1000)
        if not self._equity_curve or now_ms - self._equity_curve[-1].timestamp > 60000:
            gross_exp = position_value + short_collateral
            self._equity_curve.append(EquitySnapshot(
                timestamp=now_ms,
                equity=self._equity,
                cash=self._cash,
                gross_exposure=gross_exp,
            ))
            # Limit history size
            if len(self._equity_curve) > 10000:
                self._equity_curve = self._equity_curve[-10000:]

    def compute_metrics(self) -> Dict[str, Any]:
        """Compute competition and performance metrics."""
        total_return = (self._equity - self._initial_equity) / self._initial_equity if self._initial_equity else 0
        current_dd = (self._peak_equity - self._equity) / self._peak_equity if self._peak_equity > 0 else 0
        max_dd = self._compute_max_drawdown()

        # Daily returns for Sharpe/Sortino
        daily_returns = self._compute_daily_returns()
        sharpe = self._compute_sharpe(daily_returns)
        sortino = self._compute_sortino(daily_returns)
        calmar = self._compute_calmar(total_return, max_dd)

        # Composite score: 0.4×Sortino + 0.3×Sharpe + 0.3×Calmar
        composite = 0.4 * sortino + 0.3 * sharpe + 0.3 * calmar

        win_rate = self._wins / self._trade_count if self._trade_count > 0 else 0
        avg_win = self._total_win_amount / self._wins if self._wins > 0 else 0
        avg_loss = self._total_loss_amount / self._losses if self._losses > 0 else 0
        profit_factor = self._total_win_amount / self._total_loss_amount if self._total_loss_amount > 0 else float("inf")

        gross_exposure = sum(
            p.notional for p in self._positions.values() if p.side == "LONG"
        ) + sum(
            p.collateral for p in self._positions.values() if p.side == "SHORT"
        )

        return {
            "equity": round(self._equity, 2),
            "cash": round(self._cash, 2),
            "available_cash": round(self._cash, 2),
            "locked_cash": round(self._locked_cash, 2),
            "gross_exposure": round(gross_exposure, 2),
            "realized_pnl": round(self._realized_pnl, 2),
            "unrealized_pnl": round(sum(p.unrealized_pnl for p in self._positions.values()), 2),
            "total_pnl": round(self._realized_pnl + sum(p.unrealized_pnl for p in self._positions.values()), 2),
            "total_return": round(total_return, 6),
            "peak_equity": round(self._peak_equity, 2),
            "current_drawdown": round(current_dd, 6),
            "max_drawdown": round(max_dd, 6),
            "total_fees": round(self._total_fees, 2),
            "trade_count": self._trade_count,
            "win_rate": round(win_rate, 4),
            "wins": self._wins,
            "losses": self._losses,
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "∞",
            "sharpe": round(sharpe, 4),
            "sortino": round(sortino, 4),
            "calmar": round(calmar, 4),
            "composite_score": round(composite, 4),
            "position_count": len(self._positions),
        }

    def _compute_daily_returns(self) -> List[float]:
        """Compute daily returns from equity curve."""
        if len(self._equity_curve) < 2:
            return []

        daily: list[float] = []
        day_ms = 86400000
        prev_equity = self._equity_curve[0].equity
        prev_day = self._equity_curve[0].timestamp // day_ms

        for snap in self._equity_curve[1:]:
            current_day = snap.timestamp // day_ms
            if current_day > prev_day:
                ret = (snap.equity - prev_equity) / prev_equity if prev_equity > 0 else 0
                daily.append(ret)
                prev_equity = snap.equity
                prev_day = current_day

        return daily

    def _compute_max_drawdown(self) -> float:
        """Compute maximum drawdown from equity curve."""
        if not self._equity_curve:
            return 0.0

        peak = self._equity_curve[0].equity
        max_dd = 0.0

        for snap in self._equity_curve:
            peak = max(peak, snap.equity)
            if peak > 0:
                dd = (peak - snap.equity) / peak
                max_dd = max(max_dd, dd)

        return max_dd

    @staticmethod
    def _compute_sharpe(daily_returns: List[float], risk_free_rate: float = 0.0) -> float:
        """Annualized Sharpe ratio."""
        if len(daily_returns) < 5:
            return 0.0
        arr = np.array(daily_returns)
        excess = arr - risk_free_rate / 365.0
        std = np.std(excess, ddof=1)
        if std <= 0:
            return 0.0
        return float(np.mean(excess) / std * math.sqrt(365))

    @staticmethod
    def _compute_sortino(daily_returns: List[float], risk_free_rate: float = 0.0) -> float:
        """Annualized Sortino ratio."""
        if len(daily_returns) < 5:
            return 0.0
        arr = np.array(daily_returns)
        excess = arr - risk_free_rate / 365.0
        downside = arr[arr < 0]
        if len(downside) == 0:
            return 10.0  # Cap at 10 if no downside
        downside_std = np.std(downside, ddof=1)
        if downside_std <= 0:
            return 10.0
        return float(np.mean(excess) / downside_std * math.sqrt(365))

    @staticmethod
    def _compute_calmar(total_return: float, max_drawdown: float) -> float:
        """Calmar ratio = annualized return / max drawdown."""
        if max_drawdown <= 0:
            return 10.0 if total_return > 0 else 0.0
        # Approximate annualization (assume competition period)
        return total_return / max_drawdown

    # =======================================================================
    # Accessors
    # =======================================================================

    def get_positions(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [p.to_dict() for p in self._positions.values()]

    def get_balances(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._balances)

    def get_position(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def has_position(self, symbol: str) -> bool:
        return symbol in self._positions

    def get_position_side(self, symbol: str) -> Optional[str]:
        p = self._positions.get(symbol)
        return p.side if p else None

    @property
    def equity(self) -> float:
        return self._equity

    @property
    def cash(self) -> float:
        return self._cash

    def get_gross_exposure(self) -> float:
        return sum(
            p.notional for p in self._positions.values() if p.side == "LONG"
        ) + sum(
            p.collateral for p in self._positions.values() if p.side == "SHORT"
        )

    def get_equity_curve(self, last_n: int = 500) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in self._equity_curve[-last_n:]]

    def get_portfolio_context(self, symbol: str) -> Dict[str, Any]:
        """Get portfolio context for strategy evaluation."""
        pos = self._positions.get(symbol)
        return {
            "has_position": pos is not None,
            "position_side": pos.side if pos else None,
            "position_quantity": pos.quantity if pos else 0,
            "position_entry": pos.avg_entry if pos else 0,
            "position_unrealized_pnl": pos.unrealized_pnl if pos else 0,
            "total_positions": len(self._positions),
            "cash": self._cash,
            "equity": self._equity,
            "gross_exposure": self.get_gross_exposure(),
        }

    def save_state(self, filepath: str = "state/portfolio_state.json") -> None:
        """Persist portfolio state to disk."""
        state = {
            "timestamp": time.time(),
            "cash": self._cash,
            "locked_cash": self._locked_cash,
            "realized_pnl": self._realized_pnl,
            "total_fees": self._total_fees,
            "trade_count": self._trade_count,
            "wins": self._wins,
            "losses": self._losses,
            "total_win_amount": self._total_win_amount,
            "total_loss_amount": self._total_loss_amount,
            "peak_equity": self._peak_equity,
            "positions": {sym: p.to_dict() for sym, p in self._positions.items()},
        }
        path = pathlib.Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)

    def load_state(self, filepath: str = "state/portfolio_state.json") -> bool:
        """Load portfolio state from disk (restart recovery)."""
        path = pathlib.Path(filepath)
        if not path.exists():
            return False
        try:
            with open(path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
            self._realized_pnl = state.get("realized_pnl", 0.0)
            self._total_fees = state.get("total_fees", 0.0)
            self._trade_count = state.get("trade_count", 0)
            self._wins = state.get("wins", 0)
            self._losses = state.get("losses", 0)
            self._total_win_amount = state.get("total_win_amount", 0.0)
            self._total_loss_amount = state.get("total_loss_amount", 0.0)
            self._peak_equity = state.get("peak_equity", self._initial_equity)
            logger.info("Portfolio state loaded from %s", filepath)
            return True
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            logger.error("Failed to load portfolio state: %s", exc)
            return False
