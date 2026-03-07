@echo off
echo ===================================
echo  MIRA with ngrok + WhatsApp Bridge
echo ===================================
echo.
echo Starting all MIRA components...
echo.

REM Check ngrok
where ngrok >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: ngrok not found in PATH!
    echo Download from: https://ngrok.com/download
    pause
    exit /b 1
)

REM Check Node.js
where node >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo WARNING: Node.js not found - WhatsApp bridge will not start!
    echo Download from: https://nodejs.org
    echo Press any key to continue without WhatsApp...
    pause >nul
    set WA_SKIP=1
) else (
    set WA_SKIP=0
)

REM ── [1/6] Firecrawl ──────────────────────────────────────────────────
echo [1/6] Starting Firecrawl Docker container...
cd /d "%~dp0..\\firecrawl"
docker-compose up -d
if %ERRORLEVEL% NEQ 0 (
    echo WARNING: Firecrawl Docker failed to start!
    echo Make sure Docker Desktop is running.
    echo Press any key to continue anyway...
    pause >nul
)
cd /d "%~dp0"
echo Firecrawl started.
timeout /t 5 /nobreak >nul

REM ── [2/6] WhatsApp Bridge ─────────────────────────────────────────────
if "%WA_SKIP%"=="1" (
    echo [2/6] Skipping WhatsApp bridge (Node.js not found)
) else (
    echo [2/6] Starting WhatsApp Bridge (Baileys^)...

    REM Install npm deps if node_modules missing
    if not exist "%~dp0whatsapp_bridge\node_modules" (
        echo       First run - installing npm packages...
        cd /d "%~dp0whatsapp_bridge"
        call npm install
        cd /d "%~dp0"
    )

    start "MIRA WhatsApp Bridge" /MIN cmd /k "cd /d "%~dp0whatsapp_bridge" && node bridge.js"
    timeout /t 4 /nobreak >nul
    echo WhatsApp bridge started on port 9001.
)

REM ── Venv Python path (relative to this bat) ─────────────────────────
set VENV_PY=%~dp0venv\Scripts\python.exe

REM ── [3/6] Autonomous Scheduler ───────────────────────────────────────
echo [3/6] Starting Autonomous Scheduler...
start "MIRA Scheduler" /MIN cmd /k ""%VENV_PY%" .\scheduler\autonomous_scheduler.py --voice-mode gemini"
timeout /t 3 /nobreak >nul

REM ── [4/6] MCP Server ─────────────────────────────────────────────────
echo [4/6] Starting MCP Server (WebSocket)...
start "MIRA MCP Server" cmd /k ""%VENV_PY%" .\mcp_server\server.py"
timeout /t 3 /nobreak >nul

REM ── [5/6] Flask UI ───────────────────────────────────────────────────
echo [5/6] Starting Flask Web UI...
start "MIRA Flask UI" cmd /k ""%VENV_PY%" .\ui\app.py"
timeout /t 5 /nobreak >nul

REM ── [6/6] ngrok ──────────────────────────────────────────────────────
echo [6/6] Starting ngrok tunnel on port 5000...
echo.
echo =====================================================
echo  MIRA is live!
echo  - Copy the HTTPS URL from ngrok output below
echo  - Open it on any device to use MIRA
echo  - Click [WhatsApp] in the toolbar to link your phone
echo =====================================================
echo.
echo  Windows:    Scheduler + Bridge + MCP + Flask
echo  Tunnel:     ngrok -> localhost:5000
echo  WA Bridge:  localhost:9001
echo.
echo  Press Ctrl+C here to stop ngrok.
echo  Close the other windows to stop individual services.
echo.

ngrok http 5000
