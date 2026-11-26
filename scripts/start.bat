@echo off
rem Start the dashboard on your Dhan data. Extra arguments go to app.py, e.g.:
rem   scripts\start.bat --universe nse        scripts\start.bat --watchlist nifty50.txt --no-browser
setlocal
cd /d "%~dp0.."
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Run scripts\setup.bat first.
  exit /b 1
)
".venv\Scripts\python.exe" manage.py check --quick
if errorlevel 1 echo (check reported problems above; starting anyway)
".venv\Scripts\python.exe" app.py %*
endlocal
