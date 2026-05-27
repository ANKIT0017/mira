# MIRA Windows

MIRA Windows is the main project in this repository: a Windows-first personal AI assistant that can talk, listen, research, use local tools, create files, automate WhatsApp, manage tasks, and run multi-step workflows from one desktop UI.

It is not a demo chatbot. MIRA is a local agent runtime with a real tool layer, a live browser interface, optional voice mode, document generation, web research, memory, contacts, calendar, todo, email, WhatsApp, scheduler, and local/cloud model routing.

![MIRA walkthrough](./MIRA-Windows/assets/mira2.gif)

## What MIRA Does

- Turns natural language into multi-step tool workflows.
- Streams live plan, tool execution, task state, errors, approvals, and final output.
- Creates and sends files such as PDFs, Markdown, text, code, CSV, and Excel workbooks.
- Sends WhatsApp messages and media through the local WhatsApp bridge.
- Performs web research with source extraction and claim-aware synthesis.
- Runs local models through llama.cpp/Ollama-style adapters and can route to cloud providers when configured.
- Supports voice input/output with local `faster-whisper` STT and Piper/Windows TTS paths.
- Includes a premium fluid voice UI that visualizes listening, thinking, tool progress, approvals, failures, and completion.

## Project Layout

```text
MIRA-Windows/
  agent/              Core planner, executor, tool routing, context compiler
  mcp_server/         Local tool registry and connectors
  ui/                 Flask app and browser UI
  voice/              Local STT/TTS voice agent pieces
  whatsapp_bridge/    WhatsApp Web bridge
  scheduler/          Autonomous scheduling support
  plugins/            Local plugin examples and extension points
  assets/             UI assets and walkthrough media
```

Root-level legacy and third-party folders are not the product. The main supported build is `MIRA-Windows/`.

## Quick Start

```powershell
cd MIRA-Windows
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
Copy-Item .env.example .env
run_with_ngrok.bat
```

Then open the local UI shown by the startup script.

## Recommended Setup

1. Install Python 3.12.
2. Install Node.js if you want WhatsApp automation.
3. Install ngrok only if you want external access.
4. Configure `.env` from `MIRA-Windows/.env.example`.
5. Put local model binaries in `MIRA-Windows/` or point the startup scripts to your model path.
6. Set `MIRA_API_KEY` before exposing the UI outside localhost.

## Voice Mode

MIRA now uses a stronger local STT path:

- Preferred backend: `faster-whisper`
- Default model: `base.en`
- Fallback: `openai-whisper`
- Endpointing: adaptive local speech/silence detection
- Browser UI: records audio locally, sends it to `/api/voice/transcribe`, then streams the recognized command into the agent

Useful settings:

```powershell
$env:MIRA_STT_MODEL="base.en"   # balanced default
$env:MIRA_STT_MODEL="small.en"  # better accuracy, slower
$env:MIRA_STT_MODEL="tiny.en"   # faster, weaker
```

## WhatsApp Automation

MIRA can resolve contacts, create files, and send messages or media through the WhatsApp bridge.

Example workflows:

- “Write Bellman-Ford in C++ and send the file to Ankit on WhatsApp.”
- “Find the best books to read this month and send a PDF summary.”
- “Research verified India sector growth and send Ankit a PDF report.”

The agent validates media delivery paths before sending and prefers generated document artifacts such as PDFs over stale text files.

## Research And Tools

MIRA’s agent runtime is built around explicit tool execution. For research-heavy tasks, it searches, reads sources, extracts facts, checks whether evidence is adequate for the user’s request, and performs targeted follow-up research when the first pass is too thin.

For verified investment-style research, MIRA looks for:

- sector-level evidence
- named companies or tickers
- financial facts
- investor or ownership facts
- technical trend facts
- multiple independent or primary sources

If evidence is insufficient, MIRA should say what is missing instead of inventing confidence.

## Local Data And Privacy

The project is designed for local-first operation. Runtime files, databases, large model binaries, generated SANDBOX outputs, voice uploads, and third-party clones should stay out of git.

Tracked source should remain focused on:

- application code
- UI code
- connector code
- scripts
- example config
- docs

## Development

Run syntax checks:

```powershell
python -m py_compile agent/agent.py ui/app.py voice/stt_whisper.py voice/voice_agent.py
```

Run the UI:

```powershell
cd MIRA-Windows
.\venv\Scripts\python.exe ui\app.py
```

Run voice mode:

```powershell
cd MIRA-Windows
.\venv\Scripts\python.exe -m voice.voice_agent --stt-model base.en
```

Use `small.en` when accuracy matters more than speed.

## Documentation

- [Windows setup guide](./MIRA-Windows/README.md)
- [Project spec](./MIRA-Windows/PROJECT_SPEC.md)
- [Recommended models](./MIRA-Windows/RECOMMENDED_MODELS.md)
- [Email setup](./MIRA-Windows/EMAIL_SETUP.md)
- [Ngrok setup](./MIRA-Windows/NGROK_SETUP.md)

## Current Direction

MIRA Windows is the primary project going forward. The repository should stay centered on a polished Windows assistant experience: reliable tool execution, strong voice input, live workflow UI, careful research, robust file generation, and practical desktop automation.
