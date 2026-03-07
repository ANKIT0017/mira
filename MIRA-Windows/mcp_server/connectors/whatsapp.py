"""
WhatsApp connector for MIRA — Baileys bridge client
Talks to the local whatsapp_bridge Node.js sidecar (port 9001).
"""
import os
import requests
from ..tools import register_tool

BRIDGE_URL = "http://localhost:9001"
_SESSION    = requests.Session()


def _normalize_number(to: str) -> str:
    """
    Normalise a phone number to international format (digits only, no +).
    Rules applied in order:
      1. Strip whitespace, '+', '-', '(', ')', '.'
      2. If exactly 10 digits starting with 6-9 → Indian mobile → prepend '91'
      3. Everything else is returned as-is (group JIDs like 120363…@g.us are untouched)
    """
    if "@" in to:          # group JID or full JID — leave alone
        return to
    cleaned = to.strip().lstrip("+").replace(" ", "").replace("-", "").replace("(", "").replace(")", "").replace(".", "")
    if len(cleaned) == 10 and cleaned[0] in "6789":
        return "91" + cleaned
    return cleaned


def _bridge(method: str, path: str, **kwargs) -> dict:
    """Make a request to the Baileys bridge. Returns dict always."""
    try:
        resp = _SESSION.request(method, f"{BRIDGE_URL}{path}", timeout=15, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.ConnectionError:
        return {
            "status": "error",
            "error":  (
                "WhatsApp bridge is not running. "
                "Start it with:  cd whatsapp_bridge && npm install && npm start"
            )
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ── Tools ──────────────────────────────────────────────────────────────

@register_tool(
    "whatsapp.send",
    category="WhatsApp",
    description="Send a WhatsApp text message to a phone number or group JID",
    example='{"tool": "whatsapp.send", "args": {"to": "919876543210", "message": "Hello from MIRA!"}}',
    notes=(
        "to: phone number with country code (no + or spaces). "
        "10-digit Indian numbers (starting with 6-9) are auto-normalized to include country code 91. "
        "For groups use the full JID like 120363XXXX@g.us. "
        "WhatsApp bridge must be running and connected."
    )
)
def send_message(to: str, message: str = None, body: str = None, text: str = None, content: str = None) -> dict:
    """
    Send a WhatsApp text message.

    Args:
        to:      Phone number with country code (e.g. 919876543210) or group JID
        message: Text to send (also accepted as body / text / content)

    Returns:
        Status dict
    """
    message = message or body or text or content
    if not to or not message:
        return {"status": "error", "error": "Both 'to' and 'message' are required"}
    to = _normalize_number(to)
    return _bridge("POST", "/send", json={"to": to, "message": message})


@register_tool(
    "whatsapp.send_media",
    category="WhatsApp",
    description="Send a media file (image, video, document, audio) over WhatsApp",
    example='{"tool": "whatsapp.send_media", "args": {"to": "contact no", "url": "SANDBOX/report.pdf", "type": "document", "caption": "Here is the report"}}',
    notes="type must be one of: image, video, document, audio. url can be a public https URL OR a local SANDBOX path like 'SANDBOX/file.pdf'."
)
def send_media(to: str, url: str, type: str = "", caption: str = "") -> dict:
    """
    Send a media message over WhatsApp.

    Args:
        to:      Phone number with country code or group JID
        url:     Public URL of the media file
        type:    image | video | document | audio  (auto-detected from extension when omitted)
        caption: Optional caption text

    Returns:
        Status dict
    """
    # Auto-detect media type from file extension when not provided or empty,
    # so PDFs and office docs are never misclassified as images.
    if not type:
        ext = os.path.splitext(url.split("?")[0])[-1].lower().lstrip(".")
        _ext_map = {
            "pdf": "document", "doc": "document", "docx": "document",
            "xls": "document", "xlsx": "document", "ppt": "document", "pptx": "document",
            "txt": "document", "csv": "document",
            "mp4": "video", "mov": "video", "avi": "video", "mkv": "video", "webm": "video",
            "mp3": "audio", "ogg": "audio", "aac": "audio", "m4a": "audio", "opus": "audio",
            "jpg": "image", "jpeg": "image", "png": "image", "gif": "image",
            "webp": "image", "bmp": "image",
        }
        type = _ext_map.get(ext, "document")  # unknown extension → document (safe default)
    allowed = ["image", "video", "document", "audio"]
    if type not in allowed:
        return {"status": "error", "error": f"type must be one of: {', '.join(allowed)}"}
    to = _normalize_number(to)
    return _bridge("POST", "/send-media", json={"to": to, "url": url, "type": type, "caption": caption})


@register_tool(
    "whatsapp.status",
    category="WhatsApp",
    description="Check WhatsApp is accissible.",
    example='{"tool": "whatsapp.status", "args": {}}',
    notes="Returns connected/qr/disconnected status and linked phone JID."
)
def get_status() -> dict:
    """
    Get WhatsApp connection status.

    Returns:
        { status: 'connected'|'qr'|'connecting'|'disconnected', jid }
    """
    return _bridge("GET", "/status")
