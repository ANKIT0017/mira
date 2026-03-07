@echo off
REM MIRA with Autonomous Scheduler
REM Starts MCP server, Flask UI, and background scheduler

echo ============================================================
echo   MIRA - Autonomous Scheduling Mode
echo ============================================================
echo.

REM Activate virtual environment
call venv\Scripts\activate.bat

REM Start autonomous scheduler in background
echo [1/3] Starting Autonomous Scheduler...
start "MIRA Scheduler" /MIN python scheduler\autonomous_scheduler.py --voice-mode sapi

timeout /t 3 /nobreak >nul

REM Start MCP server in background
echo [2/3] Starting MCP Server...
start "MIRA MCP Server" /MIN python mcp_server\server.py

timeout /t 3 /nobreak >nul

REM Start Flask UI (foreground)
echo [3/3] Starting Web UI...
echo.
echo ============================================================
echo   MIRA is ready! Access at http://localhost:5000
echo   Autonomous scheduler is monitoring your calendar
echo ============================================================
echo.

python ui\app.py

REM Cleanup
echo.
echo Shutting down MIRA...
taskkill /F /FI "WINDOWTITLE eq MIRA*" >nul 2>&1
