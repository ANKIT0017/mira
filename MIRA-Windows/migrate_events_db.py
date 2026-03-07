"""
Database migration script for autonomous scheduler
Adds recurrence, enabled, and notification_message columns to events table
"""
import sqlite3
from pathlib import Path

# Use absolute path relative to this script's location
SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / "mcp_server" / "db.sqlite"

def migrate_events_table():
    """Add new columns for recurring events functionality"""
    print(f"[MIGRATION] Connecting to database: {DB_PATH}")
    
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    # Check if columns already exist
    cursor.execute("PRAGMA table_info(events)")
    columns = [column[1] for column in cursor.fetchall()]
    
    migrations_needed = []
    
    if 'recurrence' not in columns:
        migrations_needed.append(
            "ALTER TABLE events ADD COLUMN recurrence TEXT DEFAULT 'none'"
        )
    
    if 'enabled' not in columns:
        migrations_needed.append(
            "ALTER TABLE events ADD COLUMN enabled INTEGER DEFAULT 1"
        )
    
    if 'notification_message' not in columns:
        migrations_needed.append(
            "ALTER TABLE events ADD COLUMN notification_message TEXT"
        )
    
    if not migrations_needed:
        print("[MIGRATION] ✅ Database already up to date")
        conn.close()
        return True
    
    # Execute migrations
    print(f"[MIGRATION] Applying {len(migrations_needed)} migrations...")
    
    try:
        for migration in migrations_needed:
            print(f"[MIGRATION] Executing: {migration}")
            cursor.execute(migration)
        
        conn.commit()
        print("[MIGRATION] ✅ Migration completed successfully")
        
        # Verify
        cursor.execute("PRAGMA table_info(events)")
        new_columns = [column[1] for column in cursor.fetchall()]
        print(f"[MIGRATION] Table now has columns: {new_columns}")
        
        return True
        
    except Exception as e:
        print(f"[MIGRATION] ❌ Migration failed: {e}")
        conn.rollback()
        return False
        
    finally:
        conn.close()

if __name__ == "__main__":
    print("=" * 60)
    print("MIRA Database Migration - Autonomous Scheduler")
    print("=" * 60)
    migrate_events_table()
