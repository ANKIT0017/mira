# MIRA — Recommended Local Models

MIRA uses **llama.cpp** as its local inference engine (replaces Ollama).
The llama.cpp server runs on `http://localhost:8080` and exposes an
OpenAI-compatible API — no changes to agent logic needed per model swap.

---

## Current Default Model

### ✅ Gemma 4 E2B Instruct — `gemma-4-E2B-it-Q4_K_M.gguf`
- **Size**: ~3.1 GB (Q4_K_M quantization)
- **Context**: 16 384 tokens native, effective 32 768 with `--rope-scale 2`
- **Why**: Excellent instruction-following for a 2B model; fits in 4–6 GB VRAM
- **KV cache**: `--cache-type-k q4_0 --cache-type-v q4_0` saves ~40% VRAM
- **Download**:
  ```
  https://huggingface.co/unsloth/gemma-4-E2B-it-GGUF
  https://huggingface.co/bartowski/google_gemma-4-E2B-it-GGUF
  ```
- **File**: `gemma-4-E2B-it-Q4_K_M.gguf` → place in `MIRA-Windows/`
- **Start**: `start_llama_server.bat` (already configured)

---

## Quantization Guide

| Quant | Size | Quality | Recommended? |
|-------|------|---------|--------------|
| Q4_K_M | ~3.1 GB | ⭐⭐⭐⭐ Best balance | ✅ **Use this** |
| Q4_0 | ~2.9 GB | ⭐⭐⭐½ Slightly lower | ✅ OK if tight on VRAM |
| IQ4_XS | ~2.7 GB | ⭐⭐⭐½ Imatrix quant | ✅ Good alternative |
| Q3_K_M | ~2.3 GB | ⭐⭐⭐ Noticeable drop | ⚠ Avoid if possible |
| Q2_K | ~1.8 GB | ⭐⭐ Poor reasoning | ❌ Not recommended |

---

## Alternative Models (drop-in compatible)

All models below work with the same `start_llama_server.bat` — just change
`--model` path and optionally adjust `--ctx-size`.

### Llama 3.2 3B Instruct — `Llama-3.2-3B-Instruct-Q4_K_M.gguf`
- **Size**: ~2.0 GB  |  **Context**: 8 192
- **Download**: `bartowski/Llama-3.2-3B-Instruct-GGUF` on HuggingFace
- **Good for**: Very fast responses, limited memory systems

### Phi-3.5 Mini Instruct — `Phi-3.5-mini-instruct-Q4_K_M.gguf`
- **Size**: ~2.2 GB  |  **Context**: 4 096
- **Download**: `bartowski/Phi-3.5-mini-instruct-GGUF` on HuggingFace
- **Good for**: Microsoft ecosystem, structured output tasks

### Mistral 7B Instruct v0.3 — `Mistral-7B-Instruct-v0.3-Q4_K_M.gguf`
- **Size**: ~4.4 GB  |  **Context**: 32 768
- **Download**: `bartowski/Mistral-7B-Instruct-v0.3-GGUF` on HuggingFace
- **Good for**: Stronger reasoning, 8+ GB VRAM systems

---

## Setup Instructions

### 1. Download llama.cpp server binary
```
https://github.com/ggml-org/llama.cpp/releases
```
- **NVIDIA GPU**: `llama-b****-bin-win-cuda-cu12.x.x-x64.zip`
- **AMD GPU / iGPU**: `llama-b****-bin-win-vulkan-x64.zip`
- **CPU only**: `llama-b****-bin-win-noavx-x64.zip`

Extract `llama-server.exe` into `MIRA-Windows/` (excluded from git).

### 2. Place model file
```
MIRA-Windows/gemma-4-E2B-it-Q4_K_M.gguf   ← already here ✅
```

### 3. Add to `.env`
```env
LLAMA_SERVER_URL=http://127.0.0.1:8080
LLAMA_MODEL_NAME=gemma-4-E2B-it
MIRA_DEFAULT_CONTEXT_WINDOW=16384
```

### 4. Start MIRA
```bat
run.bat          ← auto-starts llama server + MCP bridge + UI
```
Or launch the server manually first:
```bat
start_llama_server.bat     ← then run.bat separately
```

---

## Server Flags Reference

| Flag | Value | Effect |
|------|-------|--------|
| `--ctx-size` | `16384` | Context window (tokens) |
| `--n-gpu-layers` | `999` | Offload all layers to GPU |
| `--cache-type-k` | `q4_0` | KV key cache quantization |
| `--cache-type-v` | `q4_0` | KV value cache quantization |
| `--rope-scale` | `2` | Effective context × 2 (32 768) |
| `-t` | `8` | CPU threads (adjust to your CPU) |
| `--temp` | `0.7` | Sampling temperature |
| `--repeat-penalty` | `1.1` | Anti-repetition penalty |

---

## Cloud Fallback

If the llama.cpp server is unreachable, MIRA automatically falls back to
cloud providers in this priority order:
1. **Groq** — llama-3.3-70b-versatile (fastest)
2. **Gemini** — gemini-2.5-flash-lite
3. **Cerebras** — llama-3.3-70b
4. **SambaNova** — Meta-Llama-3.3-70B-Instruct
5. **OpenRouter** — meta-llama/llama-3.3-70b-instruct:free

Set keys in `.env` to enable fallback providers.
