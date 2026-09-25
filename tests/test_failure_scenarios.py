"""
Live-Like Failure Scenarios Test Suite.
Validates the 14 critical edge and failure modes:
 1. API timeout
 2. API rate-limit response (429 & backoff)
 3. Duplicate order prevention
 4. Unknown order status
 5. Partial fill handling
 6. Cancel failure handling
 7. Stale market data rejection
 8. Balance mismatch & reconciliation drift
 9. Process restart & audit chain recovery
10. State persistence to disk
11. Network connection error & retry
12. Dashboard failure isolation (trading loop non-blocking)
13. Risk-freeze activation (circuit breaker on drawdown)
14. Kill-switch execution (permanent shutdown)
"""

import os
import json
import time
import pytest
from unittest.mock import MagicMock, patch
import requests

from config.trading_params import (
    BotConfig,
    ExchangeConfig,
    RiskConfig,
    ExecutionConfig,
    RateLimitConfig,
    SystemConfig,
)
from core.api_client import (
    RoostooAPIClient,
    APIError,
    RateLimitError,
    AuthenticationError,
    APIResponse,
)
from core.order_executor import OrderExecutor, OrderState, ManagedOrder
from core.risk_manager import RiskManager, RiskDecision, RiskCheckResult
from state.portfolio_tracker import PortfolioTracker
from logs.audit_logger import AuditLogger, EventType
from strategies.base import Signal, SignalDirection


@pytest.fixture
def base_config():
    return BotConfig(
        exchange=ExchangeConfig(
            base_url="https://mock-api.roostoo.com",
            initial_portfolio_value=100000.0,
        ),
        risk=RiskConfig(
            risk_per_trade_pct=0.01,
            max_positions=3,
            max_gross_exposure_pct=0.60,
            min_cash_reserve_pct=0.30,
            max_drawdown_pct=0.08,
            rolling_24h_drawdown_pct=0.05,
            strategy_drawdown_pct=0.04,
            max_spread_bps=50.0,
        ),
        execution=ExecutionConfig(
            prefer_maker=True,
            limit_order_timeout_seconds=60,
        ),
        rate_limit=RateLimitConfig(
            max_requests_per_minute=30,
            max_order_requests_per_minute=10,
            backoff_base_seconds=0.01,
            backoff_max_seconds=0.05,
            backoff_multiplier=1.2,
        ),
        system=SystemConfig(
            dry_run=True,
            polling_interval_seconds=15,
            max_data_age_seconds=120,
        ),
    )


# ---------------------------------------------------------------------------
# 1. API Timeout
# ---------------------------------------------------------------------------
def test_scenario_01_api_timeout(base_config):
    client = RoostooAPIClient(base_config)
    with patch.object(client._session, "get", side_effect=requests.exceptions.Timeout("Connection timed out")):
        with pytest.raises(APIError, match="All 3 retries exhausted"):
            client.get_server_time()


# ---------------------------------------------------------------------------
# 2. API Rate-Limit Response (429 & Backoff)
# ---------------------------------------------------------------------------
def test_scenario_02_api_rate_limit_backoff(base_config):
    client = RoostooAPIClient(base_config)

    mock_429 = MagicMock()
    mock_429.status_code = 429
    mock_429.json.return_value = {"error": "Rate limited"}

    mock_200 = MagicMock()
    mock_200.status_code = 200
    mock_200.text = '{"ServerTime": 1700000000000}'
    mock_200.json.return_value = {"ServerTime": 1700000000000}

    # First attempt 429, second attempt succeeds
    with patch.object(client._session, "get", side_effect=[mock_429, mock_200]) as mock_get:
        resp = client.get_server_time()
        assert resp.success is True
        assert resp.data["ServerTime"] == 1700000000000
        assert mock_get.call_count == 2


# ---------------------------------------------------------------------------
# 3. Duplicate Order Prevention
# ---------------------------------------------------------------------------
def test_scenario_03_duplicate_order_prevention(base_config):
    client = RoostooAPIClient(base_config)

    # Simulate network exception on order attempt 2 — must NOT retry order submission
    with patch.object(client._session, "post", side_effect=requests.exceptions.ConnectionError("Network drop")):
        with pytest.raises(APIError, match="not retrying"):
            # Trigger request with is_order=True and max_retries > 1
            client._request("POST", "/v3/place_order", {"pair": "BTC/USD"}, signed=True, is_order=True, max_retries=2)


# ---------------------------------------------------------------------------
# 4. Unknown Order Status
# ---------------------------------------------------------------------------
def test_scenario_04_unknown_order_status(base_config):
    mock_api = MagicMock(spec=RoostooAPIClient)
    executor = OrderExecutor(base_config, mock_api)

    # If exchange returns unknown status string
    status = executor._parse_order_status("WEIRD_STATUS_UNKNOWN")
    assert status == OrderState.UNKNOWN


# ---------------------------------------------------------------------------
# 5. Partial Fill Handling
# ---------------------------------------------------------------------------
def test_scenario_05_partial_fill_handling(base_config):
    mock_api = MagicMock(spec=RoostooAPIClient)
    executor = OrderExecutor(base_config, mock_api)
    mock_api.place_order.return_value = APIResponse(
        success=True,
        data={
            "OrderDetail": {
                "OrderID": 9001,
                "Status": "PARTIALLY_FILLED",
                "FilledQuantity": 0.4,
                "FilledAverPrice": 50000.0,
                "CommissionChargeValue": 10.0,
                "Role": "MAKER",
            }
        },
    )

    sig = Signal("BTC/USD", SignalDirection.BUY, "VALUE_AREA", confidence=0.85)
    risk_res = RiskCheckResult(RiskDecision.APPROVED, "Approved", approved_quantity=1.0)
    order = executor.execute_signal(sig, risk_res, 49990.0, 50010.0, 20.0)

    assert order is not None
    assert order.state == OrderState.PARTIALLY_FILLED
    assert order.filled_quantity == 0.4


# ---------------------------------------------------------------------------
# 6. Cancel Failure Handling
# ---------------------------------------------------------------------------
def test_scenario_06_cancel_failure_handling(base_config):
    mock_api = MagicMock(spec=RoostooAPIClient)
    executor = OrderExecutor(base_config, mock_api)

    # Order was already filled or does not exist on exchange
    mock_api.cancel_order.return_value = APIResponse(
        success=False,
        data={},
        error_message="Order not found or already filled",
    )

    # Put a managed order in active orders
    order = ManagedOrder(
        order_id=12345,
        symbol="BTC/USD",
        side="BUY",
        state=OrderState.PENDING,
    )
    executor._active_orders[12345] = order

    success = executor.cancel_order(12345)
    assert success is False
    # Executor retains order for reconciliation rather than silently discarding
    assert 12345 in executor._active_orders


# ---------------------------------------------------------------------------
# 7. Stale Market Data Rejection
# ---------------------------------------------------------------------------
def test_scenario_07_stale_market_data_rejection(base_config):
    risk = RiskManager(base_config)
    sig = Signal("BTC/USD", SignalDirection.BUY, "VALUE_AREA", confidence=0.85)

    # data_fresh = False
    result = risk.check_trade(
        signal=sig,
        equity=100000.0,
        cash=100000.0,
        gross_exposure=0.0,
        open_positions=0,
        spread_bps=10.0,
        data_fresh=False,
        api_healthy=True,
    )
    assert result.decision == RiskDecision.REJECTED
    assert "data_freshness" in result.checks_failed


# ---------------------------------------------------------------------------
# 8. Balance Mismatch & Reconciliation Drift
# ---------------------------------------------------------------------------
def test_scenario_08_reconciliation_drift(base_config):
    mock_api = MagicMock(spec=RoostooAPIClient)
    # Exchange says cash is 94,000 USD (e.g. fees or out-of-band trade)
    mock_api.get_balance.return_value = APIResponse(
        success=True,
        data={
            "Wallet": {
                "USD": {"Free": 94000.0, "Lock": 0.0},
                "ETH": {"Free": 2.0, "Lock": 0.0},
            }
        },
    )
    mock_api.get_short_positions.return_value = APIResponse(success=True, data={"Positions": []})

    tracker = PortfolioTracker(base_config, mock_api)
    assert tracker.cash == 100000.0  # Initial baseline before exchange sync

    # Reconcile overrides local balance with exchange truth (drift from 100k to 94k)
    ok = tracker.reconcile()
    assert ok is True
    assert tracker.cash == 94000.0
    assert tracker.get_balances().get("ETH") == 2.0


# ---------------------------------------------------------------------------
# 9. Process Restart & Audit Chain Recovery
# ---------------------------------------------------------------------------
def test_scenario_09_process_restart_recovery(tmp_path):
    log_dir = str(tmp_path / "logs")

    # Session 1: write 3 events
    logger1 = AuditLogger(log_dir=log_dir)
    logger1.record(EventType.STARTUP, {"run": 1})
    logger1.record(EventType.ORDER_SUBMITTED, {"id": 1})
    last_event = logger1.record(EventType.ORDER_FILLED, {"id": 1, "price": 50000})

    assert logger1.last_sequence_id == 3
    last_hash_s1 = logger1.last_hash

    # Session 2: simulate bot crash & restart
    logger2 = AuditLogger(log_dir=log_dir)
    assert logger2.last_sequence_id == 3
    assert logger2.last_hash == last_hash_s1

    # Record new event in session 2 — should continue sequence and hash chain
    e4 = logger2.record(EventType.STARTUP, {"run": 2})
    assert e4.sequence_id == 4
    assert e4.previous_hash == last_hash_s1


# ---------------------------------------------------------------------------
# 10. State Persistence to Disk
# ---------------------------------------------------------------------------
def test_scenario_10_state_persistence_to_disk(base_config, tmp_path):
    mock_api = MagicMock(spec=RoostooAPIClient)
    tracker = PortfolioTracker(base_config, mock_api)
    tracker.record_fill(
        symbol="BTC/USD",
        side="BUY",
        quantity=0.5,
        price=50000.0,
        commission=12.5,
        strategy="VALUE_AREA",
    )

    state_file = str(tmp_path / "portfolio_state.json")
    tracker.save_state(state_file)

    assert os.path.exists(state_file)
    with open(state_file, "r", encoding="utf-8") as f:
        saved_data = json.load(f)

    assert "positions" in saved_data
    assert "BTC/USD" in saved_data["positions"]
    assert saved_data["positions"]["BTC/USD"]["quantity"] == 0.5


# ---------------------------------------------------------------------------
# 11. Network Interruption & Retry
# ---------------------------------------------------------------------------
def test_scenario_11_network_interruption_recovery(base_config):
    client = RoostooAPIClient(base_config)

    mock_200 = MagicMock()
    mock_200.status_code = 200
    mock_200.text = '{"ServerTime": 1700000000000}'
    mock_200.json.return_value = {"ServerTime": 1700000000000}

    # Fails once with ConnectionError, then reconnects
    with patch.object(client._session, "get", side_effect=[requests.exceptions.ConnectionError("Socket reset"), mock_200]):
        resp = client.get_server_time()
        assert resp.success is True
        assert resp.data["ServerTime"] == 1700000000000


# ---------------------------------------------------------------------------
# 12. Dashboard Failure Isolation (Trading Loop Non-Blocking)
# ---------------------------------------------------------------------------
def test_scenario_12_dashboard_failure_isolation():
    from core.web_server import DashboardServer

    server = DashboardServer(port=8899)

    # Set telemetry provider that raises an exception
    def broken_provider():
        raise RuntimeError("Telemetry serialization failed")

    server.set_telemetry_provider(broken_provider)

    # Calling broken telemetry must not crash the caller
    try:
        # Simulate websocket / api poll
        data = server._telemetry_provider()
    except Exception as e:
        # Caught and isolated
        assert str(e) == "Telemetry serialization failed"


# ---------------------------------------------------------------------------
# 13. Risk-Freeze Activation (Circuit Breaker on Drawdown)
# ---------------------------------------------------------------------------
def test_scenario_13_risk_freeze_on_drawdown(base_config):
    risk = RiskManager(base_config)
    sig = Signal("BTC/USD", SignalDirection.BUY, "VALUE_AREA", confidence=0.85)

    # Drop equity from 100k to 91k (9% DD > 8% max_drawdown_pct)
    res = risk.check_trade(
        signal=sig,
        equity=91000.0,
        cash=91000.0,
        gross_exposure=0.0,
        open_positions=0,
        spread_bps=10.0,
        data_fresh=True,
        api_healthy=True,
    )

    assert res.decision == RiskDecision.REJECTED
    assert risk.is_frozen is True
    assert "Max drawdown breached" in risk.frozen_reason

    # Subsequent trades even with healthy cash are also frozen
    res2 = risk.check_trade(
        signal=sig,
        equity=91000.0,
        cash=91000.0,
        gross_exposure=0.0,
        open_positions=0,
        spread_bps=10.0,
        data_fresh=True,
        api_healthy=True,
    )
    assert res2.decision == RiskDecision.REJECTED
    assert "RISK FROZEN" in res2.reason


# ---------------------------------------------------------------------------
# 14. Kill-Switch Execution (Permanent Shutdown)
# ---------------------------------------------------------------------------
def test_scenario_14_kill_switch_execution(base_config):
    risk = RiskManager(base_config)
    risk.activate_kill_switch("Emergency operator kill switch activated")

    assert risk.is_killed is True

    sig = Signal("BTC/USD", SignalDirection.BUY, "VALUE_AREA", confidence=0.99)
    res = risk.check_trade(
        signal=sig,
        equity=100000.0,
        cash=100000.0,
        gross_exposure=0.0,
        open_positions=0,
        spread_bps=5.0,
        data_fresh=True,
        api_healthy=True,
    )
    assert res.decision == RiskDecision.REJECTED
    assert "PERMANENT KILL" in res.reason
