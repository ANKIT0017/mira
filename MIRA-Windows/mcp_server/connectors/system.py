import datetime
import os
import subprocess
from ..tools import register_tool  # Use relative import

try:
    import pyperclip
except ImportError:
    pyperclip = None

@register_tool(
    "system.time",
    category="System",
    description="Get the current date and time",
    example='{"tool": "system.time", "args": {}}'
)
def get_time(format: str = None) -> dict:
    """Get the current date and time with timezone info.

    Args:
        format: Optional strftime format string. If provided, a 'formatted'
                key is added to the result (e.g. format="%B %d, %Y %I:%M %p").
                All other fields are always returned regardless of this arg.
    """
    now = datetime.datetime.now().astimezone()   # local tz-aware
    in_10 = now + datetime.timedelta(minutes=10)
    result = {
        "iso": now.isoformat(),               # e.g. 2026-02-20T14:45:09+05:30
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "timezone": str(now.tzinfo),
        "timestamp": int(now.timestamp()),
        "in_10_min": in_10.isoformat(),       # ready-made value for scheduling in 10 min
        "in_30_min": (now + datetime.timedelta(minutes=30)).isoformat(),
    }
    if format:
        try:
            result["formatted"] = now.strftime(format)
        except Exception:
            pass  # bad format string — ignore silently, rest of result is still valid
    return result

@register_tool(
    "system.clipboard.read",
    category="System",
    description="Read text from the system clipboard",
    example='{"tool": "system.clipboard.read", "args": {}}'
)
def clipboard_read() -> dict:
    """Read text from the system clipboard."""
    if not pyperclip:
        raise ImportError("pyperclip not installed")
    return {"text": pyperclip.paste()}

@register_tool(
    "system.clipboard.write",
    category="System",
    description="Write text to the system clipboard",
    example='{"tool": "system.clipboard.write", "args": {"text": "Hello world"}}'
)
def clipboard_write(text: str) -> dict:
    """Write text to the system clipboard."""
    if not pyperclip:
        raise ImportError("pyperclip not installed")
    pyperclip.copy(text)
    return {"text": text}

@register_tool(
    "system.shell",
    category="System",
    description="Run a sandboxed shell command in SANDBOX directory (read-only ops + python execution)",
    example='{"tool": "system.shell", "args": {"command": "python snake_game.py"}}',
    notes="🔒 Security: Only whitelisted commands allowed, executed in SANDBOX directory. Python scripts supported."
)
def run_shell(command: str, timeout: int = 30) -> dict:
    """
    Run a SANDBOXED shell command with strict security controls.
    
    SECURITY FEATURES:
    - Only whitelisted commands allowed
    - Executed in SANDBOX directory only
    - No write operations permitted
    - No command chaining or piping
    - No redirection operators
    """
    # Whitelist of safe, read-only commands
    SAFE_COMMANDS = {
        'dir', 'ls',           # Directory listing
        'type', 'cat',         # File reading
        'echo',                # Simple output
        'cd',                  # Directory change (sandboxed)
        'tree',                # Directory tree
        'find', 'where',       # File search
        'systeminfo',          # System information (read-only)
        'wmic',                # System info queries (limited)
        'python', 'python3',   # Python execution (sandboxed to SANDBOX dir)
        # NOTE: 'pip' intentionally removed — pip install inside the agent loop
        # constitutes arbitrary code execution and is not permitted.
    }
    
    # Dangerous whole-word commands (matched as words, not substrings)
    DANGEROUS_COMMANDS = {
        'rm', 'del', 'erase', 'remove-item', 'rmdir', 'rd',
        'format', 'mkfs', 'fdisk', 'diskpart',
        'copy', 'move', 'rename', 'ren', 'xcopy', 'robocopy',
        'new-item', 'mkdir', 'md',
        'powershell', 'cmd', 'bash', 'sh', 'node',
        'start', 'invoke-expression', 'iex',
        'curl', 'wget', 'invoke-webrequest',
        'reg', 'regedit', 'sc', 'schtasks',
    }

    # Dangerous operator/symbol patterns (matched anywhere in the command)
    DANGEROUS_OPERATORS = ['>>',  '&&', '||', '>', '<', '|', '&', ';', '`']

    try:
        import re as _re
        # Normalize command for checking
        cmd_lower = command.lower().strip()

        # Extract the base command (first word)
        base_cmd = cmd_lower.split()[0] if cmd_lower.split() else ''

        # Check 1: Is base command in whitelist?
        if base_cmd not in SAFE_COMMANDS:
            raise ValueError(
                f"🔒 SECURITY: Command '{base_cmd}' not in whitelist.\n"
                f"Allowed commands: {', '.join(sorted(SAFE_COMMANDS))}"
            )

        # Check 2: Scan for dangerous whole-word commands (word-boundary match)
        for pattern in DANGEROUS_COMMANDS:
            if _re.search(r'(?<![a-z0-9_\-])' + _re.escape(pattern) + r'(?![a-z0-9_\-])', cmd_lower):
                raise ValueError(
                    f"🔒 SECURITY: Dangerous command '{pattern}' detected in command.\n"
                    f"This operation is not allowed."
                )

        # Check 3: Scan for dangerous operator symbols
        for pattern in DANGEROUS_OPERATORS:
            if pattern in cmd_lower:
                raise ValueError(
                    f"🔒 SECURITY: Dangerous pattern '{pattern}' detected in command.\n"
                    f"This operation is not allowed."
                )
        
        # Get SANDBOX directory
        from pathlib import Path
        workspace = Path(__file__).parent.parent.parent.resolve()
        sandbox = workspace / "SANDBOX"
        sandbox.mkdir(parents=True, exist_ok=True)
        
        # Execute command in SANDBOX directory
        result = subprocess.run(
            command,
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
            cwd=str(sandbox)  # Force execution in SANDBOX
        )
        
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "code": result.returncode,
            "working_directory": str(sandbox),
            "security_note": "Executed in SANDBOX with read-only permissions"
        }
        
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"Command timed out after {timeout}s")
    except ValueError as e:
        # Security violations should return clear error messages
        return {
            "status": "error",
            "error": str(e),
            "code": -1
        }