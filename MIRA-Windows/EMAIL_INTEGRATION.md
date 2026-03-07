# MIRA Email Integration

## ✅ Implementation Complete

I've added comprehensive email tools to MIRA with both **Gmail API** (full features) and **SMTP** (fallback) support.

---

## 🛠️ Tools Added

### 1. **`email.send_job_application`**
Automates job applications using your template.
- **Sends immediately** when `send_now: true`
- **Creates draft** when `send_now: false` (default)
- **Tracks** applied companies in `applied-contacts.csv`

**Example Usage:**
```
You: "Apply to careers@techcorp.com for TechCorp"

MIRA executes:
{
  "tool": "email.send_job_application",
  "args": {
    "company_name": "TechCorp",
    "email": "careers@techcorp.com",
    "send_now": true
  }
}
```

### 2. **`email.send`**
Send any email (with Gmail API or SMTP).

**Example:**
```
You: "Send an email to john@company.com thanking him for the meeting"

MIRA executes:
{
  "tool": "email.send",
  "args": {
    "to": "john@company.com",
    "subject": "Thank you for the meeting",
    "body": "Hi John, Thanks for taking the time to meet with me today..."
  }
}
```

### 3. **`email.fetch`**
Fetch recent emails from inbox.

**Example:**
```
You: "Show me my last 3 emails"

MIRA executes:
{
  "tool": "email.fetch",
  "args": {
    "count": 3
  }
}
```

### 4. **`email.read`**
Read full email content by ID.

---

## 📋 Setup Required

### Quick Start (Gmail API - Recommended)

1. **Get OAuth Credentials:**
   - Visit: https://console.cloud.google.com/
   - Create project → Enable "Gmail API"
   - Create OAuth 2.0 credentials (Desktop app)
   - Download `credentials.json`
   - Place in: `MIRA-Windows/credentials.json`

2. **First Use:**
   - When MIRA calls an email tool, browser opens
   - Log in with Gmail → Grant permissions
   - `token.pickle` auto-created

### Alternative (SMTP - Send Only)

Use app password from Gmail:
1. Enable 2-Step Verification
2. Generate app password at: https://myaccount.google.com/apppasswords
3. Provide in tool args:
   ```json
   {
     "smtp_config": {
       "user": "your.email@gmail.com",
       "password": "app_password"
     }
   }
   ```

---

## 📂 Files Created

- [`mcp_server/connectors/email.py`](file:///c:/Users/lenovo/Downloads/MIRA-Deliverables/MIRA-Windows/mcp_server/connectors/email.py) - Email connector
- [`EMAIL_SETUP.md`](file:///c:/Users/lenovo/Downloads/MIRA-Deliverables/MIRA-Windows/EMAIL_SETUP.md) - Detailed setup guide

---

## 🚀 Next Steps

1. **Setup Gmail API** (follow guide in EMAIL_SETUP.md)
2. **Restart MCP server** to load new tools:
   ```bash
   python mcp_server/server.py
   ```
3. **Test via web UI** or voice:
   - "Fetch my last 5 emails"
   - "Send job application to careers@openai.com for OpenAI"

---

## 📝 Template Customization

The job application template is in `email.py` (line 21). You can modify:
- Company-specific customization
- Add more placeholders: `{company_name}`, etc.
- Change signature/links

---

## 🔒 Privacy

- All credentials stored locally (`token.pickle`, `credentials.json`)
- No cloud service used (direct Gmail API)
- SMTP option available if you prefer

**Dependencies installed:** `google-api-python-client`, `google-auth-oauthlib`, `pandas`
