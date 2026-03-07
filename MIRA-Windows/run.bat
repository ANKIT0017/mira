@echo off
title MIRA Assistant
set VENV_PY=%~dp0venv\Scripts\python.exe
echo Starting MIRA MCP Bridge...
start "MIRA MCP Server" cmd /k ""%VENV_PY%" -u mcp_server\server.py"
timeout /t 2 >nul
echo Launching MIRA UI in default browser...
start "" http://localhost:5000
echo Starting Flask UI (this window will run the UI)...
"%VENV_PY%" ui\app.py
