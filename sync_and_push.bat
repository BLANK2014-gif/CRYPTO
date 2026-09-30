@echo off
setlocal enabledelayedexpansion
title Roostoo Trading Bot - Push to GitHub & Sync to AWS

set "KEY=C:\Users\daksh\Downloads\crypto2.pem"
set "HOST=13.211.41.40"
set "USER=ubuntu"
set "DEST=/home/ubuntu/roostoo-bot"

echo ============================================================================
echo      ROOSTOO BOT -- COMMIT TO GITHUB + AUTO-DEPLOY TO AWS EC2
echo ============================================================================
echo.

set /p MSG="Enter commit message (or press Enter for default): "
if "%MSG%"=="" set MSG="chore: update trading bot logic [automated sync]"

echo.
echo [1/3] Committing and pushing to GitHub...
git add .
git commit -m "%MSG%"
git push origin main

echo.
echo [2/3] Syncing latest files to AWS EC2...
call sync_to_aws.bat

echo.
echo [3/3] Deployment complete on GitHub and AWS!
pause
