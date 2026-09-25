"""
Roostoo Trading Bot — Pre-Flight Verification Script
====================================================
Runs automated health, environment, network, clock sync,
API authentication, and filesystem checks prior to competition deployment.

Usage:
    python scripts/preflight_check.py
"""

import os
import sys
import time
import socket
import pathlib
from typing import List, Tuple

# Add root directory to sys.path
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from config.trading_params import load_config, BotConfig
from core.api_client import RoostooAPIClient, APIError


# ANSI color codes for terminal display
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def print_banner():
    print(f"\n{CYAN}{BOLD}" + "=" * 70)
    print("       ROOSTOO QUANTITATIVE TRADING BOT — PRE-FLIGHT AUDIT")
    print("=" * 70 + f"{RESET}\n")


def run_checks() -> bool:
    checks: List[Tuple[str, str, str]] = []  # (Category, Status, Detail)
    overall_ok = True

    # 1. Python runtime
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 10):
        checks.append(("Runtime", "PASS", f"Python {py_ver} (>= 3.10 required)"))
    else:
        checks.append(("Runtime", "FAIL", f"Python {py_ver} is too old (>= 3.10 required)"))
        overall_ok = False

    # 2. Configuration loading
    try:
        config: BotConfig = load_config()
        checks.append(("Configuration", "PASS", "config.yaml parsed and validated"))
    except Exception as exc:
        checks.append(("Configuration", "FAIL", f"Config error: {exc}"))
        return False

    # 3. Credentials presence
    api_key = config.secrets.api_key
    secret_key = config.secrets.secret_key
    if api_key and secret_key and api_key != "your_api_key_here":
        checks.append(("Credentials", "PASS", f"API Key present ({api_key[:8]}...)"))
    else:
        checks.append(("Credentials", "WARN", "API Key / Secret missing or placeholder (required for live trading)"))

    # 4. Storage directories
    for dir_name in ["logs", "state"]:
        p = pathlib.Path(dir_name)
        try:
            p.mkdir(parents=True, exist_ok=True)
            test_file = p / ".write_test"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink()
            checks.append(("Filesystem", "PASS", f"Directory '{dir_name}/' is writable"))
        except Exception as exc:
            checks.append(("Filesystem", "FAIL", f"Directory '{dir_name}/' write failed: {exc}"))
            overall_ok = False

    # 5. Dashboard Port check
    dashboard_port = 8080
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1.0)
    is_free = False
    try:
        sock.bind(("0.0.0.0", dashboard_port))
        is_free = True
    except OSError:
        is_free = False
    finally:
        sock.close()

    if is_free:
        checks.append(("Networking", "PASS", f"Dashboard port {dashboard_port} available"))
    else:
        checks.append(("Networking", "WARN", f"Dashboard port {dashboard_port} is currently bound or in use"))

    # 6. Exchange API connectivity & Clock Skew
    api_client = RoostooAPIClient(config)
    try:
        t0 = time.time()
        resp = api_client.get_server_time()
        latency = (time.time() - t0) * 1000

        if resp.success and "ServerTime" in resp.data:
            server_time = resp.data["ServerTime"]
            local_time = int(time.time() * 1000)
            offset_ms = abs(server_time - local_time)

            checks.append(("Connectivity", "PASS", f"Roostoo mock-api reachable ({latency:.1f}ms)"))

            if offset_ms < 5000:
                checks.append(("Clock Sync", "PASS", f"Server offset: {offset_ms}ms (< 5000ms)"))
            elif offset_ms < 30000:
                checks.append(("Clock Sync", "WARN", f"Server offset: {offset_ms}ms (< 30000ms max tolerance)"))
            else:
                checks.append(("Clock Sync", "FAIL", f"Clock skew too large: {offset_ms}ms (> 30s limit)"))
                overall_ok = False
        else:
            checks.append(("Connectivity", "WARN", f"Connected but invalid server time: {resp.error_message}"))
    except Exception as exc:
        checks.append(("Connectivity", "WARN", f"Exchange unreachable (offline or mock server): {exc}"))

    # 7. Authenticated call (if credentials provided)
    if api_key and secret_key and api_key != "your_api_key_here":
        try:
            bal_resp = api_client.get_balance()
            if bal_resp.success:
                wallet = bal_resp.data.get("Wallet", {})
                usd_free = wallet.get("USD", {}).get("Free", 0.0)
                checks.append(("Authentication", "PASS", f"HMAC Signature accepted. Free USD: ${usd_free:,.2f}"))
            else:
                checks.append(("Authentication", "FAIL", f"API rejected auth: {bal_resp.error_message}"))
                overall_ok = False
        except Exception as exc:
            checks.append(("Authentication", "FAIL", f"Auth exception: {exc}"))
            overall_ok = False

    # Print summary table
    print(f"{'CATEGORY':<16} | {'STATUS':<6} | {'DETAIL'}")
    print("-" * 70)
    for category, status, detail in checks:
        if status == "PASS":
            color = GREEN
        elif status == "WARN":
            color = YELLOW
        else:
            color = RED
        print(f"{category:<16} | {color}{status:<6}{RESET} | {detail}")

    print("-" * 70)
    if overall_ok:
        print(f"\n{GREEN}{BOLD}RESULT: ALL CRITICAL CHECKS PASSED. SYSTEM IS READY FOR OPERATION.{RESET}\n")
    else:
        print(f"\n{RED}{BOLD}RESULT: ONE OR MORE CRITICAL CHECKS FAILED. RESOLVE BEFORE DEPLOYING.{RESET}\n")

    return overall_ok


if __name__ == "__main__":
    success = run_checks()
    sys.exit(0 if success else 1)
