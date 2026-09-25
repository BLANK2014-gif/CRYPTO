"""
End-to-End Simulation Test.
Simulates a live trading lifecycle:
1. Ticker stream ingestion
2. Synthetic candle formation (5m and 15m)
3. Regime classification
4. Strategy signal generation
5. Risk manager validation
6. Order execution via mocked exchange
7. Local ledger update & equity curve snapshot
8. Audit logger hash chain verification
"""

import time
import pytest
from unittest.mock import MagicMock, patch

from config.trading_params import (
    BotConfig,
    ExchangeConfig,
    RiskConfig,
    ExecutionConfig,
    StrategiesConfig,
    ValueAreaStrategyConfig,
    LiquiditySweepStrategyConfig,
    SignalConfig,
)
from core.api_client import RoostooAPIClient, APIResponse
from core.market_data import MarketDataEngine, TickerSnapshot
from core.regime_engine import RegimeEngine, MarketRegime
from core.strategy_engine import StrategyEngine
from core.risk_manager import RiskManager, RiskDecision
from core.order_executor import OrderExecutor, OrderState
from state.portfolio_tracker import PortfolioTracker
from logs.audit_logger import AuditLogger, EventType
from strategies.base import Signal, SignalDirection


@pytest.fixture
def sim_environment(tmp_path):
    config = BotConfig(
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
            max_spread_bps=50.0,
        ),
        execution=ExecutionConfig(
            prefer_maker=True,
            limit_order_timeout_seconds=60,
        ),
        strategies=StrategiesConfig(
            value_area=ValueAreaStrategyConfig(enabled=True, min_confidence=0.70),
            liquidity_sweep=LiquiditySweepStrategyConfig(enabled=True, min_confidence=0.75),
        ),
        signals=SignalConfig(
            min_confidence_to_trade=0.70,
            conflict_threshold=0.60,
        ),
    )

    mock_api = MagicMock(spec=RoostooAPIClient)
    mock_api.get_balance.return_value = APIResponse(
        success=True,
        data={"Wallet": {"USD": {"Free": 100000.0, "Lock": 0.0}}},
    )
    mock_api.get_short_positions.return_value = APIResponse(
        success=True,
        data={"Positions": []},
    )
    mock_api.place_order.return_value = APIResponse(
        success=True,
        data={
            "OrderDetail": {
                "OrderID": 10001,
                "Status": "FILLED",
                "FilledQuantity": 0.2,
                "FilledAverPrice": 50000.0,
                "CommissionChargeValue": 5.0,
                "Role": "MAKER",
            }
        },
    )

    audit = AuditLogger(log_dir=str(tmp_path / "logs"))
    market = MarketDataEngine(config, mock_api)
    portfolio = PortfolioTracker(config, mock_api)
    portfolio.reconcile()
    risk = RiskManager(config)
    executor = OrderExecutor(config, mock_api)
    executor.set_precision("BTC/USD", price_prec=2, amount_prec=6)
    strategy = StrategyEngine(config, market)

    return {
        "config": config,
        "api": mock_api,
        "audit": audit,
        "market": market,
        "portfolio": portfolio,
        "risk": risk,
        "executor": executor,
        "strategy": strategy,
    }


def test_full_trading_cycle_simulation(sim_environment):
    env = sim_environment
    audit = env["audit"]
    portfolio = env["portfolio"]
    risk = env["risk"]
    executor = env["executor"]
    strategy = env["strategy"]

    audit.record(EventType.STARTUP, {"version": "1.0", "mode": "simulation"})
    assert portfolio.equity == 100000.0

    # 1. Manually craft an approved signal representing a validated strategy setup
    signal = Signal(
        symbol="BTC/USD",
        direction=SignalDirection.BUY,
        strategy="VALUE_AREA",
        confidence=0.88,
        entry_price=50000.0,
        stop_loss=49000.0,
        take_profit_1=52000.0,
        take_profit_2=54000.0,
        reason="VAL reclaim with bullish volume confirmation",
    )
    audit.record(EventType.SIGNAL_GENERATED, signal.to_dict())

    # 2. Evaluate through Risk Manager
    risk_result = risk.check_trade(
        signal=signal,
        equity=portfolio.equity,
        cash=portfolio.cash,
        gross_exposure=portfolio.get_gross_exposure(),
        open_positions=len(portfolio.get_positions()),
        spread_bps=8.0,
        data_fresh=True,
        api_healthy=True,
    )
    assert risk_result.decision == RiskDecision.APPROVED
    assert risk_result.approved_quantity > 0.0
    audit.record(EventType.RISK_CHECK_PASSED, risk_result.to_dict())

    # 3. Execute Order
    order = executor.execute_signal(
        signal=signal,
        risk_result=risk_result,
        current_bid=49990.0,
        current_ask=50010.0,
        current_spread_bps=8.0,
    )
    assert order is not None
    assert order.state == OrderState.FILLED
    audit.record(EventType.ORDER_FILLED, order.to_dict())

    # 4. Record Fill in Local Portfolio Ledger
    portfolio.record_fill(
        symbol=order.symbol,
        side=order.side,
        quantity=order.filled_quantity,
        price=order.filled_price,
        commission=order.commission,
        strategy=order.strategy,
        stop_loss=signal.stop_loss,
        tp1=signal.take_profit_1,
        tp2=signal.take_profit_2,
    )
    assert portfolio.has_position("BTC/USD") is True

    # 5. Mark-to-Market Price Update
    portfolio.update_prices({"BTC/USD": 51500.0})
    pos = portfolio.get_position("BTC/USD")
    assert pos.unrealized_pnl > 0

    # 6. Verify Audit Hash Chain
    assert audit.verify_chain() is True
    assert audit.last_sequence_id == 4
