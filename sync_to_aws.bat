@echo off
setlocal enabledelayedexpansion
title Roostoo Trading Bot - Sync to AWS EC2

set "KEY=C:\Users\daksh\Downloads\crypto2.pem"
set "HOST=13.211.41.40"
set "USER=ubuntu"
set "DEST=/home/ubuntu/roostoo-bot"

echo ============================================================================
echo         ROOSTOO BOT -- ONE-CLICK DEPLOY TO AWS EC2 (Sydney)
echo ============================================================================
echo Target: %USER%@%HOST%:%DEST%
echo.

if not exist "%KEY%" (
    echo [ERROR] SSH Key not found at %KEY%
    pause
    exit /b 1
)

echo [1/3] Uploading modified code and configuration files to EC2...
scp -i "%KEY%" -o StrictHostKeyChecking=no -r main.py crypto_autosl_exit_engine.py config core strategies scripts web .env %USER%@%HOST%:%DEST%/

echo [2/3] Restarting roostoo-bot service on EC2...
ssh -i "%KEY%" -o StrictHostKeyChecking=no %USER%@%HOST% "sudo systemctl restart roostoo-bot"

echo [3/3] Fetching latest live service status and logs...
echo ----------------------------------------------------------------------------
ssh -i "%KEY%" -o StrictHostKeyChecking=no %USER%@%HOST% "journalctl -u roostoo-bot -n 15 --no-pager"
echo ----------------------------------------------------------------------------
echo.
echo [SUCCESS] Bot code deployed and restarted on AWS EC2!
echo ============================================================================
pause
