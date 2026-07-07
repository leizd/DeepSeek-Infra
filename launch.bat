@echo off
REM DeepSeek Infra - desktop app launcher
REM Double-click this file to start.
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM Find python.exe in PATH
for %%i in (python.exe) do set PYTHON_EXE=%%~$PATH:i

if not defined PYTHON_EXE (
    echo Python is not in PATH. Please install Python.
    pause
    exit /b
)

REM Try to find pythonw.exe in the same directory as python.exe
set PYTHONW_EXE=!PYTHON_EXE:python.exe=pythonw.exe!

if exist "!PYTHONW_EXE!" (
    REM Launch without a console window using pythonw
    start "" "!PYTHONW_EXE!" "%~dp0launch.py"
) else (
    REM Fallback to python.exe with a minimized window
    start "" /min "!PYTHON_EXE!" "%~dp0launch.py"
)
