@echo off
rem Diagnostics: Python, packages, .env, token expiry, files, holiday calendar, database, running app.
rem Extra arguments go to "manage.py check", e.g. scripts\check.bat --quick
setlocal
cd /d "%~dp0.."
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Run scripts\setup.bat first.
  exit /b 1
)
".venv\Scripts\python.exe" manage.py check %*
endlocal
