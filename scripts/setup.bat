@echo off
rem One-time setup on Windows: virtual environment, dependencies, a .env template, then a health check.
rem Usage:  scripts\setup.bat            (uses the "py" launcher's newest Python 3, else "python")
rem To choose a specific Python, first run:  set PYTHON=py -3.12   then scripts\setup.bat in the same window
setlocal
cd /d "%~dp0.."
set PYTHONUTF8=1
set "VENV_PY=.venv\Scripts\python.exe"

if exist "%VENV_PY%" goto have_venv

set "PY=%PYTHON%"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY goto no_python

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
if errorlevel 1 goto no_python

for /f "delims=" %%v in ('%PY% --version 2^>^&1') do echo Creating .venv with %%v
%PY% -m venv .venv
if errorlevel 1 (
  echo Could not create the virtual environment .venv
  exit /b 1
)

:have_venv
echo Installing packages (first time: a few minutes)...
"%VENV_PY%" -m pip install -q --upgrade pip
if errorlevel 1 goto pip_failed
"%VENV_PY%" -m pip install -q -r requirements.txt
if errorlevel 1 goto pip_failed

if not exist ".env" (
  copy /y ".env.example" ".env" >nul
  echo Created .env from .env.example: add your Dhan client id and access token ^(or use the demo^).
)

"%VENV_PY%" manage.py check
echo.
echo Next: scripts\demo.bat  (no account needed)   or   scripts\start.bat  (your Dhan data)
endlocal
exit /b 0

:no_python
echo Python 3.9 or newer was not found. Install Python 3.12 from https://www.python.org/downloads/windows/
echo (tick "Add python.exe to PATH" in the installer), open a new Command Prompt, then run scripts\setup.bat again.
echo If "python" opens the Microsoft Store, that's only a shortcut: install Python from python.org instead.
exit /b 1

:pip_failed
echo Installing the packages failed (see the messages above). Check your internet connection or proxy and run scripts\setup.bat again.
exit /b 1
