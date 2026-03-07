# MIRA Repository

This repository currently ships a working Windows build in:

- `MIRA-Windows/`

If you are setting up MIRA, start here:

- [MIRA Windows Setup Guide](./MIRA-Windows/README.md)

That guide includes:

- Complete Windows installation walkthrough
- Dependency and module coverage
- Ollama/Qwen model setup
- API provider configuration
- Voice/TTS setup notes
- Optional Docker + Firecrawl setup (only needed for scraping tools)
- Full `run_with_ngrok.bat` startup flow

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
