@echo off
title MIRA - llama.cpp Server (Gemma 4 E2B)
setlocal

REM ─────────────────────────────────────────────────────────────────────────
REM  MIRA local inference engine — llama.cpp server
REM  Model  : gemma-4-E2B-it-Q5_K_M.gguf  (~3.8 GB)
REM  Engine : llama.cpp  (OpenAI-compatible REST on localhost:8080)
REM
REM  Key flags:
REM   --ctx-size 16384          Safe context size for this 2B model
REM   --n-gpu-layers 999        Offload all layers to GPU (VRAM permitting)
REM   --cache-type-k q4_0      KV cache key   quantization (saves ~40% VRAM)
REM   --cache-type-v q4_0      KV cache value quantization (saves ~40% VRAM)
REM   --rope-scale 2            RoPE frequency scaling (effective ctx * 2)
REM   -t 8                      CPU threads (adjust to your core count)
REM   --temp 0.7                Default sampling temperature
REM   --repeat-penalty 1.1      Slight repetition penalty for coherence
REM   --host 127.0.0.1          Local-only; change to 0.0.0.0 for LAN
REM   --port 8080               MIRA reads LLAMA_SERVER_URL from .env
REM
REM  Download llama.cpp server binary from:
REM    https://github.com/ggml-org/llama.cpp/releases
REM  Place llama-server.exe in this folder (excluded from git by .gitignore).
REM ─────────────────────────────────────────────────────────────────────────

REM ── Locate llama-server binary ────────────────────────────────────────────
REM  Check local folder first, then PATH.

set "LLAMA_BIN=%~dp0llama-server.exe"
if not exist "%LLAMA_BIN%" (
    where llama-server >nul 2>&1
    if %errorlevel% equ 0 (
        set "LLAMA_BIN=llama-server"
    ) else (
        echo.
        echo  [ERROR] llama-server.exe not found!
        echo.
        echo  Download the latest release from:
        echo    https://github.com/ggml-org/llama.cpp/releases
        echo.
        echo  Look for: llama-b****-bin-win-cuda-cu12.x.x-x64.zip
        echo  (or the vulkan / cpu-only build if you have no NVIDIA GPU)
        echo.
        echo  Extract llama-server.exe into:
        echo    %~dp0
        echo.
        pause
        exit /b 1
    )
)

REM ── Locate model file ─────────────────────────────────────────────────────
set "MODEL=%~dp0gemma-4-E2B-it-Q5_K_M.gguf"
if not exist "%MODEL%" (
    echo.
    echo  [ERROR] Model file not found:
    echo    %MODEL%
    echo.
    echo  Download from:
    echo    https://huggingface.co/unsloth/gemma-4-E2B-it-GGUF
    echo  or
    echo    https://huggingface.co/bartowski/google_gemma-4-E2B-it-GGUF
    echo.
    echo  File: gemma-4-E2B-it-Q5_K_M.gguf  (~3.8 GB)
    echo.
    pause
    exit /b 1
)

REM ── Launch server ─────────────────────────────────────────────────────────
echo.
echo  Starting llama.cpp server...
echo   Model   : %MODEL%
echo   Binary  : %LLAMA_BIN%
echo   URL     : http://127.0.0.1:8080
echo   Context : 16384 tokens  (RoPE x2 = effective 32768)
echo   KV quant: q4_0 (keys + values)
echo   GPU     : offloading all layers (--n-gpu-layers 999)
echo.

"%LLAMA_BIN%" ^
    --model "%MODEL%" ^
    --ctx-size 16384 ^
    --n-gpu-layers 999 ^
    --cache-type-k q4_0 ^
    --cache-type-v q4_0 ^
    --rope-scale 2 ^
    -t 8 ^
    --temp 0.7 ^
    --repeat-penalty 1.1 ^
    --host 127.0.0.1 ^
    --port 8080 ^
    --log-disable

REM If llama-server exits, pause so you can read the error.
pause
