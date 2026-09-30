@echo off
title Roostoo Auto-Sync Watcher
cd /d "%~dp0"
powershell.exe -ExecutionPolicy Bypass -File "%~dp0watch_and_sync.ps1"
pause
