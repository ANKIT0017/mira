@echo off
title MIRA Assistant
set VENV_PY=%~dp0venv\Scripts\python.exe

echo ──────────────────────────────────────────────────────
echo   MIRA  ^|  Local AI Assistant
echo   Engine: llama.cpp  ^|  Model: Gemma 4 E2B Q4_K_M
echo ──────────────────────────────────────────────────────
echo.

REM 1) Start the llama.cpp inference server (Gemma 4 E2B)
echo [1/3] Starting llama.cpp inference server...
start "MIRA llama.cpp Server" cmd /k "%~dp0start_llama_server.bat"
timeout /t 4 >nul

REM 2) Start the MCP tool bridge
echo [2/3] Starting MIRA MCP Bridge...
start "MIRA MCP Server" cmd /k ""%VENV_PY%" -u mcp_server\server.py"
timeout /t 2 >nul

REM 3) Open browser and launch Flask UI
echo [3/3] Launching MIRA UI...
start "" http://localhost:5000
"%VENV_PY%" ui\app.py
