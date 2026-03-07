"""
Email connector for MIRA - Gmail API & SMTP support
Provides job application automation and general email tools
"""
import os
import base64
import pandas as pd
import pickle
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import mimetypes
import smtplib

# Gmail API imports (optional - will fallback to SMTP if not available)
try:
    from googleapiclient.discovery import build
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    GMAIL_API_AVAILABLE = True
except ImportError:
    GMAIL_API_AVAILABLE = False
    print("[EMAIL] Gmail API not available, using SMTP only")

from ..tools import register_tool

# Configuration
SCOPES = ["https://www.googleapis.com/auth/gmail.compose", 
          "https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/gmail.send"]

# Job application template
JOB_APPLICATION_TEMPLATE = """
<p>
I've been following what <b>{company_name}</b> is building, and I genuinely like the direction you're taking. 
Work around AI, especially <b>GenAI and agentic systems</b>, is something I'm deeply interested in, and it strongly aligns with the kind of problems I enjoy working on.
</p>

<p>
Hi Team,
</p>

<p>
I wanted to personally reach out to check if there are any opportunities or upcoming openings for AI/ML roles at 
<b>{company_name}</b>. Over the past few years, I've been working hands-on in AI/ML, focusing on building 
<b>practical, production-ready systems</b>.
</p>

<p>
My experience ranges from freelancing for small businesses, where I built <b>agents and RAG pipelines that solved real operational problems</b>, to developing full-stack AI products.
One such product is <a href="https://social-media-4r9d.vercel.app/PublicPage"><b>AURANET</b></a>, a full-stack platform I built end-to-end, where I'm currently working on its recommendation system.
</p>

<p>
I've also built <a href="https://agenthire.in"><b>AgentHire</b></a>, a live agentic AI platform focused on solving 
<b>sales, operations, and customer support workflows</b> in enterprise environments. 
AgentHire can identify and engage leads, manage internal operational tasks, and handle customer support queries autonomously. 
It is currently <b>live and in use by two customers</b>, which gave me hands-on experience with real production constraints, reliability, and iteration based on user feedback.
</p>

<p>
Some of my recent work includes:
</p>

<ul>
  <li>Curating an AI/ML jobs dataset over four months (<a href="https://www.kaggle.com/datasets/ankit0017/ai-and-ml-job-postings-linkedin-and-indeed-2025">Kaggle dataset</a>)</li>
  <li>Building a <b>Ramayana Classifier chatbot</b></li>
  <li>Developing a <a href="https://github.com/ANKIT0017/Multi_Modal_using_Ollama"><b>multimodal AI system</b></a> using LangChain</li>
  <li>Creating an <b>X/Twitter Automation Agent</b> and other agentic workflows showcased in my <a href="https://projects-gallery-orpin.vercel.app/">projects gallery</a></li>
</ul>

<p>
From a technical perspective, I'm comfortable working with <b>LangChain, LangGraph, CrewAI, LlamaIndex, vector databases, RAG pipelines, and LLM fine-tuning</b>. 
I also have experience with Computer Vision, the MERN stack, SQL, Cloudinary, and Google Cloud, which allows me to build and deploy end-to-end AI features independently.
</p>

<p>
I'd genuinely love the opportunity to be part of <b>{company_name}</b>, contribute meaningfully to the AI team, and grow through real-world challenges. 
If there's a suitable role, or even guidance on the right next step, I'd really appreciate hearing from you.
</p>

<p>
Warm regards,<br><br>
<b>Ankit</b><br>
<a href="https://x.com/AnKit0017_">X / Twitter</a> | 
<a href="https://www.linkedin.com/in/ankit0017">LinkedIn</a> | 
<a href="https://github.com/ANKIT0017">GitHub</a>
</p>
"""


def gmail_authenticate():
    """Authenticate with Gmail API using OAuth"""
    if not GMAIL_API_AVAILABLE:
        raise ImportError("Gmail API libraries not installed. Run: pip install google-api-python-client google-auth-oauthlib")
    
    creds = None
    token_path = Path("token.pickle")
    creds_path = Path("credentials.json")
    
    if token_path.exists():
        with open(token_path, "rb") as token:
            creds = pickle.load(token)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                raise FileNotFoundError(
                    "credentials.json not found. Download from Google Cloud Console: "
                    "https://console.cloud.google.com/apis/credentials"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        
        with open(token_path, "wb") as token:
            pickle.dump(creds, token)
    
    return build("gmail", "v1", credentials=creds)


# SMTP Configuration — resolved lazily at call time so load_dotenv() timing doesn't matter.
# Set SMTP_USER + SMTP_PASS in your .env file (never hard-code credentials here).
def _smtp_user() -> str:  return os.environ.get("SMTP_USER", "")
def _smtp_pass() -> str:  return os.environ.get("SMTP_PASS", "")
def _smtp_host() -> str:  return os.environ.get("SMTP_HOST", "smtp.gmail.com")
def _smtp_port() -> int:  return int(os.environ.get("SMTP_PORT", "587"))

# Backward-compat: not used internally anymore (all call sites use _smtp_*() functions)

# SANDBOX directory — all file attachments must live here.
_SANDBOX = (Path(__file__).parent.parent.parent / "SANDBOX").resolve()


def _assert_in_sandbox(filepath: str) -> Path:
    """Raise ValueError if the resolved path is outside SANDBOX."""
    resolved = Path(filepath).resolve()
    # Allow bare filenames (pre-pend SANDBOX automatically)
    if not resolved.is_absolute():
        resolved = (_SANDBOX / filepath).resolve()
    try:
        resolved.relative_to(_SANDBOX)
    except ValueError:
        raise ValueError(
            f"Security: attachment path '{filepath}' is outside the SANDBOX directory. "
            f"Only files in {_SANDBOX} may be attached."
        )
    return resolved

def _send_via_smtp(to: str, subject: str, html_body: str, user: str = None, password: str = None, attachments: list = None):
    """Internal helper to send email via SMTP with optional file attachments"""
    # Resolve credentials at call-time from env so .env loading order doesn't matter
    user = user or _smtp_user()
    password = password or _smtp_pass()
    if not user or not password:
        raise ValueError(
            "SMTP credentials not configured. Set SMTP_USER and SMTP_PASS in your .env file."
        )
    try:
        print(f"[EMAIL] Connecting to {_smtp_host()}:{_smtp_port()}")
        msg = MIMEMultipart()
        msg["From"] = user
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html"))

        # Attach files if provided
        attached_files = []

        if attachments:
            for filepath in attachments:
                file_path = _assert_in_sandbox(filepath)
                if not file_path.exists():
                    raise FileNotFoundError(f"Attachment not found: {filepath}")
                mime_type, _ = mimetypes.guess_type(str(file_path))
                if mime_type is None:
                    mime_type = "application/octet-stream"
                main_type, sub_type = mime_type.split("/", 1)
                with open(file_path, "rb") as f:
                    part = MIMEBase(main_type, sub_type)
                    part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f"attachment; filename={file_path.name}")
                msg.attach(part)
                attached_files.append(file_path.name)
                print(f"[EMAIL] Attached: {file_path.name} ({mime_type})")

        server = smtplib.SMTP(_smtp_host(), _smtp_port())
        server.set_debuglevel(0)  # Set to 1 for verbose SMTP debugging
        print(f"[EMAIL] Starting TLS...")
        server.starttls()
        print(f"[EMAIL] Logging in as {user[:10]}...")
        server.login(user, password)
        print(f"[EMAIL] Sending to {to}...")
        server.send_message(msg)
        server.quit()
        print(f"[EMAIL] ✓ Successfully sent to {to}")
        result = {"status": "success", "to": to}
        if attached_files:
            result["attachments"] = attached_files
        return result
    except smtplib.SMTPAuthenticationError as e:
        error_msg = f"SMTP Authentication failed: {str(e)}"
        print(f"[EMAIL ERROR] {error_msg}")
        raise Exception(error_msg)
    except smtplib.SMTPException as e:
        error_msg = f"SMTP error: {str(e)}"
        print(f"[EMAIL ERROR] {error_msg}")
        raise Exception(error_msg)
    except Exception as e:
        error_msg = f"Email sending failed: {str(e)}"
        print(f"[EMAIL ERROR] {error_msg}")
        raise Exception(error_msg)

@register_tool(
    "email.send_job_application",
    category="Email",
    description="Send a specialized job application email using Ankit's template, with optional file attachments",
    example='{"tool": "email.send_job_application", "args": {"company_name": "Google", "email": "jobs@google.com", "send_now": true, "attachments": ["SANDBOX/resume.pdf"]}}',
    notes="⚠️ By default sends immediately. If send_now=false, creates a Gmail draft instead. Attachments: list of file paths."
)
def send_job_application(company_name: str, email: str, send_now: bool = True, attachments: list = None) -> dict:
    """
    Send or draft a job application email to a company.

    Args:
        company_name: Name of the company
        email: Email address to send to
        send_now: If True (default), sends immediately via SMTP. If False, creates Gmail draft.
        attachments: Optional list of file paths to attach (e.g. ["SANDBOX/resume.pdf"])

    Returns:
        Status message with draft/send confirmation
    """
    try:
        subject = f"Exploring AI / Agentic AI opportunities at {company_name}"
        email_body = JOB_APPLICATION_TEMPLATE.format(company_name=company_name)

        if send_now:
            # Send immediately via SMTP (with optional attachments)
            _send_via_smtp(email, subject, email_body, attachments=attachments)

            # Track in applied-contacts.csv
            _track_applied_contact(company_name, email)

            result = {
                "status": "sent",
                "method": "smtp",
                "to": email,
                "company": company_name
            }
            if attachments:
                result["attachments"] = [Path(p).name for p in attachments]
            return result
        else:
            # Create Gmail draft with optional attachments (requires OAuth)
            service = gmail_authenticate()
            message = MIMEMultipart()
            message["to"] = email
            message["subject"] = subject
            message.attach(MIMEText(email_body, "html"))

            if attachments:
                for filepath in attachments:
                    try:
                        file_path = _assert_in_sandbox(filepath)
                    except ValueError as e:
                        return {"status": "error", "error": str(e)}
                    if not file_path.exists():
                        return {"status": "error", "error": f"Attachment not found: {filepath}"}
                    mime_type, _ = mimetypes.guess_type(str(file_path))
                    if mime_type is None:
                        mime_type = "application/octet-stream"
                    main_type, sub_type = mime_type.split("/", 1)
                    with open(file_path, "rb") as f:
                        part = MIMEBase(main_type, sub_type)
                        part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header("Content-Disposition", f"attachment; filename={file_path.name}")
                    message.attach(part)

            raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
            body = {"message": {"raw": raw}}
            draft = service.users().drafts().create(userId="me", body=body).execute()
            result = {
                "status": "draft_created",
                "draft_id": draft["id"],
                "to": email,
                "company": company_name
            }
            if attachments:
                result["attachments"] = [Path(p).name for p in attachments]
            return result

    except Exception as e:
        return {"status": "error", "error": str(e)}


@register_tool(
    "email.send",
    category="Email",
    description="Send a general email to any recipient, optionally with file attachments",
    example='{"tool": "email.send", "args": {"to": "hr@company.com", "subject": "Report", "body": "Please find attached.", "attachments": ["SANDBOX/report.txt"]}}',
    notes="Attachments: pass list of file paths (e.g. SANDBOX/file.txt). Uses SMTP credentials from env."
)
def send_email(to: str, subject: str, body: str, html: bool = False, attachments: list = None, smtp_config: dict = None) -> dict:
    """
    Send an email with optional file attachments.
    
    Args:
        to: Recipient email address
        subject: Email subject
        body: Email body (plain text or HTML)
        html: If True, treats body as HTML
        attachments: Optional list of file paths to attach
        smtp_config: Optional SMTP settings {"user": "...", "password": "..."}
    
    Returns:
        Status message
    """
    try:
        user = smtp_config.get("user", _smtp_user()) if smtp_config else _smtp_user()
        password = smtp_config.get("password", _smtp_pass()) if smtp_config else _smtp_pass()
        
        msg = MIMEMultipart()
        msg["From"] = user
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "html" if html else "plain"))
        
        # Attach files if provided
        attached_files = []
        if not user or not password:
            return {"status": "error", "error": "SMTP credentials not configured. Set SMTP_USER and SMTP_PASS in .env."}

        if attachments:
            for filepath in attachments:
                try:
                    file_path = _assert_in_sandbox(filepath)
                except ValueError as e:
                    return {"status": "error", "error": str(e)}
                if not file_path.exists():
                    return {"status": "error", "error": f"Attachment not found: {filepath}"}
                
                # Guess MIME type
                mime_type, _ = mimetypes.guess_type(str(file_path))
                if mime_type is None:
                    mime_type = "application/octet-stream"
                main_type, sub_type = mime_type.split("/", 1)
                
                # Read and encode file
                with open(file_path, "rb") as f:
                    part = MIMEBase(main_type, sub_type)
                    part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    f"attachment; filename={file_path.name}"
                )
                msg.attach(part)
                attached_files.append(file_path.name)
                print(f"[EMAIL] Attached: {file_path.name} ({mime_type})")
        
        server = smtplib.SMTP(_smtp_host(), _smtp_port())
        server.starttls()
        server.login(user, password)
        server.send_message(msg)
        server.quit()
        
        result = {
            "status": "sent",
            "method": "smtp",
            "to": to
        }
        if attached_files:
            result["attachments"] = attached_files
        return result
            
    except Exception as e:
        return {"status": "error", "error": str(e)}


@register_tool(
    "email.create_draft",
    category="Email",
    description="Save an email as a draft in the Gmail Drafts folder (uses IMAP; no OAuth required)",
    example='{"tool": "email.create_draft", "args": {"to": "hr@company.com", "subject": "Follow-up", "body": "Dear Team..."}}',
    notes="Uses IMAP APPEND with your SMTP credentials (SMTP_USER / SMTP_PASS in .env). Gmail must have IMAP enabled in Settings → Forwarding and POP/IMAP."
)
def create_draft(to: str, subject: str, body: str, html: bool = True) -> dict:
    """
    Save an email as a Gmail draft via IMAP APPEND.

    Does NOT require OAuth / credentials.json — uses the same SMTP_USER / SMTP_PASS
    already configured in .env.  Gmail must have IMAP access enabled
    (Gmail Settings → See all settings → Forwarding and POP/IMAP → Enable IMAP).

    Args:
        to: Recipient email address
        subject: Email subject
        body: Email body (plain text or HTML)
        html: If True, treats body as HTML (default: True)

    Returns:
        Status and confirmation message
    """
    import imaplib
    import email.utils as _eu
    import time

    user = _smtp_user()
    password = _smtp_pass()

    if not user or not password:
        return {
            "status": "error",
            "error": "SMTP credentials not configured. Set SMTP_USER and SMTP_PASS in .env."
        }

    try:
        # Build the MIME message
        msg = MIMEMultipart()
        msg["From"] = user
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = _eu.formatdate(localtime=True)
        msg["Message-ID"] = _eu.make_msgid()
        msg.attach(MIMEText(body, "html" if html else "plain"))

        raw_bytes = msg.as_bytes()

        # Connect to Gmail IMAP over SSL
        imap_host = os.environ.get("IMAP_HOST", "imap.gmail.com")
        imap_port = int(os.environ.get("IMAP_PORT", "993"))

        print(f"[EMAIL-DRAFT] Connecting to {imap_host}:{imap_port} as {user[:10]}…")
        with imaplib.IMAP4_SSL(imap_host, imap_port) as imap:
            imap.login(user, password)

            # Gmail's Drafts folder name — try canonical name first, then localised
            for drafts_folder in ('[Gmail]/Drafts', 'INBOX.Drafts', 'Drafts'):
                result, _ = imap.select(drafts_folder)
                if result == 'OK':
                    break
            else:
                return {
                    "status": "error",
                    "error": (
                        "Could not find a Drafts folder on the IMAP server. "
                        "Tried: [Gmail]/Drafts, INBOX.Drafts, Drafts. "
                        "Ensure IMAP is enabled in Gmail settings."
                    )
                }

            # APPEND the message as a draft (\\Draft flag, no \\Seen flag)
            imap.append(
                drafts_folder,
                '(\\Draft)',
                imaplib.Time2Internaldate(time.time()),
                raw_bytes
            )

        print(f"[EMAIL-DRAFT] ✓ Draft saved for {to}")
        return {
            "status": "draft_created",
            "method": "imap",
            "to": to,
            "subject": subject,
            "message": "Draft saved to Gmail Drafts folder via IMAP."
        }

    except imaplib.IMAP4.error as e:
        return {
            "status": "error",
            "error": (
                f"IMAP error: {e}. "
                "Make sure IMAP is enabled in Gmail → Settings → Forwarding and POP/IMAP."
            )
        }
    except Exception as e:
        return {"status": "error", "error": f"Failed to create draft: {str(e)}"}


@register_tool(
    "email.fetch",
    category="Email",
    description="Fetch recent emails from the inbox via IMAP",
    example='{"tool": "email.fetch", "args": {"count": 10, "unread_only": true}}',
    notes="Returns snippets and IDs that can be used with email.read."
)
def fetch_emails(count: int = 5, unread_only: bool = False) -> dict:
    """
    Fetch recent emails from inbox using IMAP.
    
    Args:
        count: Number of emails to fetch (default: 5, max: 20)
        unread_only: If True, only fetch unread emails
    
    Returns:
        List of email summaries
    """
    import imaplib
    import email
    from email.header import decode_header
    
    try:
        # Connect to Gmail IMAP server
        print(f"[EMAIL] Connecting to IMAP server...")
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(_smtp_user(), _smtp_pass())
        mail.select("inbox")
        
        # Search for emails
        search_criteria = "UNSEEN" if unread_only else "ALL"
        status, messages = mail.search(None, search_criteria)
        
        if status != "OK":
            return {"emails": [], "count": 0}
        
        email_ids = messages[0].split()
        email_ids = email_ids[-min(count, 20):]  # Get most recent N emails
        
        emails = []
        for email_id in reversed(email_ids):  # Most recent first
            status, msg_data = mail.fetch(email_id, "(RFC822)")
            if status != "OK":
                continue
                
            msg = email.message_from_bytes(msg_data[0][1])
            
            # Decode subject
            subject = msg.get("Subject", "")
            if subject:
                decoded = decode_header(subject)[0]
                if isinstance(decoded[0], bytes):
                    subject = decoded[0].decode(decoded[1] or "utf-8")
                else:
                    subject = decoded[0]
            
            emails.append({
                "id": email_id.decode(),
                "from": msg.get("From", "Unknown"),
                "subject": subject or "(No Subject)",
                "date": msg.get("Date", ""),
                "snippet": msg.get_payload()[:200] if isinstance(msg.get_payload(), str) else ""
            })
        
        mail.close()
        mail.logout()
        
        return {"emails": emails, "count": len(emails)}
        
    except Exception as e:
        return {"status": "error", "error": f"IMAP fetch failed: {str(e)}"}


@register_tool(
    "email.read",
    category="Email",
    description="Read the full body content of a specific email",
    example='{"tool": "email.read", "args": {"email_id": "123"}}',
    notes="Requires a valid message ID from email.fetch."
)
def read_email(email_id: str) -> dict:
    """
    Read the full content of a specific email using IMAP.
    
    Args:
        email_id: The email message ID (from email.fetch)
    
    Returns:
        Full email details
    """
    import imaplib
    import email
    from email.header import decode_header
    
    try:
        # Connect to Gmail IMAP server
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(_smtp_user(), _smtp_pass())
        mail.select("inbox")
        
        # Fetch the email by ID
        status, msg_data = mail.fetch(email_id.encode(), "(RFC822)")
        if status != "OK":
            return {"status": "error", "error": f"Email ID {email_id} not found"}
        
        msg = email.message_from_bytes(msg_data[0][1])
        
        # Decode subject
        subject = msg.get("Subject", "")
        if subject:
            decoded = decode_header(subject)[0]
            if isinstance(decoded[0], bytes):
                subject = decoded[0].decode(decoded[1] or "utf-8")
            else:
                subject = decoded[0]
        
        # Extract body
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_payload(decode=True).decode()
                    break
        else:
            body = msg.get_payload(decode=True).decode()
        
        mail.close()
        mail.logout()
        
        return {
            "id": email_id,
            "from": msg.get("From", ""),
            "to": msg.get("To", ""),
            "subject": subject,
            "date": msg.get("Date", ""),
            "body": body
        }
        
    except Exception as e:
        return {"status": "error", "error": f"IMAP read failed: {str(e)}"}


def _track_applied_contact(company_name: str, email: str):
    """Helper to track applied contacts in CSV"""
    applied_file = Path("applied-contacts.csv")
    
    new_row = pd.DataFrame([{
        "company_name": company_name,
        "email": email,
        "applied_date": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
    }])
    
    if applied_file.exists():
        existing = pd.read_csv(applied_file)
        combined = pd.concat([existing, new_row], ignore_index=True)
        combined.drop_duplicates(subset=["email"], keep="last", inplace=True)
        combined.to_csv(applied_file, index=False)
    else:
        new_row.to_csv(applied_file, index=False)
