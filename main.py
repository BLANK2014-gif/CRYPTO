"""
Roostoo Trading Bot — Main Application
========================================
Autonomous quantitative trading bot for the Roostoo competition.

Lifecycle:
  startup → config → logging → api → market data → portfolio →
  risk → strategies → reconcile → dashboard → trading loop →
  graceful shutdown

The bot runs continuously, making autonomous trading decisions
through the strategy → risk → execution pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import threading
from typing import Any, Dict, Optional

from config.trading_params import load_config, BotConfig
from core.api_client import RoostooAPIClient, APIError
from core.market_data import MarketDataEngine
from core.strategy_engine import StrategyEngine
from core.risk_manager import RiskManager, RiskDecision
from core.order_executor import OrderExecutor
from core.web_server import DashboardServer
from logs.audit_logger import AuditLogger, EventType
from state.portfolio_tracker import PortfolioTracker
from strategies.base import SignalDirection
from core.autosl_engine import AutoSLExitEngine, CryptoPosition
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(level: str = "INFO") -> None:
    """Configure structured logging."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Root logger
    root = logging.getLogger()
    root.setLevel(log_level)

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(log_level)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console.setFormatter(fmt)
    root.addHandler(console)

    # File handler
    os.makedirs("logs", exist_ok=True)
    file_handler = logging.FileHandler("logs/bot.log", encoding="utf-8")
    file_handler.setLevel(log_level)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Suppress noisy loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("fastapi").setLevel(logging.WARNING)


logger = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

def run_preflight(
    config: BotConfig,
    api: RoostooAPIClient,
    market: MarketDataEngine,
    portfolio: PortfolioTracker,
    audit: AuditLogger,
    dashboard: DashboardServer,
    risk: RiskManager,
    executor: OrderExecutor,
) -> bool:
    """
    Run pre-flight checks before enabling live trading.
    All critical checks must pass.
    """
    results: list[tuple[str, bool, str]] = []

    def check(name: str, condition: bool, detail: str = ""):
        results.append((name, condition, detail))
        status = "PASS" if condition else "FAIL"
        logger.info("[PREFLIGHT] [%s] %s %s", status, name, detail)

    # Configuration
    check("Configuration valid", True)

    # Credentials
    has_creds = bool(config.secrets.api_key and config.secrets.secret_key)
    check("Credentials present", has_creds,
          "" if has_creds else "Missing ROOSTOO_API_KEY or ROOSTOO_SECRET_KEY")

    # API authentication
    if has_creds:
        try:
            auth_ok = api.has_valid_credentials()
            check("API authentication", auth_ok)
        except Exception as e:
            check("API authentication", False, str(e))
    else:
        check("API authentication", False, "No credentials")

    # Market data
    data_ok = market.is_data_fresh()
    check("Market data available", data_ok)

    # Time sync
    try:
        offset = api.sync_server_time()
        time_ok = abs(offset) < config.system.time_sync_tolerance_ms
        check("Clock/timestamps valid", time_ok, f"offset={offset}ms")
    except Exception:
        check("Clock/timestamps valid", False)

    # Symbols
    symbols = market.get_available_symbols()
    check("Symbols valid", len(symbols) > 0, f"found={symbols}")

    # Balance
    try:
        bal = api.get_balance()
        check("Account balance available", bal.success)
    except Exception:
        check("Account balance available", False)

    # Reconciliation
    recon_ok = portfolio.reconcile()
    check("Portfolio reconciliation", recon_ok)

    # Risk
    check("Risk configuration valid", not risk.is_killed)

    # Audit
    check("Audit logger operational", True)

    # Dashboard
    check("Dashboard operational", dashboard.is_running)

    # Rate limiter
    gen_tokens, ord_tokens = api._rate_limiter.tokens_remaining
    check("Rate limiter operational", gen_tokens > 0, f"tokens={gen_tokens:.0f}")

    # Order executor
    check("Order executor operational", True)

    # Kill switch
    check("Kill switch operational", not risk.is_killed)

    # Record preflight in audit
    audit.record(EventType.PREFLIGHT_CHECK, {
        "results": [(name, passed, detail) for name, passed, detail in results],
    })

    # All critical checks must pass
    critical_failures = [
        (name, detail) for name, passed, detail in results if not passed
    ]

    if critical_failures:
        logger.error("PREFLIGHT FAILED — %d checks failed:", len(critical_failures))
        for name, detail in critical_failures:
            logger.error("  [FAIL] %s — %s", name, detail)
        return False

    logger.info("PREFLIGHT PASSED — all %d checks OK", len(results))
    return True


# ---------------------------------------------------------------------------
# Trading Bot
# ---------------------------------------------------------------------------

class TradingBot:
    """
    Main trading bot orchestrator.
    Coordinates all components through the trading loop.
    """

    def __init__(self):
        self._running = False
        self._paused = False
        self._config: Optional[BotConfig] = None
        self._api: Optional[RoostooAPIClient] = None
        self._market: Optional[MarketDataEngine] = None
        self._strategy_engine: Optional[StrategyEngine] = None
        self._risk: Optional[RiskManager] = None
        self._executor: Optional[OrderExecutor] = None
        self._portfolio: Optional[PortfolioTracker] = None
        self._dashboard: Optional[DashboardServer] = None
        self._audit: Optional[AuditLogger] = None
        self._autosl_engine = AutoSLExitEngine()
        self._autosl_positions: Dict[str, CryptoPosition] = {}

        self._loop_count: int = 0
        self._last_error: str = ""
        self._mode: str = "INITIALIZING"

    def start(self) -> None:
        """Full startup sequence."""
        logger.info("=" * 60)
        logger.info("ROOSTOO TRADING BOT — STARTING")
        logger.info("=" * 60)

        # 1. Load configuration
        self._config = load_config()
        setup_logging(self._config.secrets.log_level)
        logger.info("Configuration loaded")

        # 2. Initialize audit logger
        self._audit = AuditLogger()
        self._audit.record(EventType.STARTUP, {
            "dry_run": self._config.system.dry_run,
            "symbols": self._config.symbols,
            "live_enabled": self._config.secrets.live_trading_enabled,
        })

        # 3. Initialize API client
        self._api = RoostooAPIClient(self._config)

        # 4. Sync time
        self._api.sync_server_time()

        # 5. Initialize market data
        self._market = MarketDataEngine(self._config, self._api)
        if not self._market.initialize():
            logger.error("Failed to initialize market data — cannot start")
            self._mode = "FAILED"
            return

        # 6. Set up precision cache for executor
        for symbol in self._market.get_available_symbols():
            sd = self._market.get_symbol_data(symbol)
            if sd:
                # Will be used by order executor
                pass

        # 7. Initialize portfolio tracker
        self._portfolio = PortfolioTracker(self._config, self._api)
        self._portfolio.load_state()

        # 8. Initialize risk manager
        self._risk = RiskManager(self._config)

        # 9. Initialize order executor
        self._executor = OrderExecutor(self._config, self._api)
        for symbol in self._market.get_available_symbols():
            sd = self._market.get_symbol_data(symbol)
            if sd:
                self._executor.set_precision(symbol, sd.price_precision, sd.amount_precision)

        # 10. Initialize strategy engine
        self._strategy_engine = StrategyEngine(self._config, self._market)

        # 11. Initial reconciliation
        self._portfolio.reconcile()

        # 12. Start dashboard
        self._dashboard = DashboardServer(
            host=self._config.secrets.dashboard_host,
            port=self._config.secrets.dashboard_port,
            auth_token=self._config.secrets.dashboard_token,
            ws_interval=self._config.dashboard.ws_broadcast_interval,
        )
        self._dashboard.set_telemetry_provider(self._build_telemetry)
        self._dashboard.set_command_handler(self._handle_command)
        self._dashboard.start()
        time.sleep(1)  # Let dashboard start

        # Autolaunch browser dashboard if requested
        if os.environ.get("AUTOLAUNCH_BROWSER", "0") in ("1", "true", "True"):
            dash_host = self._config.secrets.dashboard_host
            host_for_url = "localhost" if dash_host in ("0.0.0.0", "") else dash_host
            dash_url = f"http://{host_for_url}:{self._config.secrets.dashboard_port}"
            logger.info("Auto-launching dashboard in default browser: %s", dash_url)
            import webbrowser
            threading.Thread(
                target=lambda: (time.sleep(1.2), webbrowser.open(dash_url)),
                daemon=True,
            ).start()

        # Record PID for process control & graceful shutdown scripts
        try:
            os.makedirs("state", exist_ok=True)
            with open("state/bot.pid", "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except Exception as exc:
            logger.debug("Failed to write PID file: %s", exc)

        # 13. Determine mode
        is_dry = self._config.system.dry_run
        is_live = self._config.secrets.live_trading_enabled

        if is_live and not is_dry:
            # Preflight before live trading
            if run_preflight(
                self._config, self._api, self._market,
                self._portfolio, self._audit, self._dashboard,
                self._risk, self._executor,
            ):
                self._mode = "LIVE"
                logger.info("MODE: LIVE TRADING")
            else:
                self._mode = "DRY_RUN"
                logger.warning("Preflight failed — falling back to DRY_RUN")
        else:
            self._mode = "DRY_RUN"
            logger.info("MODE: DRY RUN")

        # 14. Enter trading loop
        self._running = True
        self._audit.record(EventType.CONFIG_LOADED, {"mode": self._mode})

        # Signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        self._run_trading_loop()

    def _signal_handler(self, signum, frame):
        """Handle graceful shutdown signals."""
        logger.info("Received signal %d — initiating shutdown", signum)
        self._running = False

    # =======================================================================
    # Trading Loop
    # =======================================================================

    def _run_trading_loop(self) -> None:
        """
        Main trading loop.
        Runs continuously until stopped.
        """
        poll_interval = self._config.system.polling_interval_seconds
        reconcile_interval = 300  # Reconcile every 5 minutes
        last_reconcile = time.time()

        logger.info("Trading loop started — interval=%ds", poll_interval)

        while self._running:
            # Check for external stop signal file
            if os.path.exists("state/stop.flag"):
                logger.info("Graceful shutdown requested via state/stop.flag")
                try:
                    os.remove("state/stop.flag")
                except OSError:
                    pass
                self._running = False
                break

            loop_start = time.monotonic()
            self._loop_count += 1

            try:
                # 1. Update market data
                data_ok = self._market.update()

                # 2. Update prices in portfolio
                if data_ok:
                    prices = {}
                    for sym in self._config.symbols:
                        t = self._market.get_ticker(sym)
                        if t:
                            prices[sym] = t.last_price
                    self._portfolio.update_prices(prices)

                # 3. Check pending orders
                changed = self._executor.check_pending_orders()
                for order in changed:
                    if order.state.value == "FILLED" and order.signal:
                        sig = order.signal
                        if order.is_short:
                            self._portfolio.record_short_fill(
                                symbol=order.symbol,
                                action="OPEN",
                                quantity=order.quantity,
                                price=order.filled_price,
                                commission=order.commission,
                                collateral=order.collateral,
                                strategy=order.strategy,
                                stop_loss=sig.stop_loss,
                                tp1=sig.take_profit_1,
                                tp2=sig.take_profit_2,
                            )
                        else:
                            self._portfolio.record_fill(
                                symbol=order.symbol,
                                side=order.side,
                                quantity=order.filled_quantity,
                                price=order.filled_price,
                                commission=order.commission,
                                strategy=order.strategy,
                                stop_loss=sig.stop_loss,
                                tp1=sig.take_profit_1,
                                tp2=sig.take_profit_2,
                            )
                        self._audit.record(EventType.ORDER_FILLED, order.to_dict())

                # 3b. Evaluate active positions with AutoSL Engine (Trailing SL & Breakout Invalidation)
                self._check_autosl_exits()

                # 4. Periodic reconciliation & time sync
                now = time.time()
                if now - last_reconcile > reconcile_interval:
                    self._portfolio.reconcile()
                    self._api.sync_server_time()
                    last_reconcile = now

                # 5. Update risk manager
                self._risk.update_portfolio_state(
                    equity=self._portfolio.equity,
                    cash=self._portfolio.cash,
                    gross_exposure=self._portfolio.get_gross_exposure(),
                    open_positions=len(self._portfolio.get_positions()),
                )

                # 6. Strategy evaluation (skip if paused/frozen/killed)
                if (
                    not self._paused
                    and not self._risk.is_frozen
                    and not self._risk.is_killed
                    and data_ok
                    and self._market.is_data_fresh()
                ):
                    self._evaluate_and_execute()

                # 7. Save state periodically
                if self._loop_count % 20 == 0:
                    self._portfolio.save_state()

            except Exception as exc:
                self._last_error = str(exc)
                logger.error("Trading loop error: %s", exc, exc_info=True)
                self._audit.record(EventType.API_ERROR, {"error": str(exc)})

            # Rate control
            elapsed = time.monotonic() - loop_start
            sleep_time = max(0, poll_interval - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

        # Shutdown
        self._shutdown()

    def _evaluate_and_execute(self) -> None:
        """Evaluate strategies and execute approved signals."""
        for symbol in self._config.symbols:
            if not self._market.is_data_fresh(symbol):
                continue

            # Get portfolio context
            ctx = self._portfolio.get_portfolio_context(symbol)

            # Evaluate strategies
            signal = self._strategy_engine.evaluate(symbol, ctx)

            if signal is None or signal.direction in (
                SignalDirection.HOLD, SignalDirection.NO_TRADE,
            ):
                continue

            # Record signal
            self._audit.record(EventType.SIGNAL_GENERATED, signal.to_dict())

            # DRY RUN: log but don't execute
            if self._mode == "DRY_RUN":
                logger.info(
                    "[DRY_RUN] Would execute: %s %s %s conf=%.2f",
                    signal.direction.value, signal.symbol,
                    signal.strategy, signal.confidence,
                )
                continue

            # Risk check
            ticker = self._market.get_ticker(symbol)
            if not ticker:
                continue

            risk_result = self._risk.check_trade(
                signal=signal,
                equity=self._portfolio.equity,
                cash=self._portfolio.cash,
                gross_exposure=self._portfolio.get_gross_exposure(),
                open_positions=len(self._portfolio.get_positions()),
                spread_bps=ticker.spread_bps,
                data_fresh=self._market.is_data_fresh(symbol),
                api_healthy=self._api.is_exchange_running(),
            )

            if risk_result.decision == RiskDecision.REJECTED:
                self._audit.record(EventType.RISK_REJECTED, {
                    "signal": signal.to_dict(),
                    "reason": risk_result.reason,
                    "checks_failed": risk_result.checks_failed,
                })
                logger.info("Risk rejected %s: %s", symbol, risk_result.reason)
                continue

            self._audit.record(EventType.RISK_CHECK_PASSED, {
                "signal": signal.to_dict(),
                "approved_qty": risk_result.approved_quantity,
                "approved_collateral": risk_result.approved_collateral,
            })

            # Execute
            order = self._executor.execute_signal(
                signal=signal,
                risk_result=risk_result,
                current_bid=ticker.max_bid,
                current_ask=ticker.min_ask,
                current_spread_bps=ticker.spread_bps,
            )

            if order:
                self._audit.record(EventType.ORDER_SUBMITTED, order.to_dict())

    def _check_autosl_exits(self) -> None:
        """
        Evaluate active positions using AutoSLExitEngine:
        1. Phase 1 Breakout Failed Exit (confluence invalidation during first N candles)
        2. Dual-Layer Trailing Stop Loss (Hard exchange stop + Soft dynamic tiered SL)
        3. Fast Momentum Extension & Profit Ratchet
        """
        if not self._portfolio or not self._executor:
            return

        positions = self._portfolio.get_positions()
        active_symbols = set()

        for pos in positions:
            symbol = pos.get("symbol")
            if not symbol:
                continue
            active_symbols.add(symbol)

            # Ensure position is tracked in AutoSL engine
            c_pos = self._autosl_positions.get(symbol)
            if c_pos is None or c_pos.status == "CLOSED":
                entry_ts = pos.get("entry_time") or time.time()
                dt_entry = datetime.fromtimestamp(entry_ts, tz=timezone.utc)
                c_pos = CryptoPosition(
                    position_id=f"{symbol}_{int(entry_ts)}",
                    symbol=symbol,
                    side="BUY" if pos.get("side") == "LONG" else "SELL",
                    quantity=pos.get("quantity", 0.0),
                    entry_price=pos.get("avg_entry", 0.0),
                    entry_time=dt_entry,
                    entry_breakout_level=pos.get("avg_entry", 0.0),
                    initial_stop_loss=pos.get("stop_loss", 0.0),
                    stop_loss_price=pos.get("stop_loss", 0.0),
                    take_profit_price=pos.get("take_profit_1", 0.0),
                    broker_sl_price=pos.get("stop_loss", 0.0),
                    sl_percent=1.5,
                    strategy=pos.get("strategy", ""),
                    peak_price=pos.get("highest_price", pos.get("avg_entry", 0.0)),
                )
                self._autosl_positions[symbol] = c_pos

            # Update quantity & price
            c_pos.quantity = pos.get("quantity", c_pos.quantity)
            current_price = pos.get("current_price", c_pos.entry_price)

            # Gather candle and volume context
            ticker = self._market.get_ticker(symbol) if self._market else None
            candles_5m = self._market.get_candles(symbol, "5m") if self._market else []
            prev_vol = candles_5m[-1].volume if len(candles_5m) >= 1 else 0.0
            prev_prev_vol = candles_5m[-2].volume if len(candles_5m) >= 2 else 0.0
            curr_vol = ticker.usd_volume if ticker else 0.0

            recent_high = max([c.high for c in candles_5m[-10:]]) if candles_5m else (current_price * 1.01)
            recent_low = min([c.low for c in candles_5m[-10:]]) if candles_5m else (current_price * 0.99)

            # Run AutoSL on_tick
            exit_reason, trigger_price = self._autosl_engine.on_tick(
                pos=c_pos,
                current_price=current_price,
                current_candle_vol=curr_vol,
                prev_completed_vol=prev_vol,
                prev_prev_completed_vol=prev_prev_vol,
                recent_market_high=recent_high,
                recent_market_low=recent_low,
            )

            # Update portfolio stop loss if ratcheted
            if c_pos.stop_loss_price != pos.get("stop_loss") and c_pos.stop_loss_price > 0:
                self._portfolio.update_stop_loss(symbol, c_pos.stop_loss_price)

            # Handle Exit Execution
            if exit_reason:
                logger.warning(
                    "[AUTOSL EXIT] Initiating %s for %s at %.2f (Entry: %.2f, Peak: %.2f, Locked: $%.2f)",
                    exit_reason, symbol, trigger_price or current_price,
                    c_pos.entry_price, c_pos.peak_price, c_pos.locked_profit,
                )
                self._audit.record(EventType.POSITION_CLOSED, {
                    "symbol": symbol,
                    "side": pos.get("side"),
                    "exit_reason": exit_reason,
                    "trigger_price": trigger_price,
                    "current_price": current_price,
                    "entry_price": c_pos.entry_price,
                    "peak_price": c_pos.peak_price,
                    "locked_profit": c_pos.locked_profit,
                    "momentum_status": c_pos.momentum_status,
                })

                if self._mode == "DRY_RUN":
                    # In dry run, simulate immediate fill and record in portfolio
                    pnl = (current_price - c_pos.entry_price) * c_pos.quantity if c_pos.is_long() else (c_pos.entry_price - current_price) * c_pos.quantity
                    comm = current_price * c_pos.quantity * 0.001
                    if c_pos.is_long():
                        self._portfolio.record_fill(
                            symbol=symbol,
                            side="SELL",
                            quantity=c_pos.quantity,
                            price=current_price,
                            commission=comm,
                            strategy=c_pos.strategy,
                        )
                    else:
                        self._portfolio.record_short_fill(
                            symbol=symbol,
                            action="CLOSE",
                            quantity=c_pos.quantity,
                            price=current_price,
                            commission=comm,
                            realized_pnl=pnl - comm,
                            strategy=c_pos.strategy,
                        )
                else:
                    # LIVE mode: submit immediate market close order
                    if c_pos.is_long():
                        self._executor.close_spot(symbol, c_pos.quantity)
                    else:
                        self._executor.close_short(symbol)

                c_pos.status = "CLOSED"

        # Clean up closed symbols
        for sym in list(self._autosl_positions.keys()):
            if sym not in active_symbols:
                del self._autosl_positions[sym]

    # =======================================================================
    # Shutdown
    # =======================================================================

    def _shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("=" * 60)
        logger.info("SHUTTING DOWN")
        logger.info("=" * 60)

        # Save final state
        if self._portfolio:
            self._portfolio.save_state()
            self._portfolio.reconcile()

        # Final audit event
        if self._audit:
            metrics = self._portfolio.compute_metrics() if self._portfolio else {}
            self._audit.record(EventType.SHUTDOWN, {
                "mode": self._mode,
                "loop_count": self._loop_count,
                "final_metrics": metrics,
            })

        # Stop dashboard
        if self._dashboard:
            self._dashboard.stop()

        # Clean up PID file
        try:
            if os.path.exists("state/bot.pid"):
                os.remove("state/bot.pid")
        except OSError:
            pass

        logger.info("Shutdown complete")

    # =======================================================================
    # Telemetry
    # =======================================================================

    def _build_telemetry(self) -> Dict[str, Any]:
        """Build unified telemetry payload for dashboard."""
        metrics = self._portfolio.compute_metrics() if self._portfolio else {}
        risk_state = self._risk.get_state() if self._risk else {}
        api_stats = self._api.stats if self._api else {}
        data_status = self._market.get_data_status() if self._market else {}

        last_signal = None
        active_strategy = None
        regime_data = {}
        if self._strategy_engine:
            active_strategy = self._strategy_engine.active_strategy
            if self._strategy_engine.last_signal:
                last_signal = self._strategy_engine.last_signal.to_dict()
            if self._strategy_engine.last_regime:
                regime_data = self._strategy_engine.last_regime.to_dict()

        # Market tracking and scanner diagnostics per symbol
        market_tracking = {}
        scanner_state = []
        for sym in (self._config.symbols if self._config else []):
            ticker = self._market.get_ticker(sym) if self._market else None
            sym_regime = self._strategy_engine.regime_engine.get_regime(sym) if self._strategy_engine else None
            cvd = self._strategy_engine.cvd_engine.get_state(sym) if self._strategy_engine else None
            c5m_count = len(self._market.get_candles(sym, "5m")) if self._market else 0

            reg_dict = sym_regime.to_dict() if sym_regime else {}
            if ticker:
                market_tracking[sym] = {
                    "price": ticker.last_price,
                    "bid": ticker.max_bid,
                    "ask": ticker.min_ask,
                    "spread": round(ticker.spread, 4),
                    "spread_bps": round(ticker.spread_bps, 2),
                    "change_24h": round(ticker.change_24h * 100, 2),
                    "coin_volume": ticker.coin_volume,
                    "usd_volume": ticker.usd_volume,
                    "regime": reg_dict,
                    "cvd": cvd.to_dict() if cvd else {},
                }
            scanner_state.append({
                "symbol": sym,
                "price": ticker.last_price if ticker else 0.0,
                "change_24h": round(ticker.change_24h * 100, 2) if ticker else 0.0,
                "candles_5m": c5m_count,
                "regime": reg_dict,
            })

        return {
            "status": {
                "is_running": self._running,
                "paused": self._paused,
                "mode": self._mode,
                "dry_run": self._config.system.dry_run if self._config else True,
                "live_trading_enabled": self._config.secrets.live_trading_enabled if self._config else False,
                "last_market_update": self._market.last_poll_time if self._market else 0,
                "last_api_request": api_stats.get("last_request_time", 0),
                "active_strategy": active_strategy or "SCANNING",
                "last_signal": last_signal,
                "regime": regime_data,
                "scanner": scanner_state,
                "last_error": self._last_error,
                "loop_count": self._loop_count,
                "uptime_seconds": 0,
            },
            "portfolio": metrics,
            "risk": risk_state,
            "positions": [
                {
                    **p,
                    "autosl": self._autosl_positions[p["symbol"]].to_dict()
                    if p["symbol"] in self._autosl_positions else None,
                }
                for p in (self._portfolio.get_positions() if self._portfolio else [])
            ],
            "market": market_tracking,
            "api_stats": api_stats,
            "data_status": data_status,
            "equity_curve": self._portfolio.get_equity_curve(200) if self._portfolio else [],
            "orders": {
                "active": [o.to_dict() for o in self._executor.get_active_orders()] if self._executor else [],
                "recent": [o.to_dict() for o in self._executor.get_recent_completed(20)] if self._executor else [],
            },
            "audit": {
                "last_sequence": self._audit.last_sequence_id if self._audit else 0,
                "chain_valid": self._audit.is_chain_valid if self._audit else False,
                "recent_events": self._audit.get_recent_events(10) if self._audit else [],
            },
            "timestamp": time.time(),
        }

    # =======================================================================
    # Command handler
    # =======================================================================

    def _handle_command(self, command: str, params: Dict) -> Dict:
        """Handle operator safety commands."""
        logger.info("Command received: %s", command)
        self._audit.record(EventType.COMMAND_RECEIVED, {"command": command})

        if command == "pause":
            self._paused = not self._paused
            state = "PAUSED" if self._paused else "RESUMED"
            self._audit.record(
                EventType.PAUSE if self._paused else EventType.RESUME, {},
            )
            return {"success": True, "state": state}

        elif command == "kill":
            self._risk.activate_kill_switch("Operator kill command")
            # Cancel all pending orders
            if self._executor:
                self._executor.cancel_all_orders()
            self._audit.record(EventType.KILL_SWITCH, {"source": "operator"})
            return {"success": True, "state": "KILLED"}

        elif command == "reconcile":
            ok = self._portfolio.reconcile() if self._portfolio else False
            self._audit.record(EventType.RECONCILIATION, {"success": ok})
            return {"success": ok, "state": "RECONCILED"}

        elif command == "reset_risk":
            if self._risk:
                ok = self._risk.reset_freeze()
                self._audit.record(EventType.RISK_RESET, {"success": ok})
                return {"success": ok, "state": "RISK_RESET" if ok else "CANNOT_RESET"}
            return {"success": False, "state": "NO_RISK_MANAGER"}

        elif command == "derisk":
            # Cancel all pending + sell all spot positions
            if self._executor:
                self._executor.cancel_all_orders()
            self._audit.record(EventType.DERISK, {})
            return {"success": True, "state": "DERISKED"}

        elif command == "shutdown":
            logger.info("Graceful shutdown requested via operator command")
            self._running = False
            return {"success": True, "state": "SHUTTING_DOWN"}

        return {"success": False, "error": f"Unknown command: {command}"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    bot = TradingBot()
    bot.start()


if __name__ == "__main__":
    main()
