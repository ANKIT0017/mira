RUNBOOK - AION PoC
------------------
- Start the MCP server (Python): python mcp_server/server.py
- Start the UI (Python Flask): python ui/app.py
- Use the UI to send JSON payloads to the MCP server.

Example MCP payloads:
{"action":"list_dir","payload":{"path":"."}}
{"action":"read_file","payload":{"path":"mcp_server/server.py"}}
{"action":"add_event","payload":{"title":"Meeting X","start":"2025-11-02T09:00:00","end":"2025-11-02T10:00:00"}}
