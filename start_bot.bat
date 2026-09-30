@echo off
setlocal enabledelayedexpansion
title Roostoo Quantitative Trading Bot - Autonomous Live Trading

:: Ensure working directory is the repository directory
cd /d "%~dp0"

echo ============================================================================
echo       ROOSTOO QUANTITATIVE TRADING BOT -- AUTONOMOUS LAUNCHER
echo ============================================================================
echo.

:: Detect Python executable
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python not found in system PATH.
    echo Please install Python 3.10+ from python.org and add it to PATH.
    echo.
    pause
    exit /b 1
)

:: Activate virtual environment if present
if exist .venv\Scripts\activate.bat (
    echo [OK] Activating virtual environment .venv...
    call .venv\Scripts\activate.bat
) else if exist venv\Scripts\activate.bat (
    echo [OK] Activating virtual environment venv...
    call venv\Scripts\activate.bat
)

:: Enable browser auto-launching
set AUTOLAUNCH_BROWSER=1

echo [OK] Environment initialized.
echo.
echo Starting Quantitative Trading Bot...
echo Auto-launching live telemetry dashboard at http://localhost:8080...
echo.
echo ----------------------------------------------------------------------------
echo  OPERATOR CONTROLS:
echo    * To STOP gracefully : Press CTRL+C in this console window, OR
echo                           Double-click 'stop_bot.bat', OR
echo                           Click 'STOP BOT' on the web dashboard.
echo    * State, ledger, and cryptographic audit chains are sealed on exit.
echo ----------------------------------------------------------------------------
echo.

:: Run bot directly in foreground console
python main.py

echo.
echo ============================================================================
echo Roostoo Trading Bot has shut down.
echo ============================================================================
pause
