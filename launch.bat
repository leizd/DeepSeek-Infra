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

REM Launch using python.exe
REM The console window will be automatically hidden by the app itself.
start "" "!PYTHON_EXE!" "%~dp0launch.py"
