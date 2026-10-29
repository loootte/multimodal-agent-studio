@echo off
setlocal
REM Double-click / cmd entry: run launcher.py with local Python (no PyInstaller).
cd /d "%~dp0..\.."
set "MAS_REPO_ROOT=%CD%"
call "%~dp0find-python.cmd"
if errorlevel 1 goto :nopy
"%MAS_PY%" "%~dp0launcher.py"
if errorlevel 1 goto :failed
exit /b 0

:failed
chcp 65001 >nul
echo.
echo 启动失败（解释器：%MAS_PY%）。请看上面的报错信息。
echo 如需换一个 Python，先设置环境变量 MAS_PYTHON 指向 python.exe 再重试。
pause
exit /b 1

:nopy
chcp 65001 >nul
echo.
echo 没找到可用的 Python 3。已依次查找：
echo   MAS_PYTHON、仓库 .venv/venv、py -3、
echo   D:\ProgramData\miniconda3、%%USERPROFILE%%\miniconda3、%%USERPROFILE%%\anaconda3、
echo   C:\ProgramData\miniconda3、PATH 上的 python
echo.
echo 解决办法：设置环境变量 MAS_PYTHON 指向你的 python.exe，例如在 PowerShell 里：
echo   [Environment]::SetEnvironmentVariable("MAS_PYTHON", "D:\ProgramData\miniconda3\python.exe", "User")
echo 然后重新双击启动。
pause
exit /b 1
