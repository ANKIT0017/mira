# Email Integration Setup Guide

## Features
MIRA now has comprehensive email capabilities:

### 1. Job Application Automation
- **Tool**: `email.send_job_application`
- Automatically sends personalized job applications
- Uses your pre-defined HTML template
- Tracks applied companies in `applied-contacts.csv`

### 2. General Email Tools
- **`email.send`**: Send any email (Gmail API or SMTP)
- **`email.fetch`**: Get last N emails from inbox
- **`email.read`**: Read full email content

---

## Setup Instructions

### Option A: Gmail API (Recommended - Full Features)

1. **Enable Gmail API**:
   - Go to [Google Cloud Console](https://console.cloud.google.com/)
   - Create a new project
   - Enable "Gmail API"
   - Go to "Credentials" → "Create Credentials" → "OAuth 2.0 Client ID"
   - Choose "Desktop app"
   - Download `credentials.json`
   - Place `credentials.json` in the MIRA-Windows folder

2. **First Run**:
   - When you first use an email tool, a browser will open
   - Log in with your Gmail account
   - Grant permissions
   - A `token.pickle` file will be created for future use

### Option B: SMTP (Simple - Send Only)

For sending emails without OAuth setup, use SMTP with app password:

1. **Enable 2-Step Verification** on your Gmail
2. **Generate App Password**:
   - Go to [Google Account Security](https://myaccount.google.com/security)
   - "2-Step Verification" → "App passwords"
   - Generate password for "Mail"
3. **Use in tool calls** (example):
   ```json
   {
     "tool": "email.send",
     "args": {
       "to": "someone@example.com",
       "subject": "Test",
       "body": "Hello",
       "smtp_config": {
         "user": "your.email@gmail.com",
         "password": "your_app_password"
       }
     }
   }
   ```

---

## Usage Examples

### Send Job Application
```
User: send a job application to careers@company.com for TechCorp

MIRA will call:
{
  "tool": "email.send_job_application",
  "args": {
    "company_name": "TechCorp",
    "email": "careers@company.com",
    "send_now": true
  }
}
```

### Fetch Recent Emails
```
User: show me my last 5 emails

MIRA will call:
{
  "tool": "email.fetch",
  "args": {
    "count": 5
  }
}
```

### Send Custom Email
```
User: send an email to john@example.com saying thanks for the meeting

MIRA will call:
{
  "tool": "email.send",
  "args": {
    "to": "john@example.com",
    "subject": "Thank you",
    "body": "Thanks for the meeting today!"
  }
}
```

---

## Configuration Files

- **`credentials.json`**: Gmail API OAuth credentials (download from Google Cloud)
- **`token.pickle`**: Auto-generated after first OAuth login
- **`contacts.csv`**: (Optional) Bulk job application contacts
  - Columns: `company_name`, `email`
- **`applied-contacts.csv`**: Auto-tracked applied companies

---

## Notes

- Gmail API has generous free quotas (1 billion requests/day)
- SMTP is simpler but less feature-rich (can't fetch emails)
- Job application template can be customized in `mcp_server/connectors/email.py`
- All sent emails appear in your Gmail "Sent" folder
