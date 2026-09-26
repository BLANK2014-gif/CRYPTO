"""
Roostoo Trading Bot — Order Executor
======================================
Intelligent execution with maker-preference logic.
Handles order placement, fill verification, cancellation,
and short position management.

All exchange calls go through the API client.
"""

from __future__ import annotations

import logging
import math
import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from config.trading_params import BotConfig
from core.api_client import RoostooAPIClient, APIResponse, APIError
from core.risk_manager import RiskCheckResult
from strategies.base import Signal, SignalDirection

logger = logging.getLogger("order_executor")


class OrderState(str, Enum):
    """Order lifecycle states."""
    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTED = "SUBMITTED"
    PENDING = "PENDING"         # Limit order on book
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"         # Timed out and cancelled
    UNKNOWN = "UNKNOWN"


@dataclass
class ManagedOrder:
    """An order tracked by the executor."""
    order_id: Optional[int] = None
    symbol: str = ""
    side: str = ""               # BUY, SELL
    order_type: str = ""         # LIMIT, MARKET
    quantity: float = 0.0
    price: float = 0.0
    collateral: float = 0.0     # For shorts
    state: OrderState = OrderState.PENDING_SUBMIT
    strategy: str = ""
    signal: Optional[Signal] = None
    risk_result: Optional[RiskCheckResult] = None

    # Fill data
    filled_quantity: float = 0.0
    filled_price: float = 0.0
    commission: float = 0.0
    role: str = ""              # MAKER or TAKER

    # Timing
    submit_time: float = 0.0
    fill_time: float = 0.0
    timeout_seconds: float = 120.0

    # Short-specific
    is_short: bool = False
    short_position_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "quantity": self.quantity,
            "price": self.price,
            "state": self.state.value,
            "strategy": self.strategy,
            "filled_quantity": self.filled_quantity,
            "filled_price": self.filled_price,
            "commission": self.commission,
            "role": self.role,
            "submit_time": self.submit_time,
            "is_short": self.is_short,
        }


class OrderExecutor:
    """
    Handles order execution with maker-preference logic.

    Decides between limit and market orders based on:
    - Spread width
    - Signal urgency
    - Volatility
    - Fee savings
    """

    def __init__(self, config: BotConfig, api_client: RoostooAPIClient):
        self._config = config
        self._api = api_client
        self._exec = config.execution
        self._lock = threading.Lock()

        # Order tracking
        self._active_orders: Dict[int, ManagedOrder] = {}
        self._completed_orders: List[ManagedOrder] = []
        self._max_completed = 500

        # Symbol precision cache
        self._precision_cache: Dict[str, tuple] = {}

        logger.info("OrderExecutor initialized — prefer_maker=%s", self._exec.prefer_maker)

    def set_precision(self, symbol: str, price_prec: int, amount_prec: int) -> None:
        """Cache precision rules for a symbol."""
        self._precision_cache[symbol] = (price_prec, amount_prec)

    def _round_price(self, symbol: str, price: float) -> float:
        """Round price to exchange precision."""
        prec = self._precision_cache.get(symbol, (2, 6))
        return round(price, prec[0])

    def _round_quantity(self, symbol: str, quantity: float) -> float:
        """Round quantity to exchange precision."""
        prec = self._precision_cache.get(symbol, (2, 6))
        factor = 10 ** prec[1]
        return math.floor(quantity * factor) / factor  # Always round down

    # =======================================================================
    # Execute a signal
    # =======================================================================

    def execute_signal(
        self,
        signal: Signal,
        risk_result: RiskCheckResult,
        current_bid: float,
        current_ask: float,
        current_spread_bps: float,
    ) -> Optional[ManagedOrder]:
        """
        Execute an approved signal.

        Decides between limit and market order based on conditions.
        Returns the ManagedOrder or None on failure.
        """
        if signal.direction == SignalDirection.BUY:
            return self._execute_buy(
                signal, risk_result, current_bid, current_ask, current_spread_bps,
            )
        elif signal.direction == SignalDirection.SELL:
            return self._execute_short(
                signal, risk_result, current_bid, current_ask, current_spread_bps,
            )
        elif signal.direction == SignalDirection.DERISK:
            return self._execute_derisk(
                signal, risk_result, current_bid, current_ask,
            )
        else:
            logger.warning("Cannot execute signal direction: %s", signal.direction)
            return None

    def _decide_order_type(
        self,
        signal: Signal,
        spread_bps: float,
        is_buy: bool,
    ) -> str:
        """
        Decide between LIMIT and MARKET order.

        Prefers maker (limit) when:
        - Spread is wide enough that limit will likely fill
        - Signal is not urgent
        - Fee savings justify the risk

        Uses market when:
        - Signal is urgent (de-risk, emergency)
        - Spread is very tight (limit may not fill)
        - Time-sensitive entry
        """
        if signal.urgency == "EMERGENCY":
            logger.info("MARKET order — emergency urgency")
            return "MARKET"

        if signal.urgency == "HIGH":
            logger.info("MARKET order — high urgency")
            return "MARKET"

        if not self._exec.prefer_maker:
            return "MARKET"

        # Tight spread makes limit less likely to fill
        if spread_bps < 5:
            logger.info("MARKET order — spread too tight (%.1f bps)", spread_bps)
            return "MARKET"

        # Wide spread = good opportunity for limit
        if spread_bps > 10:
            logger.info("LIMIT order — good spread (%.1f bps)", spread_bps)
            return "LIMIT"

        # Default: use signal's preferred type
        return signal.order_type

    def _execute_buy(
        self,
        signal: Signal,
        risk_result: RiskCheckResult,
        current_bid: float,
        current_ask: float,
        spread_bps: float,
    ) -> Optional[ManagedOrder]:
        """Execute a BUY order."""
        quantity = self._round_quantity(signal.symbol, risk_result.approved_quantity)
        if quantity <= 0:
            logger.warning("Quantity rounded to zero for %s", signal.symbol)
            return None

        order_type = self._decide_order_type(signal, spread_bps, is_buy=True)

        # Determine price
        if order_type == "LIMIT":
            # Place limit slightly above bid to get maker fill
            price = self._round_price(signal.symbol, current_bid + (current_ask - current_bid) * 0.1)
        else:
            price = None

        # Create managed order
        order = ManagedOrder(
            symbol=signal.symbol,
            side="BUY",
            order_type=order_type,
            quantity=quantity,
            price=price if price else current_ask,
            state=OrderState.PENDING_SUBMIT,
            strategy=signal.strategy,
            signal=signal,
            risk_result=risk_result,
            submit_time=time.time(),
            timeout_seconds=self._exec.limit_order_timeout_seconds,
        )

        # Submit to exchange
        try:
            resp = self._api.place_order(
                pair=signal.symbol,
                side="BUY",
                quantity=quantity,
                order_type=order_type,
                price=price,
            )

            if resp.success and "OrderDetail" in resp.data:
                detail = resp.data["OrderDetail"]
                order.order_id = detail.get("OrderID")
                order.state = self._parse_order_status(detail.get("Status", ""))
                order.filled_quantity = detail.get("FilledQuantity", 0.0)
                order.filled_price = detail.get("FilledAverPrice", 0.0)
                order.commission = detail.get("CommissionChargeValue", 0.0)
                order.role = detail.get("Role", "")

                if order.state == OrderState.FILLED:
                    order.fill_time = time.time()

                with self._lock:
                    if order.order_id:
                        self._active_orders[order.order_id] = order

                logger.info(
                    "Order submitted: id=%s %s %s %.6f @ %s — status=%s role=%s",
                    order.order_id, order.side, signal.symbol, quantity,
                    price or "MARKET", order.state.value, order.role,
                )
                return order
            else:
                order.state = OrderState.FAILED
                logger.error("Order rejected: %s", resp.error_message)
                return order

        except APIError as exc:
            order.state = OrderState.FAILED
            logger.error("Order execution failed: %s", exc)
            return order

    def _execute_short(
        self,
        signal: Signal,
        risk_result: RiskCheckResult,
        current_bid: float,
        current_ask: float,
        spread_bps: float,
    ) -> Optional[ManagedOrder]:
        """Execute a SHORT via v6/short_open."""
        collateral = round(risk_result.approved_collateral, 2)
        if collateral < 1:
            logger.warning("Collateral below minimum ($1) for %s", signal.symbol)
            return None

        order_type = self._decide_order_type(signal, spread_bps, is_buy=False)
        price = None
        if order_type == "LIMIT":
            # For short limit: set price slightly above current to try maker
            price = self._round_price(signal.symbol, current_ask * 1.001)

        order = ManagedOrder(
            symbol=signal.symbol,
            side="SHORT_OPEN",
            order_type=order_type,
            collateral=collateral,
            price=price if price else current_bid,
            state=OrderState.PENDING_SUBMIT,
            strategy=signal.strategy,
            signal=signal,
            risk_result=risk_result,
            submit_time=time.time(),
            is_short=True,
            timeout_seconds=self._exec.limit_order_timeout_seconds,
        )

        try:
            resp = self._api.short_open(
                pair=signal.symbol,
                collateral=collateral,
                price=price,
            )

            if resp.success:
                data = resp.data
                order.order_id = data.get("ID")
                order.short_position_id = data.get("ID")
                status = data.get("Status", "")
                order.state = OrderState.FILLED if status == "OPEN" else OrderState.PENDING
                order.filled_price = data.get("EntryPrice", 0.0)
                order.quantity = data.get("ShortQty", 0.0)
                order.commission = data.get("OpenFee", 0.0)

                if order.state == OrderState.FILLED:
                    order.fill_time = time.time()

                with self._lock:
                    if order.order_id:
                        self._active_orders[order.order_id] = order

                logger.info(
                    "Short opened: id=%s %s collateral=%s entry=%s qty=%s status=%s",
                    order.order_id, signal.symbol, collateral,
                    order.filled_price, order.quantity, order.state.value,
                )
                return order
            else:
                order.state = OrderState.FAILED
                logger.error("Short open rejected: %s", resp.error_message)
                return order

        except APIError as exc:
            order.state = OrderState.FAILED
            logger.error("Short open failed: %s", exc)
            return order

    def _execute_derisk(
        self,
        signal: Signal,
        risk_result: RiskCheckResult,
        current_bid: float,
        current_ask: float,
    ) -> Optional[ManagedOrder]:
        """
        De-risk: sell existing spot exposure or close short.
        Uses MARKET order for urgency.
        """
        # The portfolio tracker will determine the quantity to sell
        # For now, create a de-risk order marker
        order = ManagedOrder(
            symbol=signal.symbol,
            side="SELL",
            order_type="MARKET",
            state=OrderState.PENDING_SUBMIT,
            strategy=signal.strategy,
            signal=signal,
            submit_time=time.time(),
        )

        logger.info("De-risk signal for %s — will be handled by portfolio manager", signal.symbol)
        return order

    # =======================================================================
    # Order management
    # =======================================================================

    def check_pending_orders(self) -> List[ManagedOrder]:
        """
        Check status of pending limit orders.
        Cancel expired orders.
        Returns list of orders whose state changed.
        """
        changed: list[ManagedOrder] = []
        now = time.time()

        with self._lock:
            to_check = [
                o for o in self._active_orders.values()
                if o.state == OrderState.PENDING
            ]

        for order in to_check:
            # Check timeout
            elapsed = now - order.submit_time
            if elapsed > order.timeout_seconds:
                logger.info(
                    "Order %s timed out after %.0fs — cancelling",
                    order.order_id, elapsed,
                )
                self._cancel_order(order)
                order.state = OrderState.EXPIRED
                changed.append(order)
                continue

            # Query status
            if order.order_id:
                try:
                    resp = self._api.query_order(order_id=order.order_id)
                    if resp.success and "OrderMatched" in resp.data:
                        matches = resp.data["OrderMatched"]
                        if matches:
                            detail = matches[0]
                            new_state = self._parse_order_status(detail.get("Status", ""))
                            if new_state != order.state:
                                order.state = new_state
                                order.filled_quantity = detail.get("FilledQuantity", 0.0)
                                order.filled_price = detail.get("FilledAverPrice", 0.0)
                                order.commission = detail.get("CommissionChargeValue", 0.0)
                                if new_state == OrderState.FILLED:
                                    order.fill_time = time.time()
                                changed.append(order)
                except APIError as exc:
                    logger.warning("Failed to check order %s: %s", order.order_id, exc)

        # Move completed orders to history
        with self._lock:
            for order in changed:
                if order.state in (OrderState.FILLED, OrderState.CANCELLED,
                                   OrderState.FAILED, OrderState.EXPIRED):
                    if order.order_id in self._active_orders:
                        del self._active_orders[order.order_id]
                    self._completed_orders.append(order)
                    if len(self._completed_orders) > self._max_completed:
                        self._completed_orders = self._completed_orders[-self._max_completed:]

        return changed

    def _cancel_order(self, order: ManagedOrder) -> bool:
        """Cancel a pending order."""
        if order.order_id is None:
            return False
        try:
            resp = self._api.cancel_order(order_id=order.order_id)
            if resp.success:
                order.state = OrderState.CANCELLED
                logger.info("Order %s cancelled", order.order_id)
                return True
            else:
                logger.warning("Cancel failed for %s: %s", order.order_id, resp.error_message)
                return False
        except APIError as exc:
            logger.error("Cancel error for %s: %s", order.order_id, exc)
            return False

    def cancel_order(self, order_id: int) -> bool:
        """Cancel a specific pending order by ID."""
        with self._lock:
            order = self._active_orders.get(order_id)
        if not order:
            logger.warning("Order %s not found in active orders to cancel", order_id)
            return False
        return self._cancel_order(order)

    def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        """Cancel all pending orders, optionally for a specific symbol."""
        try:
            resp = self._api.cancel_order(pair=symbol)
            if resp.success:
                cancelled = resp.data.get("CanceledList", [])
                count = len(cancelled) if isinstance(cancelled, list) else 0
                logger.info("Cancelled %d orders (symbol=%s)", count, symbol or "ALL")
                return count
            return 0
        except APIError as exc:
            logger.error("Cancel all failed: %s", exc)
            return 0

    def close_spot(
        self, symbol: str, quantity: float,
    ) -> Optional[ManagedOrder]:
        """Close a spot long position via market sell."""
        quantity = self._round_quantity(symbol, quantity)
        if quantity <= 0:
            return None

        order = ManagedOrder(
            symbol=symbol,
            side="SELL",
            order_type="MARKET",
            quantity=quantity,
            state=OrderState.PENDING_SUBMIT,
            submit_time=time.time(),
        )

        try:
            resp = self._api.place_order(
                pair=symbol,
                side="SELL",
                order_type="MARKET",
                quantity=quantity,
            )
            if resp.success:
                data = resp.data
                order.order_id = data.get("ID")
                order.state = OrderState.FILLED
                order.filled_price = data.get("FilledAverPrice", 0.0)
                order.filled_quantity = data.get("FilledQuantity", quantity)
                order.commission = data.get("CommissionChargeValue", 0.0)
                order.role = "TAKER"
                order.fill_time = time.time()

                with self._lock:
                    self._completed_orders.append(order)

                logger.info("Spot position closed: %s qty=%s at %s", symbol, quantity, order.filled_price)
                return order
            else:
                order.state = OrderState.FAILED
                logger.error("Spot close rejected: %s", resp.error_message)
                return order
        except APIError as exc:
            order.state = OrderState.FAILED
            logger.error("Spot close failed: %s", exc)
            return order

    def close_short(
        self, symbol: str, close_pct: Optional[float] = None,
    ) -> Optional[ManagedOrder]:
        """Close a short position (fully or partially)."""
        try:
            resp = self._api.short_close(pair=symbol, close_pct=close_pct)
            if resp.success:
                order = ManagedOrder(
                    symbol=symbol,
                    side="SHORT_CLOSE",
                    state=OrderState.FILLED,
                    filled_price=resp.data.get("ClosePrice", 0.0),
                    quantity=resp.data.get("ClosedQty", 0.0),
                    commission=resp.data.get("CloseFee", 0.0),
                    fill_time=time.time(),
                    is_short=True,
                )
                logger.info(
                    "Short closed: %s pnl=%s fee=%s",
                    symbol, resp.data.get("RealizedPNL"), resp.data.get("CloseFee"),
                )
                return order
            else:
                logger.warning("Short close failed: %s", resp.error_message)
                return None
        except APIError as exc:
            logger.error("Short close error: %s", exc)
            return None

    # =======================================================================
    # Helpers
    # =======================================================================

    @staticmethod
    def _parse_order_status(status: str) -> OrderState:
        """Map Roostoo order status to our enum."""
        mapping = {
            "FILLED": OrderState.FILLED,
            "PENDING": OrderState.PENDING,
            "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
            "PARTIAL_FILLED": OrderState.PARTIALLY_FILLED,
            "CANCELED": OrderState.CANCELLED,
            "CANCELLED": OrderState.CANCELLED,
        }
        return mapping.get(status.upper(), OrderState.UNKNOWN)

    def get_active_orders(self) -> List[ManagedOrder]:
        with self._lock:
            return list(self._active_orders.values())

    def get_recent_completed(self, count: int = 20) -> List[ManagedOrder]:
        return self._completed_orders[-count:]

    def get_stats(self) -> Dict[str, Any]:
        return {
            "active_orders": len(self._active_orders),
            "completed_orders": len(self._completed_orders),
            "active_list": [o.to_dict() for o in self._active_orders.values()],
        }
