# ui/app.py
from flask import Flask, render_template, request, jsonify, Response
import asyncio
import websockets
import json
import os
import re as _re
import threading
import queue
import sys
import uuid
from pathlib import Path as _Path
from functools import wraps

# Load .env BEFORE any other MIRA imports so os.environ.get() sees real values
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_Path(__file__).parent.parent / ".env", override=True)
except ImportError:
    pass

# Make mcp_server importable from ui/app.py (which lives one level down from root)
_mcp_root = str(_Path(__file__).parent.parent)
if _mcp_root not in sys.path:
    sys.path.insert(0, _mcp_root)
from mcp_server import task_store as _task_store

app = Flask(__name__, template_folder="templates")

# Ensure task DB tables exist (Flask reads from it; MCP server writes to it)
_task_store.init_db()

# Tracks the model currently selected in the MIRA UI.
# WhatsApp handler reads this so it uses the same model.
_current_model = "qwen3:4b-instruct"
_voice_stt = None
_voice_stt_lock = threading.Lock()


def _get_voice_stt():
    """Lazy-load local STT for browser microphone uploads."""
    global _voice_stt
    with _voice_stt_lock:
        if _voice_stt is not None:
            return _voice_stt
        from voice.stt_whisper import SpeechToText

        stt_model = os.environ.get("MIRA_STT_MODEL", "base.en")
        stt_backend = os.environ.get("MIRA_STT_BACKEND", "auto")
        stt_compute = os.environ.get("MIRA_STT_COMPUTE_TYPE", "int8")
        _voice_stt = SpeechToText(
            model_size=stt_model,
            backend=stt_backend,
            compute_type=stt_compute,
        )
        return _voice_stt

# ── API Key Authentication ────────────────────────────────────────────
# Set MIRA_API_KEY in .env to protect all /api/* routes when exposed via ngrok.
# When the variable is NOT set, auth is skipped (safe for local-only use).
_MIRA_API_KEY: str = os.environ.get("MIRA_API_KEY", "")
if not _MIRA_API_KEY:
    print("[SECURITY] ⚠️  MIRA_API_KEY is not set. API routes are UNPROTECTED.")
    print("[SECURITY]    Set MIRA_API_KEY in .env before exposing this app via ngrok.")


def require_auth(f):
    """Decorator: validates X-API-Key / Authorization: Bearer header when MIRA_API_KEY is set."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if _MIRA_API_KEY:
            provided = (
                request.headers.get("X-API-Key")
                or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                or request.args.get("api_key", "")
            )
            if provided != _MIRA_API_KEY:
                return jsonify({"status": "error", "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


@app.before_request
def _check_api_auth():
    """Reject unauthenticated requests to /api/* when MIRA_API_KEY is configured."""
    if not _MIRA_API_KEY:
        return  # No key set → local-only mode, no auth enforced
    if not request.path.startswith("/api/"):
        return  # Static assets and the root UI page are public
    # Whitelist the WhatsApp incoming webhook (called by the local bridge process)
    if request.path == "/api/whatsapp/incoming" and request.remote_addr in ("127.0.0.1", "::1"):
        return  # Local bridge — no external auth required
    provided = (
        request.headers.get("X-API-Key")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        or request.args.get("api_key", "")
    )
    if provided != _MIRA_API_KEY:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401


@app.route('/api/voice/transcribe', methods=['POST'])
def voice_transcribe():
    """Transcribe browser-recorded audio with the local STT backend."""
    upload = request.files.get("audio")
    if upload is None:
        return jsonify({"status": "error", "error": "No audio upload provided."}), 400

    sandbox = (_Path(__file__).parent.parent / "SANDBOX" / "voice_uploads").resolve()
    sandbox.mkdir(parents=True, exist_ok=True)
    suffix = _Path(upload.filename or "audio.webm").suffix or ".webm"
    if not _re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
        suffix = ".webm"
    audio_path = sandbox / f"voice_{uuid.uuid4().hex}{suffix}"

    try:
        upload.save(audio_path)
        stt = _get_voice_stt()
        text = stt.transcribe_file(str(audio_path))
        return jsonify({
            "status": "ok",
            "text": text,
            "backend": getattr(stt, "backend", "unknown"),
            "model": getattr(stt, "model_size", "unknown"),
        })
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500
    finally:
        try:
            audio_path.unlink(missing_ok=True)
        except Exception:
            pass
 
# --- SSE Streaming Endpoint ---
@app.route('/api/stream', methods=['GET'])
def stream():
    """Server-Sent Events endpoint for streaming agent responses."""
    global _current_model
    text = request.args.get('text', '')
    model = request.args.get('model', 'qwen3:4b-instruct')
    session_id = request.args.get('session_id', '')  # passed from browser for continuity
    _current_model = model  # keep in sync with UI selection

    if not text:
        return jsonify({"status": "error", "error": "No text provided"}), 400

    def generate_sse():
        # Use a thread-safe queue to bridge async WS → sync SSE yield
        q = queue.Queue()
        SENTINEL = object()  # Marks end of stream

        def ws_reader():
            """Run in a separate thread: read WS events, put them on the queue."""
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                async def _relay():
                    uri = "ws://localhost:9000"
                    try:
                        async with websockets.connect(uri, open_timeout=10, close_timeout=300, ping_timeout=300) as ws:
                            payload = {
                                "action": "agent_invoke_stream",
                                "payload": {"input": text, "model": model}
                            }
                            if session_id:
                                payload["payload"]["session_id"] = session_id
                            await ws.send(json.dumps(payload))
                            
                            while True:
                                try:
                                    msg = await asyncio.wait_for(ws.recv(), timeout=300)
                                    data = json.loads(msg)
                                    
                                    if data.get("action") == "task_created":
                                        # Forward task_id + session_id to browser for abort targeting
                                        q.put(f"data: {json.dumps({'type': 'task_created', 'task_id': data['task_id'], 'run_id': data.get('run_id'), 'session_id': data.get('session_id')})}\n\n")
                                    elif data.get("action") == "stream_event":
                                        q.put(f"data: {json.dumps(data['event'])}\n\n")
                                    elif data.get("action") == "stream_end":
                                        q.put(f"data: {json.dumps({'type': 'done'})}\n\n")
                                        return
                                    elif data.get("status") == "error":
                                        q.put(f"data: {json.dumps({'type': 'error', 'content': data.get('error', 'Unknown error')})}\n\n")
                                        return
                                except asyncio.TimeoutError:
                                    q.put(f"data: {json.dumps({'type': 'error', 'content': 'Response timeout'})}\n\n")
                                    return
                    except Exception as e:
                        q.put(f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n")
                
                loop.run_until_complete(_relay())
            finally:
                q.put(SENTINEL)
                loop.close()

        # Start WS reader in background thread
        t = threading.Thread(target=ws_reader, daemon=True)
        t.start()

        # Yield SSE events as they arrive from the queue
        while True:
            item = q.get()  # Blocks until an event is available
            if item is SENTINEL:
                break
            yield item

    return Response(
        generate_sse(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )

async def mcp_call(payload, timeout=180):
    """Send a single WebSocket payload to the MCP server and return the response.

    timeout: seconds to wait for the agent to reply (default 3 min).
             Pass a larger value for complex multi-step tasks (e.g. WhatsApp tasks).
    """
    uri = "ws://localhost:9000"
    try:
        async with websockets.connect(
            uri,
            open_timeout=10,
            close_timeout=timeout,
            ping_timeout=timeout
        ) as ws:
            await ws.send(json.dumps(payload))
            res = await asyncio.wait_for(ws.recv(), timeout=timeout)
            return json.loads(res)
    except asyncio.TimeoutError:
        mins = timeout // 60
        return {"error": f"Model response timeout (exceeded {mins} min)", "status": "error"}
    except Exception as e:
        return {"error": str(e), "status": "error"}

@app.route('/')
def index():
    return render_template('chat.html', mira_api_key=os.environ.get("MIRA_API_KEY", ""))

@app.route('/api/invoke', methods=['POST'])
def invoke():
    try:
        data = request.json
        
        # Handle ping (connection check)
        if data.get('action') == 'ping':
            # Test MCP server connection
            loop = asyncio.new_event_loop()
            try:
                test_payload = {"action": "ping"}
                res = loop.run_until_complete(mcp_call(test_payload))
                if res.get("error"):
                    return jsonify({"status": "error", "error": f"MCP server not responding: {res['error']}"})
                return jsonify({"status": "ok", "reply": "pong"})
            finally:
                loop.close()
        
        # Handle message
        if data.get('action') == 'handle_message':
            global _current_model
            text = data.get('text', '')
            model = data.get('model', 'qwen3:4b-instruct')  # Get model from UI
            _current_model = model  # keep in sync with UI selection
            print(f"[FLASK] Received request with model={model}")  # Debug log
            # Call MCP bridge
            loop = asyncio.new_event_loop()
            try:
                payload = {
                    "action": "agent_invoke",
                    "payload": {
                        "input": text,
                        "model": model  # Pass model selection
                    }
                }
                print(f"[FLASK] Sending to MCP: payload={payload}")  # Debug log
                res = loop.run_until_complete(mcp_call(payload))
                
                # Check if MCP returned an error
                if res.get("error"):
                    return jsonify({"status": "error", "reply": f"MCP Error: {res['error']}"})
                
                return jsonify(res)
            finally:
                loop.close()
        
        # Fallback for old format
        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(mcp_call(data))
            return jsonify(res)
        finally:
            loop.close()
            
    except Exception as e:
        # Always return JSON, never HTML
        return jsonify({"status": "error", "error": str(e), "reply": f"Server error: {str(e)}"}), 500

@app.route('/api/reset', methods=['POST'])
def reset():
    """Clear agent history in the MCP server.

    Optional JSON body: {"session_id": "..."}
    If provided, only that session is reset; otherwise MCP may clear all sessions.
    """
    body = request.json or {}
    session_id = body.get("session_id")
    loop = asyncio.new_event_loop()
    try:
        payload = {"action": "reset", "payload": {}}
        if session_id:
            payload["payload"]["session_id"] = session_id
        res = loop.run_until_complete(mcp_call(payload))
        return jsonify(res)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route('/api/answer', methods=['POST'])
def answer():
    """Deliver a user answer to a paused NEED_INPUT run.

    Expects JSON body: {"task_id": N, "answer": "text"}
    Forwards to MCP server as agent_answer action.
    """
    body = request.json or {}
    task_id = body.get("task_id")
    answer_text = body.get("answer", "")
    if task_id is None:
        return jsonify({"status": "error", "error": "task_id required"}), 400
    loop = asyncio.new_event_loop()
    try:
        res = loop.run_until_complete(mcp_call({
            "action": "agent_answer",
            "payload": {"task_id": int(task_id), "answer": answer_text}
        }))
        return jsonify(res)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500
    finally:
        loop.close()


@app.route('/api/abort', methods=['POST'])
def abort():
    """
    Accepts optional JSON body: {"task_id": N, "run_id": "...", "session_id": "..."}
    When task_id is supplied the RunController cancels that specific run;
    without it, falls back to aborting the active run for the session.
    """
    body = request.json or {}
    loop = asyncio.new_event_loop()
    try:
        payload: dict = {}
        if body.get("task_id") is not None:
            payload["task_id"] = int(body["task_id"])
        if body.get("run_id"):
            payload["run_id"] = body["run_id"]
        if body.get("session_id"):
            payload["session_id"] = body["session_id"]
        res = loop.run_until_complete(mcp_call({"action": "abort", "payload": payload}))
        return jsonify(res)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route('/api/list-files', methods=['GET'])
def list_files():
    """List all files in SANDBOX folder."""
    try:
        from pathlib import Path
        files_dir = Path(__file__).parent.parent / "SANDBOX"
        
        if not files_dir.exists():
            return jsonify({"files": []})
        
        files = [f.name for f in files_dir.iterdir() if f.is_file()]
        files.sort()  # Alphabetical order
        
        return jsonify({"files": files})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route('/api/read-file', methods=['POST'])
def read_file():
    """Read content from a file in SANDBOX folder."""
    try:
        from pathlib import Path
        data = request.json
        filename = data.get('filename')
        
        if not filename:
            return jsonify({"status": "error", "error": "No filename provided"}), 400
        
        files_dir = Path(__file__).parent.parent / "SANDBOX"
        file_path = files_dir / filename
        
        # Security: prevent directory traversal
        if not str(file_path.resolve()).startswith(str(files_dir.resolve())):
            return jsonify({"status": "error", "error": "Invalid filename"}), 400
        
        if not file_path.exists():
            return jsonify({"status": "error", "error": "File not found"}), 404
        
        content = file_path.read_text(encoding='utf-8')
        return jsonify({"content": content, "filename": filename})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route('/api/delete-file', methods=['POST'])
def delete_file():
    """Delete a file from SANDBOX folder."""
    try:
        from pathlib import Path
        data = request.json
        filename = data.get('filename')
        
        if not filename:
            return jsonify({"status": "error", "error": "No filename provided"}), 400
        
        files_dir = Path(__file__).parent.parent / "SANDBOX"
        file_path = files_dir / filename
        
        # Security: prevent directory traversal
        if not str(file_path.resolve()).startswith(str(files_dir.resolve())):
            return jsonify({"status": "error", "error": "Invalid filename"}), 400
        
        if not file_path.exists():
            return jsonify({"status": "error", "error": "File not found"}), 404
        
        file_path.unlink()  # Delete the file
        return jsonify({"status": "success", "message": f"Deleted {filename}"})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route('/api/download-file/<filename>', methods=['GET'])
def download_file(filename):
    """Download a file from SANDBOX folder."""
    try:
        from pathlib import Path
        from flask import send_file
        
        files_dir = Path(__file__).parent.parent / "SANDBOX"
        file_path = files_dir / filename
        
        # Security: prevent directory traversal
        if not str(file_path.resolve()).startswith(str(files_dir.resolve())):
            return jsonify({"status": "error", "error": "Invalid filename"}), 400
        
        if not file_path.exists():
            return jsonify({"status": "error", "error": "File not found"}), 404
        
        return send_file(file_path, as_attachment=True, download_name=filename)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500




# ── TASK MANAGEMENT ROUTES ────────────────────────────────────────────

@app.route('/api/tasks', methods=['GET'])
def list_tasks():
    """Return all tasks (newest first) as a JSON array."""
    try:
        tasks = _task_store.get_tasks(limit=100)
        return jsonify(tasks)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route('/api/tasks/running', methods=['GET'])
def running_tasks_count():
    """Return count of currently running tasks. Registered before the int route."""
    try:
        tasks = _task_store.get_tasks(limit=500)
        count = sum(1 for t in tasks if t['status'] == 'running')
        return jsonify({"count": count})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route('/api/tasks/<int:task_id>', methods=['GET'])
def get_task_detail(task_id):
    """Return full task dict plus all stored events."""
    try:
        task = _task_store.get_task(task_id)
        if not task:
            return jsonify({"status": "error", "error": "Task not found"}), 404
        events = _task_store.get_events(task_id)
        for ev in events:
            try:
                ev['event_data'] = json.loads(ev['event_json'])
            except Exception:
                ev['event_data'] = {}
        return jsonify({"task": task, "events": events})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route('/api/tasks/<int:task_id>', methods=['DELETE'])
def remove_task(task_id):
    """Delete a task and all its events."""
    try:
        _task_store.delete_task(task_id)
        return jsonify({"status": "ok", "deleted": task_id})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500



# ── WHATSAPP ROUTES ───────────────────────────────────────────────────
import requests as _requests
import time as _time
from collections import deque as _deque

_BRIDGE_URL  = "http://localhost:9001"
_wa_messages = _deque(maxlen=100)   # rolling log of WA conversation events

# Scratchpad tags that should never appear in a WhatsApp message
_WA_STRIP_PATTERNS = [
    (r'\bACTION\s*:\s*\{.*', ''),        # ACTION: {...}
    (r'\bOBSERVATION\s*:.*', ''),         # OBSERVATION: ...
    (r'⚠️\s*SYSTEM.*',       ''),         # system nudge warnings
    (r'\bFINAL ANSWER\s*:\s*', ''),        # "FINAL ANSWER: " prefix
    (r'ACTION\s*:$',           ''),        # trailing "ACTION:" (no JSON)
]

def _clean_wa_reply(text: str) -> str:
    """Strip agent scratchpad artifacts so only the human-readable answer reaches WhatsApp."""
    # If FINAL ANSWER: appears, take only what's after the last occurrence
    if 'FINAL ANSWER:' in text:
        text = text.rsplit('FINAL ANSWER:', 1)[-1]
    # Cut off anything from ACTION: or OBSERVATION: onward
    text = _re.split(r'\bACTION\s*:', text, maxsplit=1)[0]
    text = _re.split(r'\bOBSERVATION\s*:', text, maxsplit=1)[0]
    # Remove system nudge lines
    text = _re.split(r'⚠️\s*SYSTEM', text, maxsplit=1)[0]
    return text.strip()

def _wa_bridge(method, path, **kwargs):
    """Proxy helper -> WhatsApp bridge. Returns (dict, http_status)."""
    try:
        r = _requests.request(method, f"{_BRIDGE_URL}{path}", timeout=10, **kwargs)
        return r.json(), r.status_code
    except _requests.ConnectionError:
        return {"status": "error",
                "error": "WhatsApp bridge not running. cd whatsapp_bridge && npm start"}, 503
    except Exception as e:
        return {"status": "error", "error": str(e)}, 500


@app.route('/api/whatsapp/status', methods=['GET'])
def wa_status():
    data, code = _wa_bridge("GET", "/status")
    return jsonify(data), code


@app.route('/api/whatsapp/qr', methods=['GET'])
def wa_qr():
    """Proxy QR image (base64 data URL) from bridge to browser."""
    data, code = _wa_bridge("GET", "/qr")
    return jsonify(data), code


@app.route('/api/whatsapp/send', methods=['POST'])
def wa_send():
    """Send a WhatsApp message directly (bypasses agent)."""
    body = request.json or {}
    data, code = _wa_bridge("POST", "/send", json=body)
    # Log outgoing manual message
    if data.get("status") == "sent":
        _wa_messages.append({
            "role": "agent",
            "from": "MIRA (manual)",
            "text": body.get("message", ""),
            "ts":   _time.time(),
            "to":   body.get("to", "")
        })
    return jsonify(data), code


@app.route('/api/whatsapp/messages', methods=['GET'])
def wa_messages():
    """Return recent WA conversation log (newest last)."""
    since = float(request.args.get("since", 0))
    msgs  = [m for m in _wa_messages if m["ts"] > since]
    return jsonify({"messages": msgs})


@app.route('/api/whatsapp/incoming', methods=['POST'])
def wa_incoming():
    """
    Called by the Baileys bridge when a WhatsApp message arrives.
    Returns 200 immediately, then runs the agent in a background thread
    and sends the reply back through the bridge.
    """
    body   = request.json or {}
    event  = body.get("event", "whatsapp_message")
    jid    = body.get("jid")
    sender = body.get("from", jid)
    text   = body.get("message", "")

    if event == "whatsapp_connected":
        print(f"[WA] Connected as {body.get('jid')}")
        return jsonify({"status": "ok"})

    if event == "whatsapp_disconnected":
        print(f"[WA] Disconnected: {body.get('reason')}")
        return jsonify({"status": "ok"})

    if not text or not jid:
        return jsonify({"status": "ok"})

    print(f"[WA] Incoming from {sender}: {text[:80]}")

    # Log the incoming message
    _wa_messages.append({
        "role":      "user",
        "from":      sender,
        "text":      text,
        "ts":        _time.time(),
        "is_group":  body.get("is_group", False),
        "group":     body.get("group_name")
    })

    def _handle():
        loop = asyncio.new_event_loop()
        try:
            is_group   = body.get("is_group", False)
            is_self    = body.get("is_self_chat", False)
            grp_name   = body.get("group_name", "")

            # Prefix the source so MIRA knows the channel/tone.
            # Don't re-declare "You are MIRA" — the agent already has its system prompt.
            source = "[WhatsApp"
            if is_self:
                source += " self-chat"
            elif is_group and grp_name:
                source += f" group '{grp_name}'"
            source += f" from +{sender}]"

            # Clarify that FINAL ANSWER IS the reply — prevents the agent from
            # confusing "send X" (meaning reply here) with outgoing whatsapp.send calls.
            wa_input = (
                f"{source}: {text}\n"
                f"(REPLY_JID: {jid} — use this exact value as 'to' in whatsapp.send or whatsapp.send_media to reply to THIS conversation.)\n"
                f"(Your FINAL ANSWER text is automatically sent back as a text message. "
                f"If the reply is a file/media, call whatsapp.send_media(to='{jid}', url='SANDBOX/file.pdf', type='document') instead of using FINAL ANSWER. "
                f"Only call whatsapp.send with a DIFFERENT phone number if the user explicitly asks to message someone else.)"
            )

            payload = {
                "action":  "agent_invoke",
                "payload": {
                    "input": wa_input,
                    "model": _current_model
                }
            }
            result = loop.run_until_complete(mcp_call(payload, timeout=600))

            # agent_invoke returns {"status":"ok","action":"agent_invoke","result":{"reply":"..."}}
            # handle() returns {"reply": text} so result["result"] is a dict
            if result.get("error"):
                print(f"[WA] MCP error: {result['error']}")
                return

            raw = result.get("result") or {}
            if isinstance(raw, dict):
                reply = (raw.get("reply") or "").strip()
            else:
                reply = str(raw).strip()

            # Strip agent scratchpad artifacts (ACTION:, OBSERVATION:, FINAL ANSWER: prefix)
            # before sending to WhatsApp — prevents loop-detection fallback text leaking.
            reply = _clean_wa_reply(reply)

            # Never send empty or error-string replies back to WhatsApp
            if not reply:
                print(f"[WA] Agent returned empty reply for: {text[:60]}")
                return

            _requests.post(f"{_BRIDGE_URL}/send",
                           json={"to": jid, "message": reply},
                           timeout=10)
            # Log the agent reply
            _wa_messages.append({
                "role": "agent",
                "from": "MIRA",
                "text": reply,
                "ts":   _time.time()
            })
            print(f"[WA] Replied to {sender}: {reply[:80]}")
        except Exception as e:
            print(f"[WA] Agent error: {e}")
        finally:
            loop.close()

    threading.Thread(target=_handle, daemon=True).start()
    return jsonify({"status": "ok"})


if __name__ == '__main__':
    print("Starting Flask UI on http://localhost:5000")
    # debug=True exposes the Werkzeug interactive debugger (arbitrary code execution).
    # Use debug only via MIRA_DEBUG=1 env var, never leave it hardcoded.
    _debug = os.environ.get("MIRA_DEBUG", "0") == "1"
    app.run(port=5000, debug=_debug)
