@echo off
cd /d "%~dp0"

set "PYTHON=%~dp0runtime\python64\python.exe"
if exist "%PYTHON%" goto python_ready

for /f "delims=" %%P in ('py -3.12-64 -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON=%%P"
if defined PYTHON if exist "%PYTHON%" goto python_ready

for /f "delims=" %%P in ('where python 2^>nul') do if not defined FOUND_PYTHON set "FOUND_PYTHON=%%P"
set "PYTHON=%FOUND_PYTHON%"
if not defined PYTHON goto python_missing

:python_ready
"%PYTHON%" -c "import struct,sys; sys.exit(0 if struct.calcsize('P')*8 == 64 else 1)" >nul 2>&1
if errorlevel 1 (
    echo A 64-bit Python installation is required.
    pause
    exit /b 1
)

"%PYTHON%" -c "import PySide6" >nul 2>&1
if errorlevel 1 (
    echo PySide6 is unavailable. Run:
    echo "%PYTHON%" -m pip install -r "%~dp0requirements.txt"
    pause
    exit /b 1
)

"%PYTHON%" "%~dp0android_partition_tool_ui.py"
if errorlevel 1 pause
exit /b %errorlevel%

:python_missing
echo Python 3.12 64-bit was not found.
echo Install it, then run: py -3.12-64 -m pip install -r "%~dp0requirements.txt"
pause
exit /b 1
