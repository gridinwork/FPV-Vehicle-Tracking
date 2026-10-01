@echo off
setlocal EnableExtensions
cd /d "%~dp0"
echo ============================================================
echo  Autonomous VTOL Vehicle Tracking Studio
echo  Installer
echo ============================================================
echo.

set "PY="
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=py -3"
if "%PY%"=="" python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=python"
if "%PY%"=="" goto nopython

echo Using %PY%
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  %PY% -m venv .venv
  if errorlevel 1 goto fail
)

call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
if errorlevel 1 goto fail
python -m pip uninstall -y opencv-python-headless opencv-contrib-python opencv-contrib-python-headless >nul 2>&1
python -m pip install -r requirements.txt
if errorlevel 1 goto fail
python tools\verify_install.py
if errorlevel 1 goto fail

echo.
echo Install finished.
echo Start the studio with start.bat
echo.
pause
exit /b 0

:nopython
echo Python 3.10 or newer was not found.
echo Install Python from https://www.python.org/downloads/
echo and enable the "py" launcher.
echo.
pause
exit /b 1

:fail
echo.
echo Install failed. The messages above show the reason.
echo.
pause
exit /b 1
