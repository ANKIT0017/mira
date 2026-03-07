# mcp_server/server.py
# Simple WebSocket MCP bridge for local connectors and agent
import asyncio
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
import websockets

# Load .env BEFORE any connector imports so os.environ.get() sees real values
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(Path(__file__).parent.parent / ".env", override=True)
except ImportError:
    pass

# Handle imports when run as script
if __name__ == "__main__":
    sys.path.append(str(Path(__file__).parent.parent))
    from mcp_server.connectors.filesystem import list_dir, read_file
    from mcp_server.connectors.calendar import add_event, list_events
    from mcp_server import task_store
    from agent.run_controller import get_controller
else:
    # Handle imports when imported as module
    from .connectors.filesystem import list_dir, read_file
    from .connectors.calendar import add_event, list_events
    from . import task_store
    from agent.run_controller import get_controller

# ── Session-scoped agent registry ────────────────────────────────────────────
# Agents are keyed by session_id (client-supplied or auto-generated per WS
# connection). This lets history and buffer survive WebSocket reconnects while
# still isolating different browser tabs / users from each other.
_session_agents: dict = {}   # session_id → Agent instance

DB_PATH = Path(__file__).parent / "db.sqlite"

# Ensure DB exists and schema
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, title TEXT, start TEXT, end TEXT, metadata TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, key TEXT, text TEXT)')
    conn.commit()
    conn.close()

async def _run_agent_background(task_id: int, user_input: str, model: str, ws, agent, run_handle=None):
    """
    Runs the agent as a background asyncio task decoupled from the WebSocket.
    All events are persisted to mira_tasks.db. Events are forwarded to the
    WebSocket opportunistically — if the client disconnects, sends fail silently
    and execution continues until the task is complete.
    """
    # Transition to running
    task_store.update_task(
        task_id,
        status='running',
        started_at=datetime.now(timezone.utc).isoformat()
    )
    if run_handle:
        run_handle.status = "running"

    controller = get_controller()
    try:
        async for event in agent.handle_streaming(user_input, model=model, run_handle=run_handle):
            # 1. Always persist the event to DB
            task_store.add_event(task_id, event)

            # 2. Forward to WebSocket if still connected (best-effort)
            try:
                await ws.send(json.dumps({
                    "status": "ok",
                    "action": "stream_event",
                    "event": event,
                    "task_id": task_id
                }))
            except Exception:
                pass  # Client disconnected — task continues regardless

            # 3. On terminal event, mark task with explicit terminal status
            ev_type = event.get("type")
            if ev_type == "final":
                task_store.update_task(
                    task_id, status='completed',
                    result=event.get("content", ""),
                    completed_at=datetime.now(timezone.utc).isoformat()
                )
                if run_handle:
                    controller.finalize_run(run_handle, "completed")
                break
            elif ev_type == "aborted":
                task_store.update_task(
                    task_id, status='aborted',
                    error="Aborted by user",
                    completed_at=datetime.now(timezone.utc).isoformat()
                )
                if run_handle:
                    controller.finalize_run(run_handle, "aborted")
                break
            elif ev_type == "iteration_limit":
                task_store.update_task(
                    task_id, status='iteration_limit',
                    result=event.get("content", ""),
                    completed_at=datetime.now(timezone.utc).isoformat()
                )
                if run_handle:
                    controller.finalize_run(run_handle, "iteration_limit")
                break
            elif ev_type == "step_limit":
                task_store.update_task(
                    task_id, status='iteration_limit',
                    result=event.get("content", ""),
                    completed_at=datetime.now(timezone.utc).isoformat()
                )
                if run_handle:
                    controller.finalize_run(run_handle, "iteration_limit")
                break
            elif ev_type == "need_input":
                # Agent is paused waiting for user clarification.
                # Mark task as waiting_input — do NOT break; run is still live.
                task_store.update_task(task_id, status='waiting_input')
                try:
                    await ws.send(json.dumps({
                        "status": "ok",
                        "action": "need_input",
                        "task_id": task_id,
                        "question": event.get("question", ""),
                        "step": event.get("step")
                    }))
                except Exception:
                    pass  # client may have disconnected; input_queue will still receive answer

        # Notify client stream is done
        try:
            await ws.send(json.dumps({
                "status": "ok",
                "action": "stream_end",
                "task_id": task_id
            }))
        except Exception:
            pass

    except Exception as e:
        print(f"[MCP] Background task {task_id} failed: {e}")
        task_store.update_task(
            task_id,
            status='failed',
            error=str(e),
            completed_at=datetime.now(timezone.utc).isoformat()
        )
        if run_handle:
            controller.finalize_run(run_handle, "failed")


async def handle(ws, path=None):
    # Per-connection session_id: client may supply one for session continuity
    # (e.g., to resume history after a page refresh).  If absent, we generate
    # one UUID per WS connection — same behaviour as before, but now explicit.
    conn_session_id = str(uuid.uuid4())
    controller = get_controller()

    try:
        async for msg in ws:
            try:
                req = json.loads(msg)
                action = req.get("action")
                payload = req.get("payload", {})
                # Session-ID: client may send one for continuity; fall back to
                # the per-connection UUID assigned when this WS was opened.
                session_id = (payload.get("session_id") if isinstance(payload, dict) else None) or conn_session_id
                response = None
                
                if action == "list_dir":
                    base = payload.get("path", ".")
                    res = list_dir(base)
                    response = {"status":"ok","action":action,"result":res}
                elif action == "read_file":
                    p = payload.get("path")
                    res = read_file(p)
                    response = {"status":"ok","action":action,"result":res}
                elif action == "add_event":
                    ev = payload
                    add_event(DB_PATH, ev)
                    response = {"status":"ok","action":action,"result": "added"}
                elif action == "list_events":
                    res = list_events(DB_PATH)
                    response = {"status":"ok","action":action,"result":res}
                elif action == "agent_invoke":
                    # Agent runtime (LLM-backed)
                    # Session-scoped: history/buffer persist for the same session_id.
                    if session_id not in _session_agents:
                        from agent.agent import Agent
                        _session_agents[session_id] = Agent()
                    agent = _session_agents[session_id]

                    user_input = payload.get("input", "")
                    model = payload.get("model", "qwen3:4b-instruct")
                    print(f"[MCP] agent_invoke  session={session_id[:8]} model={model} input={user_input[:50]}...")
                    res = await agent.handle(user_input, model=model)
                    response = {"status":"ok","action":action,"result":res}
                elif action == "agent_invoke_stream":
                    # Session-scoped streaming: agent survives WS reconnects.
                    if session_id not in _session_agents:
                        from agent.agent import Agent
                        _session_agents[session_id] = Agent()
                    agent = _session_agents[session_id]

                    user_input = payload.get("input", "")
                    model = payload.get("model", "qwen3:4b-instruct")
                    print(f"[MCP] agent_invoke_stream  session={session_id[:8]} model={model} input={user_input[:50]}...")

                    # Create DB record immediately so client gets task_id before execution
                    task_id = task_store.create_task(user_input, model,
                                                     run_id=None, session_id=session_id)

                    # Phase-1: pre-register BEFORE the session lock so the client ack
                    # carries a real run_id and abort-by-run_id works immediately.
                    # _active_runs is NOT yet updated — that happens inside the lock
                    # (phase-2 via activate_run) to prevent two concurrent requests from
                    # overwriting each other's handle before either holds the lock.
                    run_handle = controller.pre_register_run(session_id=session_id, task_id=task_id)
                    # Persist run_id into the DB row
                    task_store.update_task(task_id, run_id=run_handle.run_id)

                    # Acknowledge to client with task_id + session_id
                    try:
                        await ws.send(json.dumps({
                            "status": "ok",
                            "action": "task_created",
                            "task_id": task_id,
                            "run_id": run_handle.run_id,
                            "session_id": session_id
                        }))
                    except websockets.exceptions.ConnectionClosedError:
                        # Client disconnected before we could send them the task_id.
                        # The task row and pre-registered run handles must be cleaned up:
                        # - Mark the DB task as aborted so it doesn’t stay stuck at 'pending'
                        # - Evict _run_by_id / _run_by_task so they don’t leak in RunController
                        task_store.update_task(
                            task_id,
                            status='aborted',
                            error="Client disconnected before task_created ack could be sent",
                            completed_at=datetime.now(timezone.utc).isoformat()
                        )
                        controller.discard_pre_registered(run_handle)
                        print("[MCP] Client disconnected before task_created — run discarded, task aborted")
                        return

                    # Fire-and-forget under the per-session lock so only one run
                    # mutates this session at a time (OpenClaw session-write-lock).
                    async def _locked_run():
                        # Phase-2: promote to active slot now that we hold the session lock.
                        # Any previously active run for this session must already be finalized.
                        controller.activate_run(run_handle)
                        await _run_agent_background(
                            task_id, user_input, model, ws, agent, run_handle
                        )
                    asyncio.ensure_future(controller.run_under_lock(session_id, _locked_run))
                    continue  # Return control to the WS message loop immediately
                elif action == "reset":
                    print("[MCP] Reset requested")
                    target_session = None
                    if isinstance(payload, dict) and payload.get("session_id"):
                        target_session = payload.get("session_id")
                    else:
                        target_session = session_id

                    if target_session in _session_agents:
                        print(f"[MCP] Clearing agent session {target_session[:8]}")
                        del _session_agents[target_session]
                        reset_msg = "Agent session reset — fresh instance will be created on next message"
                    else:
                        # Fallback for callers that didn't provide a valid session_id
                        # (e.g., /api/reset over a new WS connection).
                        cleared = len(_session_agents)
                        _session_agents.clear()
                        print(f"[MCP] Target session not found; cleared all sessions: {cleared}")
                        reset_msg = f"Target session not found; cleared {cleared} session(s)"
                    # Also re-sync tools in case connectors changed
                    init_connectors()
                    response = {"status":"ok","action":"reset","result":reset_msg}
                elif action == "agent_answer":
                    # Deliver a user answer to a paused NEED_INPUT run.
                    answer_task_id = payload.get("task_id") if isinstance(payload, dict) else None
                    answer_text = payload.get("answer", "") if isinstance(payload, dict) else ""
                    if answer_task_id is None:
                        response = {"status": "error", "error": "agent_answer requires payload.task_id"}
                    else:
                        rh = controller.get_run_by_task(int(answer_task_id))
                        if rh and hasattr(rh, 'answer_input'):
                            rh.answer_input(answer_text)
                            task_store.update_task(int(answer_task_id), status='running')
                            response = {"status": "ok", "action": "agent_answer", "result": "answer delivered"}
                        else:
                            response = {"status": "error", "error": f"No waiting run for task_id={answer_task_id}"}
                elif action == "abort":
                    # Abort targets task_id (or run_id or session_id as fallback).
                    # The RunController sets the cancel_event so the agent loop exits
                    # cleanly at the next step boundary.
                    task_id_to_abort = payload.get("task_id") if isinstance(payload, dict) else None
                    run_id_to_abort  = payload.get("run_id")  if isinstance(payload, dict) else None
                    aborted = False
                    if task_id_to_abort is not None:
                        aborted = controller.abort_run(task_id=int(task_id_to_abort))
                        if aborted:
                            task_store.mark_cancelled(int(task_id_to_abort))
                    elif run_id_to_abort:
                        aborted = controller.abort_run(run_id=run_id_to_abort)
                    else:
                        # Legacy fallback: cancel active run for this session
                        aborted = controller.abort_run(session_id=session_id)
                    print(f"[MCP] Abort request: task={task_id_to_abort} → aborted={aborted}")
                    response = {"status":"ok","action":"abort","result":{"aborted": aborted}}
                elif action == "wait":
                    # Poll until the specified task reaches a terminal state.
                    # Client sends {action:"wait", payload:{task_id:N, timeout_s:60}}
                    task_id_to_wait = payload.get("task_id") if isinstance(payload, dict) else None
                    timeout_s = float(payload.get("timeout_s", 60)) if isinstance(payload, dict) else 60
                    if task_id_to_wait is None:
                        response = {"status":"error","error":"wait requires payload.task_id"}
                    else:
                        deadline = asyncio.get_event_loop().time() + timeout_s
                        result_task = None
                        while asyncio.get_event_loop().time() < deadline:
                            result_task = task_store.get_task(int(task_id_to_wait))
                            if result_task and result_task.get("status") in task_store.TERMINAL_STATES:
                                break
                            await asyncio.sleep(1)
                        response = {"status":"ok","action":"wait","task": result_task}
                elif action == "ping":
                    response = {"status":"ok","action":"ping","result":"pong"}
                else:
                    response = {"status":"error","error":"unknown action"}
                
                # Send response with error handling
                if response:
                    try:
                        await ws.send(json.dumps(response))
                    except websockets.exceptions.ConnectionClosedError:
                        print(f"[MCP] Client disconnected before response could be sent (action: {action})")
                        return
                        
            except Exception as e:
                print(f"[MCP] Error processing message: {e}")
                try:
                    await ws.send(json.dumps({"status":"error","error":str(e)}))
                except websockets.exceptions.ConnectionClosedError:
                    print("[MCP] Client disconnected before error response could be sent")
                    return
    except websockets.exceptions.ConnectionClosedError:
        print("[MCP] Client connection closed")
    except Exception as e:
        print(f"[MCP] Unexpected error in handler: {e}")

def init_connectors():
    """Import all connectors to register their tools."""
    import importlib
    import pathlib
    import sys
    
    connectors_dir = pathlib.Path(__file__).parent / "connectors"
    for f in connectors_dir.glob("*.py"):
        if f.stem != "__init__":
            try:
                module_name = f"mcp_server.connectors.{f.stem}"
                if module_name in sys.modules:
                    print(f"Reloading connector: {f.stem}")
                    importlib.reload(sys.modules[module_name])
                else:
                    print(f"Loading connector: {f.stem}")
                    importlib.import_module(module_name)
            except Exception as e:
                print(f"Error loading connector {f.stem}: {e}")

if __name__ == '__main__':
    # Initialize in correct order:
    # 1. DB first (tools may need it)
    init_db()
    print("Database initialized")

    # 1b. Run calendar schema migrations automatically (idempotent — safe to run every boot)
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent.parent))
        from migrate_events_db import migrate_events_table
        migrate_events_table()
    except Exception as _mig_err:
        print(f"[MIGRATION] Warning: migration check failed: {_mig_err}")
        print("[MIGRATION] Run migrate_events_db.py manually if calendar features fail.")

    # 2. Initialize task persistence DB
    task_store.init_db()
    task_store.cleanup_stale_running()
    print("Task store initialized")

    # 3. Load and register all tools
    init_connectors()
    print("Connectors loaded, tools registered")
    
    # 3. Load plugins
    from mcp_server.plugin_manager import get_plugin_manager
    plugin_mgr = get_plugin_manager()
    plugin_mgr.load_all()
    print("Plugins loaded")
    
    # 4. Start the WebSocket server (Agent will be created on first use)
    print("MCP WebSocket server starting at ws://localhost:9000")
    async def main():
        # Bind to IPv4 explicitly to avoid Windows IPv6 permission issues
        async with websockets.serve(
            handle, 
            "127.0.0.1",  # IPv4 loopback instead of "localhost"
            9000,  # Changed from 8765 (Windows reserved range)
            ping_timeout=180,  # Allow 3 minutes for long model responses
            close_timeout=180
        ):
            await asyncio.Future()  # run forever
            
    asyncio.run(main())
