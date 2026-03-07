"""
Test script for autonomous scheduler
Creates a test recurring event and verifies the scheduler can detect it
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta

# Add parent to path
sys.path.append(str(Path(__file__).parent))

from mcp_server.connectors.calendar import add_recurring_event, list_events
from scheduler.autonomous_scheduler import AutonomousScheduler

def test_recurring_event_creation():
    """Test creating a recurring event."""
    print("="*60)
    print("TEST 1: Creating recurring event")
    print("="*60)
    
    # Calculate time 2 minutes from now for testing
    test_time = (datetime.now() + timedelta(minutes=2)).strftime("%H:%M")
    
    result = add_recurring_event(
        title="Test Wake Up",
        time=test_time,
        recurrence="daily",
        notification_message="This is a test notification from MIRA!"
    )
    
    print(f"Result: {result}")
    
    if result.get("status") == "success":
        print("✅ Event created successfully")
        return result["id"]
    else:
        print("❌ Failed to create event")
        return None

def test_event_listing():
    """Test listing events."""
    print("\n" + "="*60)
    print("TEST 2: Listing all events")
    print("="*60)
    
    # Direct database query for testing (bypass MCP wrapper)
    import sqlite3
    from pathlib import Path
    
    db_path = str(Path(__file__).parent / "mcp_server" / "db.sqlite")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, start, recurrence, notification_message FROM events")
    rows = cursor.fetchall()
    conn.close()
    
    print(f"Found {len(rows)} event(s):\n")
    for row in rows:
        event_id, title, time, recurrence, notif_msg = row
        print(f"  ID: {event_id}")
        print(f"  Title: {title}")
        print(f"  Time: {time}")
        print(f"  Recurrence: {recurrence or 'none'}")
        print(f"  Message: {notif_msg}")
        print(f"  ---")
    
    return len(rows) > 0

def test_scheduler_detection():
    """Test that scheduler can detect upcoming events."""
    print("\n" + "="*60)
    print("TEST 3: Scheduler event detection")
    print("="*60)
    
    scheduler = AutonomousScheduler(check_interval=10, voice_mode="sapi")
    
    print("Checking for upcoming events...")
    events = scheduler.check_upcoming_events()
    
    if events:
        print(f"✅ Scheduler detected {len(events)} upcoming event(s)")
        for event in events:
            print(f"  - {event['title']} at {event['time']}")
    else:
        print("ℹ️  No events due in the next minute")
    
    return True

def main():
    """Run all tests."""
    print("\n" + "="*60)
    print("  MIRA AUTONOMOUS SCHEDULER - TEST SUITE")
    print("="*60 + "\n")
    
    # Test 1: Create recurring event
    event_id = test_recurring_event_creation()
    
    # Test 2: List events
    test_event_listing()
    
    # Test 3: Scheduler detection
    test_scheduler_detection()
    
    print("\n" + "="*60)
    print("✅ All tests completed!")
    print("="*60)
    
    if event_id:
        print(f"\nNote: Test event #{event_id} created")
        print("You can delete it with:")
        print(f"  python -c \"from mcp_server.connectors.calendar import delete_event; delete_event({event_id})\"")
    
    print("\nTo test live notifications:")
    print("1. Run: python scheduler\\autonomous_scheduler.py --voice-mode sapi")
    print("2. Wait for the scheduled time (check added events above)")
    print("3. You should hear voice notification")

if __name__ == "__main__":
    main()
