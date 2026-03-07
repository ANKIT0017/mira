# Firecrawl Self-Hosting Setup Guide

## 🎯 Overview

This guide walks you through setting up **self-hosted Firecrawl** for MIRA. Firecrawl enables powerful web scraping capabilities including:

- ✅ JavaScript-heavy websites (React, Vue, Angular)
- ✅ Recursive website crawling
- ✅ LLM-ready markdown conversion
- ✅ AI-powered structured data extraction
- ✅ URL discovery and mapping

---

## 📋 Prerequisites

### Required Software

1. **Docker Desktop for Windows**
   - Download: [https://www.docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop)
   - Minimum requirements:
     - Windows 10/11 Pro, Enterprise, or Education (64-bit)
     - WSL 2 enabled
     - 4GB RAM (8GB recommended)
     - 20GB free disk space

2. **Git** (optional, for cloning)
   - Download: [https://git-scm.com/download/win](https://git-scm.com/download/win)

---

## 🚀 Installation Steps

### Step 1: Install Docker Desktop

1. Download and install Docker Desktop from the link above
2. During installation, ensure **"Use WSL 2 instead of Hyper-V"** is checked
3. Restart your computer if prompted
4. Launch Docker Desktop
5. Wait for the Docker Engine to start (green light in bottom-left)

**Verify Installation**:
```powershell
docker --version
# Should show: Docker version 20.x.x or higher

docker-compose --version
# Should show: Docker Compose version v2.x.x or higher
```

---

### Step 2: Clone Firecrawl Repository

Open PowerShell and navigate to a suitable directory:

```powershell
# Navigate to parent directory
cd C:\Users\lenovo\Downloads\MIRA-Deliverables

# Clone Firecrawl
git clone https://github.com/mendableai/firecrawl.git

# Navigate into the repository
cd firecrawl
```

**Without Git**: Download ZIP from [https://github.com/mendableai/firecrawl](https://github.com/mendableai/firecrawl) and extract to `C:\Users\lenovo\Downloads\MIRA-Deliverables\firecrawl`

---

### Step 3: Configure Environment Variables

1. Navigate to the API directory:
   ```powershell
   cd apps\api
   ```

2. Copy the example environment file:
   ```powershell
   copy .env.example .env
   ```

3. Edit `.env` file with Notepad or VS Code:
   ```powershell
   notepad .env
   ```

4. Update the following settings:

```env
# Required Settings
NUM_WORKERS_PER_QUEUE=8
PORT=3002
HOST=0.0.0.0

# Redis Configuration
REDIS_URL=redis://redis:6379
REDIS_RATE_LIMIT_URL=redis://redis:6379

# Authentication (disable for localhost testing)
USE_DB_AUTHENTICATION=false

# Optional: AI Extraction (requires OpenAI API key)
# OPENAI_API_KEY=sk-your-key-here

# Optional: Logging
LOG_LEVEL=info
```

> [!IMPORTANT]
> If you want to use the `firecrawl.extract` tool for AI-powered data extraction, you must add your `OPENAI_API_KEY` to the `.env` file.

5. Save and close the file
6. Navigate back to Firecrawl root:
   ```powershell
   cd ..\..
   ```

---

### Step 4: Build and Run Firecrawl

From the Firecrawl root directory:

```powershell
# Build Docker images (first time only, takes 5-10 minutes)
docker-compose build

# Start all services in detached mode
docker-compose up -d
```

**Expected Output**:
```
Creating network "firecrawl_default" with the default driver
Creating firecrawl_redis_1 ... done
Creating firecrawl_api_1   ... done
Creating firecrawl_worker_1 ... done
```

---

### Step 5: Verify Installation

**Check Container Status**:
```powershell
docker ps
```

You should see containers running:
- `firecrawl_api_1`
- `firecrawl_redis_1`
- `firecrawl_worker_1`

**Test API Health**:
```powershell
# Using PowerShell's Invoke-WebRequest
Invoke-WebRequest -Uri http://localhost:3002/health
```

**Expected Response**: `200 OK`

**Test Scraping**:
```powershell
# Create a test script
@"
{
  "url": "https://firecrawl.dev"
}
"@ | Out-File -Encoding utf8 test-scrape.json

# Send test request
Invoke-RestMethod -Uri http://localhost:3002/v1/scrape `
  -Method Post `
  -ContentType "application/json" `
  -InFile test-scrape.json
```

**Expected**: JSON response with markdown content

---

## 🔧 Managing Firecrawl

### Start Firecrawl
```powershell
cd C:\Users\lenovo\Downloads\MIRA-Deliverables\firecrawl
docker-compose up -d
```

### Stop Firecrawl
```powershell
docker-compose down
```

### View Logs
```powershell
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f api
```

### Restart After Changes
```powershell
docker-compose restart
```

### Update Firecrawl
```powershell
git pull
docker-compose build
docker-compose up -d
```

---

## 🔌 Integration with MIRA

Once Firecrawl is running, MIRA's connector will automatically use it when you:

1. Install the Python SDK:
   ```powershell
   cd C:\Users\lenovo\Downloads\MIRA-Deliverables\MIRA-Windows
   venv\Scripts\activate
   pip install firecrawl-py
   ```

2. The connector (`mcp_server/connectors/firecrawl.py`) connects to `http://localhost:3002`

3. Restart MIRA's MCP server to load the new tools:
   ```powershell
   python mcp_server\server.py
   ```

---

## 🐛 Troubleshooting

### Issue: Docker Desktop won't start

**Solution**:
1. Enable WSL 2:
   ```powershell
   wsl --install
   wsl --set-default-version 2
   ```
2. Restart computer
3. Launch Docker Desktop again

---

### Issue: Port 3002 already in use

**Solution**:
1. Find process using port:
   ```powershell
   netstat -ano | findstr :3002
   ```
2. Kill the process:
   ```powershell
   taskkill /PID <process_id> /F
   ```
3. Or change Firecrawl port in `.env`:
   ```env
   PORT=3003
   ```

---

### Issue: Redis connection failed

**Solution**:
1. Check Redis container is running:
   ```powershell
   docker ps | findstr redis
   ```
2. If not running, restart all services:
   ```powershell
   docker-compose down
   docker-compose up -d
   ```

---

### Issue: Container keeps restarting

**Solution**:
1. Check logs for errors:
   ```powershell
   docker-compose logs api
   ```
2. Common fixes:
   - Ensure `.env` file is in `apps/api/` directory
   - Verify no syntax errors in `.env`
   - Check Docker has enough memory (Settings → Resources → Memory: 4GB+)

---

### Issue: Scraping returns empty content

**Solution**:
- For AI extraction features, add `OPENAI_API_KEY` to `.env`
- Increase worker timeout in `.env`:
  ```env
  WORKER_TIMEOUT=300
  ```

---

## 📊 Resource Usage

**Typical Usage**:
- Memory: 1-2GB (idle), 3-4GB (active crawling)
- Disk: ~5GB (Docker images + cache)
- CPU: 10-30% (depends on crawl load)

**Optimization**:
- Adjust workers: `NUM_WORKERS_PER_QUEUE` (default: 8, reduce for lower memory)
- Clear cache periodically:
  ```powershell
  docker-compose down -v  # Warning: clears Redis cache
  ```

---

## 🔒 Security Notes

- Firecrawl runs on `localhost:3002` only (not exposed to internet)
- No authentication required for local testing (`USE_DB_AUTHENTICATION=false`)
- For production deployment, enable authentication and use environment-specific API keys

---

## 📚 Additional Resources

- **Firecrawl Documentation**: [https://docs.firecrawl.dev](https://docs.firecrawl.dev)
- **GitHub Repository**: [https://github.com/mendableai/firecrawl](https://github.com/mendableai/firecrawl)
- **Docker Documentation**: [https://docs.docker.com](https://docs.docker.com)

---

## ✅ Verification Checklist

- [ ] Docker Desktop installed and running
- [ ] Firecrawl repository cloned
- [ ] `.env` file configured in `apps/api/`
- [ ] `docker-compose up -d` executed successfully
- [ ] All containers running (`docker ps`)
- [ ] Health check returns 200 (`http://localhost:3002/health`)
- [ ] Test scrape successful
- [ ] `firecrawl-py` installed in MIRA's venv
- [ ] MIRA MCP server restarted

---

**Setup complete! 🎉 Firecrawl is ready to use with MIRA.**
