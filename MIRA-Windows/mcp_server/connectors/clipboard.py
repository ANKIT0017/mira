# mcp_server/connectors/clipboard.py
try:
    import pyperclip
except Exception:
    pyperclip = None

def read_clipboard():
    if pyperclip is None:
        return {"error": "pyperclip not installed"}
    try:
        return {"text": pyperclip.paste()}
    except Exception as e:
        return {"error": str(e)}

def write_clipboard(text: str):
    if pyperclip is None:
        return {"error": "pyperclip not installed"}
    try:
        pyperclip.copy(text)
        return {"status": "ok"}
    except Exception as e:
        return {"error": str(e)}
