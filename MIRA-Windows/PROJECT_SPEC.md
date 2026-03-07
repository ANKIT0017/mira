# MIRA: Advanced Agentic Intelligence & Operations Network

MIRA is a state-of-the-art, modular AI agent ecosystem designed for local and cloud-assisted autonomous operations. Built on a robust Model Context Protocol (MCP) backbone, MIRA transcends traditional chat interfaces by integrating deep system-level control, real-time information retrieval, and sophisticated document processing capabilities.

## 🏗️ System Architecture

MIRA follows a decoupled, three-tier architecture ensuring scalability, resilience, and privacy:

1.  **Frontend (UI Layer)**: A modern, responsive Flask-based web interface featuring a unified chat console, real-time voice interaction, and an integrated file management system.
2.  **Orchestration (Agent Layer)**: A sophisticated Python-based runtime that manages conversation state, selects appropriate tools, and executes multi-step workflows. It supports dynamic model switching between local (Ollama) and cloud (Groq) providers.
3.  **Connectivity (MCP Server Layer)**: A specialized Model Context Protocol server that hosts a modular "Connector" ecosystem. Each connector exposes specific system capabilities via a standardized tool registration interface.

---

## 🚀 Core Capabilities & Tool Ecosystem

MIRA is equipped with over 50 specialized tools categorized into six core domains:

### 1. 🌐 Web & Information Intelligence
- **Search Engine**: Real-time web and news searching via DuckDuckGo and NewsCatcher.
- **Deep Scraper**: Article extraction, clean text parsing (Readability), and automated summarization.
- **Raw Fetch**: Direct HTML/resource fetching for advanced data gathering.

### 2. 📧 Communication & Outreach
- **Unified Email**: Full SMTP (Send) and IMAP (Read/Fetch) integration.
- **Gmail OAuth Support**: Creation of Gmail drafts for manual review before sending.
- **Job Application Engine**: Tailored workflow for searching, drafting, and applying for roles.

### 3. 📄 Document & Data Processing
- **PDF Suite**: High-fidelity creation, extraction (OCR/Text), merging, and splitting.
- **Office Suite**: Programmatic generation and parsing of Microsoft Word (.docx) and Excel (.xlsx) files.
- **Conversions**: Seamless transformation between Markdown, HTML, and PDF.

### 4. 📝 Personal & Task Management
- **Persistence**: Local SQLite-backed task tracking.
- **Logic**: Priority-based sorting, due date management, and full-text task searching.
- **Batch Operations**: "Complete All" and "Clear History" bulk management.

### 5. ⚙️ System & Environment Control
- **Terminal Access**: Secure execution of shell commands and environment diagnostics.
- **Filesystem**: Recursive search, buffered read/write, and structured storage in `CREATEDBY-MIRA/`.
- **Status Monitoring**: Real-time memory, time, and connectivity tracking.

### 6. 🎤 Multi-Modal Interaction
- **Speech-to-Text (STT)**: Low-latency voice input recognition.
- **Text-to-Speech (TTS)**: High-quality audio synthesis for hands-free feedback.

---

## 🧠 Advanced Agent Logic

### 🔄 Multi-Step Workflow Chaining
Unlike traditional AI models that respond to single prompts, MIRA utilizes a **Workflow Continuation Mechanism**. It can break down complex instructions (e.g., *"Find 10 AI startups in Bangalore, save their info to Excel, and draft an introductory email to them"*) into discrete, sequential steps:
1.  **Discovery**: Search for data.
2.  **Processing**: Analyze results.
3.  **Persistence**: Save to structural storage.
4.  **Finalization**: Create drafts/send emails.

### 🛡️ Resilience & Reliability
- **API Key Rotation**: Automatic failover between multiple Groq API keys to bypass rate limits.
- **Error Recovery**: Graceful handling of WebSocket disconnections and tool failures with automatic retry logic.
- **Hardware Fallback**: Intelligent switching between WeasyPrint and ReportLab for PDF creation based on system dependencies (Windows/Linux).

---

## 🎨 User Experience Features

- **Integrated File Browser**: A dedicated UI component to manage `CREATEDBY-MIRA` files, allowing one-click "Insert as Context" or deletion.
- **Model Selection System**: Quick-switch between performance modes (Mistral Fast, Llama Thinking, or Cloud Speed).
- **Responsive Dark/Light UI**: A premium, glassmorphism-inspired interface with auto-resizing inputs and reactive status indicators.

---

## 🛠️ Technical Stack

- **Backend**: Python 3.12+, Flask, WebSockets (websockets library).
- **Frontend**: HTML5, Vanilla JS, CSS3 (Rich Aesthetics).
- **AI/LLM**: Ollama (Local), Groq (Cloud API), duckduckgo-search, PyPDF2, python-docx, openpyxl, pandas.
- **Database**: SQLite3.

---

## 🔒 Safety & Privacy
- **Local First**: All task data and simple histories are stored locally.
- **Security Check**: API endpoints implement directory traversal protection and validation for filesystem operations.
- **Proactive Governance**: Rule-based constraints prevent hallucination and ensure file outputs follow standard naming conventions.

---

*Project MIRA: Empowering users with the next generation of local, agentic intelligence.*
