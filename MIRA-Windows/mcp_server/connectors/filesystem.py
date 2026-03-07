import os
from pathlib import Path
import sys
from ..tools import register_tool  # Use relative import

def resolve_path(path: str) -> Path:
    """Resolve and validate a path is within SANDBOX directory.
    
    SECURITY: All file operations are restricted to SANDBOX/ only.
    This prevents access to sensitive files (.env, source code, etc.)
    """
    # Get the SANDBOX directory
    workspace = Path(__file__).parent.parent.parent.resolve()
    sandbox = workspace / "SANDBOX"
    
    # Ensure SANDBOX exists
    sandbox.mkdir(parents=True, exist_ok=True)

    # Strip leading SANDBOX/ prefix so agents don't create double-nested paths
    # e.g. "SANDBOX/file.pdf" → "file.pdf" before joining onto sandbox base
    clean = path.lstrip('/\\')
    for prefix in ('SANDBOX/', 'SANDBOX\\', 'sandbox/', 'sandbox\\'):
        if clean.startswith(prefix):
            clean = clean[len(prefix):]
            break

    # Resolve the requested path (without expanduser to prevent ~ expansion)
    try:
        # If path is relative, make it relative to SANDBOX
        if not Path(clean).is_absolute():
            p = (sandbox / clean).resolve()
        else:
            p = Path(clean).resolve()
    except Exception as e:
        raise ValueError(f"Invalid path: {path} - {e}")
    
    # SECURITY CHECK: Ensure the resolved path is inside SANDBOX
    try:
        p.relative_to(sandbox)
    except ValueError:
        raise ValueError(
            f"🔒 SECURITY: Access denied. All file operations must be within SANDBOX/ directory.\n"
            f"Requested: {path}\n"
            f"Resolved to: {p}\n"
            f"Allowed directory: {sandbox}"
        )
    
    return p

@register_tool(
    "filesystem.list",
    category="Filesystem",
    description="List files in a directory",
    example='{"tool": "filesystem.list", "args": {"path": "SANDBOX"}}'
)
def list_dir(path: str = ".") -> dict:
    """List contents of a directory."""
    p = resolve_path(path)
    if not p.exists():
        raise FileNotFoundError(f"Path not found: {p}")
    
    items = []
    for entry in p.iterdir():
        items.append({
            "name": entry.name,
            "is_dir": entry.is_dir(),
            "size": entry.stat().st_size if entry.is_file() else None
        })
    return {"path": str(p), "items": items}

@register_tool(
    "filesystem.read",
    category="Filesystem",
    description="Read content from a file",
    example='{"tool": "filesystem.read", "args": {"path": "SANDBOX/note.txt"}}'
)
def read_file(path: str) -> dict:
    """Read contents of a file."""
    p = resolve_path(path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"File not found: {p}")
    
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        return {"path": str(p), "text": f.read()}

@register_tool(
    "filesystem.write",
    category="Filesystem",
    description="Write content to a file (use SANDBOX/ folder)",
    example='{"tool": "filesystem.write", "args": {"path": "SANDBOX/test.txt", "content": "Hello MIRA"}}',
    notes="⚠️ Always save user-requested files to the SANDBOX/ directory."
)
def write_file(path: str, content: str) -> dict:
    """Write content to a file."""
    p = resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return {"path": str(p)}

@register_tool(
    "filesystem.append",
    category="Filesystem",
    description="Append content to an existing file",
    example='{"tool": "filesystem.append", "args": {"path": "SANDBOX/log.txt", "content": "New line"}}'
)
def append_file(path: str, content: str) -> dict:
    """Append content to a file."""
    p = resolve_path(path)
    if not p.exists():
        return write_file(str(p), content)
    
    with open(p, "a", encoding="utf-8") as f:
        f.write("\n" + content)
    return {"path": str(p)}

@register_tool(
    "filesystem.search",
    category="Filesystem",
    description="Search for files by name",
    example='{"tool": "filesystem.search", "args": {"query": "resume", "path": "SANDBOX"}}'
)
def search_files(query: str, path: str = ".", max_results: int = 100) -> dict:
    """Search for files containing query in their name."""
    p = resolve_path(path)
    if not p.exists():
        raise FileNotFoundError(f"Search path not found: {p}")
    
    matches = []
    for root, _, files in os.walk(p):
        for f in files:
            if query.lower() in f.lower():
                rel_path = os.path.relpath(os.path.join(root, f), p)
                matches.append(rel_path)
                if len(matches) >= max_results:
                    break
        if len(matches) >= max_results:
            break
            
    return {
        "query": query,
        "path": str(p),
        "matches": matches,
        "truncated": len(matches) >= max_results
    }
