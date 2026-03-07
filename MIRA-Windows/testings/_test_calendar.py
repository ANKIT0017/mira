import sys
sys.path.insert(0, '.')

from mcp_server.tools import TOOL_METADATA, execute_tool
from mcp_server.connectors.calendar import clear_calendar

# 1. Check has_var_keyword flags
print("=== Tool signatures ===")
for name in ['system.time', 'calendar.add_event', 'calendar.list_events', 'todo.list']:
    m = TOOL_METADATA.get(name, {})
    hvk = m.get('has_var_keyword')
    params = list(m.get('parameters', {}).keys())
    print(f"  {name}: has_var_keyword={hvk}, params={params}")

# 2. Get current time
print("\n=== system.time ===")
t = execute_tool('system.time')
print(t)
in_10 = t['result']['in_10_min']
print("in_10_min:", in_10)

# 3. Clear + add event
print("\n=== calendar.add_event ===")
clear_calendar()
r = execute_tool('calendar.add_event', title='Drink water', start=in_10)
print(r)

# 4. List events
print("\n=== calendar.list_events ===")
events = execute_tool('calendar.list_events')
print(events)
