@echo off
rem Back up the database, settings and data files to backups\<timestamp>\ (safe while the app runs).
rem Extra arguments go to "manage.py backup", e.g. scripts\backup.bat --keep 10
setlocal
cd /d "%~dp0.."
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Run scripts\setup.bat first.
  exit /b 1
)
".venv\Scripts\python.exe" manage.py backup %*
endlocal
