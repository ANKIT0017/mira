# MIRA Repository

MIRA is a Windows-first personal AI assistant stack. It combines a browser UI, an agent runtime, a local tool server, optional voice features, WhatsApp automation, scheduler support, and local/cloud model routing so a first-time user can run practical assistant workflows from one project.

![MIRA walkthrough](./MIRA-Windows/assets/mira2.gif)

## What is in this repository?

The primary working build is:

- `MIRA-Windows/`

If you are setting up MIRA for the first time, start here:

- [MIRA Windows Setup Guide](./MIRA-Windows/README.md)

That guide walks through:

- Complete Windows installation walkthrough
- Dependency and module coverage
- Ollama/Qwen model setup
- API provider configuration
- Voice/TTS setup notes
- Optional Docker + Firecrawl setup (only needed for scraping tools)
- Full `run_with_ngrok.bat` startup flow

## How MIRA Works

MIRA is organized around a few cooperating parts:

- The Flask web UI is the main place to chat with and operate MIRA.
- The agent layer decides how to answer, which model/provider to use, and when to call tools.
- The MCP WebSocket server exposes local tools that the agent can execute.
- Optional services add extra capability, including WhatsApp, scheduled tasks, voice input/output, and Firecrawl-backed web scraping.

You can run the core assistant locally without Docker. Docker is only needed when you want Firecrawl scraping tools. Node.js is only needed for the WhatsApp bridge.

## Quick Start (Windows)

```powershell
cd MIRA-Windows
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
Copy-Item .env.example .env
run_with_ngrok.bat
```

Notes:

- Docker is optional and only required for Firecrawl tools.
- Node.js is required only for WhatsApp bridge features.
- Set `MIRA_API_KEY` in `.env` before exposing through ngrok.

For architecture details, optional integrations, troubleshooting, and model/provider setup, continue with the [MIRA Windows Setup Guide](./MIRA-Windows/README.md).
