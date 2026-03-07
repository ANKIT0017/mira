# MIRA Windows

MIRA Windows is currently the primary working build in this repository.

This guide gives a complete Windows walkthrough: prerequisites, install, models (Ollama/Qwen), APIs, voice/TTS, optional Firecrawl Docker setup, and startup with `run_with_ngrok.bat`.

## What MIRA Includes

- Web UI (Flask): `ui/app.py`
- MCP tool server (WebSocket): `mcp_server/server.py`
- Agent orchestration: `agent/`
- Scheduler: `scheduler/autonomous_scheduler.py`
- Voice stack:
  - STT: Whisper (`openai-whisper`)
  - TTS: SAPI (`pywin32`) and Gemini Live (`pyaudio` + `websockets`)
- WhatsApp bridge (Node.js/Baileys): `whatsapp_bridge/`
- Optional Firecrawl integration for scraping/search extraction

## Architecture (High Level)

1. UI sends task requests.
2. Agent plans and decides tool calls.
3. MCP server executes tools.
4. Optional services (WhatsApp, Firecrawl, voice, scheduler) extend capability.

## Prerequisites (Windows)

Required:
- Python 3.11+ (3.12 works in this repo)
- Git

Recommended/Optional by feature:
- Ollama (local LLMs such as `qwen3:4b-instruct`, `qwen3.5:4b`, `mistral:7b`)
- Node.js 18+ (WhatsApp bridge)
- ngrok (public tunnel)
- Docker Desktop (only for Firecrawl and scraping-related tools)
- FFmpeg (recommended for Whisper runtime reliability)

## 1) Clone and Enter Project

```powershell
git clone <your-repo-url>
cd AION-Deliverables\MIRA-Windows
```

## 2) Create Virtual Environment and Install Python Dependencies

```powershell
python -m venv venv
.\venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

If `pyaudio` install fails, install Build Tools / wheel support and retry:

```powershell
pip install pipwin
pipwin install pyaudio
```

## 3) Configure Environment Variables

Create `.env` from `.env.example` and fill only what you need:

```powershell
Copy-Item .env.example .env
```

Minimum suggested for secure ngrok exposure:
- `MIRA_API_KEY`

Common model/API keys (optional, depending on provider):
- `GROQ_API_KEY`
- `GEMINI_API_KEY`
- `CEREBRAS_API_KEY`
- `SAMBANOVA_API_KEY`
- `OPENROUTER_API_KEY`

Email (optional):
- `SMTP_USER`, `SMTP_PASS`, `SMTP_HOST`, `SMTP_PORT`
- `IMAP_HOST`, `IMAP_PORT`

Firecrawl self-hosted endpoint (optional):
- `FIRECRAWL_API_URL=http://localhost:3100`

## 4) Install Local Model Runtime (Ollama + Qwen)

Install Ollama, then pull at least one model:

```powershell
ollama pull qwen3:4b-instruct
```

Recommended stronger models for reliability:

```powershell
ollama pull qwen3.5:4b
ollama pull mistral:7b
```

Default local model in code path is Qwen 3 family.

## 5) Optional: WhatsApp Bridge Dependencies

```powershell
cd whatsapp_bridge
npm install
cd ..
```

## 6) Optional: Firecrawl via Docker (Only Needed for Firecrawl/Scraping Tools)

You do **not** need Docker for core MIRA chat/tools.
Docker is needed only for Firecrawl-backed scraping/crawl/extract tools.

From repository root (`AION-Deliverables`):

```powershell
cd firecrawl
docker compose up -d
```

or if your setup uses legacy command:

```powershell
docker-compose up -d
```

## 7) Start MIRA

### Fast local start

```powershell
run.bat
```

### Full public-access stack (recommended walkthrough)

```powershell
run_with_ngrok.bat
```

`run_with_ngrok.bat` starts:
1. Firecrawl Docker (best effort; optional)
2. WhatsApp bridge (if Node.js exists)
3. Autonomous scheduler
4. MCP server
5. Flask UI
6. ngrok tunnel on port `5000`

Then open the HTTPS ngrok URL shown in the terminal.

## Alternative Launch Modes

Scheduler mode:

```powershell
run_with_scheduler.bat
```

Voice mode:

```powershell
run_voice.bat
```

## Models and Provider Routing

MIRA supports:
- Local Ollama models (default path)
- Cloud providers via model prefixes in adapter flow:
  - `groq:*`
  - `gemini:*`
  - `cerebras:*`
  - `sambanova:*`
  - `openrouter:*`

If cloud providers are rate-limited/unavailable, local model path is used when configured.

## Dependency Audit Summary (Important)

Imported modules were reviewed against project requirements.

Directly required and now included:
- `python-dotenv`
- `pandas`
- `pyaudio`

Already present in requirements and used:
- `Flask`, `requests`, `websockets`, `openai-whisper`, `sounddevice`, `numpy`, `pywin32`, `ddgs`, `firecrawl-py`, `beautifulsoup4`, `readability-lxml`, `html2text`, `chromadb`, `sentence-transformers`, etc.

Optional for advanced Gmail OAuth workflows (not mandatory for SMTP-only mode):
- `google-api-python-client`
- `google-auth-httplib2`
- `google-auth-oauthlib`

## Security Checklist Before Running

- Never commit `.env`
- Set `MIRA_API_KEY` before exposing via ngrok
- Keep `AION-Windows/whatsapp_bridge/auth_whatsapp/` out of version control
- Do not hardcode SMTP/app passwords in test files

## Troubleshooting

Ollama not reachable:
- Ensure Ollama app/service is running
- Verify `http://localhost:11434`

ngrok not found:
- Install ngrok and ensure it is in PATH

Node not found:
- WhatsApp bridge is skipped; core MIRA still runs

Docker not running:
- Firecrawl startup will fail; core MIRA still runs

Whisper issues:
- Install FFmpeg
- Reinstall `openai-whisper` and audio dependencies

PyAudio install issues:
- Use `pipwin install pyaudio` on Windows

## Key Files

- Main UI: `ui/app.py`
- MCP server: `mcp_server/server.py`
- Agent adapter/models: `agent/model_adapter.py`
- Full startup script: `run_with_ngrok.bat`
- Model recommendations: `RECOMMENDED_MODELS.md`

---

If you follow this guide exactly, `MIRA-Windows` should run end-to-end on Windows, with Docker required only for Firecrawl/scraping features.
