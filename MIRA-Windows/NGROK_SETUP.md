# MIRA Mobile Access with ngrok

## Overview
Access MIRA's web interface from your mobile device (or anywhere in the world) using ngrok while keeping your WebSocket server secure and local.

## Architecture
```
Mobile Device → ngrok → Flask UI (port 5000) → WebSocket (localhost:8765) → MIRA Agent
```

✅ **Mobile accesses Flask through ngrok**  
✅ **WebSocket stays local (secure)**  
✅ **No router configuration needed**

---

## Setup Steps

### 1. Install ngrok
1. Download from [ngrok.com/download](https://ngrok.com/download)
2. Extract and place `ngrok.exe` in a convenient location
3. Sign up for a free account at [ngrok.com](https://ngrok.com)

### 2. Authenticate ngrok
Run once to save your auth token:
```bash
ngrok config add-authtoken YOUR_AUTH_TOKEN
```
(Get your token from the [ngrok dashboard](https://dashboard.ngrok.com/get-started/your-authtoken))

### 3. Start MIRA
Run this batch script:
```bash
run_with_ngrok.bat
```

This will start:
- MCP Server (WebSocket on port 8765)
- Flask UI (port 5000)
- ngrok tunnel to Flask

### 4. Access from Mobile
After starting, you'll see output like:
```
Forwarding https://abc123.ngrok-free.app -> http://localhost:5000
```

**On your mobile device**, open:
```
https://abc123.ngrok-free.app
```

---

## Important Notes

⚠️ **WebSocket stays local** - The `ws://localhost:8765` connection is made by Flask on your laptop, NOT by your mobile device. This is correct and secure.

⚠️ **Free ngrok limits**:
- 1 online ngrok process
- URL changes on each restart (you can get a fixed URL with paid plan)
- 40 connections/minute

⚠️ **Security**: Anyone with your ngrok URL can access MIRA. For production use:
- Add authentication to Flask
- Use ngrok's password protection feature
- Or use a paid ngrok plan with fixed domains and access control

---

## Troubleshooting

### ngrok URL changes
Free tier generates a new URL each time. Copy the new URL from the terminal output.

### WebSocket connection failed
Make sure:
1. MCP server is running on port 8765
2. Flask app is running on port 5000
3. Both are running on the **same machine** as ngrok

### Slow response
- ngrok adds ~50-200ms latency (normal for free tier)
- Voice assistant may be too slow over mobile; use text chat instead

---

## Manual Steps (Alternative)

If you don't want to use the batch script:

**Terminal 1:**
```bash
cd c:\Users\lenovo\Downloads\MIRA-Deliverables\MIRA-Windows
python .\mcp_server\server.py
```

**Terminal 2:**
```bash
cd c:\Users\lenovo\Downloads\MIRA-Deliverables\MIRA-Windows
python .\ui\app.py
```

**Terminal 3:**
```bash
ngrok http 5000
```

Then copy the `https://` URL from ngrok's output and open it on your mobile.
