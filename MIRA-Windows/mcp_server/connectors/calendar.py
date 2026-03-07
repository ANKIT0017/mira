# mcp_server/connectors/calendar.py
import sqlite3, json
from datetime import datetime
from pathlib import Path
from ..tools import register_tool


def _default_db_path() -> str:
    return str(Path(__file__).parent.parent / "db.sqlite")


def _ensure_events_schema(conn: sqlite3.Connection) -> None:
    """Ensure events table exists and contains required columns."""
    c = conn.cursor()
    c.execute(
        '''CREATE TABLE IF NOT EXISTS events (
               id INTEGER PRIMARY KEY,
               title TEXT,
               start TEXT,
               end TEXT,
               metadata TEXT,
               recurrence TEXT DEFAULT 'none',
               enabled INTEGER DEFAULT 1,
               notification_message TEXT
           )'''
    )

    c.execute('PRAGMA table_info(events)')
    cols = {row[1] for row in c.fetchall()}

    if 'recurrence' not in cols:
        c.execute("ALTER TABLE events ADD COLUMN recurrence TEXT DEFAULT 'none'")
    if 'enabled' not in cols:
        c.execute("ALTER TABLE events ADD COLUMN enabled INTEGER DEFAULT 1")
    if 'notification_message' not in cols:
        c.execute("ALTER TABLE events ADD COLUMN notification_message TEXT")
    if 'metadata' not in cols:
        c.execute("ALTER TABLE events ADD COLUMN metadata TEXT")

    conn.commit()


def _connect_calendar_db(db_path: str | None = None) -> sqlite3.Connection:
    """Connect to calendar DB and guarantee schema is ready."""
    path = db_path or _default_db_path()
    conn = sqlite3.connect(path)
    _ensure_events_schema(conn)
    return conn

@register_tool(
    "calendar.add_event",
    category="Calendar",
    description="Add a one-time event/reminder to the calendar",
    example='{"tool": "calendar.add_event", "args": {"title": "Drink water", "start": "2026-02-20T16:05:00+05:30", "end": "2026-02-20T16:05:00+05:30"}}'
)
def add_event(
    title: str = None,
    start: str = None,
    end: str = None,
    notification_message: str = None,
    # legacy shape: ev dict passed by older callers
    ev: dict = None,
    db_path: str = None,
    **kwargs
):
    """Add a one-time event to the calendar.

    Args:
        title: Event title (required)
        start: Start datetime as ISO string or 'YYYY-MM-DD HH:MM' (required)
        end: End datetime (optional, defaults to start)
        notification_message: Custom reminder text (optional)
    """
    if db_path is None:
        db_path = _default_db_path()

    # Support legacy ev={...} dict shape
    if ev and isinstance(ev, dict):
        title = title or ev.get("title") or ev.get("summary")
        start = start or ev.get("start")
        end = end or ev.get("end")
        notification_message = notification_message or ev.get("notification_message")

    # Also accept 'summary' alias from kwargs
    if not title and "summary" in kwargs:
        title = kwargs["summary"]

    if not title:
        return {"status": "error", "error": "Missing required field: 'title'"}
    if not start:
        return {"status": "error", "error": "Missing required field: 'start'. Use ISO format e.g. '2026-02-20T16:05:00+05:30'"}

    # Default end = start (point-in-time event)
    if not end:
        end = start

    conn = _connect_calendar_db(db_path)
    c = conn.cursor()
    c.execute(
        '''INSERT INTO events (title, start, end, metadata, recurrence, enabled, notification_message)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (
            title,
            start,
            end,
            json.dumps({}),
            'none',
            1,
            notification_message or f"Reminder: {title}"
        )
    )
    rowid = c.lastrowid
    conn.commit()
    conn.close()
    print(f"[CALENDAR] Added event #{rowid}: '{title}' at {start}")
    return {"id": rowid, "title": title, "start": start, "end": end}

@register_tool(
    "calendar.list_events",
    category="Calendar",
    description="List events from the calendar",
    example='{"tool": "calendar.list_events", "args": {"limit": 10}}'
)
def list_events(db_path=None, all: bool = False, limit: int = 100, **kwargs):
    """List events from the calendar.

    Accepts optional parameters for compatibility with different tool call shapes:
    - db_path: optional path to sqlite DB (defaults to package db)
    - all: if True, return all events (ignore `limit`)
    - limit: max number of rows to return (default 100)
    - any extra kwargs are ignored
    """
    if db_path is None:
        db_path = _default_db_path()
    conn = _connect_calendar_db(db_path)
    c = conn.cursor()
    base_query = 'SELECT id,title,start,end,metadata FROM events ORDER BY id DESC'
    if all:
        c.execute(base_query)
    else:
        # defensive: ensure limit is int
        try:
            limit_val = int(limit)
        except Exception:
            limit_val = 100
        c.execute(base_query + ' LIMIT ?', (limit_val,))
    rows = c.fetchall()
    conn.close()
    events = []
    for r in rows:
        events.append({
            "id": r[0],
            "title": r[1] or "Untitled Event",
            "start": r[2] if r[2] else "Unspecified",
            "end": r[3] if r[3] else "Unspecified",
            "metadata": json.loads(r[4] or "{}")
        })
    return events

@register_tool(
    "calendar.delete_event",
    category="Calendar",
    description="Delete an event from the calendar by ID",
    example='{"tool": "calendar.delete_event", "args": {"id": 5}}'
)
def delete_event(id: int, db_path=None, **kwargs):
    """Delete an event from the calendar by ID."""
    if db_path is None:
        db_path = _default_db_path()
    
    conn = _connect_calendar_db(db_path)
    c = conn.cursor()
    c.execute('DELETE FROM events WHERE id = ?', (id,))
    count = c.rowcount
    conn.commit()
    conn.close()
    
    if count == 0:
        return {"status": "error", "error": f"No event found with ID {id}"}
    return {"status": "success", "message": f"Deleted event with ID {id}"}

@register_tool(
    "calendar.clear",
    category="Calendar",
    description="Wipe all events from the calendar",
    example='{"tool": "calendar.clear", "args": {}}'
)
def clear_calendar(db_path=None, **kwargs):
    """Wipe all events from the calendar."""
    if db_path is None:
        db_path = _default_db_path()
    
    conn = _connect_calendar_db(db_path)
    c = conn.cursor()
    c.execute('DELETE FROM events')
    count = c.rowcount
    conn.commit()
    conn.close()
    
    return {"status": "success", "message": f"Calendar cleared. Deleted {count} event(s)."}

@register_tool(
    "calendar.add_recurring_event",
    category="Calendar",
    description="Add a recurring event (daily, weekly, monthly) to the calendar with custom notification message",
    example='{"tool": "calendar.add_recurring_event", "args": {"title": "Wake up", "time": "06:00", "recurrence": "daily", "notification_message": "Good morning! Time to wake up!"}}'
)
def add_recurring_event(
    title: str,
    time: str,
    recurrence: str = "daily",
    notification_message: str = None,
    enabled: bool = True,
    db_path=None,
    **kwargs
):
    """
    Add a recurring event to the calendar.
    
    Args:
        title: Event title (e.g., "Wake up", "Breakfast")
        time: Time in HH:MM format (e.g., "06:00", "19:30")
        recurrence: "daily", "weekly", or "monthly"
        notification_message: Custom message to speak (optional, defaults to title)
        enabled: Whether event is active (default True)
        db_path: Database path (optional)
    
    Returns:
        dict with event ID
    """
    if db_path is None:
        db_path = _default_db_path()
    
    # Validate recurrence
    valid_recurrence = ["daily", "weekly", "monthly", "none"]
    if recurrence not in valid_recurrence:
        return {"status": "error", "error": f"Invalid recurrence. Must be one of: {valid_recurrence}"}
    
    # Validate time format (basic check)
    try:
        hour, minute = time.split(":")
        if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
            raise ValueError()
    except:
        return {"status": "error", "error": "Invalid time format. Use HH:MM (e.g., '06:00')"}
    
    # Use time as the start field (no date for recurring events)
    # Scheduler will calculate next occurrence dynamically
    conn = _connect_calendar_db(db_path)
    c = conn.cursor()
    c.execute(
        '''INSERT INTO events (title, start, end, metadata, recurrence, enabled, notification_message) 
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (
            title,
            time,  # Store time only
            None,  # No end time for recurring events
            json.dumps({"recurring": True}),
            recurrence,
            1 if enabled else 0,
            notification_message or f"Reminder: {title}"
        )
    )
    rowid = c.lastrowid
    conn.commit()
    conn.close()
    
    return {
        "status": "success",
        "id": rowid,
        "message": f"Created {recurrence} event '{title}' at {time}"
    }

@register_tool(
    "calendar.toggle_event",
    category="Calendar",
    description="Enable or disable a recurring event without deleting it",
    example='{"tool": "calendar.toggle_event", "args": {"id": 5, "enabled": false}}'
)
def toggle_event(id: int, enabled: bool, db_path=None, **kwargs):
    """Toggle an event's enabled status."""
    if db_path is None:
        db_path = _default_db_path()
    
    conn = _connect_calendar_db(db_path)
    c = conn.cursor()
    c.execute('UPDATE events SET enabled = ? WHERE id = ?', (1 if enabled else 0, id))
    count = c.rowcount
    conn.commit()
    conn.close()
    
    if count == 0:
        return {"status": "error", "error": f"No event found with ID {id}"}
    
    status_text = "enabled" if enabled else "disabled"
    return {"status": "success", "message": f"Event {id} {status_text}"}
