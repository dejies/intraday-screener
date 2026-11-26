@echo off
rem Run any project command with the project's own Python, e.g.
rem   scripts\run.bat history.py backfill --watchlist nifty50.txt
rem   scripts\run.bat history.py alerts --days 1
rem   scripts\run.bat backtest.py --demo
rem   scripts\run.bat -m pytest
setlocal
cd /d "%~dp0.."
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Run scripts\setup.bat first.
  exit /b 1
)
if "%~1"=="" (
  echo Usage: scripts\run.bat ^<script.py^> [arguments]   e.g. scripts\run.bat history.py status
  exit /b 1
)
".venv\Scripts\python.exe" %*
endlocal
