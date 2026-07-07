@echo off
REM DeepSeek Infra - desktop app launcher
REM Double-click this file to start.
setlocal
cd /d "%~dp0"

set DEEPSEEK_API_KEY=your_deepseek_api_key_here
set TAVILY_API_KEY=your_tavily_api_key_here

start "" pythonw "%~dp0launch.py"
