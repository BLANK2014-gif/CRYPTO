@echo off
setlocal
title Stop Roostoo Quantitative Trading Bot

:: Ensure working directory is the repository directory
cd /d "%~dp0"

:: Activate virtual environment if present
if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
) else if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
)

:: Run graceful stop script
python scripts\stop_bot.py

echo.
echo Press any key or close this window to exit...
pause >nul
