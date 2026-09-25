# Roostoo Autonomous Quantitative Trading Bot

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-55%20passed-brightgreen.svg)]()
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)]()

An institutional-grade, fully autonomous quantitative trading system engineered for the **Roostoo Mock Exchange Competition** ($100,000 initial portfolio).

The bot operates continuously without human intervention, synthesizing market microstructure signals from real-time ticker feeds, enforcing mathematical risk controls, executing intelligent maker orders, and providing full cryptographically verifiable auditability via a SHA-256 hash-chained ledger and live terminal dashboard.

---

## Table of Contents
1. [Competition Objective](#competition-objective)
2. [System Architecture](#system-architecture)
3. [Quantitative Strategies & Mathematics](#quantitative-strategies--mathematics)
4. [Institutional Risk Management](#institutional-risk-management)
5. [Execution & Maker-Preference Engine](#execution--maker-preference-engine)
6. [Portfolio Ledger & Metrics](#portfolio-ledger--metrics)
7. [SHA-256 Audit Hash Chain](#sha-256-audit-hash-chain)
8. [Institutional Terminal Dashboard](#institutional-terminal-dashboard)
9. [Pre-Flight Verification & Testing](#pre-flight-verification--testing)
10. [Quickstart & Deployment](#quickstart--deployment)

---

## Competition Objective

* **Initial Capital**: $100,000 virtual USD.
* **Target Optimization**: Maximize risk-adjusted return and **Composite Score**:
  $$\text{Composite Score} = 0.4 \times \text{Sortino} + 0.3 \times \text{Sharpe} + 0.3 \times \text{Calmar}$$
* **Constraint Handling**:
  - Roostoo API has no native candle or historical depth endpoint; the bot synthesizes 5m and 15m OHLCV candles directly from high-frequency ticker polling.
  - Zero tolerance for liquidation; hard circuit breakers trigger at 8% maximum portfolio drawdown or 5% rolling 24-hour drawdown.
  - Maker fee advantage (0.05% vs 0.10% taker) is actively exploited through intelligent limit order placement.

---

## System Architecture

```
                          +------------------------------------+
                          |       Roostoo REST API Engine      |
                          | (ticker, balances, orders, shorts) |
                          +-----------------+------------------+
                                            ^
                                            | (HMAC-SHA256 Auth / Rate-Limited)
                                            v
+---------------------------------------------------------------------------------------+
|                                ROOSTOO TRADING BOT                                    |
|                                                                                       |
|  +--------------------+    +--------------------+    +--------------------+           |
|  | Market Data Engine |    |   CVD & Volume     |    |   Regime Engine    |           |
|  | - Ticker polling   |--->|   Profile Engine   |--->| - ADX, ATR, Vol %  |           |
|  | - Synthetic Candles|    | - POC, VAH, VAL    |    | - Trend vs Range   |           |
|  +--------------------+    +--------------------+    +---------+----------+           |
|                                                                |                      |
|                                                                v                      |
|  +--------------------------------------------------------------------+               |
|  | Strategy Engine                                                    |               |
|  | - Strategy A: Value Area Rotation (Mean reversion in range)        |               |
|  | - Strategy B: Liquidity Sweep & MSS (Breakout/reversal in trend)   |               |
|  | - Signal Aggregator & Conflict Resolver                            |               |
|  +-----------------------------------+--------------------------------+               |
|                                      |                                                |
|                                      v                                                |
|  +--------------------------------------------------------------------+               |
|  | Risk Manager (Gatekeeper)                                          |               |
|  | - Position Sizing (ATR-based, 1% risk) - Cash Reserve (>= 30%)     |               |
|  | - Gross Exposure (<= 60%)              - Circuit Breakers (DD limit)|               |
|  +-----------------------------------+--------------------------------+               |
|                                      |                                                |
|                                      v                                                |
|  +--------------------------------------------------------------------+               |
|  | Order Executor                                                     |               |
|  | - Maker preference (post-only/spread capture)                      |               |
|  | - Limit timeout & fill tracking                                    |               |
|  | - Short position collateral allocation                             |               |
|  +-----------------------------------+--------------------------------+               |
|                                      |                                                |
|             +------------------------+------------------------+                       |
|             v                                                 v                       |
|  +--------------------+                            +--------------------+             |
|  |  Portfolio Ledger  |                            | SHA-256 Audit Chain|             |
|  | - Exchange Recon   |                            | - Immutable hashes |             |
|  | - Realized / MTM   |                            | - Tamper detection |             |
|  | - Sharpe & Sortino |                            | - Event history    |             |
|  +----------+---------+                            +----------+---------+             |
|             |                                                 |                       |
+-------------|-------------------------------------------------|-----------------------+
              |                                                 |
              +------------------------+------------------------+
                                       |
                                       v
                     +-----------------------------------+
                     |      FastAPI + WebSocket Server   |
                     |  - Institutional Dark Terminal UI |
                     |  - Real-time telemetry broadcast  |
                     +-----------------------------------+
```

---

## Quantitative Strategies & Mathematics

### 1. Strategy A — Volume Profile & Value Area Rotation (`VALUE_AREA`)
Designed for **RANGE** and **LOW_VOLATILITY** regimes (ADX < 20).
* **Value Area Construction**: Divides the price distribution into 24 bins over a rolling 48-period window. Bins covering 70% of total volume establish Value Area High (VAH) and Value Area Low (VAL), with the highest-volume bin designated as Point of Control (POC).
* **Long Entry**: Price wicks below VAL, rejects lower auction, and reclaims VAL with positive tick delta.
* **Exit Targets**:
  - Take Profit 1 (TP1): 50% scale-out at POC.
  - Take Profit 2 (TP2): Remaining 50% at VAH.
  - Stop Loss: 1.0 ATR below lowest sweep wick (or fallback 0.5%).

### 2. Strategy B — Liquidity Sweep & Market Structure Shift (`LIQUIDITY_SWEEP`)
Designed for **TREND** and **HIGH_VOLATILITY** regimes (ADX > 25).
* **Liquidity Identification**: Detects swing highs/lows and equal levels (EQL/EQH within 0.1% tolerance).
* **Market Structure Shift (MSS)**: When price breaches a liquidity pool and immediately prints a strong displacement candle (body > 60% of total range) leaving a Fair Value Gap (FVG) or Order Block (OB).
* **Execution**: Limit entry upon FVG or OB retest.
* **Risk/Reward**: Minimum 1:2 R:R; trailing stop moves to breakeven upon TP1.

### 3. Market Regime Engine
The classifier continuously tracks:
* **ADX (14)**: Trend strength index ($>25$ = Trending, $<20$ = Ranging).
* **ATR (14)**: True market volatility.
* **Volatility Percentile**: Rolling 50-period percentile rank ($>95\%$ = Extreme volatility, triggering temporary NO_TRADE hold).
* **VWAP**: Session volume-weighted average price benchmark.

---

## Institutional Risk Management & Sizing Policy

Every proposed trade moves through a deterministic risk pipeline before any order is constructed:

```
Signal (Strategy Output)
  ↓
Risk Budget Calculation (Risk Amount = Equity × 1.0%)
  ↓
Position Size Calculation (Quantity = min(Risk / StopDistance, Cash / Price, Exposure / Price))
  ↓
Hard 1% Maximum Loss-at-Stop Constraint
  ↓
60% Gross Portfolio Exposure Cap
  ↓
30% Unencumbered Cash Reserve Floor
  ↓
Drawdown Circuit Breakers (8% max DD, 5% rolling 24h DD)
  ↓
Approved Order Execution
```

### Exact Mathematical Policy
1. **Loss-at-Stop Cap (1.0% Equity Risk Target)**:
   $$\text{Risk Amount} = \text{Equity} \times 0.01$$
   $$\text{Stop Distance} = |\text{Entry Price} - \text{Stop Loss}|$$
   $$\text{Quantity}_{\text{risk}} = \frac{\text{Risk Amount}}{\text{Stop Distance}}$$
   *Theoretical loss at the designated stop price is capped at 1.0% of current equity before fees and execution slippage. Actual realized loss may differ because stop execution is software-triggered (the exchange provides LIMIT and MARKET endpoints without native exchange-side conditional orders) and fast-moving market conditions or slippage can impact execution.*
2. **Cash Reserve Floor (30% + Fee Allowance)**:
   $$\text{Available Cash} = \text{Free USD} - (\text{Equity} \times 0.30)$$
   $$\text{Quantity}_{\text{cash}} = \frac{\text{Available Cash}}{\text{Entry Price} \times (1 + \text{taker\_fee})}$$
   *Accounts for buy-side commission (e.g. 0.10%) to strictly protect the unallocated cash floor from being breached by fees.*
3. **Gross Exposure Ceiling (60%)**:
   $$\text{Remaining Headroom} = (\text{Equity} \times 0.60) - \text{Current Gross Exposure}$$
   $$\text{Quantity}_{\text{exposure}} = \frac{\text{Remaining Headroom}}{\text{Entry Price}}$$
4. **Final Executable Quantity**:
   $$\text{Approved Quantity} = \max\left(0, \min\left(\text{Quantity}_{\text{risk}}, \text{Quantity}_{\text{cash}}, \text{Quantity}_{\text{exposure}}\right)\right)$$
5. **Drawdown Circuit Breakers**:
   - Portfolio Max Drawdown $\ge 8\%$: Immediate freeze on opening positions.
   - Rolling 24-Hour Drawdown $\ge 5\%$: Immediate freeze on opening positions.
   - Strategy Drawdown $\ge 4\%$: Disables offending strategy.
   - Permanent Kill Switch: Operator emergency stop or critical insolvency threshold.
6. **Spot-Only Mode vs. Shorting Mode**:
   Configurable via `allow_shorting` in `config.yaml`. If a competition round is strictly spot-only, setting `allow_shorting: false` disables `/v6` calls and restricts `SELL` signals exclusively to de-risking existing spot holdings.

---

## Execution & Maker-Preference Engine

* **Maker Advantage**: Places limit orders priced inside the spread (10% depth from best bid/ask) to capture maker fees (0.05% vs 0.10% taker fee).
* **Urgency Overrides**: Emergency liquidation and stop-loss fills switch automatically to MARKET orders.
* **Lifecycle Management**: Limit orders that remain unfilled after 120 seconds are automatically evaluated for cancel-replace or market fill.
* **Short Position Collateral Handling**: When enabled (`allow_shorting: true`), routes collateral sizing to `/v6/short_open`. In spot-only rounds, SELL signals close spot inventory.

---

## Portfolio Ledger & Metrics

The bot maintains an internal shadow ledger that synchronizes with Roostoo after every fill:
* **Realized PnL** and **Unrealized PnL** (mark-to-market).
* **Sharpe Ratio**: Annualized return vs. return standard deviation.
* **Sortino Ratio**: Annualized return vs. downside deviation only.
* **Calmar Ratio**: Annualized return vs. maximum drawdown.
* **Composite Score**: Competition benchmark equation.

---

## SHA-256 Audit Hash Chain

Every trading decision, order fill, risk check, and lifecycle state change is cryptographically sealed into a sequential JSON-Lines audit trail (`logs/audit_*.jsonl`):
```
Block N:   SHA256(Sequence + Timestamp + EventType + Details + PreviousHash) -> CurrentHash
Block N+1: PreviousHash = Block N's CurrentHash
```
Any tampering, deletion, or modification of historical records breaks the cryptographic chain and is instantly detected by `verify_chain()`.

---

## Institutional Terminal Dashboard

Runs on port `8080` (FastAPI + WebSockets):
* **Live Portfolio HUD**: Real-time equity curve, free cash, locked collateral, exposure gauge.
* **Competition Metrics Panel**: Live Sharpe, Sortino, Calmar, Win Rate, Profit Factor, Composite Score.
* **Active Position & Order Matrix**: Entry prices, unrealized PnL, stops, take-profit levels.
* **Cryptographic Audit Stream**: Live display of sequence IDs, event types, and SHA-256 hash linkages.
* **Operator Safety Panel**: Manual pause, resume, de-risk, and emergency kill switch controls.

---

## Pre-Flight Verification & Testing

### Run Automated Unit & Simulation Tests
The project features a full test suite covering API client, technical indicators, regime engine, risk manager, portfolio tracker, audit logger, strategies, order execution, and end-to-end simulation:

```bash
python -m pytest tests/ -v
```
*Expected: 55 passed.*

### Run Competition Pre-Flight Check
Verifies exchange network reachability, millisecond clock skew, directory permissions, and configuration:

```bash
python scripts/preflight_check.py
```

---

## Quickstart & Deployment

### 1. Local Setup
```bash
# Clone and navigate
git clone <repo-url> roostoo-bot
cd roostoo-bot

# Install dependencies
pip install -r requirements.txt

# Configure credentials
cp .env.example .env
nano .env

# Run pre-flight check
python scripts/preflight_check.py

# Launch trading bot
python main.py
```

### 2. Docker Deployment
```bash
# Start container in detached mode
docker compose up --build -d

# View live trading logs
docker compose logs -f

# Access dashboard
http://localhost:8080
```

For complete AWS cloud setup, reverse proxy configuration, and systemd service instructions, see [DEPLOYMENT.md](file:///d:/crypto/roostoo-bot/DEPLOYMENT.md).

---

## License
MIT License. Open-source quantitative trading architecture built for the Roostoo competition.
