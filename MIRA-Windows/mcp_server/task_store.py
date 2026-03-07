"""
mcp_server/task_store.py

Persistent task store for MIRA background agent execution.
Uses a dedicated mira_tasks.db (separate from the connectors' db.sqlite).
All functions open and close their own SQLite connection — safe for concurrent asyncio tasks.
"""
import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "mira_tasks.db"

# Explicit terminal states — once a task reaches one of these it cannot
# transition again.  Mirrors OpenClaw's agent terminal state machine.
TERMINAL_STATES: frozenset = frozenset(
    {"completed", "failed", "aborted", "iteration_limit", "timeout"}
)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # Safe for concurrent reads/writes
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── SETUP ────────────────────────────────────────────────────────────

def init_db():
    """Create tables if they don't exist. Call once at server startup."""
    conn = _connect()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            text         TEXT    NOT NULL,
            model        TEXT    NOT NULL DEFAULT 'qwen3:4b-instruct',
            status       TEXT    NOT NULL DEFAULT 'pending',
            run_id       TEXT,
            session_id   TEXT,
            created_at   TEXT    NOT NULL,
            started_at   TEXT,
            completed_at TEXT,
            result       TEXT,
            error        TEXT
        )
    """)
    # Idempotent migrations for existing databases that predate run_id / session_id
    for col, col_type in [("run_id", "TEXT"), ("session_id", "TEXT")]:
        try:
            c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {col_type}")
        except Exception:
            pass  # Column already exists
    c.execute("""
        CREATE TABLE IF NOT EXISTS task_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id    INTEGER NOT NULL,
            event_type TEXT    NOT NULL,
            event_json TEXT    NOT NULL,
            created_at TEXT    NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
    """)
    conn.commit()
    conn.close()


def cleanup_stale_running():
    """
    Called at server startup. Any task stuck in a non-terminal state means
    the server restarted mid-execution. Mark those as 'failed' so the UI
    doesn't show them as forever-running.
    """
    non_terminal = ', '.join(f"'{s}'" for s in ["running", "pending"])
    conn = _connect()
    conn.execute(
        f"UPDATE tasks SET status = 'failed', error = 'Server restarted during execution' "
        f"WHERE status IN ({non_terminal})"
    )
    conn.commit()
    conn.close()


# ── WRITE OPERATIONS ─────────────────────────────────────────────────

def create_task(
    text: str,
    model: str,
    run_id: str = None,
    session_id: str = None,
) -> int:
    """Insert a new task with status='pending'. Returns the new task id."""
    conn = _connect()
    c = conn.cursor()
    c.execute(
        "INSERT INTO tasks (text, model, status, run_id, session_id, created_at) "
        "VALUES (?, ?, 'pending', ?, ?, ?)",
        (text, model, run_id, session_id, _now_iso())
    )
    task_id = c.lastrowid
    conn.commit()
    conn.close()
    return task_id


def update_task(task_id: int, **kwargs):
    """
    Update arbitrary columns on a task row.
    Accepted kwargs: status, result, error, started_at, completed_at, run_id, session_id
    Guards against overwriting a terminal status with a non-terminal one.
    """
    allowed = {'status', 'result', 'error', 'started_at', 'completed_at', 'run_id', 'session_id'}
    # Protect terminal states: if the incoming status is not terminal but the
    # current DB status already is, skip the status update.
    new_status = kwargs.get('status')
    if new_status and new_status not in TERMINAL_STATES:
        current = get_task(task_id)
        if current and current.get('status') in TERMINAL_STATES:
            kwargs.pop('status', None)
    cols = {k: v for k, v in kwargs.items() if k in allowed}
    if not cols:
        return
    set_clause = ", ".join(f"{k} = ?" for k in cols)
    values = list(cols.values()) + [task_id]
    conn = _connect()
    conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
    conn.commit()
    conn.close()


def add_event(task_id: int, event_dict: dict):
    """Append one streaming event to task_events for the given task."""
    conn = _connect()
    conn.execute(
        "INSERT INTO task_events (task_id, event_type, event_json, created_at) VALUES (?, ?, ?, ?)",
        (task_id, event_dict.get("type", "unknown"), json.dumps(event_dict), _now_iso())
    )
    conn.commit()
    conn.close()


def mark_cancelled(task_id: int) -> bool:
    """
    Transition a task to 'aborted' status.  Safe to call from any context
    (the RunController also signals cancel_event in-memory; this persists
    the cancel to the DB so polling clients see the correct terminal state).
    Returns True if the row was updated.
    """
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE tasks SET status = 'aborted', completed_at = ? "
        "WHERE id = ? AND status NOT IN ('completed', 'failed', 'aborted', 'iteration_limit', 'timeout')",
        (_now_iso(), task_id)
    )
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def delete_task(task_id: int):
    """Delete a task and all its events (CASCADE handles task_events rows)."""
    conn = _connect()
    conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()


# ── READ OPERATIONS ───────────────────────────────────────────────────

def get_tasks(limit: int = 100) -> list:
    """Return up to `limit` tasks ordered newest first."""
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_task(task_id: int):
    """Return a single task as a dict, or None if not found."""
    conn = _connect()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_events(task_id: int) -> list:
    """Return all events for a task in chronological order."""
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? ORDER BY id ASC", (task_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
