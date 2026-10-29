@echo off
REM Resolve the Python interpreter for MAS desktop entries.
REM Sets MAS_PY (console python.exe) and MAS_PYW (pythonw.exe, or MAS_PY if absent).
REM Order: MAS_PYTHON -> repo .venv/venv -> py -3 -> common conda dirs -> python on PATH.
REM Exit code 1 if nothing usable. "--print" echoes MAS_PY= / MAS_PYW= lines.
set "MAS_PY="
set "MAS_PYW="
set "_MAS_ROOT=%~dp0..\.."
if defined MAS_PYTHON call :try "%MAS_PYTHON%"
if not defined MAS_PY call :try "%_MAS_ROOT%\.venv\Scripts\python.exe"
if not defined MAS_PY call :try "%_MAS_ROOT%\venv\Scripts\python.exe"
if not defined MAS_PY call :trypy
if not defined MAS_PY call :try "D:\ProgramData\miniconda3\python.exe"
if not defined MAS_PY call :try "%USERPROFILE%\miniconda3\python.exe"
if not defined MAS_PY call :try "%USERPROFILE%\anaconda3\python.exe"
if not defined MAS_PY call :try "C:\ProgramData\miniconda3\python.exe"
if not defined MAS_PY call :try "C:\ProgramData\anaconda3\python.exe"
if not defined MAS_PY call :trypath
if not defined MAS_PY exit /b 1
if /i "%~1"=="--print" echo MAS_PY=%MAS_PY%
if /i "%~1"=="--print" echo MAS_PYW=%MAS_PYW%
exit /b 0

:try
if "%~1"=="" goto :eof
if not exist "%~1" goto :eof
echo "%~1" | find /i "\WindowsApps\" >nul && goto :eof
set "_MAS_C=%~f1"
set "_MAS_D=%~dp1"
if /i "%~n1"=="pythonw" set "_MAS_C=%~dp1python.exe"
if not exist "%_MAS_C%" goto :eof
"%_MAS_C%" -c "import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)" >nul 2>&1 || goto :eof
set "MAS_PY=%_MAS_C%"
set "MAS_PYW=%_MAS_C%"
if exist "%_MAS_D%pythonw.exe" set "MAS_PYW=%_MAS_D%pythonw.exe"
goto :eof

:trypy
where py >nul 2>&1 || goto :eof
for /f "usebackq delims=" %%I in (`py -3 -c "import sys; print(sys.executable)" 2^>nul`) do call :try "%%I"
goto :eof

:trypath
for /f "delims=" %%I in ('where python.exe 2^>nul') do if not defined MAS_PY call :try "%%I"
goto :eof
