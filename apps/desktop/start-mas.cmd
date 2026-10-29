@echo off
setlocal
REM Double-click / cmd fallback: run launcher.py with local Python (no PyInstaller).
cd /d "%~dp0..\.."
set "MAS_REPO_ROOT=%CD%"
set "LAUNCHER=%~dp0launcher.py"
if defined MAS_PYTHON (
  set "PY=%MAS_PYTHON%"
) else (
  where pythonw >nul 2>&1 && set "PY=pythonw" || set "PY=python"
)
"%PY%" "%LAUNCHER%"
if errorlevel 1 (
  echo.
  echo Launch failed. Need local Python 3 and this full repo checkout.
  echo Interpreter: %PY%
  pause
)
