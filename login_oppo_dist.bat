@echo off
cd /d %~dp0
set PY=python
python --version >nul 2>nul
if errorlevel 1 (
  if exist "C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe" (
    set "PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
  ) else (
    echo [!] Python not found. Please install Python 3.9+ first: https://www.python.org/downloads/
    pause
    exit /b 1
  )
)
"%PY%" -c "import playwright,requests" >nul 2>nul
if errorlevel 1 (
  echo [i] Installing dependencies: playwright requests ...
  "%PY%" -m pip install --quiet playwright requests
)
"%PY%" "%~dp0export_login.py"
pause
