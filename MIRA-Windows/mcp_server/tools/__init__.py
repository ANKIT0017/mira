from typing import Callable, Dict, Any, Optional
import functools
import inspect

# Global registry of tools with metadata
TOOLS: Dict[str, Callable] = {}
TOOL_METADATA: Dict[str, Dict[str, Any]] = {}

def register_tool(
    name: str, 
    description: Optional[str] = None,
    category: Optional[str] = None,
    example: Optional[str] = None,
    notes: Optional[str] = None
):
    """
    Enhanced decorator to register a function as a tool with rich metadata.
    
    Args:
        name: Tool name (e.g., "firecrawl.scrape")
        description: Short description of what the tool does
        category: Category for grouping (e.g., "Web Scraping", "Email", "Filesystem")
        example: JSON example of how to call this tool
        notes: Additional notes or warnings (e.g., "⚠️ Requires API key")
    """
    def wrapper(func):
        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            try:
                inner = func(*args, **kwargs)
                # If the function itself returned an error dict, propagate it so the
                # agent sees status:error rather than a confusing status:ok wrapper.
                if isinstance(inner, dict) and inner.get("status") == "error":
                    return inner
                return {"status": "ok", "result": inner}
            except Exception as e:
                return {"status": "error", "error": str(e)}
        
        TOOLS[name] = wrapped
        
        # Extract metadata from function
        sig = inspect.signature(func)
        params = {}
        has_var_keyword = False
        for param_name, param in sig.parameters.items():
            if param.kind == inspect.Parameter.VAR_KEYWORD:  # **kwargs
                has_var_keyword = True
                continue  # don't list **kwargs as a named param
            if param.kind == inspect.Parameter.VAR_POSITIONAL:  # *args
                continue
            _ann = param.annotation
            if _ann == inspect.Parameter.empty:
                _ann_name = "any"
            elif hasattr(_ann, "__name__"):
                _ann_name = _ann.__name__
            else:
                _ann_name = str(_ann).replace("typing.", "")
            param_info = {
                "type": _ann_name,
                "default": param.default if param.default != inspect.Parameter.empty else None,
                "required": param.default == inspect.Parameter.empty
            }
            params[param_name] = param_info
        
        # Store metadata
        TOOL_METADATA[name] = {
            "name": name,
            "description": description or func.__doc__ or "No description available",
            "category": category or "Other",
            "example": example,
            "notes": notes,
            "parameters": params,
            "has_var_keyword": has_var_keyword,  # True = accepts **kwargs, don't strip extras
            "function": func
        }
        
        return wrapped
    return wrapper

def list_tools() -> list[str]:
    """Return list of registered tool names."""
    return sorted(TOOLS.keys())

def get_tool_metadata(name: str) -> Optional[Dict[str, Any]]:
    """Get metadata for a specific tool."""
    return TOOL_METADATA.get(name)

def get_tools_by_category() -> Dict[str, list[Dict[str, Any]]]:
    """Return tools grouped by category."""
    categorized = {}
    for tool_name, metadata in TOOL_METADATA.items():
        category = metadata.get("category", "Other")
        if category not in categorized:
            categorized[category] = []
        categorized[category].append(metadata)
    return categorized

def generate_tool_documentation() -> str:
    """
    Auto-generate tool documentation for the agent's system prompt.
    Returns a formatted string with all tools organized by category.
    """
    categorized = get_tools_by_category()
    doc = "AVAILABLE TOOLS (USE ONLY THESE — EXACT NAMES AND ARGS):\n\n"
    
    for category in sorted(categorized.keys()):
        tools = categorized[category]
        doc += f"{len(doc.split('.'))} . {category} Tools\n"
        
        for tool in tools:
            doc += f"   - {tool['name']}: {tool['description']}\n"
            
            # Add example if provided
            if tool.get('example'):
                doc += f"     {tool['example']}\n"
            
            # Add notes if provided
            if tool.get('notes'):
                doc += f"     {tool['notes']}\n"
            
            doc += "\n"
        
    return doc

def compact_tool_docs(categories: Optional[set[str]] = None) -> str:
    """
    Compact tool reference for system prompts.
    Format: [Category]\\n  tool.name(req_arg, req_arg2): one-line description
    ~15-25 tokens per tool — fits small-model context without overflow.
    Full arg signatures are injected on error via JIT doc cards in _safe_execute.
    If categories is provided, only emit matching categories (filter by name).
    """
    categorized = get_tools_by_category()
    requested = {c.strip().lower() for c in (categories or set()) if isinstance(c, str) and c.strip()}
    lines = ["AVAILABLE TOOLS (exact names — use ONLY these):"]
    for category in sorted(categorized.keys()):
        if requested and category.strip().lower() not in requested:
            continue
        tools = categorized[category]
        lines.append(f"\n[{category}]")
        for tool in sorted(tools, key=lambda t: t["name"]):
            desc = (tool.get("description") or "").split("\n")[0].strip()[:80]
            params = tool.get("parameters", {})
            req_args = [k for k, v in params.items() if v.get("required")]
            opt_args = [k for k, v in params.items() if not v.get("required")]
            if req_args:
                arg_hint = f"({', '.join(req_args)})"
            elif opt_args:
                arg_hint = f"([{', '.join(opt_args[:2])}])"
            else:
                arg_hint = "()"
            lines.append(f"  {tool['name']}{arg_hint}: {desc}")
    return "\n".join(lines)


def full_tool_schemas(categories: Optional[set[str]] = None) -> str:
    """
    Full per-tool schema cards for executor context.
    Includes all args (type + required/optional + default), example JSON call, and notes.
    ~40-60 tokens per tool — provides the executor with everything needed for correct calls.
    If categories is provided, only emit matching categories.
    """
    categorized = get_tools_by_category()
    requested = {c.strip().lower() for c in (categories or set()) if isinstance(c, str) and c.strip()}
    lines = ["TOOLS FOR THIS STEP (use exact names and args — no other tools exist):"]
    for category in sorted(categorized.keys()):
        if requested and category.strip().lower() not in requested:
            continue
        tools = categorized[category]
        lines.append(f"\n[{category}]")
        for tool in sorted(tools, key=lambda t: t["name"]):
            desc = (tool.get("description") or "").split("\n")[0].strip()[:100]
            params = tool.get("parameters", {})
            notes  = tool.get("notes") or ""
            example = tool.get("example") or ""

            # Build arg list: required first, then optional
            arg_parts = []
            for pname, pinfo in params.items():
                ptype    = pinfo.get("type", "any")
                required = pinfo.get("required", False)
                default  = pinfo.get("default")
                if required:
                    arg_parts.append(f"{pname}: {ptype} [required]")
                else:
                    dflt_str = f", default={default!r}" if default is not None else ""
                    arg_parts.append(f"{pname}: {ptype} [optional{dflt_str}]")

            lines.append(f"  {tool['name']}: {desc}")
            if arg_parts:
                lines.append("    Args: " + " | ".join(arg_parts))
            if example:
                lines.append(f"    Example: {example}")
            if notes:
                lines.append(f"    Note: {notes}")
    return "\n".join(lines)


def full_tool_schemas_for_tools(tool_names: list[str]) -> str:
    """
    Full schema cards for an explicit tool-name subset.
    """
    if not tool_names:
        return "TOOLS FOR THIS STEP: none"
    lines = ["TOOLS FOR THIS STEP (use exact names and args):"]
    seen = set()
    for name in tool_names:
        if not name or name in seen:
            continue
        seen.add(name)
        tool = TOOL_METADATA.get(name)
        if not tool:
            continue
        desc = (tool.get("description") or "").split("\n")[0].strip()[:100]
        params = tool.get("parameters", {}) or {}
        notes = tool.get("notes") or ""
        example = tool.get("example") or ""
        arg_parts = []
        for pname, pinfo in params.items():
            ptype = pinfo.get("type", "any")
            required = pinfo.get("required", False)
            default = pinfo.get("default")
            if required:
                arg_parts.append(f"{pname}: {ptype} [required]")
            else:
                dflt_str = f", default={default!r}" if default is not None else ""
                arg_parts.append(f"{pname}: {ptype} [optional{dflt_str}]")
        lines.append(f"  {name}: {desc}")
        if arg_parts:
            lines.append("    Args: " + " | ".join(arg_parts))
        if example:
            lines.append(f"    Example: {example}")
        if notes:
            lines.append(f"    Note: {notes}")
    return "\n".join(lines)


def execute_tool(tool_name: str, **kwargs) -> Dict[str, Any]:
    """Execute a registered tool by name with given arguments."""
    func = TOOLS.get(tool_name)
    if not func:
        return {"status": "error", "error": f"Tool '{tool_name}' not found"}
    return func(**kwargs)
