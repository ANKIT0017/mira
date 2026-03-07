@echo off
echo ========================================
echo   MIRA Voice Assistant
echo ========================================
echo.
echo Starting voice mode...
echo.
echo Controls:
echo - Speak naturally after the beep
echo - Say "exit" or "quit" to stop
echo - Press Ctrl+C for emergency stop
echo.
pause

venv\Scripts\python.exe voice\voice_agent.py %*
