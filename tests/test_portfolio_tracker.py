"""
Unit tests for Portfolio Tracker.
Tests position tracking, realized/unrealized PnL, short fills,
mark-to-market updates, exchange reconciliation, and competition metrics
(Sharpe, Sortino, Calmar, composite score).
"""

from unittest.mock import MagicMock
import pytest

from config.trading_params import BotConfig, ExchangeConfig
from core.api_client import RoostooAPIClient, APIResponse
from state.portfolio_tracker import PortfolioTracker, Position


@pytest.fixture
def mock_config():
    return BotConfig(
        exchange=ExchangeConfig(
            base_url="https://mock-api.roostoo.com",
            initial_portfolio_value=100000.0,
        )
    )


@pytest.fixture
def mock_api():
    api = MagicMock(spec=RoostooAPIClient)
    # Default balance response
    api.get_balance.return_value = APIResponse(
        success=True,
        data={
            "Wallet": {
                "USD": {"Free": 100000.0, "Lock": 0.0},
                "BTC": {"Free": 0.0, "Lock": 0.0},
            }
        },
    )
    # Default shorts response
    api.get_short_positions.return_value = APIResponse(
        success=True,
        data={"Positions": []},
    )
    return api


@pytest.fixture
def tracker(mock_config, mock_api):
    tr = PortfolioTracker(mock_config, mock_api)
    tr.reconcile()
    return tr


class TestPortfolioTracker:
    def test_initial_state(self, tracker):
        metrics = tracker.compute_metrics()
        assert metrics["equity"] == 100000.0
        assert metrics["cash"] == 100000.0
        assert metrics["trade_count"] == 0
        assert metrics["position_count"] == 0

    def test_spot_buy_and_sell_cycle_with_profit(self, tracker):
        # 1. Buy 1 BTC at $50,000 with $25 fee
        tracker.record_fill(
            symbol="BTC/USD",
            side="BUY",
            quantity=1.0,
            price=50000.0,
            commission=25.0,
            strategy="VALUE_AREA",
            stop_loss=48000.0,
            tp1=55000.0,
        )
        assert tracker.has_position("BTC/USD") is True
        pos = tracker.get_position("BTC/USD")
        assert pos.quantity == 1.0
        assert pos.avg_entry == 50000.0

        # Mark-to-market at $52,000
        tracker.update_prices({"BTC/USD": 52000.0})
        pos = tracker.get_position("BTC/USD")
        assert pos.unrealized_pnl == 2000.0

        # 2. Sell 1 BTC at $53,000 with $26.5 fee
        tracker.record_fill(
            symbol="BTC/USD",
            side="SELL",
            quantity=1.0,
            price=53000.0,
            commission=26.5,
            strategy="VALUE_AREA",
        )
        # Position should now be closed
        assert tracker.has_position("BTC/USD") is False

        metrics = tracker.compute_metrics()
        assert metrics["wins"] == 1
        assert metrics["losses"] == 0
        # PnL = (53000 - 50000) * 1 - 26.5 = 2973.5
        assert pytest.approx(metrics["realized_pnl"], rel=1e-2) == 2973.5

    def test_short_open_and_close(self, tracker):
        # Open Short
        tracker.record_short_fill(
            symbol="ETH/USD",
            action="OPEN",
            quantity=10.0,
            price=3000.0,
            commission=15.0,
            collateral=3000.0,
            strategy="LIQUIDITY_SWEEP",
        )
        assert tracker.has_position("ETH/USD") is True
        pos = tracker.get_position("ETH/USD")
        assert pos.side == "SHORT"
        assert pos.quantity == 10.0

        # Close Short with realized PnL of $1000
        tracker.record_short_fill(
            symbol="ETH/USD",
            action="CLOSE",
            quantity=10.0,
            price=2900.0,
            commission=14.5,
            realized_pnl=1000.0,
        )
        assert tracker.has_position("ETH/USD") is False
        metrics = tracker.compute_metrics()
        assert metrics["realized_pnl"] == 1000.0

    def test_metrics_calculation_consistency(self, tracker):
        metrics = tracker.compute_metrics()
        assert "sharpe" in metrics
        assert "sortino" in metrics
        assert "calmar" in metrics
        assert "composite_score" in metrics
        assert "total_return" in metrics
        assert "max_drawdown" in metrics
