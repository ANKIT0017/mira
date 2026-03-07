"""
Contacts management connector for MIRA
Provides persistent contact storage with search, tagging, and email integration.
"""
import sqlite3
from pathlib import Path
from datetime import datetime
from ..tools import register_tool

# Database file location - same directory as todos.db
DB_PATH = Path(__file__).parent.parent.parent / "contacts.db"


def _get_db():
    """Initialize database connection and create tables if needed."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT,
        phone TEXT,
        profession TEXT,
        company TEXT,
        category TEXT DEFAULT 'general',
        notes TEXT DEFAULT '',
        tags TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    # Full-text search index
    c.execute('''CREATE INDEX IF NOT EXISTS idx_contacts_name ON contacts(name)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_contacts_category ON contacts(category)''')
    conn.commit()
    return conn


def _row_to_dict(row):
    """Convert a sqlite3.Row to a dictionary."""
    return dict(row)


# --- Tools ---

@register_tool(
    "contacts.add",
    category="Contacts",
    description="Add a new contact with name, email, profession, company, etc.",
    example='{"tool": "contacts.add", "args": {"name": "John Doe", "email": "john@example.com", "profession": "Developer", "company": "Google", "category": "professional"}}'
)
def add_contact(name: str, email: str = None, phone: str = None,
                profession: str = None, company: str = None,
                category: str = "general", notes: str = "", tags: str = "") -> dict:
    """
    Add a new contact.
    
    Args:
        name: Full name (required)
        email: Email address
        phone: Phone number
        profession: Job title / profession
        company: Company / organization
        category: Category - 'professional', 'personal', 'recruiter', 'client', 'general'
        notes: Additional notes about this contact
        tags: Comma-separated tags (e.g., "ai, startup, delhi")
    
    Returns:
        Dict with new contact ID and confirmation
    """
    try:
        conn = _get_db()
        c = conn.cursor()
        c.execute(
            '''INSERT INTO contacts (name, email, phone, profession, company, category, notes, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (name, email, phone, profession, company, category.lower(), notes, tags.lower())
        )
        conn.commit()
        contact_id = c.lastrowid
        conn.close()
        
        return {
            "status": "success",
            "message": f"Contact '{name}' added",
            "id": contact_id,
            "name": name,
            "email": email
        }
    except Exception as e:
        return {"status": "error", "error": f"Failed to add contact: {str(e)}"}


@register_tool(
    "contacts.search",
    category="Contacts",
    description="Search contacts by name, email, profession, company, category, or tags",
    example='{"tool": "contacts.search", "args": {"query": "Google developer"}}'
)
def search_contacts(query: str = None, category: str = None, tag: str = None) -> dict:
    """
    Search contacts with flexible filtering.
    
    Args:
        query: Search text (matches name, email, profession, company, notes)
        category: Filter by category ('professional', 'personal', 'recruiter', 'client', 'general')
        tag: Filter by tag
    
    Returns:
        Dict with matching contacts
    """
    try:
        conn = _get_db()
        c = conn.cursor()
        
        conditions = []
        params = []
        
        if query:
            search = f"%{query}%"
            conditions.append(
                "(name LIKE ? OR email LIKE ? OR profession LIKE ? OR company LIKE ? OR notes LIKE ?)"
            )
            params.extend([search, search, search, search, search])
        
        if category:
            conditions.append("category = ?")
            params.append(category.lower())
        
        if tag:
            conditions.append("tags LIKE ?")
            params.append(f"%{tag.lower()}%")
        
        where = " AND ".join(conditions) if conditions else "1=1"
        
        c.execute(f'''SELECT * FROM contacts WHERE {where} ORDER BY name''', params)
        contacts = [_row_to_dict(row) for row in c.fetchall()]
        conn.close()
        
        return {
            "status": "ok",
            "contacts": contacts,
            "count": len(contacts),
            "query": query,
            "category": category
        }
    except Exception as e:
        return {"status": "error", "error": f"Search failed: {str(e)}"}


@register_tool(
    "contacts.list",
    category="Contacts",
    description="List all contacts, optionally filtered by category",
    example='{"tool": "contacts.list", "args": {"category": "professional"}}'
)
def list_contacts(category: str = None, limit: int = 50) -> dict:
    """
    List contacts with optional category filter.
    
    Args:
        category: Filter by category (optional)
        limit: Max number of contacts to return (default: 50)
    
    Returns:
        Dict with contact list
    """
    try:
        conn = _get_db()
        c = conn.cursor()
        
        if category:
            c.execute(
                '''SELECT * FROM contacts WHERE category = ? ORDER BY name LIMIT ?''',
                (category.lower(), limit)
            )
        else:
            c.execute('''SELECT * FROM contacts ORDER BY name LIMIT ?''', (limit,))
        
        contacts = [_row_to_dict(row) for row in c.fetchall()]
        conn.close()
        
        # Count by category
        conn = _get_db()
        c = conn.cursor()
        c.execute('''SELECT category, COUNT(*) as count FROM contacts GROUP BY category''')
        categories = {row["category"]: row["count"] for row in c.fetchall()}
        c.execute('''SELECT COUNT(*) as total FROM contacts''')
        total = c.fetchone()["total"]
        conn.close()
        
        return {
            "status": "ok",
            "contacts": contacts,
            "count": len(contacts),
            "total": total,
            "by_category": categories
        }
    except Exception as e:
        return {"status": "error", "error": f"List failed: {str(e)}"}


@register_tool(
    "contacts.get",
    category="Contacts",
    description="Get a specific contact by ID or exact name",
    example='{"tool": "contacts.get", "args": {"name": "John Doe"}}'
)
def get_contact(id: int = None, name: str = None) -> dict:
    """
    Get a specific contact.
    
    Args:
        id: Contact ID (optional, takes priority if both provided)
        name: Contact name to look up (optional)
    
    Returns:
        Dict with contact info
    """
    try:
        conn = _get_db()
        c = conn.cursor()
        
        if id:
            c.execute('''SELECT * FROM contacts WHERE id = ?''', (id,))
        elif name:
            c.execute('''SELECT * FROM contacts WHERE name LIKE ?''', (f"%{name}%",))
        else:
            return {"status": "error", "error": "Provide either 'id' or 'name'"}
        
        row = c.fetchone()
        conn.close()
        
        if row:
            return {"status": "ok", "contact": _row_to_dict(row)}
        return {"status": "error", "error": f"Contact not found"}
    except Exception as e:
        return {"status": "error", "error": f"Get failed: {str(e)}"}


@register_tool(
    "contacts.update",
    category="Contacts",
    description="Update an existing contact's details",
    example='{"tool": "contacts.update", "args": {"id": 1, "profession": "Senior Developer", "company": "Microsoft"}}'
)
def update_contact(id: int, name: str = None, email: str = None, phone: str = None,
                   profession: str = None, company: str = None, category: str = None,
                   notes: str = None, tags: str = None) -> dict:
    """
    Update a contact's details.
    
    Args:
        id: Contact ID (required)
        name: New name (optional)
        email: New email (optional)
        phone: New phone (optional)
        profession: New profession (optional)
        company: New company (optional)
        category: New category (optional)
        notes: New notes (optional)
        tags: New tags (optional)
    
    Returns:
        Dict with updated contact
    """
    try:
        conn = _get_db()
        c = conn.cursor()
        
        # Check contact exists
        c.execute('''SELECT * FROM contacts WHERE id = ?''', (id,))
        existing = c.fetchone()
        if not existing:
            conn.close()
            return {"status": "error", "error": f"Contact ID {id} not found"}
        
        # Build update
        updates = []
        params = []
        for field, value in [("name", name), ("email", email), ("phone", phone),
                              ("profession", profession), ("company", company),
                              ("category", category), ("notes", notes), ("tags", tags)]:
            if value is not None:
                updates.append(f"{field} = ?")
                params.append(value.lower() if field in ("category", "tags") else value)
        
        if not updates:
            conn.close()
            return {"status": "error", "error": "No fields to update"}
        
        updates.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(id)
        
        c.execute(f'''UPDATE contacts SET {", ".join(updates)} WHERE id = ?''', params)
        conn.commit()
        
        # Return updated contact
        c.execute('''SELECT * FROM contacts WHERE id = ?''', (id,))
        updated = _row_to_dict(c.fetchone())
        conn.close()
        
        return {"status": "success", "message": f"Contact updated", "contact": updated}
    except Exception as e:
        return {"status": "error", "error": f"Update failed: {str(e)}"}


@register_tool(
    "contacts.delete",
    category="Contacts",
    description="Delete a contact by ID",
    example='{"tool": "contacts.delete", "args": {"id": 3}}'
)
def delete_contact(id: int) -> dict:
    """
    Delete a contact.
    
    Args:
        id: Contact ID
    
    Returns:
        Dict with deletion confirmation
    """
    try:
        conn = _get_db()
        c = conn.cursor()
        
        c.execute('''SELECT name FROM contacts WHERE id = ?''', (id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return {"status": "error", "error": f"Contact ID {id} not found"}
        
        name = row["name"]
        c.execute('''DELETE FROM contacts WHERE id = ?''', (id,))
        conn.commit()
        conn.close()
        
        return {"status": "success", "message": f"Deleted contact '{name}'"}
    except Exception as e:
        return {"status": "error", "error": f"Delete failed: {str(e)}"}


@register_tool(
    "contacts.import_csv",
    category="Contacts",
    description="Bulk import contacts from a CSV file in SANDBOX",
    example='{"tool": "contacts.import_csv", "args": {"filepath": "SANDBOX/contacts.csv"}}'
)
def import_contacts_csv(filepath: str) -> dict:
    """
    Import contacts from a CSV file. Expected columns: name, email, phone, profession, company, category, tags
    
    Args:
        filepath: Path to CSV file (within SANDBOX)
    
    Returns:
        Dict with import results
    """
    import csv
    
    try:
        sandbox = Path(__file__).parent.parent.parent / "SANDBOX"
        file_path = Path(filepath.replace("SANDBOX/", "").replace("SANDBOX\\", ""))
        full_path = (sandbox / file_path).resolve()

        # SANDBOX enforcement: reject any path that escapes the SANDBOX tree
        try:
            full_path.relative_to(sandbox.resolve())
        except ValueError:
            return {"status": "error", "error": f"Security: CSV path '{filepath}' is outside the SANDBOX directory."}

        if not full_path.exists():
            return {"status": "error", "error": f"File not found in SANDBOX: {filepath}"}
        
        conn = _get_db()
        c = conn.cursor()
        imported = 0
        skipped = 0
        
        with open(full_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get('name', '').strip()
                if not name:
                    skipped += 1
                    continue
                
                c.execute(
                    '''INSERT INTO contacts (name, email, phone, profession, company, category, tags)
                       VALUES (?, ?, ?, ?, ?, ?, ?)''',
                    (
                        name,
                        row.get('email', '').strip() or None,
                        row.get('phone', '').strip() or None,
                        row.get('profession', '').strip() or None,
                        row.get('company', '').strip() or None,
                        row.get('category', 'general').strip().lower(),
                        row.get('tags', '').strip().lower()
                    )
                )
                imported += 1
        
        conn.commit()
        conn.close()
        
        return {
            "status": "success",
            "message": f"Imported {imported} contacts ({skipped} skipped)",
            "imported": imported,
            "skipped": skipped
        }
    except Exception as e:
        return {"status": "error", "error": f"Import failed: {str(e)}"}
