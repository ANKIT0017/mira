# MIRA Windows

**Version:** v0.2

MIRA Windows is the main project in this repository: a Windows-first personal AI assistant that can talk, listen, research, use local tools, create files, automate WhatsApp, manage tasks, and run multi-step workflows from one desktop UI.

It is not a demo chatbot. MIRA is a local agent runtime with a real tool layer, a live browser interface, optional voice mode, document generation, web research, memory, contacts, calendar, todo, email, WhatsApp, scheduler, and local/cloud model routing.

![MIRA walkthrough](./MIRA-Windows/assets/mira2.gif)

## v0.2 Latest Updates

MIRA v0.2 is focused on reliability: better research, stronger voice input, safer tool execution, more complete file generation, and improved context handoff between steps.

- Research now keeps search queries scoped to the actual research question instead of mixing in unrelated workflow instructions.
- Research falls back from blocked news search to regular web search, and can run targeted follow-up searches when the first pass does not gather enough evidence.
- Verified research tasks are more evidence-aware: MIRA should separate facts from opinions and call out missing support instead of inventing confidence.
- Voice input now uses a stronger local STT path with `faster-whisper`, adaptive endpoint detection, browser recording, and fallback support.
- Tool usage is more resilient across model output styles, including nested `args`, inline key-value calls, generated file paths, media sends, and contact/todo workflows.
- File generation is more reliable for code, PDFs, documents, CSV, JSON, HTML, and other generated artifacts before they are sent or reused.
- Context management now carries raw tool outputs, artifact paths, buffers, and slot values more carefully into later steps and final synthesis.
- Todo and contact flows are smarter: due dates like "tomorrow" are inferred, business-day spreading is supported, and explicit confirmation lookups are not skipped.
- WhatsApp media sending now prefers the real generated artifact path and validates document delivery more carefully.
- The repository direction now treats `MIRA-Windows/` as the main supported project.

## What MIRA Does

- Turns natural language into multi-step tool workflows.
- Streams live plan, tool execution, task state, errors, approvals, failures, and final output.
- Creates and sends files such as PDFs, Markdown, text, code, CSV, JSON, HTML, and Excel workbooks.
- Sends WhatsApp messages and media through the local WhatsApp bridge.
- Performs web research with source extraction, adequacy checks, and claim-aware synthesis.
- Runs local models through llama.cpp/Ollama-style adapters and can route to cloud providers when configured.
- Supports voice input/output with local `faster-whisper` STT and Piper/Windows TTS paths.
- Includes a fluid voice UI that visualizes listening, thinking, tool progress, approvals, failures, and completion.

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

MIRA v0.2 uses a stronger local STT path:

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

Run voice mode:

```powershell
cd MIRA-Windows
.\venv\Scripts\python.exe -m voice.voice_agent --stt-model base.en
```

Use `small.en` when accuracy matters more than speed.

## Research And Reasoning

MIRA's research runtime is designed to gather enough information before answering. It searches, reads sources, extracts facts, checks whether the evidence is adequate for the user's request, and performs targeted follow-up research when the first pass is too thin.

For verified investment-style research, MIRA looks for:

- sector-level evidence
- named companies or tickers
- financial facts
- investor or ownership facts
- technical trend facts
- multiple independent or primary sources

If evidence is insufficient, MIRA should say what is missing instead of inventing confidence.

## Tool Execution

MIRA v0.2 improves how the agent handles tool calls from local models:

- accepts strict JSON, nested `args`, and inline key-value tool calls
- preserves generated artifact paths across later steps
- avoids redundant but required confirmation skips
- validates file/media paths before sending
- infers due dates such as "tomorrow"
- spreads todo due dates over business days when requested
- routes school/homework/general factual questions to direct reasoning instead of unnecessary knowledge-base or web tools

## File Generation

MIRA can generate and reuse files across workflows:

- source code files such as `.cpp`, `.py`, `.js`, `.html`, `.css`, and `.json`
- PDFs and Markdown reports
- CSV files and Excel workbooks
- text notes and generated content
- WhatsApp document attachments

Generated outputs are saved under `MIRA-Windows/SANDBOX/` unless a connector explicitly returns a different safe artifact path.

## WhatsApp Automation

MIRA can resolve contacts, create files, and send messages or media through the WhatsApp bridge.

Example workflows:

- "Write Bellman-Ford in C++ and send the file to Ankit on WhatsApp."
- "Find the best books to read this month and send a PDF summary."
- "Research verified India sector growth and send Ankit a PDF report."

The agent validates media delivery paths before sending and prefers generated document artifacts such as PDFs over stale text files.

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

## Documentation

- [Windows setup guide](./MIRA-Windows/README.md)
- [Project spec](./MIRA-Windows/PROJECT_SPEC.md)
- [Recommended models](./MIRA-Windows/RECOMMENDED_MODELS.md)
- [Email setup](./MIRA-Windows/EMAIL_SETUP.md)
- [Ngrok setup](./MIRA-Windows/NGROK_SETUP.md)

## Current Direction

MIRA Windows is the primary project going forward. The repository should stay centered on a polished Windows assistant experience: reliable tool execution, strong voice input, live workflow UI, careful research, robust file generation, and practical desktop automation.
