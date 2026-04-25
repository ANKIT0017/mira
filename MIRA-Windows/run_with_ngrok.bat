@echo off
echo ==========================================================
echo   MIRA  ^|  Full Stack  ^|  llama.cpp + ngrok + WhatsApp
echo ==========================================================
echo.
echo Starting all MIRA components...
echo.

REM ── Dependency checks ─────────────────────────────────────────────────────

REM Check ngrok
where ngrok >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: ngrok not found in PATH!
    echo Download from: https://ngrok.com/download
    pause
    exit /b 1
)

REM Check llama-server binary
set "LLAMA_BIN=%~dp0llama-server.exe"
if not exist "%LLAMA_BIN%" (
    where llama-server >nul 2>nul
    if %ERRORLEVEL% EQU 0 (
        set "LLAMA_BIN=llama-server"
    ) else (
        echo.
        echo  [WARNING] llama-server.exe not found!
        echo  The agent will fall back to cloud providers (Groq/Gemini/etc.)
        echo  To enable local inference, download from:
        echo    https://github.com/ggml-org/llama.cpp/releases
        echo  Place llama-server.exe in: %~dp0
        echo.
        echo  Press any key to continue without local model...
        pause >nul
        set LLAMA_SKIP=1
    )
)

REM Check model file
set "MODEL=%~dp0gemma-4-E2B-it-Q5_K_M.gguf"
if not exist "%MODEL%" (
    echo.
    echo  [WARNING] Model file not found: %MODEL%
    echo  Download Q5_K_M from:
    echo    https://huggingface.co/unsloth/gemma-4-E2B-it-GGUF
    echo  Continuing with cloud fallback...
    echo.
    set LLAMA_SKIP=1
)

REM Check Node.js for WhatsApp bridge
where node >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo  [WARNING] Node.js not found - WhatsApp bridge will not start.
    echo  Download from: https://nodejs.org
    echo  Press any key to continue without WhatsApp...
    pause >nul
    set WA_SKIP=1
) else (
    set WA_SKIP=0
)

REM ── Venv Python path (relative to this bat) ───────────────────────────────
set VENV_PY=%~dp0venv\Scripts\python.exe

echo.

REM ── [1/7] llama.cpp Inference Server ─────────────────────────────────────
if "%LLAMA_SKIP%"=="1" (
    echo [1/7] Skipping llama.cpp server (binary or model not found^)
    echo        Cloud providers will be used as fallback.
) else (
    echo [1/7] Starting llama.cpp inference server...
    echo        Model   : %MODEL%
    echo        URL     : http://127.0.0.1:8080
    echo        Context : 16384 tokens  (RoPE x2 = effective 32768^)
    echo        KV quant: q4_0  GPU: all layers offloaded
    start "MIRA llama.cpp Server" cmd /k "^"%LLAMA_BIN%^" --model ^"%MODEL%^" --ctx-size 16384 --n-gpu-layers 999 --cache-type-k q4_0 --cache-type-v q4_0 --rope-scale 2 -t 8 --temp 0.7 --repeat-penalty 1.1 --host 127.0.0.1 --port 8080 --log-disable"
    timeout /t 5 /nobreak >nul
    echo        llama.cpp server started.
)
echo.

REM ── [2/7] Firecrawl ───────────────────────────────────────────────────────
echo [2/7] Starting Firecrawl Docker container...
cd /d "%~dp0..\firecrawl"
docker-compose up -d
if %ERRORLEVEL% NEQ 0 (
    echo  [WARNING] Firecrawl Docker failed to start!
    echo  Make sure Docker Desktop is running.
    echo  Press any key to continue anyway...
    pause >nul
)
cd /d "%~dp0"
echo        Firecrawl started.
timeout /t 4 /nobreak >nul
echo.

REM ── [3/7] WhatsApp Bridge ─────────────────────────────────────────────────
if "%WA_SKIP%"=="1" (
    echo [3/7] Skipping WhatsApp bridge (Node.js not found^)
) else (
    echo [3/7] Starting WhatsApp Bridge (Baileys^)...
    if not exist "%~dp0whatsapp_bridge\node_modules" (
        echo        First run - installing npm packages...
        cd /d "%~dp0whatsapp_bridge"
        call npm install
        cd /d "%~dp0"
    )
    start "MIRA WhatsApp Bridge" /MIN cmd /k "cd /d "%~dp0whatsapp_bridge" && node bridge.js"
    timeout /t 4 /nobreak >nul
    echo        WhatsApp bridge started on port 9001.
)
echo.

REM ── [4/7] Autonomous Scheduler ────────────────────────────────────────────
echo [4/7] Starting Autonomous Scheduler...
start "MIRA Scheduler" /MIN cmd /k ""%VENV_PY%" .\scheduler\autonomous_scheduler.py --voice-mode gemini"
timeout /t 3 /nobreak >nul
echo.

REM ── [5/7] MCP Server ──────────────────────────────────────────────────────
echo [5/7] Starting MCP Server (WebSocket tool bridge^)...
start "MIRA MCP Server" cmd /k ""%VENV_PY%" .\mcp_server\server.py"
timeout /t 3 /nobreak >nul
echo.

REM ── [6/7] Flask UI ────────────────────────────────────────────────────────
echo [6/7] Starting Flask Web UI...
start "MIRA Flask UI" cmd /k ""%VENV_PY%" .\ui\app.py"
timeout /t 5 /nobreak >nul
echo.

REM ── [7/7] ngrok ───────────────────────────────────────────────────────────
echo [7/7] Starting ngrok tunnel on port 5000...
echo.
echo ==========================================================
echo   MIRA is LIVE!
echo.
echo   Local inference : http://127.0.0.1:8080  (llama.cpp)
echo   Local UI        : http://localhost:5000
echo   Public tunnel   : copy HTTPS URL from ngrok output below
echo.
echo   WhatsApp link   : open UI → click [WhatsApp] in toolbar
echo   WA Bridge port  : localhost:9001
echo.
echo   Press Ctrl+C here to stop ngrok.
echo   Close individual windows to stop other services.
echo ==========================================================
echo.

ngrok http 5000
