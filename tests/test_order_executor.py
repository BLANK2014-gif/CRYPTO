"""
Unit tests for Order Executor.
Tests precision rounding, maker/taker decision logic,
order placement, order state tracking, and lifecycle management.
"""

from unittest.mock import MagicMock
import pytest

from config.trading_params import BotConfig, ExecutionConfig
from core.api_client import RoostooAPIClient, APIResponse
from core.order_executor import OrderExecutor, OrderState, ManagedOrder
from core.risk_manager import RiskCheckResult, RiskDecision
from strategies.base import Signal, SignalDirection


@pytest.fixture
def mock_api():
    return MagicMock(spec=RoostooAPIClient)


@pytest.fixture
def executor(mock_api):
    config = BotConfig(
        execution=ExecutionConfig(
            prefer_maker=True,
            limit_order_timeout_seconds=60,
        )
    )
    ex = OrderExecutor(config, mock_api)
    ex.set_precision("BTC/USD", price_prec=2, amount_prec=6)
    return ex


class TestOrderExecutor:
    def test_precision_rounding(self, executor):
        # Round price to 2 decimals
        assert executor._round_price("BTC/USD", 54321.6789) == 54321.68
        # Round quantity down (floor) to 6 decimals
        assert executor._round_quantity("BTC/USD", 0.12345678) == 0.123456

    def test_decide_order_type(self, executor):
        sig_urgent = Signal(
            symbol="BTC/USD",
            direction=SignalDirection.BUY,
            strategy="VALUE_AREA",
            confidence=0.85,
            urgency="HIGH",
        )
        assert executor._decide_order_type(sig_urgent, spread_bps=20.0, is_buy=True) == "MARKET"

        sig_normal = Signal(
            symbol="BTC/USD",
            direction=SignalDirection.BUY,
            strategy="VALUE_AREA",
            confidence=0.85,
            urgency="NORMAL",
        )
        # Tight spread (< 5 bps) -> MARKET
        assert executor._decide_order_type(sig_normal, spread_bps=3.0, is_buy=True) == "MARKET"
        # Wide spread (> 10 bps) -> LIMIT
        assert executor._decide_order_type(sig_normal, spread_bps=15.0, is_buy=True) == "LIMIT"

    def test_execute_buy_limit_success(self, executor, mock_api):
        mock_api.place_order.return_value = APIResponse(
            success=True,
            data={
                "OrderDetail": {
                    "OrderID": 55512,
                    "Status": "FILLED",
                    "FilledQuantity": 0.5,
                    "FilledAverPrice": 50000.0,
                    "CommissionChargeValue": 12.5,
                    "Role": "MAKER",
                }
            },
        )

        sig = Signal(
            symbol="BTC/USD",
            direction=SignalDirection.BUY,
            strategy="VALUE_AREA",
            confidence=0.85,
            urgency="NORMAL",
        )
        risk_res = RiskCheckResult(
            decision=RiskDecision.APPROVED,
            reason="Approved",
            approved_quantity=0.5,
        )

        order = executor.execute_signal(
            signal=sig,
            risk_result=risk_res,
            current_bid=49990.0,
            current_ask=50010.0,
            current_spread_bps=20.0,
        )

        assert order is not None
        assert order.order_id == 55512
        assert order.state == OrderState.FILLED
        assert order.role == "MAKER"
        assert order.filled_price == 50000.0

    def test_execute_short_success(self, executor, mock_api):
        mock_api.short_open.return_value = APIResponse(
            success=True,
            data={
                "ID": 7771,
                "Pair": "BTC/USD",
                "Status": "OPEN",
                "Collateral": 1000.0,
                "EntryPrice": 50000.0,
                "ShortQty": 0.02,
                "OpenFee": 2.5,
            },
        )

        sig = Signal(
            symbol="BTC/USD",
            direction=SignalDirection.SELL,
            strategy="LIQUIDITY_SWEEP",
            confidence=0.85,
        )
        risk_res = RiskCheckResult(
            decision=RiskDecision.APPROVED,
            reason="Approved",
            approved_collateral=1000.0,
        )

        order = executor.execute_signal(
            signal=sig,
            risk_result=risk_res,
            current_bid=49990.0,
            current_ask=50010.0,
            current_spread_bps=20.0,
        )

        assert order is not None
        assert order.is_short is True
        assert order.state == OrderState.FILLED
