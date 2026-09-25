"""
Unit tests for Risk Manager.
Tests trade authorization, position sizing, circuit breakers,
drawdown tracking, spread checks, cash reserve, and kill switch.
"""

import pytest
from config.trading_params import BotConfig, RiskConfig, ExchangeConfig
from core.risk_manager import RiskManager, RiskDecision, RiskCheckResult
from strategies.base import Signal, SignalDirection


@pytest.fixture
def risk_manager():
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
            rolling_24h_drawdown_pct=0.05,
            strategy_drawdown_pct=0.04,
            max_spread_bps=50.0,
        ),
    )
    return RiskManager(config)


def create_signal(direction=SignalDirection.BUY, symbol="BTC/USD", price=50000.0, stop_loss=49000.0):
    return Signal(
        symbol=symbol,
        direction=direction,
        strategy="VALUE_AREA",
        entry_price=price,
        stop_loss=stop_loss,
        take_profit_1=52000.0,
        confidence=0.8,
        reason="Test signal",
    )


class TestRiskManager:
    def test_approve_valid_trade(self, risk_manager):
        signal = create_signal()
        res = risk_manager.check_trade(
            signal=signal,
            equity=100000.0,
            cash=100000.0,
            gross_exposure=0.0,
            open_positions=0,
            spread_bps=10.0,
            data_fresh=True,
            api_healthy=True,
        )
        assert res.decision == RiskDecision.APPROVED
        assert res.approved_quantity > 0.0
        assert "cash_reserve" in res.checks_passed

    def test_reject_stale_data(self, risk_manager):
        signal = create_signal()
        res = risk_manager.check_trade(
            signal=signal,
            equity=100000.0,
            cash=100000.0,
            gross_exposure=0.0,
            open_positions=0,
            spread_bps=10.0,
            data_fresh=False,  # STALE
            api_healthy=True,
        )
        assert res.decision == RiskDecision.REJECTED
        assert "data_freshness" in res.checks_failed

    def test_reject_unhealthy_api(self, risk_manager):
        signal = create_signal()
        res = risk_manager.check_trade(
            signal=signal,
            equity=100000.0,
            cash=100000.0,
            gross_exposure=0.0,
            open_positions=0,
            spread_bps=10.0,
            data_fresh=True,
            api_healthy=False,  # UNHEALTHY
        )
        assert res.decision == RiskDecision.REJECTED
        assert "api_health" in res.checks_failed

    def test_reject_wide_spread(self, risk_manager):
        signal = create_signal()
        res = risk_manager.check_trade(
            signal=signal,
            equity=100000.0,
            cash=100000.0,
            gross_exposure=0.0,
            open_positions=0,
            spread_bps=65.0,  # > 50 bps limit
            data_fresh=True,
            api_healthy=True,
        )
        assert res.decision == RiskDecision.REJECTED
        assert "spread" in res.checks_failed

    def test_reject_max_positions_reached(self, risk_manager):
        signal = create_signal()
        res = risk_manager.check_trade(
            signal=signal,
            equity=100000.0,
            cash=80000.0,
            gross_exposure=20000.0,
            open_positions=3,  # Already at max 3
            spread_bps=10.0,
            data_fresh=True,
            api_healthy=True,
        )
        assert res.decision == RiskDecision.REJECTED
        assert "max_positions" in res.checks_failed

    def test_reject_cash_reserve_violation(self, risk_manager):
        signal = create_signal()
        # min reserve is 30% of 100k = 30k. If cash is only 25k, reject.
        res = risk_manager.check_trade(
            signal=signal,
            equity=100000.0,
            cash=25000.0,
            gross_exposure=50000.0,
            open_positions=1,
            spread_bps=10.0,
            data_fresh=True,
            api_healthy=True,
        )
        assert res.decision == RiskDecision.REJECTED
        assert "cash_reserve" in res.checks_failed

    def test_reject_max_drawdown_breach(self, risk_manager):
        signal = create_signal()
        # Peak equity is 100k, current equity dropped to 91k (9% DD > 8% limit)
        res = risk_manager.check_trade(
            signal=signal,
            equity=91000.0,
            cash=91000.0,
            gross_exposure=0.0,
            open_positions=0,
            spread_bps=10.0,
            data_fresh=True,
            api_healthy=True,
        )
        assert res.decision == RiskDecision.REJECTED
        assert "max_drawdown" in res.checks_failed

    def test_kill_switch_blocks_all_trades(self, risk_manager):
        risk_manager.activate_kill_switch("Emergency operator kill")
        signal = create_signal()
        res = risk_manager.check_trade(
            signal=signal,
            equity=100000.0,
            cash=100000.0,
            gross_exposure=0.0,
            open_positions=0,
            spread_bps=10.0,
            data_fresh=True,
            api_healthy=True,
        )
        assert res.decision == RiskDecision.REJECTED
        assert "PERMANENT KILL" in res.reason
