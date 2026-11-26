@echo off
rem Simulated market, no account needed. Extra arguments go to app.py, e.g. scripts\demo.bat --demo-start 09:25
setlocal
cd /d "%~dp0.."
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Run scripts\setup.bat first.
  exit /b 1
)
".venv\Scripts\python.exe" app.py --demo %*
endlocal
