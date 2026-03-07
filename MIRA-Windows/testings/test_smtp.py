"""
Quick SMTP test (safe version).
Reads credentials from environment variables instead of hardcoding secrets.
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))


def test_smtp() -> None:
    print("Testing SMTP connection...")
    print(f"Host: {SMTP_HOST}:{SMTP_PORT}")

    if not SMTP_USER or not SMTP_PASS:
        print("Missing SMTP_USER / SMTP_PASS in environment.")
        return

    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = SMTP_USER
        msg["Subject"] = "MIRA SMTP Test"
        msg.attach(MIMEText("This is a test email from MIRA.", "plain"))

        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)
        server.quit()

        print(f"SUCCESS: email sent to {SMTP_USER}")

    except smtplib.SMTPAuthenticationError as exc:
        print(f"AUTH FAILED: {exc}")
    except Exception as exc:
        print(f"ERROR: {exc}")


if __name__ == "__main__":
    test_smtp()
