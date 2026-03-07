"""
Autonomous Scheduler Service for MIRA
Monitors calendar database and triggers voice notifications at scheduled times
"""
import sqlite3
import time
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent.parent))

from voice.gemini_live_tts import GeminiLiveVoice

class AutonomousScheduler:
    """
    Background service that monitors calendar events and triggers
    voice notifications autonomously.
    """
    
    def __init__(self, db_path: str = None, check_interval: int = 60, voice_mode: str = "gemini"):
        """
        Initialize the autonomous scheduler.
        
        Args:
            db_path: Path to calendar database (default: mcp_server/db.sqlite)
            check_interval: How often to check for due events (seconds, default: 60)
            voice_mode: "gemini" for Gemini Live API, "sapi" for Windows SAPI
        """
        if db_path is None:
            db_path = str(Path(__file__).parent.parent / "mcp_server" / "db.sqlite")
        
        self.db_path = db_path
        self.check_interval = check_interval
        self.voice_mode = voice_mode
        
        # Initialize voice system
        if voice_mode == "gemini":
            print("[SCHEDULER] Using Gemini Live API for voice notifications")
            try:
                self.voice = GeminiLiveVoice()
            except Exception as e:
                print(f"[SCHEDULER] Failed to initialize Gemini Live, falling back to SAPI: {e}")
                self.voice_mode = "sapi"
                self._init_sapi()
        else:
            print("[SCHEDULER] Using Windows SAPI for voice notifications")
            self._init_sapi()
        
        # Track last notification times to avoid duplicates
        self.last_notified = {}
        
        print(f"[SCHEDULER] Initialized with DB: {db_path}")
        print(f"[SCHEDULER] Check interval: {check_interval} seconds")
    
    def _init_sapi(self):
        """Initialize Windows SAPI TTS as fallback."""
        import win32com.client
        self.sapi_speaker = win32com.client.Dispatch("SAPI.SpVoice")
    
    def calculate_next_occurrence(self, event_time: str, recurrence: str) -> datetime:
        """
        Calculate the next occurrence time for a recurring event.
        
        Args:
            event_time: Time in HH:MM format (e.g., "06:00") OR full datetime for one-time events
            recurrence: "daily", "weekly", "monthly", or "none"
        
        Returns:
            datetime object for next occurrence
        """
        now = datetime.now()
        
        # Handle one-time events with full datetime
        if recurrence == "none" or recurrence is None:
            # Try multiple datetime formats
            formats = [
                "%Y-%m-%d %H:%M:%S",    # 2026-02-10 13:13:00
                "%Y-%m-%d %H:%M",        # 2026-02-10 13:13
                "%Y-%m-%dT%H:%M:%S",     # ISO format
                "%Y-%m-%dT%H:%M",        # ISO without seconds
                "%Y-%m-%d",              # Date only (midnight)
                "%H:%M:%S",              # Time only (today)
                "%H:%M",                 # Time only short (today)
            ]
            for fmt in formats:
                try:
                    parsed = datetime.strptime(event_time.strip(), fmt)
                    # If only time was parsed (no date), assume today
                    if fmt in ("%H:%M:%S", "%H:%M"):
                        parsed = now.replace(hour=parsed.hour, minute=parsed.minute, 
                                           second=parsed.second, microsecond=0)
                    return parsed
                except ValueError:
                    continue
            
            # Last resort: try to extract digits from the string
            import re
            time_match = re.search(r'(\d{1,2}):(\d{2})(?::(\d{2}))?', str(event_time))
            if time_match:
                h, m = int(time_match.group(1)), int(time_match.group(2))
                s = int(time_match.group(3)) if time_match.group(3) else 0
                return now.replace(hour=h, minute=m, second=s, microsecond=0)
            
            print(f"[SCHEDULER] Warning: Cannot parse one-time event time: {event_time}")
            return now + timedelta(days=99999)  # Far future = never trigger
        
        # Handle recurring events (time-only format like "06:00")
        try:
            hour, minute = map(int, event_time.split(":"))
        except (ValueError, AttributeError):
            print(f"[SCHEDULER] Warning: Invalid time format for recurring event: {event_time}")
            return now + timedelta(days=99999)  # Far future = never trigger
        
        if recurrence == "daily":
            # Today at the specified time
            next_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            
            # If time has passed today, schedule for tomorrow
            if next_time <= now:
                next_time += timedelta(days=1)
            
            return next_time
        
        elif recurrence == "weekly":
            # Same day next week
            next_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_time <= now:
                next_time += timedelta(weeks=1)
            return next_time
        
        elif recurrence == "monthly":
            # Same day next month (approximate)
            next_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_time <= now:
                # Add roughly 30 days
                next_time += timedelta(days=30)
            return next_time
        
        else:
            # Unknown recurrence type
            print(f"[SCHEDULER] Warning: Unknown recurrence type: {recurrence}")
            return now + timedelta(days=99999)  # Far future = never trigger
    
    def check_upcoming_events(self) -> list:
        """
        Check database for events that are due now (within 1-minute window).
        
        Returns:
            list of event dicts that should be notified
        """
        now = datetime.now()
        events_to_notify = []
        
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Get all enabled events
            cursor.execute("""
                SELECT id, title, start, recurrence, notification_message, enabled
                FROM events
                WHERE enabled = 1
            """)
            
            rows = cursor.fetchall()
            conn.close()
            
            for row in rows:
                event_id, title, start_time, recurrence, notification_msg, enabled = row
                
                if not recurrence:
                    recurrence = "none"
                
                # Calculate when this event should trigger
                try:
                    next_occurrence = self.calculate_next_occurrence(start_time, recurrence)
                except Exception as e:
                    print(f"[SCHEDULER] Error parsing event {event_id}: {e}")
                    continue
                
                # Check if event is due (within 1-minute window)
                time_diff = (next_occurrence - now).total_seconds()
                
                if 0 <= time_diff <= 60:
                    # Check if we already notified recently (avoid duplicates)
                    last_notif = self.last_notified.get(event_id)
                    if last_notif and (now - last_notif).total_seconds() < 120:
                        continue  # Skip, notified less than 2 minutes ago
                    
                    events_to_notify.append({
                        "id": event_id,
                        "title": title,
                        "time": start_time,
                        "recurrence": recurrence,
                        "notification_message": notification_msg or f"Reminder: {title}",
                        "next_occurrence": next_occurrence
                    })
                    
                    # Mark as notified
                    self.last_notified[event_id] = now
            
            return events_to_notify
            
        except Exception as e:
            print(f"[SCHEDULER] Error checking events: {e}")
            return []
    
    def auto_delete_event(self, event_id: int):
        """Delete a one-time event after it has been notified."""
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('DELETE FROM events WHERE id = ?', (event_id,))
            conn.commit()
            conn.close()
            print(f"[SCHEDULER] 🗑️ Auto-deleted one-time event ID {event_id}")
        except Exception as e:
            print(f"[SCHEDULER] Error auto-deleting event {event_id}: {e}")
    
    def trigger_notification(self, event: dict):
        """
        Trigger voice notification for an event.
        
        Args:
            event: Event dict with notification details
        """
        print(f"\n{'='*60}")
        print(f"[SCHEDULER] 🔔 NOTIFICATION TRIGGERED")
        print(f"[SCHEDULER] Event: {event['title']}")
        print(f"[SCHEDULER] Time: {event['time']}")
        print(f"[SCHEDULER] Message: {event['notification_message']}")
        print(f"{'='*60}\n")
        
        try:
            if self.voice_mode == "gemini":
                # Use Gemini Live API (conversational)
                self.voice.speak_notification_sync(event)
            else:
                # Use Windows SAPI (simple TTS)
                message = event['notification_message']
                # Repeat 2 times for emphasis
                for i in range(2):
                    self.sapi_speaker.Speak(message)
                    time.sleep(1)
        
        except Exception as e:
            print(f"[SCHEDULER] Error during notification: {e}")
            # Last resort fallback - system beep
            print("\a" * 3)  # System beep
    
    def run_forever(self):
        """
        Main scheduler loop - runs indefinitely checking for due events.
        """
        print(f"\n{'='*60}")
        print("🚀 MIRA AUTONOMOUS SCHEDULER STARTED")
        print(f"{'='*60}")
        print(f"Voice Mode: {self.voice_mode.upper()}")
        print(f"Database: {self.db_path}")
        print(f"Check Interval: {self.check_interval}s")
        print(f"{'='*60}\n")
        
        try:
            while True:
                # Check for due events
                events = self.check_upcoming_events()
                
                if events:
                    print(f"[SCHEDULER] Found {len(events)} event(s) to notify")
                    for event in events:
                        self.trigger_notification(event)
                        # Auto-delete one-time events after notification
                        if event.get('recurrence') in ('none', None, ''):
                            self.auto_delete_event(event['id'])
                else:
                    # Silent check (don't spam logs)
                    pass
                
                # Wait before next check
                time.sleep(self.check_interval)
                
        except KeyboardInterrupt:
            print("\n[SCHEDULER] Shutting down gracefully...")
            if hasattr(self, 'voice') and hasattr(self.voice, 'cleanup'):
                self.voice.cleanup()
        except Exception as e:
            print(f"[SCHEDULER] Fatal error: {e}")
            raise


def main():
    """Entry point for scheduler service."""
    import argparse
    
    parser = argparse.ArgumentParser(description="MIRA Autonomous Scheduler")
    parser.add_argument(
        "--voice-mode",
        choices=["gemini", "sapi"],
        default="gemini",
        help="Voice notification system to use"
    )
    parser.add_argument(
        "--check-interval",
        type=int,
        default=60,
        help="Seconds between calendar checks (default: 60)"
    )
    
    args = parser.parse_args()
    
    scheduler = AutonomousScheduler(
        voice_mode=args.voice_mode,
        check_interval=args.check_interval
    )
    
    scheduler.run_forever()


if __name__ == "__main__":
    main()
