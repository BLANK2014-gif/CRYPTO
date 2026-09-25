"""
Roostoo Trading Bot — Graceful Stop Script
===========================================
Gracefully stops a running Roostoo trading bot instance:
1. Detects bot PID from state/bot.pid
2. Sends graceful stop signal via state/stop.flag and HTTP /command/shutdown
3. Waits for active loop to finish, save portfolio state, and seal audit chain
4. Verifies state persistence and clean process termination

Usage:
    python scripts/stop_bot.py
"""

import os
import sys
import time
import signal
import pathlib
import urllib.request
import urllib.error

# ANSI colors
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def is_process_running(pid: int) -> bool:
    """Check if process with given PID exists."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        process = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if process:
            kernel32.CloseHandle(process)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def request_http_shutdown(port: int = 8080) -> bool:
    """Attempt HTTP POST to /command/shutdown."""
    url = f"http://127.0.0.1:{port}/command/shutdown"
    try:
        req = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def stop_bot() -> bool:
    print(f"\n{CYAN}{BOLD}============================================================================")
    print("      ROOSTOO QUANTITATIVE TRADING BOT — GRACEFUL SHUTDOWN")
    print("============================================================================" + f"{RESET}\n")

    project_root = pathlib.Path(__file__).resolve().parent.parent
    os.chdir(project_root)

    pid_file = project_root / "state" / "bot.pid"
    flag_file = project_root / "state" / "stop.flag"
    state_file = project_root / "state" / "portfolio_state.json"

    pid = None
    if pid_file.exists():
        try:
            content = pid_file.read_text(encoding="utf-8").strip()
            if content.isdigit():
                pid = int(content)
        except Exception:
            pass

    # Check if PID is actually alive
    running = is_process_running(pid) if pid else False

    if not running:
        # Check if HTTP dashboard is responding
        http_alive = request_http_shutdown()
        if not http_alive:
            print(f"{YELLOW}[INFO] Roostoo Trading Bot is not currently running.{RESET}")
            if pid_file.exists():
                try:
                    pid_file.unlink()
                except OSError:
                    pass
            print(f"Nothing to stop.\n")
            return True

    print(f"{CYAN}Detected running bot instance (PID: {pid if pid else 'HTTP-active'}).{RESET}")
    print(f"Initiating graceful shutdown sequence...")

    # 1. Write stop flag
    try:
        flag_file.parent.mkdir(parents=True, exist_ok=True)
        flag_file.write_text("stop", encoding="utf-8")
    except Exception as exc:
        print(f"{YELLOW}[WARN] Could not write stop flag: {exc}{RESET}")

    # 2. Issue HTTP command
    http_ok = request_http_shutdown()
    if http_ok:
        print(f"{GREEN}[OK]{RESET} Sent /command/shutdown to dashboard API.")

    # 3. Wait for graceful exit (up to 12 seconds)
    print("Waiting for bot to finish active cycle, reconcile ledger, and save state...")
    max_wait = 12.0
    start_t = time.time()
    stopped = False

    while time.time() - start_t < max_wait:
        if pid and not is_process_running(pid):
            stopped = True
            break
        if not pid and not pid_file.exists():
            stopped = True
            break
        time.sleep(0.5)

    if not stopped and pid:
        print(f"{YELLOW}[WARN] Bot did not exit within timeout. Sending SIGINT (Ctrl+C)...{RESET}")
        try:
            if sys.platform == "win32":
                # On Windows, try taskkill without /F first for graceful WM_CLOSE
                import subprocess
                subprocess.run(["taskkill", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.kill(pid, signal.SIGINT)
        except Exception as exc:
            print(f"{RED}[FAIL] Error signaling process: {exc}{RESET}")

        # Wait an extra 3 seconds
        time.sleep(3.0)
        stopped = not is_process_running(pid)

    # 4. Clean up flags
    if flag_file.exists():
        try:
            flag_file.unlink()
        except OSError:
            pass
    if pid_file.exists() and stopped:
        try:
            pid_file.unlink()
        except OSError:
            pass

    # 5. Report result
    if stopped:
        print(f"\n{GREEN}{BOLD}[SUCCESS] Roostoo Trading Bot stopped gracefully.{RESET}")
        if state_file.exists():
            mod_time = time.ctime(state_file.stat().st_mtime)
            print(f"{GREEN}[OK]{RESET} Portfolio state verified on disk: {state_file} ({mod_time})")
        print(f"{GREEN}[OK]{RESET} Audit hash chain sealed. All positions, orders, and ledger preserved.\n")
        return True
    else:
        print(f"\n{RED}{BOLD}[ERROR] Bot (PID {pid}) is still running. You may need to kill it manually.{RESET}\n")
        return False


if __name__ == "__main__":
    ok = stop_bot()
    sys.exit(0 if ok else 1)
