"""
Todo list management connector for MIRA
Provides persistent task tracking using SQLite
"""
import sqlite3
from pathlib import Path
from datetime import datetime
from ..tools import register_tool

# Database file location
DB_PATH = Path(__file__).parent.parent.parent / "todos.db"

def _get_db():
    """Initialize database connection and create tables if needed."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row  # Enable dict-like access
    
    # Create table if not exists
    db.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            priority TEXT DEFAULT 'medium',
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            due_date DATE
        )
    ''')
    db.commit()
    return db


def _normalize_priority(priority: str = "medium") -> str:
    p = str(priority or "medium").strip().lower()
    aliases = {
        "urgent": "high",
        "critical": "high",
        "important": "high",
        "normal": "medium",
        "med": "medium",
        "default": "medium",
        "minor": "low",
    }
    return aliases.get(p, p)


@register_tool(
    "todo.add",
    category="Todo List",
    description="Add one new task to the persistent todo list",
    example='{"tool": "todo.add", "args": {"title": "Follow up on application", "priority": "high", "due_date": "2026-02-01"}}',
    notes="Adds one task. Priority is normalized case-insensitively to 'low', 'medium', or 'high'. Use todo.add_many for multiple tasks."
)
def add_task(title: str, description: str = "", priority: str = "medium", due_date: str = None) -> dict:
    """
    Add a new task to the todo list.
    
    Args:
        title: Task title (required)
        description: Detailed description (optional)
        priority: Task priority - 'low', 'medium', or 'high' (default: 'medium')
        due_date: Due date in YYYY-MM-DD format (optional)
    
    Returns:
        Dict with task ID and confirmation
    """
    try:
        priority = _normalize_priority(priority)
        # Validate priority
        if priority not in ['low', 'medium', 'high']:
            return {"status": "error", "error": "Priority must be 'low', 'medium', or 'high'"}
        
        db = _get_db()
        cursor = db.execute(
            "INSERT INTO tasks (title, description, priority, due_date) VALUES (?, ?, ?, ?)",
            (title, description, priority, due_date)
        )
        db.commit()
        task_id = cursor.lastrowid
        db.close()
        
        print(f"[TODO] Added task #{task_id}: {title}")
        
        return {
            "id": task_id,
            "title": title,
            "priority": priority,
            "status": "pending",
            "message": f"Task added successfully"
        }
        
    except Exception as e:
        return {"status": "error", "error": f"Failed to add task: {str(e)}"}


@register_tool(
    "todo.add_many",
    category="Todo List",
    description="Add multiple tasks to the persistent todo list in one call",
    example='{"tool": "todo.add_many", "args": {"tasks": [{"title": "Set SMART goals", "priority": "high", "due_date": "2026-05-29"}, {"title": "List tasks", "priority": "medium"}]}}',
    notes="Use for batch todo creation. Each task supports title, description, priority, and due_date. Priority is case-insensitive."
)
def add_many_tasks(tasks: list) -> dict:
    """
    Add multiple tasks atomically enough for agent workflows.

    Args:
        tasks: List of task dicts with title, description, priority, due_date.

    Returns:
        Dict with created task records and count.
    """
    if not isinstance(tasks, list) or not tasks:
        return {"status": "error", "error": "tasks must be a non-empty list"}

    clean = []
    for idx, item in enumerate(tasks, start=1):
        if not isinstance(item, dict):
            return {"status": "error", "error": f"Task #{idx} must be an object"}
        title = str(item.get("title") or "").strip()
        if not title:
            return {"status": "error", "error": f"Task #{idx} missing required title"}
        priority = _normalize_priority(item.get("priority") or "medium")
        if priority not in ['low', 'medium', 'high']:
            return {"status": "error", "error": f"Task #{idx}: Priority must be 'low', 'medium', or 'high'"}
        clean.append({
            "title": title,
            "description": str(item.get("description") or "").strip(),
            "priority": priority,
            "due_date": item.get("due_date") or None,
        })

    try:
        db = _get_db()
        created = []
        for item in clean:
            cursor = db.execute(
                "INSERT INTO tasks (title, description, priority, due_date) VALUES (?, ?, ?, ?)",
                (item["title"], item["description"], item["priority"], item["due_date"])
            )
            created.append({
                "id": cursor.lastrowid,
                "title": item["title"],
                "priority": item["priority"],
                "status": "pending",
                "due_date": item["due_date"],
            })
        db.commit()
        db.close()
        print(f"[TODO] Added {len(created)} tasks")
        return {
            "status": "success",
            "count": len(created),
            "tasks": created,
            "message": f"Added {len(created)} tasks successfully",
        }
    except Exception as e:
        return {"status": "error", "error": f"Failed to add tasks: {str(e)}"}


@register_tool(
    "todo.list",
    category="Todo List",
    description="List and filter tasks from the todo list",
    example='{"tool": "todo.list", "args": {"status": "pending", "priority": "high"}}'
)
def list_tasks(status: str = None, priority: str = None, limit: int = 100, **kwargs) -> dict:
    """
    List tasks with optional filtering.
    
    Args:
        status: Filter by status - 'pending' or 'completed' (optional, shows all if not specified)
        priority: Filter by priority - 'low', 'medium', or 'high' (optional)
    
    Returns:
        Dict with list of tasks and count
    """
    try:
        db = _get_db()
        
        # Build query with filters
        query = "SELECT * FROM tasks WHERE 1=1"
        params = []
        
        if status:
            query += " AND status = ?"
            params.append(status)
        
        if priority:
            query += " AND priority = ?"
            params.append(priority)
        
        query += " ORDER BY created_at DESC"
        try:
            limit_value = int(limit)
        except Exception:
            limit_value = 100
        if limit_value < 1:
            limit_value = 1
        query += " LIMIT ?"
        params.append(limit_value)
        
        cursor = db.execute(query, params)
        tasks = [dict(row) for row in cursor.fetchall()]
        db.close()
        
        print(f"[TODO] Listed {len(tasks)} tasks")
        
        return {
            "tasks": tasks,
            "count": len(tasks)
        }
        
    except Exception as e:
        return {"status": "error", "error": f"Failed to list tasks: {str(e)}"}


@register_tool(
    "todo.complete",
    category="Todo List",
    description="Mark a specific task as completed",
    example='{"tool": "todo.complete", "args": {"id": 5}}'
)
def complete_task(id: int) -> dict:
    """
    Mark a task as completed.
    
    Args:
        id: Task ID
    
    Returns:
        Dict with confirmation
    """
    try:
        db = _get_db()
        
        # Check if task exists
        task = db.execute("SELECT * FROM tasks WHERE id = ?", (id,)).fetchone()
        if not task:
            db.close()
            return {"status": "error", "error": f"Task #{id} not found"}
        
        # Update task
        db.execute(
            "UPDATE tasks SET status = 'completed', completed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (id,)
        )
        db.commit()
        db.close()
        
        print(f"[TODO] Completed task #{id}")
        
        return {
            "id": id,
            "title": task['title'],
            "status": "completed",
            "message": f"Task marked as completed"
        }
        
    except Exception as e:
        return {"status": "error", "error": f"Failed to complete task: {str(e)}"}


@register_tool(
    "todo.complete_all",
    category="Todo List",
    description="Mark all pending tasks as completed",
    example='{"tool": "todo.complete_all", "args": {"status": "pending"}}'
)
def complete_all_tasks(status: str = "pending") -> dict:
    """
    Mark all tasks with a given status as completed.
    
    Args:
        status: Status filter - which tasks to complete (default: 'pending')
    
    Returns:
        Dict with count of completed tasks
    """
    try:
        db = _get_db()
        
        # Get all tasks with the given status
        cursor = db.execute("SELECT id, title FROM tasks WHERE status = ?", (status,))
        tasks = cursor.fetchall()
        
        if not tasks:
            db.close()
            return {
                "count": 0,
                "message": f"No {status} tasks to complete"
            }
        
        # Mark all as completed
        db.execute(
            "UPDATE tasks SET status = 'completed', completed_at = CURRENT_TIMESTAMP WHERE status = ?",
            (status,)
        )
        db.commit()
        db.close()
        
        count = len(tasks)
        print(f"[TODO] Completed {count} tasks")
        
        return {
            "count": count,
            "message": f"Marked {count} task(s) as completed"
        }
        
    except Exception as e:
        return {"status": "error", "error": f"Failed to complete tasks: {str(e)}"}


@register_tool(
    "todo.delete",
    category="Todo List",
    description="Permanently delete a task from the list",
    example='{"tool": "todo.delete", "args": {"id": 3}}'
)
def delete_task(id: int) -> dict:
    """
    Delete a task from the todo list.
    
    Args:
        id: Task ID
    
    Returns:
        Dict with confirmation
    """
    try:
        db = _get_db()
        
        # Check if task exists
        task = db.execute("SELECT * FROM tasks WHERE id = ?", (id,)).fetchone()
        if not task:
            db.close()
            return {"status": "error", "error": f"Task #{id} not found"}
        
        # Delete task
        db.execute("DELETE FROM tasks WHERE id = ?", (id,))
        db.commit()
        db.close()
        
        print(f"[TODO] Deleted task #{id}")
        
        return {
            "id": id,
            "title": task['title'],
            "message": f"Task deleted successfully"
        }
        
    except Exception as e:
        return {"status": "error", "error": f"Failed to delete task: {str(e)}"}


@register_tool(
    "todo.update",
    category="Todo List",
    description="Update an existing task's details",
    example='{"tool": "todo.update", "args": {"id": 1, "title": "Updated title", "priority": "medium"}}'
)
def update_task(id: int, title: str = None, description: str = None, priority: str = None, due_date: str = None) -> dict:
    """
    Update task details.
    
    Args:
        id: Task ID (required)
        title: New title (optional)
        description: New description (optional)
        priority: New priority - 'low', 'medium', or 'high' (optional)
        due_date: New due date in YYYY-MM-DD format (optional)
    
    Returns:
        Dict with updated task info
    """
    try:
        db = _get_db()
        
        # Check if task exists
        task = db.execute("SELECT * FROM tasks WHERE id = ?", (id,)).fetchone()
        if not task:
            db.close()
            return {"status": "error", "error": f"Task #{id} not found"}
        
        # Build update query
        updates = []
        params = []
        
        if title is not None:
            updates.append("title = ?")
            params.append(title)
        if description is not None:
            updates.append("description = ?")
            params.append(description)
        if priority is not None:
            priority = _normalize_priority(priority)
            if priority not in ['low', 'medium', 'high']:
                db.close()
                return {"status": "error", "error": "Priority must be 'low', 'medium', or 'high'"}
            updates.append("priority = ?")
            params.append(priority)
        if due_date is not None:
            updates.append("due_date = ?")
            params.append(due_date)
        
        if not updates:
            db.close()
            return {"status": "error", "error": "No fields to update"}
        
        # Execute update
        query = f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?"
        params.append(id)
        db.execute(query, params)
        db.commit()
        
        # Get updated task
        updated_task = db.execute("SELECT * FROM tasks WHERE id = ?", (id,)).fetchone()
        db.close()
        
        print(f"[TODO] Updated task #{id}")
        
        return {
            "id": id,
            "task": dict(updated_task),
            "message": "Task updated successfully"
        }
        
    except Exception as e:
        return {"status": "error", "error": f"Failed to update task: {str(e)}"}


@register_tool(
    "todo.search",
    category="Todo List",
    description="Search tasks by keyword in title or description",
    example='{"tool": "todo.search", "args": {"query": "application"}}'
)
def search_tasks(query: str) -> dict:
    """
    Search tasks by title or description.
    
    Args:
        query: Search query string
    
    Returns:
        Dict with matching tasks
    """
    try:
        db = _get_db()
        
        # Search in title and description
        cursor = db.execute(
            "SELECT * FROM tasks WHERE title LIKE ? OR description LIKE ? ORDER BY created_at DESC",
            (f"%{query}%", f"%{query}%")
        )
        tasks = [dict(row) for row in cursor.fetchall()]
        db.close()
        
        print(f"[TODO] Found {len(tasks)} tasks matching '{query}'")
        
        return {
            "query": query,
            "tasks": tasks,
            "count": len(tasks)
        }
        
    except Exception as e:
        return {"status": "error", "error": f"Search failed: {str(e)}"}


@register_tool(
    "todo.clear",
    category="Todo List",
    description="Delete ALL tasks permanently (or all tasks with a given status). Use this when asked to 'remove all tasks'.",
    example='{"tool": "todo.clear", "args": {}}  OR  {"tool": "todo.clear", "args": {"status": "pending"}}',
    notes="Omit status to delete everything. Pass status='pending' or status='completed' to only delete that subset."
)
def clear_tasks(status: str = None) -> dict:
    """
    Delete tasks from the todo list in bulk.

    Args:
        status: Optional filter — 'pending' or 'completed'. Omit to delete ALL tasks.

    Returns:
        Dict with count of deleted tasks.
    """
    try:
        db = _get_db()

        if status:
            count = db.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (status,)).fetchone()[0]
        else:
            count = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

        if count == 0:
            db.close()
            label = f"{status} " if status else ""
            return {"count": 0, "message": f"No {label}tasks found — nothing to delete"}

        if status:
            db.execute("DELETE FROM tasks WHERE status = ?", (status,))
        else:
            db.execute("DELETE FROM tasks")

        db.commit()
        db.close()

        label = f"{status} " if status else ""
        print(f"[TODO] Cleared {count} {label}tasks")
        return {
            "count": count,
            "message": f"Deleted {count} {label}task(s) successfully"
        }

    except Exception as e:
        return {"status": "error", "error": f"Failed to clear tasks: {str(e)}"}
