import json
import re
import asyncio
import hashlib
import dataclasses
import os
import time
from datetime import datetime
from pathlib import Path
from .model_adapter import (
    generate,
    generate_with_messages,
    generate_with_native_tools,
    supports_ollama_native_tools,
)
from .run_controller import get_controller, TERMINAL_STATES
from mcp_server.tools import (
    execute_tool,
    list_tools,
    compact_tool_docs,
    full_tool_schemas,
    full_tool_schemas_for_tools,
    TOOL_METADATA,
)

# Context engineering modules (v2)
from .context_compiler import ContextCompiler, ContextSections, StepType
from .tool_records import ToolRecordStore, ToolRecord
from .context_memory import ShortTermMemory, LongTermMemory
from .token_budget import TokenBudget, count_tokens
# ProviderStateManager is accessed via _context_compiler.provider_state
from .context_metrics import ContextMetricsTracker

# --- Configuration ---
MAX_AUTONOMOUS_STEPS  = 50   # Hard step cap (was 275; tighter = more deterministic)
MAX_RETRIES_PER_STEP = int(os.getenv("MIRA_MAX_RETRIES_PER_STEP", "4"))
MAX_PLAN_REVISIONS   = 3    # Replan budget for splitting/revising remaining plan
REPLAN_CONSECUTIVE_FAILURES = int(os.getenv("MIRA_REPLAN_CONSECUTIVE_FAILURES", "3"))
MAX_HISTORY_TURNS    = 4    # Keep history short for small local models
MAX_PLAN_HISTORY_TURNS = 2  # Planner only sees the last 2 turns
SANDBOX_DIR = "SANDBOX"
MEMORY_FILE = "MEMORY.md"
SOUL_FILE = "SOUL.md"
USER_FILE = "USER.md"
MAX_MEMORY_CONTEXT_CHARS = 6000
MAX_PROFILE_CONTEXT_CHARS = int(os.getenv("MIRA_MAX_PROFILE_CONTEXT_CHARS", "3500"))
MAX_PLAN_MEMORY_ITEMS = int(os.getenv("MIRA_MAX_PLAN_MEMORY_ITEMS", "5"))
MAX_PLAN_USER_FACTS = int(os.getenv("MIRA_MAX_PLAN_USER_FACTS", "3"))
MAX_RECENT_TOOL_OUTPUTS = int(os.getenv("MIRA_MAX_RECENT_TOOL_OUTPUTS", "3"))
MAX_RECENT_TOOL_OUTPUT_CHARS = int(os.getenv("MIRA_MAX_RECENT_TOOL_OUTPUT_CHARS", "1000"))
PROMPT_TOKEN_SOFT_LIMIT = int(os.getenv("MIRA_PROMPT_TOKEN_SOFT_LIMIT", "2000"))
CHARS_PER_TOKEN_ESTIMATE = 4
BUFFER_THRESHOLD_CHARS = 2000
MAX_BUFFER_CONTEXT_ITEMS = int(os.getenv("MIRA_MAX_BUFFER_CONTEXT_ITEMS", "6"))
FORCE_BUFFER_PREFIXES = ("search.", "firecrawl.")
EMAIL_REGEX = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"

#  Synthesizer bypass 
# Set True to skip the LLM synthesis call and emit the raw step results directly.
# Useful for testing execution quality without the synthesizer layer.
BYPASS_SYNTHESIZER: bool = False

# Tool category keywords used for fallback matching when planner categories are missing.
CATEGORY_KEYWORDS: dict = {
    "Web Scraping": ["scrape", "crawl", "website", "url", "web"],
    "Web & Search": ["search", "find", "lookup", "internet", "online", "news"],
    "Documents": ["document", "pdf", "markdown", "report", "convert", "excel", "xlsx", "spreadsheet", "table", "formula", "column", "row"],
    "Conversion": ["convert", "pdf", "html", "markdown"],
    "Filesystem": ["file", "folder", "read", "write", "save", "sandbox"],
    "Contacts": ["contact", "phone number", "phone", "person", "recipient", "jid"],
    "WhatsApp": ["whatsapp", "whatsapp message", "chat", "jid"],
    # Use specific phrases so bare words like "email" (in "email address") or
    # "mail" don't accidentally pull in the Email category for non-email steps.
    "Email": ["send email", "read email", "email inbox", "email draft", "fetch email", "check inbox", "inbox"],
    "Calendar": ["calendar", "event", "schedule", "meeting", "reminder"],
    # Remove "task" and "complete"  too generic; they appear in step descriptions
    # like "(User clarification: drop this task)" triggering false category matches.
    "Todo List": ["todo", "todo list", "checklist"],
    "Knowledge Base": ["knowledge", "remember", "notes"],
    "System": ["current time", "current date", "system status", "system"],
    "Workflows": ["workflow", "template", "reuse", "repeat task", "automation"],
    # Buffer: steps that process, gather, compile, or synthesize data already in buffers
    "Buffer": ["buffer", "from results", "from the results", "gather list", "compile list",
               "extract list", "process results", "summarize results", "list of books",
               "list of items", "list from"],
}

_ALWAYS_CATEGORIES = set()

_SIDE_EFFECT_PREFIXES = (
    "whatsapp.send",
    "email.send", "email.create_draft",
    "contacts.add", "contacts.update", "contacts.delete",
    "calendar.add_event", "calendar.add_recurring_event",
    "calendar.delete_event", "calendar.clear", "calendar.toggle_event",
    "filesystem.write", "filesystem.append",
    "todo.add", "todo.update", "todo.delete",
    "todo.complete", "todo.complete_all", "todo.clear",
    "knowledge.ingest",
    "memory.write",
    "memory.update",
    "user.write",
    "user.update",
    "soul.write",
    "soul.update",
    "profile.write",
    "profile.update",
    "buffer.write",
    "buffer.update",
    "workflow.save",
    "convert.",
    "pdf.create",
    "pdf.merge",
)


def _parse_tool_constraints(text: str) -> tuple:
    """
    Parse runtime allow/deny constraints from user prompt text.

    Recognized patterns (case-insensitive):
      "Use ONLY: filesystem.*, todo.*"    only these prefixes are permitted
      "Do NOT use: web/email/whatsapp"    these prefixes are hard-blocked

    Returns (allowed: set[str], denied: set[str]).  Each token is a lowercase
    tool prefix; matching uses  tool_name == tok  or  tool_name.startswith(tok + '.').
    """
    allowed: set = set()
    denied:  set = set()

    m = re.search(r'Use\s+ONLY[:\s]+([^\n]+)', text, re.IGNORECASE)
    if m:
        for tok in re.split(r'[,/\s]+', m.group(1).strip()):
            tok = tok.strip().strip('.*').lower()
            if tok and not tok.startswith('#') and tok not in ('tools', 'and', 'or'):
                allowed.add(tok)

    m2 = re.search(
        r"Do\s+NOT\s+use[:\s]+([^\n]+)|Don[\u2019']?t\s+use[:\s]+([^\n]+)",
        text, re.IGNORECASE
    )
    if m2:
        raw = (m2.group(1) or m2.group(2) or '').strip()
        for tok in re.split(r'[,/\s]+', raw):
            tok = tok.strip().strip('.*').lower()
            if tok and not tok.startswith('#') and tok not in ('and', 'or', 'the'):
                denied.add(tok)

    return allowed, denied


def _humanize_error(error: str, tool_name: str, tool_args: dict) -> str:
    """Translate raw error messages into plain-English fix hints for the retry prompt."""
    e = str(error)
    # Strip injected TOOL DOC card prefix so pattern matches work on the real error
    if "[TOOL DOC:" in e:
        e = e[e.index("\nError:") + 1:].strip() if "\nError:" in e else e.split("]", 1)[-1].strip()
    # RFC 5321 / invalid SMTP recipient (phone number used as email address)
    if "5.1.3" in e or "not a valid RFC 5321" in e or "is not a valid" in e.lower():
        to_val = (tool_args or {}).get("to", "")
        return (
            f"The 'to' value '{to_val}' is NOT a valid email address  it looks like a "
            "phone number. email.send requires an address like 'name@domain.com'. "
            "If the contact has no email, use whatsapp.send with the phone number instead."
        )
    # Contact/record ID not found  LLM hallucinated an id instead of reading from a prior result
    if "contact id" in e.lower() and "not found" in e.lower():
        bad_id = (tool_args or {}).get("id", "")
        return (
            f"Contact ID {bad_id!r} does not exist. You must pass the integer 'id' that was "
            f"returned by a previous contacts.search or contacts.get call. "
            f"Look in COMPLETED STEPS above  find the 'id' field in those results and pass "
            f"that exact integer value to {tool_name}. Do NOT invent or guess an id."
        )
    # Generic record-not-found (covers task, event, note IDs)
    if "not found" in e.lower() and "id" in e.lower() and not ("tool" in e.lower()):
        bad_id = (tool_args or {}).get("id", "")
        return (
            f"Record ID '{bad_id}' was not found. Make sure you are passing the integer 'id' "
            "from the GET/list result, not a name or label string."
        )
    # Missing attachment
    if "attachment not found" in e.lower() or "no such file" in e.lower():
        return (
            "The attachment file does not exist. Either create it first with "
            "filesystem.write inside SANDBOX/, or remove the 'attachments' argument entirely."
        )
    # Missing required args / invalid format
    if re.search(r"missing required field|is required|cannot be empty|required", e.lower()):
        return (
            f"The call to {tool_name} is missing required arguments. "
            "Use the exact TOOL format and include all required args shown in the tool error/example."
        )
    # Tool not found  check BEFORE the generic 'not found + id' case
    if "not found" in e.lower() and ("tool" in e.lower() or tool_name in e):
        # Only if the error is specifically about a missing tool, not a missing record
        if "id" not in e.lower():
            return (
                f"The tool '{tool_name}' does not exist. Check the AVAILABLE TOOLS list "
                "and use only the exact names shown there."
            )
    # Connection / network
    if "connection refused" in e.lower() or "network" in e.lower() or "timeout" in e.lower():
        return "The service is unreachable. Try a different tool or skip this step."
    # Generic fallback
    return f"Error was: {e[:300]}"


#  Tool Policy Engine 
# Policies are keyed by tool prefix/family and define deterministic success,
# failure, and escalation behaviour so the execution loop never needs LLM text
# reasoning to detect empty results, loops, or user-information gaps.
# Each entry in _TOOL_POLICY_REGISTRY is (prefix_str, ToolPolicy). The first
# matching prefix wins, so order more-specific entries FIRST.

@dataclasses.dataclass
class ToolPolicy:
    """Per-family policy for result validation and failure escalation."""
    family: str                    # human label
    empty_is_failure: bool = False # 0-item / empty-string result treated as failure
    # fallback_sequence: ordered recovery steps before LLM replan.
    #   ("tool",       composer)   composer(tool_name, args, error) -> (new_tool, new_args) | None
    #   ("need_input", composer)   composer(tool_name, args, error) -> question_str
    fallback_sequence: tuple = ()
    # Last-resort question generator (also used for direct need_input verdict)
    need_input_question: object = None   # callable(tool_name, args, error) -> str | None


#  Fallback composers (pure functions for use in fallback_sequence) 

def _fb_list_sandbox(tn, args, err):
    """search-family fallback #1: list SANDBOX root so model sees what exists."""
    return ("filesystem.list", {"path": "SANDBOX"})

def _fb_list_parent(tn, args, err):
    """read-family fallback #1: list the failed file's parent directory."""
    path = args.get("path") or args.get("file_path") or args.get("filename") or "SANDBOX"
    parent = str(Path(str(path)).parent) if ("/" in str(path) or "\\" in str(path)) else "SANDBOX"
    return ("filesystem.list", {"path": parent})

def _ni_search(tn, args, err):
    """filesystem.search: file not found in SANDBOX."""
    q = args.get("query", "")
    return (
        f"No files matched '{q}' in SANDBOX. What is the correct filename or folder? "
        "(I can list SANDBOX to show what's there.)"
        if q else "No files found. What filename or folder should I look in?"
    )

def _ni_todo_search(tn, args, err):
    """todo.search: no matching tasks."""
    q = args.get("query", "")
    return (
        f"No todo tasks matched '{q}'. Did you mean a different keyword, "
        "or should I list all tasks instead?"
        if q else "No matching todos found. What task name are you looking for?"
    )

def _ni_knowledge_search(tn, args, err):
    """knowledge.search: empty semantic search."""
    q = args.get("query", "")
    return (
        f"No knowledge-base entries matched '{q}'. "
        "Should I list all indexed documents, or was a different query intended?"
        if q else "Knowledge base search returned nothing. What topic should I look for?"
    )

def _ni_web_search(tn, args, err):
    """search.duckduckgo / search.news: no results or service down."""
    q = args.get("query", "")
    return (
        f"Web search for '{q}' returned no results. "
        "Should I try a different query, or is the search service unavailable?"
        if q else "Web search returned nothing. What would you like me to search for?"
    )

def _ni_read(tn, args, err):
    f = args.get("path") or args.get("file_path") or args.get("filename") or ""
    return (
        f"The file '{f}' was not found. What is the correct filename, or should I create it first?"
        if f else "A required file was not found. What is the correct filename?"
    )

def _ni_contact(tn, args, err):
    q = args.get("query") or args.get("name") or ""
    return (
        f"I couldn't find the contact '{q}'. What is their exact name or phone number?"
        if q else "Contact not found. What is the exact name or phone number?"
    )

def _ni_convert(tn, args, err):
    return (
        "File conversion failed or produced invalid output. "
        "Should I try a different format, or can you provide the source content directly?"
    )

def _ni_record(tn, args, err):
    rid = args.get("id") or args.get("task_id") or ""
    return (
        f"Record ID '{rid}' was not found. Please provide the correct ID."
        if rid else "A required record was not found. Please provide the correct ID."
    )


#  Policy objects 

# filesystem.search   fall back to listing SANDBOX so the model can see what's there
_POLICY_FILE_SEARCH = ToolPolicy(
    family="file_search",
    empty_is_failure=True,
    fallback_sequence=(
        ("tool",       _fb_list_sandbox),
        ("need_input", _ni_search),
    ),
    need_input_question=_ni_search,
)

# todo.search / knowledge.search   DB queries; filesystem listing is irrelevant
_POLICY_TODO_SEARCH = ToolPolicy(
    family="todo_search",
    empty_is_failure=True,
    fallback_sequence=(("need_input", _ni_todo_search),),
    need_input_question=_ni_todo_search,
)

_POLICY_KNOWLEDGE_SEARCH = ToolPolicy(
    family="knowledge_search",
    empty_is_failure=True,
    fallback_sequence=(("need_input", _ni_knowledge_search),),
    need_input_question=_ni_knowledge_search,
)

# search.duckduckgo / search.news   web queries; may also be service-down
_POLICY_WEB_SEARCH = ToolPolicy(
    family="web_search",
    empty_is_failure=True,
    fallback_sequence=(("need_input", _ni_web_search),),
    need_input_question=_ni_web_search,
)

_POLICY_READ = ToolPolicy(
    family="read",
    empty_is_failure=True,
    fallback_sequence=(
        ("tool",       _fb_list_parent),
        ("need_input", _ni_read),
    ),
    need_input_question=_ni_read,
)

_POLICY_LIST = ToolPolicy(
    family="list",
    empty_is_failure=False,   # empty list is a valid answer (no items exist)
)

_POLICY_CONTACT_SEARCH = ToolPolicy(
    family="contacts.search",
    empty_is_failure=True,
    fallback_sequence=(("need_input", _ni_contact),),
    need_input_question=_ni_contact,
)

_POLICY_CONVERT = ToolPolicy(
    family="convert",
    empty_is_failure=True,
    fallback_sequence=(("need_input", _ni_convert),),
    need_input_question=_ni_convert,
)

_POLICY_SEND = ToolPolicy(
    family="send",
    empty_is_failure=False,   # errors on send = hard-fail (side-effect may have occurred)
)

_POLICY_STATUS = ToolPolicy(family="status", empty_is_failure=False)
_POLICY_THINK  = ToolPolicy(family="think",  empty_is_failure=True)   # empty think = useless
_POLICY_DEFAULT = ToolPolicy(family="default", empty_is_failure=False)
_POLICY_BUFFER_EXTRACT = ToolPolicy(family="buffer.extract", empty_is_failure=True)
_POLICY_BUFFER_SEARCH  = ToolPolicy(family="buffer.search",  empty_is_failure=True)
_POLICY_BUFFER_SLICE   = ToolPolicy(family="buffer.slice",   empty_is_failure=True)
_POLICY_MEMORY_WRITE   = ToolPolicy(family="memory.write",   empty_is_failure=True)
_POLICY_MEMORY_UPDATE  = ToolPolicy(family="memory.update",  empty_is_failure=True)
_POLICY_AUTO_QUERY     = ToolPolicy(family="auto.query",     empty_is_failure=True)


# Registry: (prefix, policy). More-specific prefixes MUST come before shorter ones.
_TOOL_POLICY_REGISTRY: tuple = (
    ("contacts.search",    _POLICY_CONTACT_SEARCH),
    ("contacts.get",       _POLICY_CONTACT_SEARCH),
    ("filesystem.search",  _POLICY_FILE_SEARCH),
    ("filesystem.read",    _POLICY_READ),
    ("filesystem.list",    _POLICY_LIST),
    ("knowledge.search",   _POLICY_KNOWLEDGE_SEARCH),
    ("knowledge.list",     _POLICY_LIST),
    ("todo.search",        _POLICY_TODO_SEARCH),
    ("todo.list",          _POLICY_LIST),
    ("calendar.list",      _POLICY_LIST),
    ("contacts.list",      _POLICY_LIST),
    ("search.",            _POLICY_WEB_SEARCH),   # search.duckduckgo, search.news
    ("pdf.read",           _POLICY_READ),
    ("word.read",          _POLICY_READ),
    ("excel.read",         _POLICY_READ),
    ("convert.",           _POLICY_CONVERT),
    ("pdf.create",         _POLICY_CONVERT),
    ("word.create",        _POLICY_CONVERT),
    ("whatsapp.send",      _POLICY_SEND),
    ("email.send",         _POLICY_SEND),
    ("email.create_draft", _POLICY_SEND),
    ("calendar.add_event", _POLICY_SEND),
    ("calendar.add_recurring_event", _POLICY_SEND),
    ("calendar.delete_event", _POLICY_SEND),
    ("calendar.clear",     _POLICY_SEND),
    ("calendar.toggle_event", _POLICY_SEND),
    ("filesystem.write",   _POLICY_SEND),
    ("filesystem.append",  _POLICY_SEND),
    ("system.clipboard.read", _POLICY_READ),
    ("system.clipboard.write", _POLICY_SEND),
    ("whatsapp.status",    _POLICY_STATUS),
    ("system.time",        _POLICY_STATUS),
    ("buffer.search",      _POLICY_BUFFER_SEARCH),
    ("buffer.extract",     _POLICY_BUFFER_EXTRACT),
    ("buffer.slice",       _POLICY_BUFFER_SLICE),
    ("memory.write",       _POLICY_MEMORY_WRITE),
    ("memory.update",      _POLICY_MEMORY_UPDATE),
    ("buffer.write",       _POLICY_MEMORY_WRITE),
    ("buffer.update",      _POLICY_MEMORY_UPDATE),
    ("think",              _POLICY_THINK),
)


def _get_policy(tool_name: str) -> ToolPolicy:
    """Return the best-matching ToolPolicy for the given tool_name."""
    for prefix, policy in _TOOL_POLICY_REGISTRY:
        if tool_name == prefix or tool_name.startswith(prefix):
            return policy
    op = tool_name.lower().split(".")[-1]
    if op in {"search", "read", "get", "extract", "list", "find", "list_events"}:
        return _POLICY_AUTO_QUERY
    if op in {
        "add", "add_event", "add_recurring_event", "update", "delete", "clear",
        "toggle_event", "write", "append", "send", "create", "ingest",
        "complete", "complete_all",
    }:
        return _POLICY_SEND
    return _POLICY_DEFAULT


#  5-way tool result classifier 
# Returns one of: "success" | "retryable" | "need_input" | "hard_fail"
# Driven by ToolPolicy; never relies on LLM text output to determine outcome.

_NI_ERROR_PATTERNS: tuple = (
    r'file not found|no such file|path.*does not exist|not found in sandbox',
    r'no contacts found|0 contacts|contacts.*empty',
    r'buffer \d+ not found',
    r'multiple.*found|found \d+ match|ambiguous',
    r'is required.*missing|required.*field|cannot be empty',
    r'record.*not found|no (task|event|todo).*found',
)


def _classify_tool_result(
    policy: ToolPolicy,
    tool_name: str,
    tool_args: dict,
    tool_res,          # raw result object (None when error)
    error: str,        # error string (empty string when success)
    step_result: str,  # compressed observation string
) -> str:
    """
    5-way deterministic classifier.
    Returns: "success" | "retryable" | "need_input" | "hard_fail"
    """
    if error:
        e = error.lower()
        for pat in _NI_ERROR_PATTERNS:
            if re.search(pat, e):
                return "need_input"
        if policy.family == "send":
            return "hard_fail"   # side-effect may already have occurred
        return "retryable"

    #  Success path: check emptiness according to policy 
    if policy.empty_is_failure:
        _is_empty = (
            tool_res is None
            or (isinstance(tool_res, dict) and tool_res == {})
            or (isinstance(tool_res, dict) and tool_res.get("count") == 0)
            or (isinstance(tool_res, dict) and tool_res.get("matches")   == [])
            or (isinstance(tool_res, dict) and tool_res.get("results")   == [])
            or (isinstance(tool_res, dict) and tool_res.get("items")     == [])
            or (isinstance(tool_res, dict) and tool_res.get("tasks")     == [])
            or (isinstance(tool_res, dict) and tool_res.get("events")    == [])
            or (isinstance(tool_res, dict) and tool_res.get("contacts")  == [])
            or (
                isinstance(tool_res, dict)
                and "contact" in tool_res
                and tool_res.get("contact") in (None, {}, "")
            )
            or (
                isinstance(tool_res, dict)
                and "data" in tool_res
                and tool_res.get("data") in (None, [], "")
            )
            or (isinstance(tool_res, list) and len(tool_res) == 0)
            or (isinstance(tool_res, str)  and not tool_res.strip())
            or (
                isinstance(tool_res, str)
                and re.search(r"\b(found\s+0\s+matches|no\s+keyword\s+match|no\s+matches?)\b", tool_res.lower())
            )
            or step_result in ("no result", "", "null", "None")
        )
        if _is_empty:
            return "need_input"

    if (
        step_result.startswith("ERROR:")
        or step_result.lower().startswith("error:")
        or step_result in ("no result", "", "null", "None")
    ):
        return "retryable"

    return "success"


class Agent:
    def __init__(self):
        """Initialize agent with list of available tools."""
        self.tools = list_tools()
        # simple in-memory conversation history: list of (user, assistant) tuples
        self.history = []
        self.stop_requested = False
        # Context Buffer: stores large tool results so LLM doesn't overflow
        self.buffer = {}        # {id: full_text_string}
        self._buffer_id = 0
        self._input_answers: dict = {}  # {step_idx: user_answer} for NEED_INPUT pause/resume
        self._task_contract: dict = {}
        self._working_state: dict = {"slots": {}, "artifacts": {}}
        self._handoff_payload: str = ""  # latest non-delivery payload for downstream send steps
        self._pending_workflow_candidate: dict | None = None
        self._ensure_memory_file()
        self._ensure_profile_files()
        self._build_system_prompt()
        self._register_internal_tools()  # expose buffer tools to compact_tool_docs

        #  Context Engineering v2 
        self._context_compiler = ContextCompiler(
            system_prompt=self.system_prompt,
            memory_file=self._memory_file_path(),
        )
        # Note: provider_state lives inside _context_compiler; no separate instance needed.
    
    def reset(self):
        """Clear conversation history and buffer."""
        self.history = []
        self.stop_requested = False
        self.buffer = {}
        self._buffer_id = 0
        self._input_answers = {}
        self._task_contract = {}
        self._working_state = {"slots": {}, "artifacts": {}}
        self._handoff_payload = ""
        self._pending_workflow_candidate = None
        # Reset context engineering state for new session
        self._context_compiler.reset()
        return {"status": "ok", "message": "History and buffer cleared"}
    
    def stop(self):
        """Signal the agent to stop current execution."""
        self.stop_requested = True
        return {"status": "ok", "message": "Stop signal sent"}
    
    def _register_internal_tools(self) -> None:
        """Register agent-internal tools (buffer, memory) into TOOL_METADATA so they
        appear in compact_tool_docs() and the planner can assign them via 'Buffer' category."""
        _buffer_tools = [
            {
                "name": "buffer.search",
                "description": "Search or semantically extract content from a stored buffer by keyword or LLM prompt",
                "category": "Buffer",
                "parameters": {
                    "id": {"type": "int", "required": True, "default": None},
                    "query": {"type": "str", "required": False, "default": None},
                    "prompt": {"type": "str", "required": False, "default": None},
                },
                "example": None, "notes": None, "has_var_keyword": False, "function": None,
            },
            {
                "name": "buffer.slice",
                "description": "Return a line range slice from a stored buffer (use to read raw buffer contents)",
                "category": "Buffer",
                "parameters": {
                    "id": {"type": "int", "required": True, "default": None},
                    "start": {"type": "int", "required": False, "default": 0},
                    "end": {"type": "int", "required": False, "default": 50},
                },
                "example": None, "notes": None, "has_var_keyword": False, "function": None,
            },
            {
                "name": "buffer.pipe",
                "description": "Save a buffer's full content to a SANDBOX file",
                "category": "Buffer",
                "parameters": {
                    "id": {"type": "int", "required": True, "default": None},
                    "filename": {"type": "str", "required": True, "default": None},
                },
                "example": None, "notes": None, "has_var_keyword": False, "function": None,
            },
            {
                "name": "buffer.extract",
                "description": "Extract structured data (email/url/phone/number or custom regex) from a buffer",
                "category": "Buffer",
                "parameters": {
                    "id": {"type": "int", "required": True, "default": None},
                    "pattern": {"type": "str", "required": True, "default": None},
                },
                "example": None, "notes": None, "has_var_keyword": False, "function": None,
            },
        ]
        for _t in _buffer_tools:
            TOOL_METADATA.setdefault(_t["name"], _t)

    def _build_system_prompt(self):
        """Build tiny fixed system prompt for small local models."""
        self.system_prompt = (
            "You are MIRA, a local AI assistant running on the user's PC.\n"
            "Rules:\n"
            "1. Use tools to act. Output ONE tool call per turn.\n"
            "2. If step goal says to read a buffer first (e.g. buffer.slice id=1), call that FIRST next turn.\n"
            "3. Confirm success only when tool output says so. Never invent results.\n"
            "4. Only ask the user if information cannot be found with any tool."
        )

    def _make_buffer_observation(self, buf_id: int, data: str, tool_name: str) -> str:
        """Create a compact buffer observation with digest + key refs."""
        word_count = len(data.split())
        digest = self._compress_obs(data, tool_name)[:180]
        refs = self._extract_key_data(data)
        ref_parts = []
        if refs.get("buffers"):
            ref_parts.append("buffers=" + ",".join(refs["buffers"][:3]))
        if refs.get("files"):
            ref_parts.append("files=" + ",".join(refs["files"][:3]))
        if refs.get("phones"):
            ref_parts.append("phones=" + ",".join(refs["phones"][:2]))
        refs_line = f" KeyRefs: {' | '.join(ref_parts)}." if ref_parts else ""
        return (
            f"[BUFFER:{buf_id}] {tool_name} buffered ({word_count} words, {len(data)} chars). "
            f"Digest: {digest}.{refs_line}"
        )

    def _memory_file_path(self) -> Path:
        return (Path(__file__).parent.parent / MEMORY_FILE).resolve()

    def _profile_file_path(self, filename: str) -> Path:
        return (Path(__file__).parent.parent / filename).resolve()

    def _ensure_memory_file(self) -> None:
        path = self._memory_file_path()
        if not path.exists():
            path.write_text("# MEMORY\n\n", encoding="utf-8")

    def _ensure_profile_files(self) -> None:
        for _name, _title in ((SOUL_FILE, "SOUL"), (USER_FILE, "USER")):
            _path = self._profile_file_path(_name)
            if not _path.exists():
                _path.write_text(f"# {_title}\n\n", encoding="utf-8")

    def _read_memory_file(self) -> str:
        self._ensure_memory_file()
        try:
            return self._memory_file_path().read_text(encoding="utf-8")
        except Exception:
            return ""

    def _read_profile_file(self, filename: str) -> str:
        try:
            return self._profile_file_path(filename).read_text(encoding="utf-8")
        except Exception:
            return ""

    def _extract_emails(self, text: str) -> list[str]:
        if not text:
            return []
        found = re.findall(EMAIL_REGEX, text)
        out = []
        seen = set()
        for item in found:
            email = item.strip().lower()
            if email and email not in seen:
                seen.add(email)
                out.append(email)
        return out

    def _extract_phone_like(self, text: str) -> str:
        """Extract a likely phone/JID from free text."""
        if not text:
            return ""
        jid_match = re.search(r'\b\d{8,25}@(?:g\.us|s\.whatsapp\.net)\b', text, re.IGNORECASE)
        if jid_match:
            return jid_match.group(0)
        phone_match = re.search(r'\+?\d[\d\-\s().]{8,18}\d', text)
        if not phone_match:
            return ""
        digits = re.sub(r'[^0-9]', '', phone_match.group(0))
        if 10 <= len(digits) <= 15:
            return digits
        return ""

    def _build_task_contract(self, text: str) -> dict:
        """Extract an immutable, compact task contract from the user prompt."""
        raw = (text or "").strip()
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        lower = raw.lower()

        formulas = []
        for ln in lines:
            for match in re.findall(r'=[A-Z]+\([^)]*\)', ln):
                if match not in formulas:
                    formulas.append(match)

        row_examples = [
            ln[:180] for ln in lines
            if "|" in ln and len([p for p in ln.split("|") if p.strip()]) >= 3
        ][:6]

        columns = []
        for ln in lines:
            if re.search(r'\bdate\b', ln, re.IGNORECASE) and re.search(r'\bcategory\b', ln, re.IGNORECASE) and re.search(r'\bamount\b', ln, re.IGNORECASE):
                parts = [p.strip() for p in re.split(r'\||\t', ln) if p.strip()]
                columns = parts[:8]
                break

        deliverables = []
        if re.search(r'\bexcel|xlsx|spreadsheet\b', lower):
            deliverables.append("excel_file")
        if "whatsapp" in lower and re.search(r'\b(send|share|deliver|forward)\b', lower):
            deliverables.append("whatsapp_delivery")
        if "email" in lower and re.search(r'\b(send|share|deliver|forward)\b', lower):
            deliverables.append("email_delivery")

        required_slots = []
        if "whatsapp_delivery" in deliverables:
            required_slots.append("recipient_phone_or_jid")
        if "email" in lower and re.search(r'\bsend\b', lower):
            required_slots.append("recipient_email")

        known_slots = {}
        maybe_phone = self._extract_phone_like(raw)
        if maybe_phone:
            known_slots["recipient_phone_or_jid"] = maybe_phone
        maybe_email = self._extract_emails(raw)
        if maybe_email:
            known_slots["recipient_email"] = maybe_email[0]

        rows_spec = ""
        m_rows = re.search(r'(\d+\s*[-]\s*\d+)\s*rows', raw, re.IGNORECASE)
        if m_rows:
            rows_spec = m_rows.group(1).replace("", "-") + " rows"

        provided_data = {}
        if columns:
            provided_data["columns"] = columns
        if row_examples:
            provided_data["row_examples"] = row_examples
        if formulas:
            provided_data["formulas"] = formulas
        if rows_spec:
            provided_data["rows_spec"] = rows_spec

        goal = lines[0][:220] if lines else raw[:220]
        source_excerpt = "\n".join(lines[:8])[:1000]
        missing_slots = [s for s in required_slots if not known_slots.get(s)]
        return {
            "goal": goal,
            "deliverables": deliverables,
            "required_slots": required_slots,
            "known_slots": known_slots,
            "missing_slots": missing_slots,
            "provided_data": provided_data,
            "source_excerpt": source_excerpt,
        }

    def _init_working_state(self, contract: dict) -> dict:
        """Create mutable run-scoped state from task contract."""
        required_slots = list(contract.get("required_slots", []))
        known_slots = dict(contract.get("known_slots", {}))
        slots = {slot: known_slots.get(slot) for slot in required_slots}
        deliverables = set(contract.get("deliverables", []))
        artifacts = {}
        if "excel_file" in deliverables:
            artifacts.update({
                "excel_created": False,
                "rows_added": False,
                "formulas_added": False,
                "excel_file_path": "",
            })
        if any(d in deliverables for d in ("whatsapp_delivery", "email_delivery")):
            artifacts["delivery_sent"] = False
        return {
            "slots": slots,
            "artifacts": artifacts,
        }

    def _slot_missing(self, slot: str) -> bool:
        val = self._working_state.get("slots", {}).get(slot)
        return val in (None, "", [], {})

    def _classify_need_input_slot(self, question: str, tool_name: str = "", tool_args: dict | None = None) -> str:
        text = f"{question or ''} {tool_name or ''} {json.dumps(tool_args or {}, default=str)}".lower()
        if any(k in text for k in ("phone", "number", "jid", "whatsapp", "contact")):
            return "recipient_phone_or_jid"
        if "email" in text:
            return "recipient_email"
        if any(k in text for k in ("file", "path", "filename")):
            return "source_file_path"
        return ""

    def _should_request_need_input(self, question: str, tool_name: str = "", tool_args: dict | None = None) -> tuple:
        slot = self._classify_need_input_slot(question, tool_name, tool_args)
        if not slot:
            return True, "", ""
        required = set(self._task_contract.get("required_slots", []))
        if required and slot not in required:
            return False, slot, f"Slot '{slot}' is not required by task contract."
        slots = self._working_state.setdefault("slots", {})
        if slot not in slots:
            slots[slot] = None
        if self._slot_missing(slot):
            return True, slot, ""
        val = str(slots.get(slot, ""))[:120]
        return False, slot, f"Slot '{slot}' already set to '{val}'. Continue without NEED_INPUT."

    def _ingest_user_answer(self, step_idx: int, question: str, answer: str, slot: str = "") -> None:
        self._input_answers[step_idx] = answer
        if not slot:
            slot = self._classify_need_input_slot(question)
        if not slot:
            return
        slots = self._working_state.setdefault("slots", {})
        val = answer.strip()
        if slot == "recipient_phone_or_jid":
            val = self._extract_phone_like(answer) or val
        elif slot == "recipient_email":
            emails = self._extract_emails(answer)
            val = emails[0] if emails else val
        slots[slot] = val

    def _update_working_state_from_tool_result(self, tool_name: str, tool_args: dict, tool_res) -> None:
        slots = self._working_state.setdefault("slots", {})
        artifacts = self._working_state.setdefault("artifacts", {})

        if tool_name == "excel.create":
            artifacts.setdefault("excel_created", False)
            artifacts["excel_created"] = True
            if isinstance(tool_res, dict):
                artifacts["excel_file_path"] = str(tool_res.get("path", "") or artifacts.get("excel_file_path", ""))

        if tool_name == "excel.append":
            artifacts.setdefault("rows_added", False)
            if isinstance(tool_res, dict) and int(tool_res.get("rows_added", 0) or 0) > 0:
                artifacts["rows_added"] = True
            data = tool_args.get("data", [])
            if isinstance(data, list):
                for row in data:
                    if isinstance(row, dict) and any(isinstance(v, str) and v.strip().startswith("=") for v in row.values()):
                        artifacts.setdefault("formulas_added", False)
                        artifacts["formulas_added"] = True
                        break

        if tool_name in ("whatsapp.send", "whatsapp.send_media"):
            artifacts.setdefault("delivery_sent", False)
            artifacts["delivery_sent"] = True
            to_val = str(tool_args.get("to", "")).strip()
            if to_val:
                slots["recipient_phone_or_jid"] = self._extract_phone_like(to_val) or to_val

        if not tool_name.startswith("contacts."):
            return
        phone = ""
        email = ""
        if isinstance(tool_res, dict):
            if isinstance(tool_res.get("contact"), dict):
                phone = str(tool_res["contact"].get("phone", "") or "")
                email = str(tool_res["contact"].get("email", "") or "")
            elif isinstance(tool_res.get("contacts"), list) and tool_res["contacts"]:
                first = tool_res["contacts"][0]
                if isinstance(first, dict):
                    phone = str(first.get("phone", "") or "")
                    email = str(first.get("email", "") or "")
        phone = self._extract_phone_like(phone or "")
        if phone:
            slots["recipient_phone_or_jid"] = phone
        if email and "@" in email:
            slots["recipient_email"] = email

    def _get_task_contract_context(self) -> str:
        if not self._task_contract:
            return ""
        c = self._task_contract
        lines = [
            "TASK CONTRACT (immutable; extracted from user request):",
            f"- Goal: {c.get('goal', '')[:220]}",
        ]
        if c.get("deliverables"):
            lines.append("- Deliverables: " + ", ".join(c.get("deliverables", [])[:6]))
        pd = c.get("provided_data", {})
        if pd.get("columns"):
            lines.append("- Columns: " + ", ".join(pd.get("columns", [])[:8]))
        if pd.get("rows_spec"):
            lines.append(f"- Rows: {pd.get('rows_spec')}")
        if pd.get("formulas"):
            lines.append("- Formulas: " + ", ".join(pd.get("formulas", [])[:8]))
        if pd.get("row_examples"):
            lines.append("- Example rows: " + " || ".join(pd.get("row_examples", [])[:2]))
        req_slots = c.get("required_slots", [])
        if req_slots:
            lines.append("- Required slots: " + ", ".join(req_slots))
            missing = [s for s in req_slots if self._slot_missing(s)]
            lines.append("- Missing slots: " + (", ".join(missing) if missing else "none"))
        return "\n".join(lines) + "\n\n"

    def _get_working_state_context(self) -> str:
        ws = self._working_state or {}
        slots = ws.get("slots", {})
        artifacts = ws.get("artifacts", {})
        if not slots and not artifacts:
            return ""
        lines = ["WORKING STATE (run-scoped):"]
        if slots:
            for k in sorted(slots.keys()):
                v = slots.get(k)
                vv = str(v)[:120] if v not in (None, "", [], {}) else "missing"
                lines.append(f"- slot.{k}: {vv}")
        if artifacts:
            preferred = ["excel_created", "rows_added", "formulas_added", "excel_file_path", "delivery_sent"]
            rendered = set()
            for k in preferred:
                if k in artifacts:
                    lines.append(f"- artifact.{k}: {artifacts.get(k)}")
                    rendered.add(k)
            for k in sorted(artifacts.keys()):
                if k not in rendered:
                    lines.append(f"- artifact.{k}: {artifacts.get(k)}")
        return "\n".join(lines) + "\n\n"

    def _step_needs_history(self, step_desc: str) -> bool:
        s = (step_desc or "").lower()
        if re.search(r'\b(previous|earlier|as before|same as|continue|again|follow[- ]up|based on prior)\b', s):
            return True
        if self._step_requires_handoff(step_desc):
            return True
        return bool(re.search(r'\b(compile|summari[sz]e|format|based on|from above|from prior)\b', s))

    def _step_requires_handoff(self, step_desc: str) -> bool:
        s = (step_desc or "").lower()
        sendish = bool(re.search(r'\b(send|share|forward|message|whatsapp|email)\b', s))
        refers_payload = bool(
            re.search(
                r'\b(them|it|this|that|these|those|list|results?|summary|titles?|themes?|findings|report|books?|data)\b',
                s,
            )
        )
        return sendish and refers_payload

    def _build_handoff_hint(self, completed: list, step_desc: str) -> str:
        """Compact dependency handoff so downstream steps can reuse prior results."""
        if not completed or not self._step_requires_handoff(step_desc):
            return ""
        lines = [
            "DEPENDENCY HANDOFF:",
            "Reuse prior payload below. Do not send an empty message.",
        ]
        kept = 0
        for idx in range(len(completed) - 1, -1, -1):
            item = completed[idx]
            if item.get("status") != "done":
                continue
            tool = str(item.get("tool", "") or "")
            if tool.startswith("whatsapp.") or tool.startswith("email."):
                continue
            result = str(item.get("result", "") or "").strip()
            if not result or result.lower() in ("ok", "no result", "none", "null"):
                continue
            artifacts = item.get("artifacts", {}) if isinstance(item, dict) else {}
            refs = []
            if artifacts.get("buffers"):
                refs.append("buffers=" + ",".join(str(x) for x in artifacts["buffers"][:3]))
            if artifacts.get("files"):
                refs.append("files=" + ",".join(str(x) for x in artifacts["files"][:2]))
            lines.append(f"- Step {idx+1}: {item.get('step', '')[:120]}")
            lines.append(f"  Payload: {result[:420]}")
            if refs:
                lines.append(f"  Refs: {' | '.join(refs)}")
            kept += 1
            if kept >= 2:
                break
        if kept == 0:
            return ""
        lines.append("For whatsapp.send, include non-empty text in message/body/text/content.")
        return "\n".join(lines) + "\n"

    def _maybe_update_handoff_payload(self, step_tool: str, step_result: str) -> None:
        """Keep latest useful non-delivery text as fallback payload for send steps."""
        t = (step_tool or "").lower()
        if not step_result:
            return
        if t.startswith((
            "whatsapp.", "email.", "contacts.", "calendar.", "system.", "memory.",
            "user.", "soul.", "profile."
        )):
            return
        text = str(step_result).strip()
        if len(text) < 12:
            return
        if text.lower() in ("ok", "no result", "none", "null"):
            return
        self._handoff_payload = text[:1800]

    def _persist_user_email(self, email: str) -> None:
        """Persist preferred user email to USER.md and MEMORY.md."""
        e = (email or "").strip().lower()
        if not e:
            return

        # Update USER.md (upsert an Email field)
        user_path = self._profile_file_path(USER_FILE)
        user_text = self._read_profile_file(USER_FILE)
        if not user_text:
            user_text = "# USER\n\n"
        if re.search(r"^\s*-\s*\*\*Email:\*\*", user_text, flags=re.IGNORECASE | re.MULTILINE):
            user_text = re.sub(
                r"^\s*-\s*\*\*Email:\*\*.*$",
                f"- **Email:** {e}",
                user_text,
                flags=re.IGNORECASE | re.MULTILINE,
            )
        else:
            anchor = r"(^\s*-\s*\*\*What to call them:\*\*.*$)"
            if re.search(anchor, user_text, flags=re.IGNORECASE | re.MULTILINE):
                user_text = re.sub(
                    anchor,
                    r"\1\n- **Email:** " + e,
                    user_text,
                    count=1,
                    flags=re.IGNORECASE | re.MULTILINE,
                )
            else:
                if not user_text.endswith("\n"):
                    user_text += "\n"
                user_text += f"- **Email:** {e}\n"
        user_path.write_text(user_text, encoding="utf-8")

        # Update MEMORY.md (single tagged bullet)
        mem_path = self._memory_file_path()
        mem_text = self._read_memory_file()
        if not mem_text:
            mem_text = "# MEMORY\n\n"
        tag_prefix = "- [user-email] "
        lines = mem_text.splitlines()
        replaced = False
        for idx, line in enumerate(lines):
            if line.strip().lower().startswith(tag_prefix):
                lines[idx] = f"- [user-email] {e}"
                replaced = True
                break
        if not replaced:
            if lines and lines[-1].strip() != "":
                lines.append("")
            lines.append(f"- [user-email] {e}")
        mem_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _get_preferred_user_email(self) -> str:
        """Resolve preferred user email from USER.md first, then MEMORY.md."""
        candidates = []
        user_text = self._read_profile_file(USER_FILE)
        mem_text = self._read_memory_file()
        candidates.extend(self._extract_emails(user_text))
        candidates.extend(self._extract_emails(mem_text))
        for email in candidates:
            if email.endswith("@example.com"):
                continue
            return email
        return ""

    def _get_buffer_context(self) -> str:
        """Build context string listing a bounded set of active buffers."""
        if not self.buffer:
            return "ACTIVE BUFFERS: none. Use memory.write to store memory notes; use buffer.search/extract/slice/pipe after data is stored as [BUFFER:<id>].\n\n"
        ids = sorted(self.buffer.keys())[-MAX_BUFFER_CONTEXT_ITEMS:]
        lines = [f"ACTIVE BUFFERS (showing {len(ids)} of {len(self.buffer)}; data already available):"]
        for buf_id in ids:
            buf = self.buffer.get(buf_id, {})
            tool = buf.get("tool", "unknown") if isinstance(buf, dict) else "unknown"
            chars = buf.get("chars", 0) if isinstance(buf, dict) else len(buf)
            lines.append(
                f"  [BUFFER:{buf_id}] from {tool}, {chars} chars. "
                f"Use buffer.search/extract/slice/pipe with id={buf_id}."
            )
        return "\n".join(lines) + "\n\n"

    def _get_memory_context(self) -> str:
        """Return MEMORY.md content with explicit memory_note_id labels."""
        memory_text = self._read_memory_file().strip()
        if not memory_text:
            return ""

        lines = memory_text.splitlines()
        note_id = 0
        rendered = []
        for line in lines:
            if line.strip().startswith("-"):
                note_id += 1
                rendered.append(f"- [memory_note_id:{note_id}] {line.strip()[1:].strip()}")
            else:
                rendered.append(line)

        memory_view = "\n".join(rendered).strip()
        if len(memory_view) > MAX_MEMORY_CONTEXT_CHARS:
            memory_view = "[truncated to recent section]\n" + memory_view[-MAX_MEMORY_CONTEXT_CHARS:]

        return (
            f"PERSISTENT MEMORY ({MEMORY_FILE}, auto-injected each turn):\n"
            f"{memory_view}\n\n"
        )

    def _get_profile_context(self, include_user: bool = True, include_soul: bool = True) -> str:
        """Return profile context with safe truncation."""
        soul = self._read_profile_file(SOUL_FILE).strip() if include_soul else ""
        user = self._read_profile_file(USER_FILE).strip() if include_user else ""

        if soul and len(soul) > MAX_PROFILE_CONTEXT_CHARS:
            soul = soul[:MAX_PROFILE_CONTEXT_CHARS] + "\n...[truncated]"
        if user and len(user) > MAX_PROFILE_CONTEXT_CHARS:
            user = user[:MAX_PROFILE_CONTEXT_CHARS] + "\n...[truncated]"

        blocks = []
        if soul:
            blocks.append(f"SOUL CONTEXT ({SOUL_FILE}):\n{soul}")
        if user:
            blocks.append(f"USER CONTEXT ({USER_FILE}):\n{user}")

        if not blocks:
            return ""
        return "\n\n".join(blocks) + "\n\n"

    def _get_plan_profile_context(self) -> str:
        """
        Return only a few key USER facts for planning.
        """
        user_text = self._read_profile_file(USER_FILE).strip()
        if not user_text:
            return ""
        lines = [ln.strip() for ln in user_text.splitlines() if ln.strip().startswith("-")]
        if not lines:
            return ""
        top = lines[:MAX_PLAN_USER_FACTS]
        return "USER FACTS (planner):\n" + "\n".join(top) + "\n\n"

    def _get_relevant_memory_context(self, task: str, max_items: int = MAX_PLAN_MEMORY_ITEMS) -> str:
        """
        Inject only task-relevant memory bullets + recent notes.
        """
        memory_text = self._read_memory_file().strip()
        if not memory_text:
            return ""
        lines = [ln.strip() for ln in memory_text.splitlines() if ln.strip().startswith("-")]
        if not lines:
            return ""
        task_words = {w for w in re.findall(r"[a-zA-Z0-9_]{3,}", (task or "").lower())}
        scored = []
        for ln in lines:
            ln_words = {w for w in re.findall(r"[a-zA-Z0-9_]{3,}", ln.lower())}
            overlap = len(task_words & ln_words)
            scored.append((overlap, ln))
        top = [ln for _, ln in sorted(scored, key=lambda x: x[0], reverse=True)[:max_items]]
        recent = lines[-2:]
        merged = []
        for ln in top + recent:
            if ln not in merged:
                merged.append(ln)
        picked = merged[:max_items]
        if not picked:
            return ""
        return "RELEVANT MEMORY:\n" + "\n".join(picked) + "\n\n"

    def _build_history_text(
        self,
        turns: list[tuple[str, str]],
        max_turns: int = 2,
        user_chars: int = 180,
        assistant_chars: int = 180,
    ) -> str:
        """
        Compact conversation history for small-model prompts.
        """
        if not turns:
            return ""
        out = []
        for u, a in turns[-max_turns:]:
            uu = (u or "").strip()
            aa = (a or "").strip()
            if len(uu) > user_chars:
                uu = uu[:user_chars] + "...[truncated]"
            if len(aa) > assistant_chars:
                aa = aa[:assistant_chars] + "...[truncated]"
            out.append(f"User: {uu}\nMIRA: {aa}")
        return "\n".join(out) + ("\n" if out else "")

    def _normalize_categories(self, categories: list) -> set:
        """Normalize raw category strings to canonical CATEGORY_KEYWORDS keys.

        Handles casing variants ("web & search", "Web&Search", "web and search") and
        maps them to the exact keys used in CATEGORY_KEYWORDS / TOOL_METADATA so that
        tool-prediction and category-gate logic works reliably.
        """
        CANONICAL = {
            "web & search": "Web & Search",
            "web&search": "Web & Search",
            "web and search": "Web & Search",
            "web search": "Web & Search",
            "search": "Web & Search",
            "web scraping": "Web Scraping",
            "scraping": "Web Scraping",
            "filesystem": "Filesystem",
            "file": "Filesystem",
            "files": "Filesystem",
            "email": "Email",
            "calendar": "Calendar",
            "todo list": "Todo List",
            "todo": "Todo List",
            "todos": "Todo List",
            "whatsapp": "WhatsApp",
            "contacts": "Contacts",
            "knowledge base": "Knowledge Base",
            "knowledge": "Knowledge Base",
            "buffer": "Buffer",
            "system": "System",
            "workflows": "Workflows",
            "workflow": "Workflows",
            "memory": "Memory",
            "documents": "Documents",
            "pdf": "Documents",
        }
        out = set()
        for cat in (categories or []):
            s = str(cat).strip()
            lower = s.lower()
            if lower in CANONICAL:
                out.add(CANONICAL[lower])
            else:
                # Fall back to title-case as-is (handles exact matches like "Filesystem")
                out.add(s if s else "System")
        return out

    def _predict_step_tools(self, step_desc: str, categories: list[str] | None = None) -> list[str]:
        """
        Deterministically predict the most likely tool + one backup for this step.
        """
        cats = self._normalize_categories(categories or [])
        step_l = (step_desc or "").lower()

        rules = [
            ("Web & Search", r"\bnews\b", "search.news"),
            ("Web & Search", r"\b(search|find|lookup|look up)\b", "search.duckduckgo"),
            ("Filesystem", r"\b(write|save|create file)\b", "filesystem.write"),
            ("Filesystem", r"\b(read|open|view)\b", "filesystem.read"),
            ("Filesystem", r"\b(list|show files|directory)\b", "filesystem.list"),
            ("Email", r"\bsend\b", "email.send"),
            ("Email", r"\b(list|read|inbox|fetch)\b", "email.list"),
            ("Calendar", r"\b(add|create|schedule)\b", "calendar.add_event"),
            ("Calendar", r"\b(list|show|upcoming)\b", "calendar.list_events"),
            ("Todo List", r"\b(add|create)\b", "todo.add"),
            ("Todo List", r"\b(list|show)\b", "todo.list"),
            ("Contacts", r"\b(find|lookup|search|get)\b", "contacts.search"),
            ("WhatsApp", r"\b(document|pdf|file)\b", "whatsapp.send_media"),
            ("WhatsApp", r"\b(send|message|text)\b", "whatsapp.send"),
            ("Workflows", r"\b(list workflows|show workflows)\b", "workflow.list"),
            ("Workflows", r"\b(run workflow|execute workflow)\b", "workflow.run"),
            ("Workflows", r"\b(save workflow|remember workflow)\b", "workflow.save"),
        ]

        predicted = ""
        for cat, pattern, tool in rules:
            if cat in cats and re.search(pattern, step_l):
                if tool in self.tools:
                    predicted = tool
                    break

        if not predicted:
            for cat in cats:
                pool = [
                    name for name, meta in TOOL_METADATA.items()
                    if str(meta.get("category", "")).strip().lower() == cat.strip().lower()
                    and name in self.tools
                ]
                if pool:
                    predicted = sorted(pool)[0]
                    break

        if not predicted:
            return []

        pred_meta = TOOL_METADATA.get(predicted, {})
        pred_cat = str(pred_meta.get("category", "")).strip().lower()
        backups = [
            name for name, meta in TOOL_METADATA.items()
            if name != predicted
            and name in self.tools
            and str(meta.get("category", "")).strip().lower() == pred_cat
        ]
        backup = sorted(backups)[0] if backups else ""
        return [predicted] + ([backup] if backup else [])

    def _build_native_tool_defs(self, tool_names: list[str]) -> list[dict]:
        """
        Convert internal tool metadata to Ollama/OpenAI function-tool schema.
        """
        out = []
        for name in tool_names:
            meta = TOOL_METADATA.get(name)
            if not meta:
                continue
            props = {}
            required = []
            for p_name, p_info in (meta.get("parameters", {}) or {}).items():
                p_type = str((p_info or {}).get("type", "string")).lower()
                j_type = "string"
                if any(x in p_type for x in ("int", "float", "number")):
                    j_type = "number"
                elif "bool" in p_type:
                    j_type = "boolean"
                elif any(x in p_type for x in ("list", "array")):
                    j_type = "array"
                elif any(x in p_type for x in ("dict", "object", "map")):
                    j_type = "object"
                props[p_name] = {"type": j_type}
                if p_info.get("required"):
                    required.append(p_name)
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": str(meta.get("description", ""))[:300],
                        "parameters": {
                            "type": "object",
                            "properties": props,
                            "required": required,
                        },
                    },
                }
            )
        return out

    def _coerce_kv_value(self, raw: str):
        s = (raw or "").strip()
        if not s:
            return ""
        if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
            return s[1:-1]
        sl = s.lower()
        if sl in ("true", "false"):
            return sl == "true"
        if sl in ("null", "none"):
            return None
        if re.fullmatch(r"-?\d+", s):
            try:
                return int(s)
            except Exception:
                return s
        if re.fullmatch(r"-?\d+\.\d+", s):
            try:
                return float(s)
            except Exception:
                return s
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            try:
                return json.loads(s)
            except Exception:
                return s
        return s

    def _extract_tool_kv_call(self, text: str):
        """
        Parse simplified ACT protocol:
          TOOL: tool.name | arg=value | arg2=value
          or
          TOOL: tool.name
          args: arg=value, arg2=value
        """
        if not text:
            return None
        lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
        if not lines:
            return None
        first = lines[0]
        m = re.match(r"^\s*TOOL\s*:\s*([a-zA-Z0-9._\-]+)\s*(.*)$", first, re.IGNORECASE)
        if not m:
            return None
        tool = m.group(1).strip()
        tail = m.group(2).strip()
        arg_blob = tail
        if len(lines) > 1 and re.match(r"^\s*args\s*:", lines[1], re.IGNORECASE):
            arg_blob = lines[1].split(":", 1)[-1].strip()
        args = {}
        if arg_blob:
            parts = [p.strip() for p in re.split(r"\s*\|\s*|,\s*", arg_blob) if p.strip()]
            for part in parts:
                if "=" not in part:
                    continue
                k, v = part.split("=", 1)
                key = k.strip()
                if not key:
                    continue
                args[key] = self._coerce_kv_value(v)
        return tool, args, ""

    def _should_skip_synthesis(self, completed: list) -> bool:
        """
        Skip synthesis for simple successful tasks.
        """
        if not completed:
            return False
        if not all(s.get("status") == "done" for s in completed):
            return False
        if len(completed) == 1:
            return True
        action_roots = ("calendar.", "email.", "todo.", "contacts.", "whatsapp.")
        return all(str(s.get("tool", "")).startswith(action_roots) for s in completed)

    def _render_simple_completion(self, completed: list) -> str:
        """
        Deterministic final message when synthesis is skipped.
        """
        if not completed:
            return "Done."
        if len(completed) == 1:
            s = completed[0]
            return f"Done. {str(s.get('result', '')).strip()[:280]}".strip()
        lines = ["Done. Completed steps:"]
        for idx, s in enumerate(completed[:6], start=1):
            lines.append(f"{idx}. {str(s.get('result', '')).strip()[:140]}")
        return "\n".join(lines)

    def _maybe_stage_workflow_suggestion(
        self,
        task_text: str,
        completed: list,
        final_response: str,
        workflow_was_auto_used: bool = False,
    ) -> str:
        """
        For successful multi-step runs, stage a yes/no workflow-save prompt.
        """
        if workflow_was_auto_used:
            return final_response
        if self._pending_workflow_candidate:
            return final_response
        if not completed or len(completed) < 2:
            return final_response
        if not all(str(s.get("status", "")) == "done" for s in completed):
            return final_response

        steps = []
        for s in completed:
            goal = str(s.get("step", "")).strip()
            if not goal:
                continue
            cats = s.get("categories", [])
            if isinstance(cats, str):
                cats = [c.strip() for c in re.split(r"[,/|]+", cats) if c.strip()]
            if not isinstance(cats, list):
                cats = []
            steps.append({"goal": goal, "categories": self._normalize_categories(cats)})
        if len(steps) < 2:
            return final_response

        self._pending_workflow_candidate = {
            "task": task_text,
            "name": self._suggest_workflow_name(task_text),
            "steps": steps,
            "trigger_keywords": self._suggest_workflow_triggers(task_text),
            "variables": [],
        }
        n = len(steps)
        prompt = f"I completed this in {n} steps. Save this as a reusable workflow? (yes/no)"
        return (final_response.rstrip() + "\n\n" + prompt).strip()

    def _workflow_dir_path(self) -> Path:
        return (Path(__file__).parent.parent / "SANDBOX" / "workflows").resolve()

    def _load_workflow_library(self) -> list[dict]:
        wdir = self._workflow_dir_path()
        wdir.mkdir(parents=True, exist_ok=True)
        items = []
        for f in sorted(wdir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data["_path"] = str(f)
                    items.append(data)
            except Exception:
                continue
        return items

    def _infer_workflow_variables(self, text: str, wf: dict, matched_kw: str) -> dict:
        vars_out = {}
        variables = [str(v).strip() for v in (wf.get("variables", []) or []) if str(v).strip()]
        if not variables:
            return vars_out
        t = (text or "").strip()
        quoted = re.findall(r'"([^"]+)"|\'([^\']+)\'', t)
        qflat = [a or b for a, b in quoted if (a or b)]
        for i, var in enumerate(variables):
            if i < len(qflat):
                vars_out[var] = qflat[i]
        if variables and variables[0] not in vars_out:
            rem = t
            if matched_kw:
                idx = rem.lower().find(matched_kw.lower())
                if idx >= 0:
                    rem = rem[idx + len(matched_kw):].strip(" :,-")
            if rem:
                vars_out[variables[0]] = rem
        return vars_out

    def _expand_workflow_steps(self, wf: dict, variables: dict | None = None) -> list[dict]:
        vars_map = {str(k): str(v) for k, v in (variables or {}).items()}
        out = []
        for st in (wf.get("steps", []) or []):
            if not isinstance(st, dict):
                continue
            goal = str(st.get("goal", ""))
            for k, v in vars_map.items():
                goal = goal.replace("{" + k + "}", v)
            cats = st.get("categories", [])
            if isinstance(cats, str):
                cats = [c.strip() for c in re.split(r"[,/|]+", cats) if c.strip()]
            out.append({"goal": goal, "categories": self._normalize_categories(cats if isinstance(cats, list) else [])})
        return out

    def _match_workflow_trigger(self, text: str) -> tuple[dict | None, dict]:
        """
        Return best matching workflow and inferred variables from user text.
        """
        msg = (text or "").lower()
        best = None
        best_score = -1
        best_kw = ""
        for wf in self._load_workflow_library():
            kws = [str(k).strip().lower() for k in (wf.get("trigger_keywords", []) or []) if str(k).strip()]
            for kw in kws:
                if kw and kw in msg:
                    score = len(kw)
                    if score > best_score:
                        best = wf
                        best_score = score
                        best_kw = kw
        if not best:
            return None, {}
        vars_map = self._infer_workflow_variables(text, best, best_kw)
        return best, vars_map

    def _suggest_workflow_name(self, task_text: str) -> str:
        raw = (task_text or "").strip().lower()
        words = re.findall(r"[a-z0-9]+", raw)
        keep = [w for w in words if w not in {"the", "a", "an", "to", "for", "and", "with", "on", "in"}]
        if not keep:
            keep = ["workflow"]
        name = "_".join(keep[:4])
        return name[:64] or "workflow"

    def _suggest_workflow_triggers(self, task_text: str) -> list[str]:
        text = (task_text or "").lower()
        chunks = [c.strip() for c in re.split(r"[,.]| and | then ", text) if c.strip()]
        out = []
        for ch in chunks[:3]:
            words = [w for w in re.findall(r"[a-z0-9]+", ch) if len(w) > 2]
            if words:
                out.append(" ".join(words[:3]))
        # de-duplicate, keep order
        dedup = []
        for x in out:
            if x not in dedup:
                dedup.append(x)
        return dedup[:3]

    def _get_recent_tool_outputs_context(self, recent_outputs: list[dict], step_desc: str = "", step_idx: int = 0) -> str:
        """
        Return tool outputs context using relevance-ranked retrieval.
        
        Falls back to legacy fixed-window if tool record store has no data
        (backward compatibility), otherwise uses the structured ToolRecordStore
        for similarity + dependency-based ranking.
        """
        # Use new relevance-ranked retrieval when available
        if self._context_compiler.tool_records.all_records:
            return self._context_compiler.tool_records.render_recent_context(
                current_step_desc=step_desc,
                current_step_idx=step_idx,
                max_records=6,
                max_raw_chars=800,
            )

        # Legacy fallback for backward compatibility
        if not recent_outputs:
            return ""
        tail = recent_outputs[-MAX_RECENT_TOOL_OUTPUTS:]
        lines = ["RECENT RAW TOOL OUTPUTS (latest first):"]
        for item in reversed(tail):
            tool = item.get("tool", "unknown")
            status = item.get("status", "ok")
            raw = str(item.get("raw", ""))
            summary = self._compress_obs(raw, tool)
            if len(raw) > MAX_RECENT_TOOL_OUTPUT_CHARS:
                raw = raw[:MAX_RECENT_TOOL_OUTPUT_CHARS] + "\n...[truncated]"
            lines.append(f"--- {tool} [{status}] ---")
            lines.append(f"Summary: {summary[:220]}")
            lines.append(raw)
        return "\n".join(lines) + "\n\n"

    def _estimate_tokens(self, text: str) -> int:
        """Token counting  delegates to accurate tokenizer (tiktoken or fallback)."""
        return count_tokens(text)

    def _summarize_block(self, text: str, max_chars: int, prefer_tail: bool = False) -> str:
        if not text or len(text) <= max_chars:
            return text
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            return text[:max_chars]
        if len(lines) <= 12:
            core = "\n".join(lines)
        else:
            core = "\n".join(lines[:6] + ["...[middle omitted for context budget]..."] + lines[-6:])
        if len(core) > max_chars:
            core = core[-max_chars:] if prefer_tail else core[:max_chars]
        return core

    def _fit_context_budget(self, sections: dict[str, str], step_type: str = "act") -> tuple[dict[str, str], str]:
        """Budget-fit using TokenBudget engine (accurate token counting + adaptive allocation)."""
        return self._context_compiler.fit_context_budget_compat(sections, step_type=step_type)

    def _append_file_bullet(self, path: Path, section_header: str, bullet: str) -> None:
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            text = ""
        if not text:
            text = f"# {path.stem}\n\n"
        if section_header not in text:
            if not text.endswith("\n"):
                text += "\n"
            text += f"\n{section_header}\n"
        if not text.endswith("\n"):
            text += "\n"
        text += f"- {bullet}\n"
        path.write_text(text, encoding="utf-8")

    def _should_buffer_result(self, tool_name: str, observation: str) -> bool:
        """Decide whether to persist a tool result into in-memory buffer."""
        if tool_name.startswith("buffer.") or tool_name.startswith("memory."):
            return False
        if any(tool_name.startswith(prefix) for prefix in FORCE_BUFFER_PREFIXES):
            return True
        return len(observation) > BUFFER_THRESHOLD_CHARS

    def _get_tool_categories_summary(self) -> str:
        """Return one-line summary of tool categories for planning stage."""
        from mcp_server.tools import get_tools_by_category

        cats = get_tools_by_category()
        lines = ["TOOL CATEGORIES (choose from these names exactly):"]
        for cat in sorted(cats.keys()):
            names = sorted(t["name"] for t in cats[cat])
            preview = ", ".join(names[:4])
            suffix = f" (+{len(names)-4} more)" if len(names) > 4 else ""
            lines.append(f"- {cat}: {preview}{suffix}")
        return "\n".join(lines)

    def _normalize_categories(self, categories: list[str] | None) -> list[str]:
        """Normalize and deduplicate category names against registered categories."""
        if not categories:
            return []
        from mcp_server.tools import get_tools_by_category

        available = {c.strip().lower(): c for c in get_tools_by_category().keys()}
        out = []
        seen = set()
        for raw in categories:
            key = str(raw).strip().lower()
            if not key:
                continue
            if key in available and key not in seen:
                seen.add(key)
                out.append(available[key])
        return out

    def _parse_plan(self, text: str) -> list:
        """Extract numbered steps and optional categories from planning output."""
        steps = []
        for line in text.strip().splitlines():
            line = line.strip()
            m = re.match(
                r'^(?:step\s*)?(\d+)[.):\-\s]+(.+?)(?:\s*\|\s*categories?\s*:\s*(.+))?$',
                line,
                re.IGNORECASE,
            )
            if not m:
                continue
            goal = m.group(2).strip()
            if not goal or len(goal) <= 3:
                continue
            raw_cats = m.group(3) or ""
            cats = [c.strip() for c in re.split(r'[,/|]+', raw_cats) if c.strip()]
            steps.append({"goal": goal, "categories": self._normalize_categories(cats)})
        return steps

    def _should_split_compound_task(self, user_text: str, steps: list) -> bool:
        """Return True when planner produced a single broad step for a multi-intent task.

        Catches patterns like:
          - "find X and send to Y via mail"  (no explicit linker, just "and")
          - "search X then email Y"
        """
        if len(steps) != 1:
            return False
        t = (user_text or "").lower()
        if not t.strip():
            return False
        # Explicit linker words always trigger split
        has_linker = bool(re.search(r'\b(and then|then|after that|next|finally|also)\b', t))
        if has_linker:
            return True
        # Count DISTINCT cross-category intents — 2+ means it's a compound task
        intent_count = 0
        if re.search(r'\b(find|search|research|look up|gather|get)\b', t):
            intent_count += 1
        if re.search(r'\b(pdf|document|doc|report|convert|summari[sz]e|excel|xlsx|spreadsheet|table|formula)\b', t):
            intent_count += 1
        if re.search(r'\b(send|share|forward|email|mail|message)\b', t):
            intent_count += 1
        if re.search(r'\b(whatsapp|wa|telegram|slack)\b', t):
            intent_count += 1
        return intent_count >= 2

    def _split_compound_task(self, user_text: str) -> list:
        """Deterministically split compound user task into atomic, buffer-aware steps.

        Produces concrete step goals instead of vague generic labels.
        The [BUFFER:?] placeholder is replaced with the real ID by
        _inject_buffer_refs_into_steps() once the search step completes.
        """
        text = (user_text or "").strip()
        if not text:
            return []

        # Try linker-based split first ("find X, then send")
        parts = [
            p.strip(" ,.;")
            for p in re.split(r'\b(?:and then|then|after that|next|finally|also)\b', text, flags=re.IGNORECASE)
            if p and p.strip(" ,.;")
        ]
        out = []
        for part in parts:
            cats = sorted(self._select_categories(part, user_message=text))
            out.append({"goal": part, "categories": cats})

        # Build atomic steps when linker-split didn't produce multiple parts
        if len(out) <= 1:
            t = text.lower()
            out = []

            # Step A: Search / Research
            if re.search(r'\b(find|search|research|look up|gather|get)\b', t):
                # Extract subject: text after the verb, before "and"/delivery verb
                m = re.search(
                    r'\b(?:find|search|research|look\s+up|gather|get)\b\s+(.+?)'
                    r'(?:\s+and\b|\s+then\b|\s+(?:send|email|mail|forward|share|whatsapp)\b|$)',
                    text, re.IGNORECASE
                )
                subject = m.group(1).strip().rstrip(' .,;') if m else text
                out.append({
                    "goal": f"Search for: {subject}",
                    "categories": sorted(self._select_categories("search web", user_message=text)),
                })

            # Step B: Create document artifact if requested
            if re.search(r'\b(pdf|document|doc|report|convert|summari[sz]e|excel|xlsx|spreadsheet|table|formula)\b', t):
                out.append({
                    "goal": "Create the requested document using data from [BUFFER:?] (call buffer.slice id=<id> first to read it).",
                    "categories": sorted(self._select_categories("create document", user_message=text)),
                })

            if re.search(r'\b(send|share|forward|email|mail|message|whatsapp)\b', t):
                # Extract recipient name
                m_recv = re.search(
                    r'\b(?:send|share|forward|email|mail|message|whatsapp)\b[\w\s]*?'
                    r'(?:to|via)?\s+([A-Za-z][a-z]+)',
                    text, re.IGNORECASE
                )
                recipient = m_recv.group(1).strip() if m_recv else None
                via_mail = bool(re.search(r'\b(mail|email|gmail)\b', t))
                via_wa   = bool(re.search(r'\b(whatsapp|wa)\b', t))

                # Step C: Look up contact first
                if recipient:
                    out.append({
                        "goal": f"Find {recipient}'s contact details using contacts.search name='{recipient}'.",
                        "categories": ["Contacts"],
                    })

                # Step D: Send
                if via_mail:
                    recip_hint = f" to {recipient}" if recipient else ""
                    out.append({
                        "goal": (
                            f"Send email{recip_hint}: use buffer.slice id=<id> to read [BUFFER:?], "
                            "compose a concise body, then email.send."
                        ),
                        "categories": ["Email", "Buffer"],
                    })
                elif via_wa:
                    out.append({
                        "goal": "Send the information via whatsapp.send using recipient's phone number from contacts.",
                        "categories": ["WhatsApp", "Buffer"],
                    })
                else:
                    out.append({
                        "goal": "Send the gathered information to the recipient via the appropriate channel.",
                        "categories": ["Email", "WhatsApp", "Contacts", "Buffer"],
                    })

        # Fallback: single step
        if not out:
            out = [{"goal": text, "categories": sorted(self._select_categories(text, user_message=text))}]

        return out

    def _inject_buffer_refs_into_steps(
        self, steps: list, completed_step: dict, buffer: dict
    ) -> list:
        """Replace [BUFFER:?] placeholders with the real buffer ID after a search step.

        Called in the execute loop immediately after a search/scraping step succeeds
        so downstream email/send steps know exactly which buffer to read.
        """
        step_tool = completed_step.get("tool", "")
        if not step_tool.startswith(("search.", "firecrawl.")):
            return steps
        # Find the highest numeric buffer ID that exists
        latest_id = max(
            (int(k) for k in buffer.keys() if str(k).isdigit()), default=None
        )
        if latest_id is None:
            return steps
        buf_ref = f"[BUFFER:{latest_id}]"
        updated = []
        for s in steps:
            goal = str(s.get("goal", ""))
            # Replace placeholder
            goal = re.sub(r'\[BUFFER:\?\]', buf_ref, goal)
            # Also replace generic "id=<id>" hint with real id
            goal = re.sub(r'id=<id>', f'id={latest_id}', goal)
            updated.append({**s, "goal": goal})
        return updated


    def _distill_search_result(self, raw_obs: str, step_goal: str, model: str) -> str:
        """Compress noisy search/scrape JSON into a clean bullet-point fact list.

        Called synchronously (already inside asyncio.to_thread) before storing
        in the buffer.  Falls back to raw_obs if the LLM call fails or is slow.

        Budget: ~80 tokens prompt, ~200 tokens completion → negligible on small models.
        """
        # Skip distillation for very small results — already compact
        if len(raw_obs) < 500:
            return raw_obs
        # Extract just the text snippets from JSON to reduce prompt size
        try:
            import json as _json
            data = _json.loads(raw_obs)
            # Flatten: pull title + snippet from results list
            if isinstance(data, dict) and "results" in data:
                lines = []
                for r in data["results"][:8]:
                    title   = str(r.get("title", "")).strip()
                    snippet = str(r.get("snippet", "")).strip()
                    url     = str(r.get("url", "")).strip()
                    if title or snippet:
                        lines.append(f"- {title}: {snippet[:200]}")
                raw_text = "\n".join(lines)
            else:
                raw_text = raw_obs[:1800]
        except Exception:
            raw_text = raw_obs[:1800]

        goal_short = (step_goal or "").strip()[:120]
        distill_prompt = (
            f"Goal: {goal_short}\n\n"
            "Extract ONLY the key facts relevant to the goal from the data below.\n\n"
            f"Data:\n{raw_text}\n\n"
            "Key facts:"
        )
        try:
            from agent.model_adapter import generate
            distilled = generate(distill_prompt, model=model, temperature=0.0)
            distilled = (distilled or "").strip()
            if len(distilled) > 50:
                print(f"[DISTILL] Compressed {len(raw_obs)} chars -> {len(distilled)} chars")
                return distilled
        except Exception as _e:
            print(f"[DISTILL] Skipped (error): {_e}")
        return raw_obs

    def _prefetch_buffer_content(self, step_desc: str, buffer: dict) -> str:
        """Pre-read buffer(s) referenced in step_desc and return inline content block.

        This implements the orchestrator-level read pre-fetch:
        instead of the LLM calling buffer.slice (which it often forgets or cargocults),
        we read the buffer HERE in Python and inject the content directly into the
        step context, so the model can use it immediately for composing email bodies etc.

        Returns a compact string to append to the step prompt's working state, or "".
        Max 600 chars per buffer to stay inside token budget.
        """
        if not buffer:
            return ""
        MAX_PREFETCH_CHARS = 600
        # Find all [BUFFER:N] references in the step goal
        refs = re.findall(r'\[BUFFER:(\d+)\]', step_desc)
        if not refs:
            return ""
        parts = []
        for buf_id_str in refs:
            buf_id = int(buf_id_str)
            raw = buffer.get(buf_id) or buffer.get(str(buf_id))
            if not raw:
                continue
            # Truncate to budget
            content = str(raw).strip()
            if len(content) > MAX_PREFETCH_CHARS:
                content = content[:MAX_PREFETCH_CHARS] + "\n...[truncated, use buffer.slice for full content]"
            parts.append(f"--- BUFFER:{buf_id} content (pre-read) ---\n{content}\n--- END BUFFER:{buf_id} ---")
        if not parts:
            return ""
        return "\n".join(parts) + "\n\n"

    def _select_categories(self, step_desc: str, user_message: str = "", planner_categories: list[str] | None = None) -> set:
        """Select per-step categories from planner hints, then fallback to keyword matching.
        
        IMPORTANT: keyword matching uses ONLY step_desc to prevent the full
        user message from leaking every category into every step. The planner
        should provide categories via planner_categories when the step description
        alone is insufficient.
        """
        selected = set(self._normalize_categories(planner_categories or []))
        # If planner already provided categories, trust that set and do not
        # expand it with keyword heuristics. This keeps tool scope per-step.
        if selected:
            return selected | _ALWAYS_CATEGORIES
        # Only match keywords against the step description  NOT the user message.
        # Using user_message caused every step to inherit all task-level categories
        # (e.g. "send via whatsapp as pdf" leaking WhatsApp+Documents into a
        # "retrieve contact" step).
        # Strip any appended user-clarification suffix before keyword matching so
        # text like "(User clarification: drop this task)" doesn't pull in extra
        # categories (e.g. "task"  Todo List).
        _clean_desc = re.sub(r'\s*\(User clarification:[^)]*\)', '', step_desc, flags=re.IGNORECASE).strip()
        combined = _clean_desc.lower()
        for category, keywords in CATEGORY_KEYWORDS.items():
            for k in keywords:
                _kw = str(k).strip().lower()
                if not _kw:
                    continue
                if " " in _kw:
                    if _kw in combined:
                        selected.add(category)
                        break
                else:
                    if re.search(rf'\b{re.escape(_kw)}\b', combined):
                        selected.add(category)
                        break
        return selected | _ALWAYS_CATEGORIES

    def _get_step_tools(self, step_desc: str, user_message: str = "", planner_categories: list[str] | None = None) -> str:
        """Return predicted tool docs (1 primary + 1 backup) for this step."""
        cats = self._select_categories(step_desc, user_message, planner_categories)
        # Store for execution-time category gate in _safe_execute
        self._current_step_cats = {c.strip().lower() for c in cats}
        predicted = self._predict_step_tools(step_desc, list(cats))
        if not predicted:
            # Fallback to one tool per category at most, to keep context small.
            fallback = []
            for cat in sorted(cats):
                cat_tools = [
                    name for name, meta in TOOL_METADATA.items()
                    if str(meta.get("category", "")).strip().lower() == cat.strip().lower()
                    and name in self.tools
                ]
                if cat_tools:
                    fallback.append(sorted(cat_tools)[0])
                if len(fallback) >= 2:
                    break
            predicted = fallback
        # Buffer hydration guard:
        # If the step goal explicitly references [BUFFER:N], the model MUST be able
        # to call buffer.slice to read its contents before the primary tool.
        # Without this, the model cargo-cults the reference as a literal string.
        _has_buffer_ref = bool(re.search(rb'\[BUFFER:\d+\]|buffer\.slice|buffer\.extract', step_desc.encode()))
        if _has_buffer_ref:
            for _buf_tool in ("buffer.slice", "buffer.extract"):
                if _buf_tool in self.tools and _buf_tool not in predicted:
                    predicted.insert(0, _buf_tool)
        # Contacts guard: if the goal asks to look up a contact, inject contacts.search
        _has_contact_lookup = bool(re.search(r'contacts\.search|find .{0,30}contact|look up .{0,20}email|look up .{0,20}phone', step_desc, re.IGNORECASE))
        if _has_contact_lookup and "contacts.search" in self.tools and "contacts.search" not in predicted:
            predicted.insert(0, "contacts.search")
        self._current_step_tool_names = set(predicted)
        return full_tool_schemas_for_tools(predicted)

    def _extract_key_data(self, text: str) -> dict:
        """Extract compact artifact references from observations/summaries."""
        data = {"buffers": [], "files": [], "phones": []}
        if not text:
            return data
        for m in re.finditer(r'\[BUFFER:(\d+)\]|BUFFER:(\d+)', text):
            bid = (m.group(1) or m.group(2) or "").strip()
            if bid and bid not in data["buffers"]:
                data["buffers"].append(bid)
        # Match both forward-slash (SANDBOX/foo.pdf) and Windows absolute paths
        # (C:\...\SANDBOX\foo.pdf). Normalize everything to SANDBOX/... format.
        for m in re.finditer(
            r'(?:[A-Za-z]:\\(?:[^\s"\'<>|*?\\]+\\)*)?SANDBOX[/\\]([\w./\\_-]+)',
            text, re.IGNORECASE
        ):
            sub = m.group(1).replace('\\', '/')
            fp = f"SANDBOX/{sub}"
            if fp not in data["files"]:
                data["files"].append(fp)
        for m in re.finditer(r'\b\d{10,15}\b', text):
            ph = m.group(0)
            if ph not in data["phones"]:
                data["phones"].append(ph)
        return data

    def _merge_artifacts(self, artifacts: dict, extracted: dict):
        """Merge extracted key-data into mutable per-step artifact map."""
        for key in ("buffers", "files", "phones"):
            for value in extracted.get(key, []):
                if value not in artifacts[key]:
                    artifacts[key].append(value)

    def _format_completed_block(self, completed: list) -> str:
        """Render completed-step context with concise artifact and call details."""
        if not completed:
            return ""
        lines = ["COMPLETED STEPS:"]
        for idx, item in enumerate(completed):
            icon = "" if item.get("status") == "done" else ""
            lines.append(f"  [{icon}] Step {idx+1}: {item.get('step','')[:160]}")
            lines.append(f"      Result: {str(item.get('result',''))[:220]}")
            artifacts = item.get("artifacts", {}) if isinstance(item, dict) else {}
            keys = []
            if artifacts.get("buffers"):
                keys.append("buffers=" + ",".join(str(x) for x in artifacts["buffers"][:4]))
            if artifacts.get("files"):
                keys.append("files=" + ",".join(str(x) for x in artifacts["files"][:3]))
            if artifacts.get("phones"):
                keys.append("phones=" + ",".join(str(x) for x in artifacts["phones"][:3]))
            if keys:
                lines.append("      Key data: " + " | ".join(keys))
            calls = item.get("calls", []) if isinstance(item, dict) else []
            for call in calls[-2:]:
                lines.append(
                    f"      Call: {call.get('tool','')} [{call.get('status','')}] -> "
                    f"{str(call.get('summary',''))[:140]}"
                )
        return "\n".join(lines) + "\n\n"

    def _format_recent_done_block(self, completed: list, max_steps: int = 2, max_chars: int = 80) -> str:
        """
        Tiny completed-step block for ACT prompts on small models.
        """
        if not completed:
            return ""
        tail = completed[-max_steps:]
        lines = ["RECENT COMPLETED STEPS:"]
        for idx, item in enumerate(tail, start=max(1, len(completed) - len(tail) + 1)):
            status = "ok" if item.get("status") == "done" else "failed"
            step_txt = str(item.get("step", "")).strip()[:max_chars]
            res_txt = str(item.get("result", "")).strip()[:max_chars]
            # Always show buffer references in full — they are short and critical for next step
            result_full = str(item.get("result", "")).strip()
            import re as _re
            buf_refs = _re.findall(r'\[BUFFER:\d+\]', result_full)
            if buf_refs and not any(b in res_txt for b in buf_refs):
                res_txt = res_txt + "  " + " ".join(buf_refs)
            lines.append(f"- [{status}] {idx}. {step_txt} -> {res_txt}")
        return "\n".join(lines) + "\n\n"

    async def _route_intent_mode(
        self,
        text: str,
        model: str,
        history_text: str,
        profile_context: str,
        memory_context: str,
    ) -> str:
        """
        Lightweight router model:
        - TASK: needs planning/tool execution
        - CHAT: conversational response (may still update memory/profile)
        """
        route_prompt = (
            f"{self.system_prompt}\n\n"
            f"{profile_context}{memory_context}{history_text}"
            "Classify the user intent.\n"
            "Return exactly one token:\n"
            "- TASK  -> requires multi-step execution and/or tool use\n"
            "- CHAT  -> conversation, advice, explanation, small talk, or direct response\n\n"
            f"User: {text}\n"
            "Route:"
        )
        try:
            raw = await asyncio.to_thread(generate, route_prompt, model=model, temperature=0.0)
            first = (raw or "").strip().splitlines()[0].strip().upper()
            if "CHAT" in first:
                return "chat"
        except Exception:
            pass
        return "task"

    async def _run_chat_mode(
        self,
        text: str,
        model: str,
        history_text: str,
        profile_context: str,
        memory_context: str,
    ) -> tuple[list[dict], str]:
        """
        Chat mode responder with optional profile/memory updates.
        Allowed tools here are limited to memory/user/soul update tools.
        """
        allowed_chat_tools = {
            "memory.write", "memory.update",
            "user.write", "user.update",
            "soul.write", "soul.update",
        }
        events: list[dict] = []
        feedback_block = ""

        for _ in range(3):
            prompt = (
                f"{self.system_prompt}\n\n"
                f"{profile_context}{memory_context}{history_text}"
                f"{feedback_block}"
                "CHAT MODE:\n"
                "- Reply conversationally unless a durable profile/memory update is needed.\n"
                "- If update needed, output exactly one ACTION using ONLY: "
                "memory.write/memory.update/user.write/user.update/soul.write/soul.update.\n"
                "- Otherwise output CHAT: <reply>.\n\n"
                "Output format (exactly one):\n"
                "- ACTION: {\"tool\":\"...\",\"args\":{...}}\n"
                "- CHAT: <reply>\n\n"
                f"User: {text}\n"
                "MIRA:"
            )
            reply = await asyncio.to_thread(generate, prompt, model=model, temperature=0.2)
            call = self._extract_tool_call(reply or "")
            if call is not None:
                tool_name, tool_args, _ = call
                if tool_name not in allowed_chat_tools:
                    return events, "I can chat directly, but I cannot run that tool in chat mode."
                events.append({"type": "tool_call", "tool": tool_name, "args": tool_args, "step": 0})
                result = await asyncio.to_thread(self._safe_execute, tool_name, tool_args if isinstance(tool_args, dict) else {})
                status = "error" if isinstance(result, dict) and result.get("status") == "error" else "ok"
                events.append({"type": "tool_result", "tool": tool_name, "result": result, "status": status, "step": 0})
                feedback_block += f"\nPrevious ACTION result ({tool_name}): {json.dumps(result, default=str)[:700]}\n"
                continue

            chat_match = re.search(r'^\s*CHAT\s*:\s*(.*)$', reply or "", re.IGNORECASE | re.DOTALL)
            if chat_match:
                return events, chat_match.group(1).strip()
            if "FINAL ANSWER:" in (reply or ""):
                return events, (reply or "").split("FINAL ANSWER:")[-1].strip()
            if (reply or "").strip():
                return events, (reply or "").strip()

        return events, "How can I help?"

    async def _revise_plan(
        self,
        task: str,
        completed: list,
        failed_step: str,
        failed_reason: str,
        failed_attempts: list | None,
        user_input: str | None,
        remaining_steps: list,
        model: str,
        history_text: str,
        profile_context: str | None = None,
        memory_context: str | None = None,
        tool_categories_summary: str | None = None,
    ) -> list:
        """Revise remaining plan by splitting/adjusting from failed step onward.

        Now routed through ContextCompiler (compile  apply_budget  prepare_for_generation)
        so REPLAN gets the same token-accurate budgeting, structured messages,
        temporal context injection, and metrics as PLAN/ACT/SYNTHESIZE.
        """
        done_lines = self._format_completed_block(completed).strip() or "(none)"
        rem_lines = "\n".join(
            f"{idx+1}. {s.get('goal', '')}" for idx, s in enumerate(remaining_steps)
        ) or "(none)"
        attempt_lines = "\n".join(f"- {a}" for a in (failed_attempts or [])) or "(none)"
        user_input_block = (
            f"USER PROVIDED CLARIFICATION (must be incorporated):\n{user_input}\n\n"
            if user_input else ""
        )

        # Use None as sentinel so callers that pass "" intentionally (e.g. exec
        # context without USER/SOUL) do not trigger a fallback to the full profile.
        if profile_context is None:
            profile_context = self._get_profile_context()
        if memory_context is None:
            memory_context = self._get_memory_context()
        if tool_categories_summary is None:
            tool_categories_summary = self._get_tool_categories_summary()

        rem_lines = self._summarize_block(rem_lines, max_chars=1500, prefer_tail=True)
        attempt_lines = self._summarize_block(attempt_lines, max_chars=1000, prefer_tail=True)

        # Build replan-specific context for goal + constraints sections
        _replan_goal = f"TASK: {task}"
        _replan_constraints = (
            f"FAILED STEP: {failed_step}\n"
            f"FAILURE REASON: {failed_reason}\n\n"
            f"{user_input_block}"
            f"ATTEMPTS IN FAILED STEP:\n{attempt_lines}\n\n"
            f"OLD REMAINING PLAN:\n{rem_lines}"
        )

        # Use ContextCompiler pipeline (compile  budget  generate)
        _replan_sections = self._context_compiler.compile(
            step_type=StepType.REPLAN,
            user_message=task,
            step_desc=f"Replan from: {failed_step[:80]}",
            step_tools=tool_categories_summary,
            profile_context=profile_context,
            memory_context=memory_context,
            history_text=history_text,
            done_block=done_lines + "\n\n",
            task_contract="",
            working_state="",
            retry_note=_replan_constraints,
        )
        # Override goal with replan-specific goal (compile() would set "User: {task}")
        _replan_sections.goal = _replan_goal

        _replan_sections, _replan_budget_note, _ = self._context_compiler.apply_budget(
            _replan_sections, step_type=StepType.REPLAN
        )

        _replan_output_format = (
            "REPLAN MODE:\n"
            "- Replace FAILED STEP onward with revised smaller steps\n"
            "- Split complex goals into simpler executable steps\n"
            "- For each step, include categories from TOOL CATEGORIES\n"
            "- For USER.md/SOUL.md/MEMORY.md updates, use user.update/soul.update/memory.update (not filesystem tools)\n"
            "- goal is to complete the task "
            "Format exactly:\n"
            "1. <goal> | categories: <Cat1>, <Cat2>\n"
            "2. <goal> | categories: <CatX>\n"
            "MIRA:"
        )

        # Also keep render_prompt for logging
        plan_prompt = self._context_compiler.render_prompt(
            _replan_sections,
            step_type=StepType.REPLAN,
            budget_note=_replan_budget_note,
            output_format=_replan_output_format,
        )

        # Use structured messages when provider supports it
        _flat, _msgs = self._context_compiler.prepare_for_generation(
            _replan_sections,
            step_type=StepType.REPLAN,
            budget_note=_replan_budget_note,
            output_format=_replan_output_format,
        )
        if _msgs is not None:
            replan_raw = await asyncio.to_thread(generate_with_messages, _msgs, model=model, temperature=0.2, thinking=True)
        else:
            replan_raw = await asyncio.to_thread(generate, _flat or plan_prompt, model=model, temperature=0.2)

        print(f"\n{''*70}")
        print(f"[STREAM] REPLAN  |  model={model}")
        print(f"{''*70}")
        print("[STREAM][PROMPT LLM]")
        print(plan_prompt)
        print(f"{''*70}")
        print("[STREAM][LLM REPLY]")
        print(replan_raw)
        print(f"{''*70}")

        revised = self._parse_plan(replan_raw)
        # If the LLM emitted a control output (NEED_INPUT:, CLARIFY:, etc.) instead of
        # numbered steps, _parse_plan returns []. Detect these and return remaining_steps
        # unchanged so the executor can skip the failed step and continue rather than
        # entering a dead-end fallback loop.
        if not revised:
            stripped = replan_raw.strip()
            if re.search(r'^(NEED_INPUT|CLARIFY|WAIT|BLOCKED)[:\s]', stripped,
                         re.IGNORECASE | re.MULTILINE):
                print("[AGENT] REPLAN emitted control output, preserving remaining steps")
                return remaining_steps
        return revised

    def _compress_obs(self, obs: str, tool_name: str = '') -> str:
        """Compress a raw JSON tool observation to a brief one-line summary."""
        if not obs:
            return 'no result'
        text = obs.strip()
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            # Plain text  truncate
            return text.replace('\n', ' ')[:150]

        if isinstance(data, dict) and data.get('status') == 'error':
            err = str(data.get('error', 'unknown'))
            return f"ERROR: {err[:100]}"

        # whatsapp.status: return ONLY connectivity  never expose the bot's own JID
        if tool_name == 'whatsapp.status':
            connected = str(data.get('status', 'unknown'))
            return f"whatsapp: {connected}"

        #  filesystem.list / filesystem.search  show top filenames, not just count 
        if tool_name in ('filesystem.list', 'filesystem.search'):
            if isinstance(data, list):
                items = data
            else:
                items = data.get('items') or data.get('matches') or data.get('files') or []
                if not items and isinstance(data, dict):
                    for v in data.values():
                        if isinstance(v, list):
                            items = v
                            break
            if not items:
                return 'no files found'
            names = []
            for it in items:
                if isinstance(it, dict):
                    name = it.get('name') or it.get('path') or ''
                    is_dir = it.get('is_dir', False)
                    # Skip the confusing nested SANDBOX/ subdirectory
                    if name.upper() == 'SANDBOX' and is_dir:
                        continue
                    # Show only filename, never absolute path  prevents SANDBOX/SANDBOX double-prefix
                    label = Path(name).name if name else str(it)[:60]
                    if is_dir:
                        label += '/'
                    names.append(label)
                else:
                    # Strip absolute path to just the filename
                    names.append(Path(str(it)).name or str(it)[:60])
            if not names:
                return 'no files found'
            top = ', '.join(names[:8])
            suffix = f' (+{len(names)-8} more)' if len(names) > 8 else ''
            return f"{len(names)} file(s): {top}{suffix}  [use path: SANDBOX/<filename>]"

        #  filesystem.read  show path + size + first heading 
        # search tools: preserve concrete titles/snippets for downstream handoff
        if tool_name in ('search.duckduckgo', 'search.news'):
            results = []
            if isinstance(data, dict):
                _r = data.get('results')
                if isinstance(_r, list):
                    results = _r
            elif isinstance(data, list):
                results = data
            if results:
                parts = []
                for idx, item in enumerate(results[:5], start=1):
                    if isinstance(item, dict):
                        title = str(item.get('title') or item.get('name') or '').strip()
                        snippet = str(item.get('snippet') or item.get('body') or item.get('description') or '').strip()
                        title = re.sub(r'\s+', ' ', title)[:90]
                        snippet = re.sub(r'\s+', ' ', snippet)[:120]
                        if title and snippet:
                            parts.append(f"{idx}) {title} - {snippet}")
                        elif title:
                            parts.append(f"{idx}) {title}")
                    else:
                        raw_item = re.sub(r'\s+', ' ', str(item)).strip()[:140]
                        if raw_item:
                            parts.append(f"{idx}) {raw_item}")
                if parts:
                    return "Top results: " + " | ".join(parts)
            if isinstance(data, dict) and data.get('count') is not None:
                return f"search returned {data.get('count')} result(s)"

        if tool_name == 'filesystem.read':
            content = (data.get('content') or data.get('text') or '') if isinstance(data, dict) else str(data)
            path_hint = (data.get('path') or data.get('filename') or '') if isinstance(data, dict) else ''
            if not content and not path_hint:
                return 'file read: empty'
            lines = content.splitlines()
            headings = [l.strip() for l in lines if l.startswith('#')][:2]
            heading_text = ' | '.join(headings) if headings else (lines[0][:80] if lines else '')
            return f"file:{path_hint} ({len(content)} chars) {heading_text}".strip()

        #  todo.* mutations  surface title so LLM can track which item was added 
        if tool_name in ('todo.add', 'todo.update', 'todo.complete'):
            if isinstance(data, dict):
                title  = data.get('title', '')
                tid    = data.get('id', '')
                status = data.get('message', data.get('status', 'ok'))
                if title:
                    return f"todo: id={tid} title={title!r}  {status}"

        if isinstance(data, dict):
            # 'jid' is the bot's own WhatsApp identifier  never surface it as a number
            skip = {'status', 'hint', 'query', 'category', 'action', 'jid'}
            parts = []
            # Single contact (contacts.get)  must include id so downstream steps can use it
            if 'contact' in data and isinstance(data['contact'], dict):
                c = data['contact']
                parts.append(
                    f"contact: id={c.get('id','?')} name={c.get('name','?')} "
                    f"phone={c.get('phone','?')} email={c.get('email','?')}"
                )
                skip = skip | {'contact'}
            # Contact list (contacts.search / contacts.list)  include id for each hit
            elif 'contacts' in data and isinstance(data['contacts'], list):
                cs = data['contacts']
                if cs:
                    c = cs[0]
                    parts.append(
                        f"contact: id={c.get('id','?')} name={c.get('name','?')} "
                        f"phone={c.get('phone','?')} email={c.get('email','?')}"
                    )
                    if len(cs) > 1:
                        parts.append(f"(+{len(cs)-1} more contacts)")
                else:
                    return "no contacts found"
            for k, v in data.items():
                if k in skip or k == 'contacts':
                    continue
                if isinstance(v, (str, int, float, bool)):
                    parts.append(f"{k}: {str(v)[:120]}")
                elif isinstance(v, list) and v:
                    # Show first few items for lists, not just count
                    preview = ', '.join(str(x)[:40] for x in v[:3])
                    suffix = f'...+{len(v)-3}' if len(v) > 3 else ''
                    parts.append(f"{k}: [{preview}{suffix}]")
            return ', '.join(parts[:6]) if parts else 'ok'

        if isinstance(data, list):
            if not data:
                return '0 items'
            # Show first few items for short lists
            preview = ', '.join(str(x)[:50] for x in data[:4])
            suffix = f'...+{len(data)-4}' if len(data) > 4 else ''
            return f"{len(data)} items: {preview}{suffix}"
        return str(data)[:120]


    async def handle(self, text: str, model: str = "qwen3:4b-instruct", run_handle=None) -> dict:
        """
        Entry point for non-streaming callers (WhatsApp, /api/invoke, etc.).
        Single execution engine: delegates entirely to handle_streaming() so there
        is no duplicated loop logic to diverge.  Returns {"reply": "<answer>"}.
        """
        final_response = ""
        async for event in self.handle_streaming(text, model=model, run_handle=run_handle):
            if event.get("type") in ("final", "step_limit", "aborted", "iteration_limit"):
                final_response = event.get("content", "")
                break
        return {"reply": final_response or "Task completed."}

    async def handle_streaming(self, text: str, model: str = "qwen3:4b-instruct", run_handle=None):
        """
        Streaming version of handle(). Yields events as they happen for real-time UI.
        
        Yields dicts with:
          {"type": "thought", "content": "..."}
          {"type": "tool_call", "tool": "...", "args": {...}}
          {"type": "tool_result", "tool": "...", "result": "...", "status": "ok|error"}
          {"type": "final", "content": "..."}
          {"type": "step_limit", "content": "..."}
        """
        #  Initialize context engineering for this run 
        # Clear per-task NEED_INPUT answers; prevents previous session answers bleeding in.
        # /miraclr - hard session reset command
        if text.strip().lower() == "/miraclr":
            self.history.clear()
            self._input_answers = {}
            self.buffer = {}
            self._buffer_id = 0
            self._task_contract = {}
            self._working_state = {"slots": {}, "artifacts": {}}
            self._handoff_payload = ""
            self._pending_workflow_candidate = None
            self._context_compiler.reset()
            yield {"type": "final", "content": "Session cleared. Fresh start!"}
            return

        # Pending workflow save confirmation from previous successful multi-step run.
        _decision = (text or "").strip().lower()
        if self._pending_workflow_candidate and _decision in {"yes", "y", "no", "n"}:
            cand = self._pending_workflow_candidate
            self._pending_workflow_candidate = None
            if _decision in {"yes", "y"}:
                save_args = {
                    "name": cand.get("name") or self._suggest_workflow_name(cand.get("task", "")),
                    "steps": cand.get("steps", []),
                    "trigger_keywords": cand.get("trigger_keywords", []),
                    "variables": cand.get("variables", []),
                    "description": "Auto-saved from successful multi-step execution",
                }
                saved = await asyncio.to_thread(self._safe_execute, "workflow.save", save_args)
                if isinstance(saved, dict) and saved.get("status") == "error":
                    msg = f"I couldn't save the workflow: {saved.get('error', 'unknown error')}"
                else:
                    res = saved.get("result", saved) if isinstance(saved, dict) else saved
                    wf_name = res.get("name", save_args["name"]) if isinstance(res, dict) else save_args["name"]
                    msg = f"Saved workflow '{wf_name}'."
                yield {"type": "final", "content": msg}
                self.history.append((text, msg))
                return
            msg = "Okay, I won't save that as a workflow."
            yield {"type": "final", "content": msg}
            self.history.append((text, msg))
            return

        self._input_answers = {}
        # Run-scoped buffers prevent stale cross-task context leakage.
        self.buffer = {}
        self._buffer_id = 0
        self._task_contract = self._build_task_contract(text)
        self._working_state = self._init_working_state(self._task_contract)
        self._handoff_payload = ""
        self._context_compiler.model = model
        self._context_compiler.reset()  # fresh tool records + short-term memory per run

        #  Workflow auto-trigger 
        # Check if a saved workflow matches the user message. If so, skip LLM planning
        # and directly load the workflow steps (saves one full LLM call).
        _workflow_steps_preloaded: list | None = None
        _matched_wf, _wf_vars = self._match_workflow_trigger(text)
        if _matched_wf:
            _workflow_steps_preloaded = self._expand_workflow_steps(_matched_wf, _wf_vars)
            if _workflow_steps_preloaded:
                print(
                    f"[AGENT] Workflow '{_matched_wf.get('name')}' auto-triggered "
                    f"({len(_workflow_steps_preloaded)} steps)"
                )
                yield {
                    "type": "thought",
                    "content": f"Using saved workflow: {_matched_wf.get('display_name', _matched_wf.get('name'))}",
                }
        self._context_compiler.metrics = ContextMetricsTracker(
            run_id=getattr(run_handle, 'run_id', '') if run_handle else '',
            model=model,
            task_text=text,
        )
        _run_start = time.time()

        # Context management: cap to last N turns and truncate long assistant responses
        # to prevent context overflow on 4B models with long histories.
        recent_history = self.history[-MAX_HISTORY_TURNS:]
        history_text = self._build_history_text(
            recent_history,
            max_turns=MAX_PLAN_HISTORY_TURNS,
            user_chars=180,
            assistant_chars=180,
        )

        self._exec_model = model  # stored so buffer.search LLM calls can use it
        self._current_task_text = text or ""
        # Note: datetime is now injected centrally by ContextCompiler.compile()

        # Learn user email from explicit preference messages (durable profile memory).
        _user_text_l = (text or "").lower()
        _email_candidates = self._extract_emails(text or "")
        if _email_candidates and re.search(r"\b(my email|email address|correct email|send me|myself|to me)\b", _user_text_l):
            try:
                self._persist_user_email(_email_candidates[0])
            except Exception:
                pass

        _recent_tool_outputs: list[dict] = []

        #  Runtime allow/deny constraints 
        # Parse "Use ONLY: X, Y" / "Do NOT use: A, B" from user prompt and store
        # on self so _safe_execute() can enforce them for every tool call this run.
        self._allowed_tools, self._denied_tools = _parse_tool_constraints(text)
        if self._allowed_tools:
            print(f"[AGENT] Tool allow-list active: {sorted(self._allowed_tools)}")
        if self._denied_tools:
            print(f"[AGENT] Tool deny-list  active: {sorted(self._denied_tools)}")

        # Per-run static context cache (small-model prompt diet).
        _profile_context_plan_cached = self._get_plan_profile_context()
        _profile_context_exec_cached = ""
        _memory_context_plan_cached = self._get_relevant_memory_context(text, max_items=MAX_PLAN_MEMORY_ITEMS)
        _memory_context_exec_cached = ""
        _tool_categories_summary_cached = self._get_tool_categories_summary()

        final_response = ""

        #  Phase 0: Plan 
        # Use ContextCompiler for typed section assembly + adaptive budget
        _plan_sections = self._context_compiler.compile(
            step_type=StepType.PLAN,
            user_message=text,
            profile_context=_profile_context_plan_cached,
            memory_context=_memory_context_plan_cached,
            history_text=history_text,
            step_tools=_tool_categories_summary_cached,
            task_contract="",
            working_state="",
        )
        _plan_sections, _plan_budget_note, _plan_diag = self._context_compiler.apply_budget(
            _plan_sections, step_type=StepType.PLAN
        )
        plan_prompt = self._context_compiler.render_prompt(
            _plan_sections,
            step_type=StepType.PLAN,
            budget_note=_plan_budget_note,
            output_format=(
                "PLANNING MODE: DO NOT call any tools yet.\n"
                "Is this conversational? -> CHAT: <reply>\n"
                "Otherwise output a numbered plan (one atomic action per step):\n"
                "  1. <what to do> | categories: <Cat1>, <Cat2>\n"
                "Rules:\n"
                "- Split compound tasks: 'find X and email Y' = step1: search, step2: contact lookup, step3: email.\n"
                "- Each step does exactly ONE thing (one read OR one write).\n"
                "- If sending, add a contact-lookup step BEFORE the send step.\n"
                "- Keep the plan to 2-4 steps maximum.\n"
                "MIRA: Plan:\n"
            ),
        )
        # Use structured messages when provider supports it
        _plan_flat, _plan_msgs = self._context_compiler.prepare_for_generation(
            _plan_sections,
            step_type=StepType.PLAN,
            budget_note=_plan_budget_note,
            output_format=(
                "PLANNING MODE: DO NOT call any tools yet.\n"
                "Is this conversational? -> CHAT: <reply>\n"
                "Otherwise plan (one atomic action per step):\n"
                "  1. <what to do> | categories: <Cat1>, <Cat2>\n"
                "Compound tasks MUST be split: find+email = 3 steps.\n"
                "MIRA: Plan:\n"
            ),
        )
        if _workflow_steps_preloaded:
            plan_raw = "WORKFLOW AUTO-TRIGGER"
        elif _plan_msgs is not None:
            plan_raw = await asyncio.to_thread(generate_with_messages, _plan_msgs, model=model, thinking=True)
        else:
            plan_raw = await asyncio.to_thread(generate, _plan_flat or plan_prompt, model=model)
        print(f"\n{''*70}")
        print(f"[STREAM] Phase 0  PLAN  |  model={model}")
        print(f"{''*70}")
        print("[STREAM][PROMPT LLM]")
        print(plan_prompt)
        print(f"{''*70}")
        print("[STREAM][LLM REPLY]")
        print(plan_raw)
        print(f"{''*70}")

        steps = list(_workflow_steps_preloaded or self._parse_plan(plan_raw))

        #  Planner direct-reply handling 
        # CHAT: <reply>    conversational; no tools needed
        # FINAL ANSWER:    task answered without steps (e.g. simple calculation)
        # Both bypass the executor entirely.
        _first_non_empty = next((ln.strip() for ln in plan_raw.splitlines() if ln.strip()), "")
        _first_upper = _first_non_empty.upper()

        if (not _workflow_steps_preloaded) and _first_upper.startswith("CHAT:") and not steps:
            _chat_reply = re.sub(r'^CHAT\s*:', '', _first_non_empty, flags=re.IGNORECASE).strip()
            # If CHAT: was on its own line, take the rest of the output as the reply
            if not _chat_reply:
                _lines = [ln for ln in plan_raw.splitlines() if ln.strip()]
                _chat_reply = "\n".join(_lines[1:]).strip() if len(_lines) > 1 else ""
            final_response = _chat_reply or "How can I help?"
            print(f"[AGENT] Planner routed to CHAT: {final_response[:80]}")
            _run_metrics = self._context_compiler.metrics.finalize()
            print(self._context_compiler.metrics.render_metrics_block())
            self.history.append((text, final_response))
            yield {"type": "final", "content": final_response}
            return

        # Accept direct planner answer ONLY when the response is pure FINAL ANSWER
        # (no numbered steps). Some models emit both a plan and a hallucinated
        # FINAL ANSWER line; in that case we must execute the plan.
        _pure_final = _first_upper.startswith("FINAL ANSWER:") and not steps
        if (not _workflow_steps_preloaded) and _pure_final and "ACTION:" not in plan_raw and "TOOL:" not in plan_raw:
            final_response = plan_raw.split("FINAL ANSWER:")[-1].strip()
            self.history.append((text, final_response))
            yield {"type": "final", "content": final_response}
            return

        if not steps:
            steps = [{"goal": text, "categories": []}]  # fallback single step
        if self._should_split_compound_task(text, steps):
            _split_steps = self._split_compound_task(text)
            if len(_split_steps) >= 2:
                print(f"[AGENT] Plan auto-split: 1 -> {len(_split_steps)} steps")
                steps = _split_steps
        steps_block = "\n".join(
            f"{i+1}. {s['goal']}"
            + (f" | categories: {', '.join(s.get('categories', []))}" if s.get("categories") else "")
            for i, s in enumerate(steps)
        )
        yield {"type": "thought", "content": f"Plan:\n{steps_block}"}

        #  Phase 1: Execute each step with retry + replan 
        completed = []   # [{"step": str, "status": "done|failed", "result": str}]
        final_response = ""
        seen_calls: set = set()   # idempotency: skip exact duplicate (tool, args, step)

        # Mutable steps list  continuation steps may be appended mid-run for
        # quantified batch steps (e.g. "add 6 todos" expands to 6 sub-steps).
        _plan_revisions = 0
        _consecutive_failures = 0
        _step_idx = 0
        while _step_idx < len(steps) and _step_idx < MAX_AUTONOMOUS_STEPS:
            i         = _step_idx
            step_item = steps[i]
            step_desc = step_item.get("goal", "")
            step_categories = step_item.get("categories", [])
            _step_idx += 1

            #  Cancel check (OpenClaw-style abort) 
            if run_handle and run_handle.is_cancelled():
                print(f"[STREAM] Run aborted at step {i+1}  cancel_event set")
                yield {"type": "aborted", "content": "Run aborted by user.", "step": i + 1}
                return

            #  Iteration-limit terminal state 
            if i >= MAX_AUTONOMOUS_STEPS - 1 and i > 0:
                msg = f"Reached {MAX_AUTONOMOUS_STEPS}-step limit after {i} completed steps."
                print(f"[STREAM] {msg}")
                yield {"type": "iteration_limit", "content": msg, "steps_completed": i}
                return

            if self.stop_requested:
                self.stop_requested = False
                final_response = f" Stopped after {i} steps."
                break

            step_result = None
            step_status = "failed"
            last_error = None
            last_failed_tool = None
            last_failed_args = None
            _step_tool = ""  # tracks the last tool used in this step
            _special_retry_note = ""  # overrides standard retry_note (e.g. multi-ACTION)
            _step_rewound = False    # set True by NEED_INPUT classifier rewind (skip completed.append)
            _step_actions = []       # compact progress entries for this step
            _step_calls = []         # structured call records for this step
            _step_artifacts = {"buffers": [], "files": [], "phones": []}
            _step_tools = self._get_step_tools(step_desc, text, step_categories)
            _force_plan_revise = False  # bypass fallback and jump to plan revision when strategy is wrong
            _latest_user_input = None   # latest explicit user clarification for replanning

            #  Quantified step tracking 
            # Detect numeric goals in step description (e.g. "add 6 todos").
            # Skip detection for continuation steps already annotated "[item N of M]".
            _step_quant_n    = 0   # expected count (0 = no quant goal)
            _step_quant_done = 0   # successful side-effect calls in this step
            if "[item " not in step_desc:
                _step_l = step_desc.lower()
                _batch_like = bool(
                    re.search(r'\b(add|create|send|append|insert|ingest)\b', _step_l)
                    and re.search(r'\b(todo|task|event|contact|message|item)s?\b', _step_l)
                )
                if _batch_like:
                    _sq = re.search(r'\b([2-9]|[1-9]\d+)\b', step_desc)
                    if _sq:
                        _n = int(_sq.group(1))
                        if 2 <= _n <= 50:   # safety cap: ignore absurd counts
                            _step_quant_n = _n

            # Start step tracking BEFORE the retry loop (once per logical step)
            self._context_compiler.metrics.start_step(i, "act")

            for attempt in range(MAX_RETRIES_PER_STEP):
                done_block = ""
                if completed:
                    done_block = self._format_recent_done_block(completed, max_steps=2, max_chars=80)

                retry_note = ""
                if _special_retry_note:
                    retry_note = _special_retry_note
                    _special_retry_note = ""
                elif attempt > 0 and last_error:
                    hint = _humanize_error(last_error, last_failed_tool or "", last_failed_args or {})
                    retry_note = (
                        f" Attempt {attempt} FAILED.\n"
                        f"  Tool called: {last_failed_tool}\n"
                        f"  Args used:   {json.dumps(last_failed_args, default=str)[:300]}\n"
                        f"  Fix needed:  {hint}\n"
                        f"Do NOT repeat the same wrong call. Fix the issue above and retry.\n\n"
                    )

                buffer_context = self._get_buffer_context()
                recent_outputs_context = self._get_recent_tool_outputs_context(
                    _recent_tool_outputs, step_desc=step_desc, step_idx=i
                )
                _progress_block = ""
                if _step_actions:
                    _lines = [f"  {idx+1}. {entry}" for idx, entry in enumerate(_step_actions)]
                    _progress_block = "STEP PROGRESS:\n" + "\n".join(_lines) + "\n\n"

                #  Quantified step hint 
                # For continuation steps ("[item N of M]") tell the model exactly
                # which item it is processing.  For first-pass batch steps, remind
                # it to output ONE action and let the loop handle the rest.
                _quant_hint = ""
                _cont_m = re.search(r'\[item (\d+) of (\d+)\]', step_desc)
                if _cont_m:
                    _this_item   = int(_cont_m.group(1))
                    _total_items = int(_cont_m.group(2))
                    _quant_hint = (
                        f"SEQUENCE ITEM {_this_item} of {_total_items}: "
                        f"Check COMPLETED STEPS to see what was already added. "
                        f"Add only the NEXT item not yet listed there. ONE ACTION.\n\n"
                    )
                elif _step_quant_n > 0:
                    _quant_hint = (
                        f"BATCH STEP ({_step_quant_n} items total): "
                        f"Output ONE ACTION for the first item only  "
                        f"the loop runs this step again for each remaining item automatically.\n\n"
                    )

                _media_hint = ""
                _combined_step_text = step_desc.lower()
                # Only inject when the PRIMARY action is send/share/forward a document.
                # Exclude steps that are primarily retrieval/contact-lookup with "send"
                # appearing only as a subordinate purpose (e.g. "retrieve contact to send PDF").
                _is_contact_retrieval = bool(re.search(
                    r'\b(retriev|find|get|look\s*up|fetch|search)\b.{0,40}\b(contact|number|phone)\b'
                    r'|\b(contact|number|phone)\b.{0,40}\b(retriev|find|get|look\s*up|fetch|search)\b',
                    _combined_step_text
                ))
                if (re.search(r'\b(send|share|forward)\b', _combined_step_text)
                        and re.search(r'\b(pdf|document|doc|file)\b', _combined_step_text)
                        and not _is_contact_retrieval):
                    _media_hint = (
                        "MEDIA DELIVERY RULE: This step is sending a file/document. "
                        "Use whatsapp.send_media with type='document' and a SANDBOX file path. "
                        "Do NOT use whatsapp.send for document delivery.\n\n"
                    )

                #  User-input hint for NEED_INPUT resumed steps 
                _user_input_hint = ""
                # Show ALL collected answers so later steps can SKIP if goal already satisfied
                _all_prior = [(k, v) for k, v in sorted(self._input_answers.items()) if isinstance(k, int)]
                _prior_for_step = self._input_answers.get(i, "")
                if _all_prior:
                    if _prior_for_step:
                        _hint_lines = [f"  - {v}" for _, v in _all_prior]
                        _user_input_hint = (
                            " USER PROVIDED INFO (all collected answers):\n"
                            + "\n".join(_hint_lines) + "\n"
                            "Use these answers to complete this step.\n"
                            "If ANY answer satisfies this step goal -> output DONE immediately.\n\n"
                        )
                    else:
                        _hint_lines = [f"  - Step {k+1}: {v}" for k, v in _all_prior]
                        _user_input_hint = (
                            " PRIOR USER ANSWERS (from earlier steps):\n"
                            + "\n".join(_hint_lines) + "\n"
                            "If this step goal is already satisfied -> output DONE.\n\n"
                        )

                step_temp = 0.1 if attempt == 0 else 0.3

                #  Compile context using ContextCompiler (typed sections + adaptive budget) 
                _short_term_ctx = self._context_compiler.short_term.render_context(i)
                # On retries that already have successful results: add a priority hint so
                # the model outputs DONE instead of calling redundant verification tools.
                _step_done_hint = ""
                if attempt > 0 and _step_actions:
                    _step_done_hint = (
                        "\u26a1 PRIORITY CHECK: You already have successful results for this step "
                        "(see STEP PROGRESS above).\n"
                        "If ANY result satisfies the step goal -> output DONE right now.\n"
                        "Do NOT call tools to verify, confirm, or check data you already retrieved.\n"
                    )
                _extra_hints = "\n".join(filter(None, [
                    _quant_hint,
                    _media_hint,
                    self._build_handoff_hint(completed, step_desc),
                    _user_input_hint,
                    _step_done_hint
                ]))
                _act_history = ""
                # Orchestrator read pre-fetch: if the step goal references a buffer,
                # read it now and inject the content into the working_state so the
                # model can use it directly without needing to call buffer.slice first.
                _prefetched = self._prefetch_buffer_content(step_desc, self.buffer)
                _step_sections = self._context_compiler.compile(
                    step_type=StepType.ACT,
                    user_message=text,
                    step_desc=step_desc,
                    step_idx=i,
                    step_tools=_step_tools,
                    profile_context=_profile_context_exec_cached,
                    memory_context=_memory_context_exec_cached,
                    buffer_context=buffer_context,
                    history_text=_act_history,
                    done_block=done_block,
                    task_contract="",
                    working_state=_prefetched,
                    retry_note=retry_note,
                    progress_block=_progress_block,
                    extra_hints=_extra_hints,
                )
                _step_sections, _budget_note, _step_diag = self._context_compiler.apply_budget(
                    _step_sections, step_type=StepType.ACT
                )

                # Build a step-type-aware output format.
                # If the step goal asks to read a buffer first, make the sequence explicit.
                _has_buf_in_goal = bool(re.search(r'\[BUFFER:\d+\]', step_desc))
                if _has_buf_in_goal:
                    _seq_hint = (
                        "SEQUENCE for this step:\n"
                        "  turn 1 -> TOOL: buffer.slice | id=<N>   (reads the buffer)\n"
                        "  turn 2 -> TOOL: <send/write tool> | ...  (uses the content)\n"
                        "Do NOT write the buffer reference as a literal string.\n\n"
                    )
                else:
                    _seq_hint = ""
                _act_output_format = (
                    f"Current step: {step_desc}\n\n"
                    f"{_seq_hint}"
                    "Output exactly ONE of:\n"
                    "TOOL: tool.name | arg=value | arg2=value2\n"
                    "DONE: <one-line summary>\n"
                    "NEED: <one specific question>\n\n"
                    "MIRA:"
                )
                step_prompt = self._context_compiler.render_prompt(
                    _step_sections,
                    step_type=StepType.ACT,
                    budget_note=_budget_note,
                    output_format=_act_output_format,
                )
                _act_flat, _act_msgs = self._context_compiler.prepare_for_generation(
                    _step_sections,
                    step_type=StepType.ACT,
                    budget_note=_budget_note,
                    output_format=_act_output_format,
                )
                _native_act = None
                if (
                    _act_msgs is not None
                    and supports_ollama_native_tools(model)
                    and getattr(self, "_current_step_tool_names", None)
                ):
                    _native_defs = self._build_native_tool_defs(sorted(self._current_step_tool_names))
                    if _native_defs:
                        _native_act = await asyncio.to_thread(
                            generate_with_native_tools,
                            _act_msgs,
                            _native_defs,
                            model=model,
                            temperature=step_temp,
                        )
                if _native_act is not None and not _native_act.get("error"):
                    reply = str(_native_act.get("content", "") or "")
                elif _act_msgs is not None:
                    reply = await asyncio.to_thread(generate_with_messages, _act_msgs, model=model, temperature=step_temp)
                else:
                    reply = await asyncio.to_thread(generate, _act_flat or step_prompt, model=model, temperature=step_temp)

                print(f"\n{'='*70}")
                print(f"[STREAM] Step {i+1}/{len(steps)} attempt {attempt+1}  |  {step_desc[:50]}  |  model={model}  temp={step_temp}")
                print(f"{'-'*70}")
                print("[STREAM][PROMPT LLM]")
                print(step_prompt)
                print(f"{'-'*70}")
                print("[STREAM][LLM REPLY]")
                print(reply)
                print(f"{'-'*70}")

                #  Post-reply context quality verification 
                _quality_signals = self._context_compiler.verify_context_quality(
                    reply, step_idx=i, step_type="act"
                )
                if any(_quality_signals.values()):
                    print(f"[CONTEXT] Quality signals: {_quality_signals}")

                #  Multi-ACTION guard 
                # A 4B model often outputs multiple tool blocks when asked to "add 6 items".
                # Reject the whole reply and force ONE action per invocation.
                # The quantified-step continuation loop will re-run this step for
                # each remaining item automatically.
                _action_count = len(re.findall(r'\b(?:TOOL|ACTION)\s*:', reply, re.IGNORECASE))
                if _action_count > 1:
                    _special_retry_note = (
                        f" MULTI-TOOL ERROR: You output {_action_count} tool blocks.\n"
                        "OUTPUT CONTRACT: exactly ONE TOOL call per invocation.\n"
                        f"For steps needing multiple items: output the FIRST item's TOOL only  "
                        "the loop automatically re-runs this step for each remaining item.\n"
                        "Now output ONE TOOL or DONE:\n\n"
                    )
                    last_error = f"multi-tool ({_action_count} tool lines in one reply)"
                    last_failed_tool = ""
                    last_failed_args = {}
                    print(f"[AGENT]  Multi-tool rejected step {i+1}: {_action_count} actions")
                    yield {"type": "thought",
                           "content": f" Multi-tool output rejected ({_action_count})  retrying",
                           "step": i + 1}
                    continue  # retry loop

                tool_call_data = None
                if isinstance(_native_act, dict) and not _native_act.get("error"):
                    _tcalls = _native_act.get("tool_calls") or []
                    if _tcalls:
                        _tc = _tcalls[0] if isinstance(_tcalls, list) else {}
                        _fn = _tc.get("function", {}) if isinstance(_tc, dict) else {}
                        _tn = str((_fn.get("name") or _tc.get("name") or "")).strip()
                        _ta = _fn.get("arguments", {}) if isinstance(_fn, dict) else _tc.get("arguments", {})
                        if isinstance(_ta, str):
                            try:
                                _ta = json.loads(_ta)
                            except Exception:
                                _ta = {}
                        if _tn:
                            tool_call_data = (_tn, _ta if isinstance(_ta, dict) else {}, "")
                if tool_call_data is None:
                    tool_call_data = self._extract_tool_kv_call(reply) or self._extract_tool_call(reply)

                #  Mutual-exclusion guard 
                # A valid reply should be EITHER a tool call OR a control token.
                # In practice, small local models often emit ACTION plus trailing
                # control text; when ACTION is valid, we execute it and ignore the
                # extra control token instead of forcing a retry/replan loop.
                _has_control_line = bool(
                    re.search(
                        r'^\s*[-*]?\s*(DONE|NEED|NEED_INPUT|STEP_DONE|STEP_FAILED|SKIP|FINAL ANSWER)\s*:',
                        reply,
                        re.IGNORECASE | re.MULTILINE,
                    )
                )
                if tool_call_data is not None and _has_control_line:
                    print(
                        f"[AGENT] Warning: mixed output in step {i+1}; "
                        "executing TOOL and ignoring control token(s)"
                    )
                    yield {
                        "type": "thought",
                        "content": "Mixed output tolerated: executing TOOL and ignoring control token",
                        "step": i + 1,
                    }

                if tool_call_data is None:
                    answer = reply.strip()
                    _answer_u = answer.upper()
                    if _answer_u.startswith("DONE:") or _answer_u.startswith("STEP_DONE:") or _answer_u.startswith("SKIP:"):
                        _prefix_len = 5 if _answer_u.startswith("DONE:") else (10 if _answer_u.startswith("STEP_DONE:") else 5)
                        step_result = answer[_prefix_len:].strip()[:220] or (step_result or "step complete")
                        step_status = "done"
                        break
                    if _answer_u.startswith("STEP_FAILED:"):
                        last_error = answer[12:].strip()[:220] or "step failed"
                        step_status = "failed"
                        break
                    # NEED/NEED_INPUT: model requests clarification from user
                    if _answer_u.startswith("NEED:") or _answer_u.startswith("NEED_INPUT:"):
                        _qlen = 5 if _answer_u.startswith("NEED:") else 11
                        question = answer[_qlen:].strip().split("\n")[0][:300]
                        _ask_user, _slot_name, _slot_note = self._should_request_need_input(
                            question, last_failed_tool or "", last_failed_args or {}
                        )
                        if not _ask_user:
                            last_error = f"need_input suppressed: {_slot_note[:180]}"
                            _special_retry_note = (
                                f"NEED suppressed: {_slot_note}\n"
                                "Use existing provided information and continue this step.\n\n"
                            )
                            if attempt == MAX_RETRIES_PER_STEP - 1:
                                _step_rewound = True
                                _step_idx = i
                                step_status = "failed"
                                break
                            continue
                        print(f"[AGENT] \u2753 NEED_INPUT step {i+1}: {question}")
                        self._context_compiler.metrics.record_need_input()
                        self._context_compiler.short_term.open_loop(
                            f"need_input_s{i}", f"Waiting for user: {question[:100]}", i
                        )
                        yield {"type": "need_input", "question": question, "step": i + 1}
                        if run_handle and hasattr(run_handle, 'input_queue') \
                                and not run_handle.is_cancelled():
                            try:
                                _ui_ans = await asyncio.wait_for(
                                    run_handle.input_queue.get(), timeout=300
                                )
                                self._ingest_user_answer(i, question, _ui_ans, _slot_name)
                                _latest_user_input = _ui_ans
                                _step_actions.append(f"user_input -> {_ui_ans[:120]}")
                                last_error = f"Need input answered by user: {_ui_ans[:180]}"
                                # Record in short-term memory
                                self._context_compiler.short_term.record_user_input(i, question, _ui_ans)
                                self._context_compiler.short_term.close_loop(f"need_input_s{i}", _ui_ans[:100])
                                _special_retry_note = (
                                    f"USER ANSWER RECEIVED: {_ui_ans[:180]}\n"
                                    "Continue this SAME step using the answer. Do not ask NEED_INPUT again for the same slot.\n\n"
                                )
                                if attempt == MAX_RETRIES_PER_STEP - 1:
                                    _ui_compact = _ui_ans.strip()[:180]
                                    _clarified_desc = step_desc
                                    if _ui_compact and _ui_compact.lower() not in _clarified_desc.lower():
                                        _clarified_desc = f"{step_desc} (User clarification: {_ui_compact})"
                                    steps[i] = {"goal": _clarified_desc, "categories": step_categories}
                                    _step_idx = i
                                    _step_rewound = True
                                    step_status = "failed"
                                    break
                                continue
                            except asyncio.TimeoutError:
                                print(f"[AGENT] \u23f1 NEED_INPUT timed out step {i+1}")
                                step_result = f"Timed out waiting for user input: {question}"
                                step_status = "failed"
                                break
                        else:
                            step_result = f"Needs user input (non-interactive mode): {question}"
                            step_status = "failed"
                            break
                    if attempt == MAX_RETRIES_PER_STEP - 1 and step_result:
                        step_status = "done"
                        break
                    last_error = answer[:220] or "no actionable output"
                    continue

                tool_name, tool_args, pre_text = tool_call_data

                if pre_text.strip():
                    thought = pre_text.strip()
                    if "THOUGHT:" in thought:
                        thought = thought.split("THOUGHT:")[-1].strip()
                    yield {"type": "thought", "content": thought, "step": i + 1}

                if not isinstance(tool_args, dict):
                    tool_args = ({"command": tool_args} if "shell" in tool_name else {"content": tool_args}) \
                        if isinstance(tool_args, str) else {}

                #  Idempotency guard 
                # Side-effect tools (send / add / create / write / delete / update):
                #   key = tool:args_hash   (NO step index  same call anywhere in
                #   the run is blocked after first success, regardless of step number)
                # Read/query tools:
                #   key = tool:args_hash:step  (allowed to re-read at different steps)
                _is_side_effect = any(tool_name.startswith(p) for p in _SIDE_EFFECT_PREFIXES)

                # Normalize args for semantic dedup  phone number variants and
                # file path casing should not bypass duplicate detection.
                _dedup_args = dict(tool_args)
                if _is_side_effect:
                    for _dk in ("phone", "to", "recipient", "number"):
                        _pv = _dedup_args.get(_dk)
                        if isinstance(_pv, str) and _pv:
                            # Strip +, leading zeros, spaces, dashes  bare digits
                            _dedup_args[_dk] = re.sub(r'[^0-9]', '', _pv).lstrip('0') or _pv
                    for _dk in ("path", "media_path", "file_path", "file", "filename"):
                        _fv = _dedup_args.get(_dk)
                        if isinstance(_fv, str) and _fv:
                            # Normalize path separators, case, trailing slashes
                            _dedup_args[_dk] = _fv.replace('\\', '/').strip('/').lower()

                _args_hash = hashlib.md5(
                    json.dumps(_dedup_args, sort_keys=True, default=str).encode()
                ).hexdigest()[:8]
                _call_sig = f"{tool_name}:{_args_hash}" if _is_side_effect else f"{tool_name}:{_args_hash}:{i}"
                if _call_sig in seen_calls:
                    obs_skip = f"(already executed this run  result already recorded above)"
                    print(f"[AGENT]  Idempotency skip [{_call_sig}]")
                    last_error = f"duplicate tool call blocked: {tool_name} with same args"
                    _step_calls.append({
                        "tool": tool_name,
                        "status": "blocked",
                        "summary": last_error,
                        "args": json.dumps(tool_args, default=str)[:140],
                    })
                    _special_retry_note = (
                        f"{obs_skip}\n"
                        "Do not repeat the same call. Either output DONE if this step is complete, "
                        "or choose a different tool/args for this step.\n\n"
                    )
                    if attempt == MAX_RETRIES_PER_STEP - 1:
                        step_status = "failed"
                        break
                    continue
                # Both read tools AND side-effect tools add to seen_calls ONLY after
                # confirmed success (see success block below). Previously read tools were
                # added before execution, causing a failed read to be permanently marked
                # "already executed"  blocking all retries for that step.
                _step_tool = tool_name  # record for completed.append

                yield {"type": "tool_call", "tool": tool_name, "args": tool_args, "step": i + 1}
                print(f"[STREAM][TOOL CALL] {tool_name}")
                print(f"[STREAM][TOOL ARGS] {json.dumps(tool_args, indent=2, default=str)}")

                try:
                    result = await asyncio.to_thread(self._safe_execute, tool_name, tool_args)
                except Exception as e:
                    result = {"status": "error", "error": f"Internal crash: {str(e)}"}

                if isinstance(result, dict) and result.get("status") == "error":
                    last_error = result.get("error", "unknown error")
                    last_failed_tool = tool_name
                    last_failed_args = tool_args
                    observation = f"Error: {last_error}"
                    _step_calls.append({
                        "tool": tool_name,
                        "status": "error",
                        "summary": last_error[:180],
                        "args": json.dumps(tool_args, default=str)[:140],
                    })
                    _recent_tool_outputs.append({
                        "tool": tool_name,
                        "status": "error",
                        "raw": observation,
                    })
                    # Record in structured ToolRecordStore
                    self._context_compiler.tool_records.add(
                        tool_name=tool_name, args=tool_args, status="error",
                        result_digest=last_error[:220], raw_result=observation,
                        step_idx=i, attempt=attempt,
                    )
                    self._context_compiler.metrics.record_tool_call(
                        tool_name, success=False,
                        error_type="missing_arg" if "arg" in last_error.lower() else "",
                        is_retry=attempt > 0,
                    )
                    print(f"[STREAM][TOOL RESULT ] {observation}")
                    yield {"type": "tool_result", "tool": tool_name, "result": observation,
                           "status": "error", "step": i + 1}
                    _policy_e = _get_policy(tool_name)
                    _verdict_e = _classify_tool_result(
                        _policy_e, tool_name, tool_args, None, str(last_error), ""
                    )
                    if _verdict_e == "retryable":
                        continue
                    step_status = "failed"
                    if _verdict_e == "hard_fail":
                        _force_plan_revise = True
                    break
                else:
                    tool_res = result.get("result", result) if isinstance(result, dict) else result
                    self._update_working_state_from_tool_result(tool_name, tool_args, tool_res)
                    raw_obs = json.dumps(tool_res, indent=2, default=str)
                    print(f"[STREAM][TOOL RESULT ] ({len(raw_obs)} chars):")
                    print(raw_obs[:2000] + ("\n...[truncated]" if len(raw_obs) > 2000 else ""))
                    observation = raw_obs
                    _recent_tool_outputs.append({
                        "tool": tool_name,
                        "status": "ok",
                        "raw": raw_obs,
                    })
                    _buf_id_for_record = None
                    if self._should_buffer_result(tool_name, observation):
                        # Distill noisy search/scrape results before buffering.
                        # This keeps the buffer compact and removes navigation noise,
                        # so the pre-fetch injected into downstream steps is signal-only.
                        _store_data = observation
                        if tool_name.startswith(("search.", "firecrawl.")):
                            _store_data = self._distill_search_result(
                                observation, step_desc, model
                            )
                        self._buffer_id += 1
                        self.buffer[self._buffer_id] = {
                            "data": _store_data, "tool": tool_name, "chars": len(_store_data)
                        }
                        _buf_id_for_record = self._buffer_id
                        # Show digest from the distilled (stored) data, not the raw noisy obs
                        _buf_stored_data = self.buffer[self._buffer_id]["data"]
                        observation = self._make_buffer_observation(self._buffer_id, _buf_stored_data, tool_name)
                    yield {"type": "tool_result", "tool": tool_name, "result": observation,
                           "status": "ok", "step": i + 1}
                    # Always summarize from raw tool output (not the buffer note),
                    # so downstream steps receive meaningful content.
                    step_result = self._compress_obs(raw_obs, tool_name)
                    if _buf_id_for_record is not None:
                        step_result = f"{step_result} [BUFFER:{_buf_id_for_record}]"
                    step_status = "done"
                    _step_calls.append({
                        "tool": tool_name,
                        "status": "ok",
                        "summary": step_result[:180],
                        "args": json.dumps(tool_args, default=str)[:140],
                    })
                    _step_actions.append(
                        f"{tool_name} -> {step_result[:120]}"
                    )
                    self._merge_artifacts(_step_artifacts, self._extract_key_data(observation + "\n" + step_result))
                    # Record in structured ToolRecordStore
                    self._context_compiler.tool_records.add(
                        tool_name=tool_name, args=tool_args, status="ok",
                        result_digest=step_result[:220], raw_result=raw_obs,
                        step_idx=i, attempt=attempt,
                        buffer_id=_buf_id_for_record,
                        artifacts=dict(_step_artifacts),
                    )
                    self._context_compiler.metrics.record_tool_call(tool_name, success=True)
                    print(f"[STREAM][CONTEXTNEXT] {step_result}")

                    #  Mark seen AFTER confirmed success for ALL tool types 
                    # Read tools use step-indexed keys (tool:hash:step) so they can still
                    # re-read at different step indices; side-effects use step-free keys
                    # (tool:hash) to block all duplicates globally across the run.
                    seen_calls.add(_call_sig)
                    # Only count genuinely per-item additive tools toward the batch
                    # quota.  filesystem.write/append write ALL content in one call;
                    # counting them would falsely trigger 5 spurious continuation steps.
                    _BATCH_ADDITIVE_TOOLS = frozenset({
                        "todo.add", "contacts.add",
                        "calendar.add_event", "calendar.add_recurring_event",
                        "knowledge.ingest",
                        "whatsapp.send", "whatsapp.send_media",
                        "email.send", "email.send_job_application", "email.create_draft",
                    })
                    if _is_side_effect and tool_name in _BATCH_ADDITIVE_TOOLS:
                        _step_quant_done += 1  # count completed items for batch steps

                    #  Policy-driven result validator 
                    # Consults ToolPolicy to detect bad/empty results for
                    # non-side-effect tools without any LLM text reasoning.
                    # For side-effect tools, check for explicit failure indicators in the
                    # raw result that the simple error-status check above would miss
                    # (e.g. {"status": "failed", "reason": "..."} or {"error": "..."}).
                    if _is_side_effect:
                        # Side-effect tools: detect hidden failures in result dict
                        _se_status = (
                            tool_res.get("status", "") if isinstance(tool_res, dict) else ""
                        )
                        _se_failed = (
                            str(_se_status).lower() in ("failed", "failure", "rejected", "blocked")
                            or (isinstance(tool_res, dict) and bool(tool_res.get("error")))
                        )
                        if _se_failed:
                            _se_reason = (
                                tool_res.get("error")
                                or tool_res.get("reason")
                                or tool_res.get("message")
                                or str(tool_res)[:200]
                            ) if isinstance(tool_res, dict) else str(tool_res)[:200]
                            print(f"[AGENT]  Side-effect tool {tool_name} returned soft-failure: {_se_reason}")
                            # Downgrade from ok to failed
                            _step_calls[-1]["status"] = "soft_fail"
                            _step_calls[-1]["summary"] = f"soft-fail: {str(_se_reason)[:160]}"
                            last_error = f"Tool returned failure status: {str(_se_reason)[:200]}"
                            last_failed_tool = tool_name
                            last_failed_args = tool_args
                            step_status = "failed"
                            step_result = None
                            continue  # retry loop
                    elif not _is_side_effect:
                        _policy_v  = _get_policy(tool_name)
                        _verdict_v = _classify_tool_result(
                            _policy_v, tool_name, tool_args, tool_res, "", step_result
                        )
                        if _verdict_v != "success":
                            _empty_matches = (
                                isinstance(tool_res, dict)
                                and tool_res.get("matches") == []
                            )
                            _empty_contacts = (
                                tool_name in ("contacts.search", "contacts.get")
                                and isinstance(tool_res, dict)
                                and (
                                    tool_res.get("contacts") == []
                                    or tool_res.get("contact") in (None, {}, "")
                                    or tool_res.get("count") == 0
                                )
                            )
                            replan_reason = (
                                f"{tool_name} found no matching files  check filename or use filesystem.list"
                                if _empty_matches else
                                f"tool {tool_name} returned unusable result: {step_result[:120]}"
                            )
                            if _empty_contacts:
                                replan_reason = (
                                    f"{tool_name} found no matching contact for provided name. "
                                    "Current strategy cannot continue. Replan from this step."
                                )
                                _force_plan_revise = True
                            print(f"[AGENT]  Policy fail step {i+1}: {replan_reason}")
                            yield {"type": "thought",
                                   "content": f" **Policy fail step {i+1}:** {replan_reason}",
                                   "step": i + 1}
                            last_error = replan_reason
                            last_failed_tool = tool_name
                            last_failed_args = tool_args
                            step_status = "failed"
                            step_result = None
                            continue  # retry loop

                    # Side-effect calls should not be followed by extra calls in same step.
                    if _is_side_effect:
                        break

                    if attempt < MAX_RETRIES_PER_STEP - 1:
                        # Build an explicit summary of what was already found so the model
                        # doesn't call redundant tools trying to "confirm" data it has.
                        _found_summary = "\n".join(
                            f"  - {a}" for a in _step_actions
                        ) if _step_actions else "  - (result recorded above)"
                        _special_retry_note = (
                            f"ALREADY RETRIEVED for this step:\n{_found_summary}\n\n"
                            "If ANY of the results above satisfy the step goal, output DONE immediately.\n"
                            "Do NOT call more tools to verify, confirm, or double-check data you already have.\n\n"
                        )
                        continue
                    break

            #  Policy-driven fallback executor 
            # Runs BEFORE LLM replan. Iterates policy.fallback_sequence in order:
            #   "tool" entries execute a cheap recovery action (e.g. filesystem.list).
            #   "need_input" entries pause the run and rewind the step.
            # LLM replan fires only when all policy fallbacks are exhausted.
            if step_status == "failed" and not _step_rewound and not _force_plan_revise:
                _policy_fb  = _get_policy(last_failed_tool or "")
                _verdict_fb = _classify_tool_result(
                    _policy_fb, last_failed_tool or "", last_failed_args or {},
                    None, last_error or "", ""
                )
                _fb_handled = False

                # Fast path: need_input verdict with no fallback tools to run first
                if _verdict_fb == "need_input" and not _policy_fb.fallback_sequence:
                    _ni_fn  = _policy_fb.need_input_question
                    _auto_q = (
                        _ni_fn(last_failed_tool or "", last_failed_args or {}, last_error or "")
                        if _ni_fn else
                        f"I need more information to proceed. Error: {(last_error or '')[:200]}"
                    )
                    _ask_user, _slot_name, _slot_note = self._should_request_need_input(
                        _auto_q, last_failed_tool or "", last_failed_args or {}
                    )
                    if not _ask_user:
                        last_error = f"need_input suppressed: {_slot_note[:180]}"
                        _step_actions.append(f"need_input_suppressed -> {_slot_note[:120]}")
                        _step_rewound = True
                        _step_idx = i
                        step_status = "failed"
                        _fb_handled = True
                        print(f"[AGENT] Need-input suppressed at step {i+1}: {_slot_note}")
                    if run_handle and hasattr(run_handle, 'input_queue') \
                            and not run_handle.is_cancelled() and not _fb_handled:
                        print(f"[AGENT]  Policy NEED_INPUT step {i+1}: {_auto_q}")
                        yield {"type": "need_input", "question": _auto_q, "step": i + 1}
                        try:
                            _ui_ans = await asyncio.wait_for(
                                run_handle.input_queue.get(), timeout=300
                            )
                            self._ingest_user_answer(i, _auto_q, _ui_ans, _slot_name)
                            _latest_user_input = _ui_ans
                            _step_actions.append(f"user_input -> {_ui_ans[:120]}")
                            last_error = f"Need input answered by user: {_ui_ans[:180]}"
                            step_status = "failed"
                            _ui_compact = _ui_ans.strip()[:180]
                            _clarified_desc = step_desc
                            if _ui_compact and _ui_compact.lower() not in _clarified_desc.lower():
                                _clarified_desc = f"{step_desc} (User clarification: {_ui_compact})"
                            steps[i] = {"goal": _clarified_desc, "categories": step_categories}
                            _step_idx = i
                            _step_rewound = True
                            _fb_handled = True
                            print(f"[AGENT]  Need input captured; rewinding step {i+1} with clarification")
                        except asyncio.TimeoutError:
                            print(f"[AGENT]  NEED_INPUT timed out step {i+1}  falling to replan")

                # Structured fallback sequence (tool calls followed by optional need_input)
                if not _fb_handled and not _step_rewound and _policy_fb.fallback_sequence:
                    for _fb_type, _fb_composer in _policy_fb.fallback_sequence:
                        if _step_rewound or _fb_handled:
                            break
                        if _fb_type == "tool":
                            _fb_out = _fb_composer(
                                last_failed_tool or "", last_failed_args or {}, last_error or ""
                            )
                            if _fb_out is None:
                                continue
                            _fb_tn, _fb_ta = _fb_out
                            _fb_sig = (
                                f"{_fb_tn}:"
                                f"{hashlib.md5(json.dumps(_fb_ta, sort_keys=True, default=str).encode()).hexdigest()[:8]}"
                            )
                            if _fb_sig in seen_calls:
                                print(f"[AGENT]  Fallback skip (already done): {_fb_tn}")
                                continue
                            print(f"[AGENT]  Policy fallback: {_fb_tn}({json.dumps(_fb_ta)[:80]})")
                            yield {"type": "thought",
                                   "content": f" Policy fallback: {_fb_tn}", "step": i + 1}
                            yield {"type": "tool_call", "tool": _fb_tn, "args": _fb_ta, "step": i + 1}
                            try:
                                _fb_res = await asyncio.to_thread(
                                    self._safe_execute, _fb_tn, _fb_ta
                                )
                            except Exception as _fb_e:
                                _fb_res = {"status": "error", "error": str(_fb_e)}
                            if isinstance(_fb_res, dict) and _fb_res.get("status") == "error":
                                yield {"type": "tool_result", "tool": _fb_tn,
                                       "result": f"Error: {_fb_res.get('error','unknown')}",
                                       "status": "error", "step": i + 1}
                            else:
                                _fb_tr  = (
                                    _fb_res.get("result", _fb_res)
                                    if isinstance(_fb_res, dict) else _fb_res
                                )
                                self._update_working_state_from_tool_result(_fb_tn, _fb_ta, _fb_tr)
                                _fb_obs = json.dumps(_fb_tr, indent=2, default=str)
                                _recent_tool_outputs.append({
                                    "tool": _fb_tn,
                                    "status": "ok",
                                    "raw": _fb_obs,
                                })
                                if self._should_buffer_result(_fb_tn, _fb_obs):
                                    _fb_store = _fb_obs
                                    if _fb_tn.startswith(("search.", "firecrawl.")):
                                        _fb_store = self._distill_search_result(
                                            _fb_obs, step_desc, model
                                        )
                                    self._buffer_id += 1
                                    self.buffer[self._buffer_id] = {
                                        "data": _fb_store, "tool": _fb_tn, "chars": len(_fb_store)
                                    }
                                    _fb_obs = self._make_buffer_observation(
                                        self._buffer_id, _fb_obs, _fb_tn
                                    )
                                seen_calls.add(_fb_sig)
                                step_result = self._compress_obs(_fb_obs, _fb_tn)
                                step_status = "done"
                                _step_tool  = _fb_tn
                                _fb_handled = True
                                yield {"type": "tool_result", "tool": _fb_tn,
                                       "result": _fb_obs, "status": "ok", "step": i + 1}
                                print(f"[AGENT]  Policy fallback succeeded: {step_result[:80]}")
                        elif _fb_type == "need_input":
                            _auto_q = _fb_composer(
                                last_failed_tool or "", last_failed_args or {}, last_error or ""
                            )
                            _ask_user, _slot_name, _slot_note = self._should_request_need_input(
                                _auto_q, last_failed_tool or "", last_failed_args or {}
                            )
                            if not _ask_user:
                                last_error = f"need_input suppressed: {_slot_note[:180]}"
                                _step_actions.append(f"need_input_suppressed -> {_slot_note[:120]}")
                                _step_rewound = True
                                _step_idx = i
                                step_status = "failed"
                                _fb_handled = True
                                print(f"[AGENT] Need-input suppressed at step {i+1}: {_slot_note}")
                                break
                            if run_handle and hasattr(run_handle, 'input_queue') \
                                    and not run_handle.is_cancelled():
                                print(f"[AGENT]  Policy fallback NEED_INPUT step {i+1}: {_auto_q}")
                                yield {"type": "need_input", "question": _auto_q, "step": i + 1}
                                try:
                                    _ui_ans = await asyncio.wait_for(
                                        run_handle.input_queue.get(), timeout=300
                                    )
                                    self._ingest_user_answer(i, _auto_q, _ui_ans, _slot_name)
                                    _latest_user_input = _ui_ans
                                    _step_actions.append(f"user_input -> {_ui_ans[:120]}")
                                    last_error = f"Need input answered by user: {_ui_ans[:180]}"
                                    step_status = "failed"
                                    _ui_compact = _ui_ans.strip()[:180]
                                    _clarified_desc = step_desc
                                    if _ui_compact and _ui_compact.lower() not in _clarified_desc.lower():
                                        _clarified_desc = f"{step_desc} (User clarification: {_ui_compact})"
                                    steps[i] = {"goal": _clarified_desc, "categories": step_categories}
                                    _step_idx = i
                                    _step_rewound = True
                                    _fb_handled = True
                                    print(f"[AGENT]  Need input captured; rewinding step {i+1} with clarification")
                                except asyncio.TimeoutError:
                                    print(f"[AGENT]  NEED_INPUT timed out step {i+1}")
                            break  # stop sequence after need_input attempt

            if not _step_rewound:
                if step_status == "failed":
                    _consecutive_failures += 1
                else:
                    _consecutive_failures = 0

            # -- Replan-as-step-splitting: revise remaining plan from failed step onward --
            if (
                step_status == "failed"
                and not _step_rewound
                and _plan_revisions < MAX_PLAN_REVISIONS
                and _consecutive_failures >= REPLAN_CONSECUTIVE_FAILURES
            ):
                yield {
                    "type": "thought",
                    "content": (
                        f" Replanning from step {i+1} "
                        f"after {_consecutive_failures} consecutive failures: {step_desc[:90]}"
                    ),
                    "step": i + 1,
                }
                revised_steps = await self._revise_plan(
                    task=text,
                    completed=completed,
                    failed_step=step_desc,
                    failed_reason=(last_error or "unknown"),
                    failed_attempts=_step_actions,
                    user_input=_latest_user_input,
                    remaining_steps=steps[i + 1:],
                    model=model,
                    history_text=history_text,
                    profile_context=_profile_context_exec_cached,
                    memory_context=_memory_context_exec_cached,
                    tool_categories_summary=_tool_categories_summary_cached,
                )
                if revised_steps:
                    _plan_revisions += 1
                    _consecutive_failures = 0
                    self._context_compiler.metrics.record_plan_revision()
                    self._context_compiler.short_term.record_decision(
                        i, f"Replanned from step {i+1}: {step_desc[:80]}",
                        rationale=last_error[:120] if last_error else ""
                    )
                    steps = steps[:i] + revised_steps
                    _step_idx = i
                    completed.append({
                        "step": step_desc,
                        "status": "failed",
                        "result": f"replanned: {(last_error or 'step failed')[:160]}",
                        "tool": _step_tool,
                        "calls": _step_calls,
                        "artifacts": _step_artifacts,
                        "categories": step_categories,
                    })
                    # Close step metrics before continuing to revised plan
                    self._context_compiler.metrics.end_step(i, success=False)
                    continue
                # If the replan model failed to emit parseable numbered steps, do not
                # terminate immediately. Retry the same step with user clarification
                # injected so execution can continue instead of forced FINAL ANSWER.
                if _latest_user_input:
                    _plan_revisions += 1
                    self._context_compiler.metrics.record_plan_revision()
                    _clarified_desc = step_desc
                    _ui_compact = _latest_user_input.strip()[:180]
                    if _ui_compact and _ui_compact.lower() not in _clarified_desc.lower():
                        _clarified_desc = f"{step_desc} (User clarification: {_ui_compact})"
                    steps[i] = {
                        "goal": _clarified_desc,
                        "categories": step_categories,
                    }
                    _step_idx = i
                    yield {
                        "type": "thought",
                        "content": f" Replan parse fallback: retrying step {i+1} with clarification",
                        "step": i + 1,
                    }
                    # Close step metrics before retrying with clarification
                    self._context_compiler.metrics.end_step(i, success=False)
                    continue

            # Do not force a final answer on single-step failures.
            # Keep executing remaining steps; synthesis will report partial success/failures.

            #  Quantified step continuation 
            # If the step had a numeric goal (N items) and only k < N side-effects
            # succeeded, inject (N-k) continuation sub-steps into the step list so
            # the while-loop processes each remaining item one at a time.
            # Guard: require _step_quant_done > 0 to avoid runaway on broken tools.
            if (
                step_status == "done"
                and _step_quant_n > 0
                and _step_quant_done > 0
                and _step_quant_done < _step_quant_n
            ):
                _remaining = _step_quant_n - _step_quant_done
                _base_desc = step_desc.split(" [item ")[0]  # strip prior annotation
                for _k in range(_remaining):
                    _item_num  = _step_quant_done + _k + 1
                    _cont_desc = f"{_base_desc} [item {_item_num} of {_step_quant_n}]"
                    steps.insert(i + 1 + _k, {"goal": _cont_desc, "categories": step_categories})
                print(
                    f"[AGENT]  Quantified continuation: {_step_quant_done}/{_step_quant_n} done, "
                    f"injected {_remaining} continuation step(s)"
                )

            if not _step_rewound:  # don't log rewound steps; they'll re-run and log on success
                if step_status == "done":
                    self._maybe_update_handoff_payload(_step_tool, step_result or "")
                _completed_entry = {
                    "step": step_desc,
                    "status": step_status,
                    "result": step_result if step_result else (last_error[:100] if last_error else "failed"),
                    "tool": _step_tool,
                    "calls": _step_calls,
                    "artifacts": _step_artifacts,
                    "categories": step_categories,
                }
                completed.append(_completed_entry)
                # Track step completion in metrics
                self._context_compiler.metrics.end_step(i, success=(step_status == "done"))
                # After a search/scraping step succeeds, annotate remaining steps with
                # the real BUFFER ID so downstream email/send steps know which buffer to read.
                if step_status == "done" and _step_tool.startswith(("search.", "firecrawl.")):
                    _remaining_steps = steps[_step_idx:]
                    if _remaining_steps:
                        steps[_step_idx:] = self._inject_buffer_refs_into_steps(
                            _remaining_steps, _completed_entry, self.buffer
                        )


            if final_response:
                break

        #  Phase 1.5: Postcondition  count verification for quantified tasks 
        # When the task asked for N todos/events/contacts and matching side-effect
        # tools were used, verify the actual stored count matches the expectation.
        # Injects a warning into the synthesis prompt if the count is short.
        _postcond_warning = ""
        if not final_response:
            _quant_task = re.search(
                r'\b(\d+)\s+(?:same\s+)?(?:item|todo|task|event|contact|checklist[- ]?item)s?\b',
                text, re.IGNORECASE
            )
            if _quant_task:
                _expected_n  = int(_quant_task.group(1))
                _entity_type = _quant_task.group(2).lower().replace("-", "").replace(" ", "")
                _VERIFY_MAP  = {
                    "todo":          "todo.list",
                    "task":          "todo.list",
                    "checklistitem": "todo.list",
                    "event":         "calendar.list_events",
                }
                _verify_tool = _VERIFY_MAP.get(_entity_type)
                _entity_root = _entity_type.replace("checklist", "")  # "item"  skip
                _had_matching_steps = any(
                    s.get("tool", "").startswith(_entity_root) or
                    s.get("tool", "") == _verify_tool
                    for s in completed if s["status"] == "done"
                )
                if _verify_tool and _had_matching_steps:
                    try:
                        _vr = await asyncio.to_thread(self._safe_execute, _verify_tool, {})
                        _actual_n = (
                            _vr.get("count", len(_vr.get("tasks", _vr.get("events", []))))
                            if isinstance(_vr, dict) else
                            len(_vr) if isinstance(_vr, list) else 0
                        )
                        if _actual_n < _expected_n:
                            _postcond_warning = (
                                f"\n POSTCONDITION FAILURE: task required {_expected_n} "
                                f"{_entity_type}(s), but {_verify_tool} shows only {_actual_n} "
                                f"total entries. The batch may be incomplete  report this.\n"
                            )
                            print(f"[AGENT]  Postcondition: expected {_expected_n}, got {_actual_n}")
                        else:
                            print(f"[AGENT]  Postcondition: {_actual_n} >= {_expected_n} ")
                    except Exception as _pce:
                        print(f"[AGENT] Postcondition check error: {_pce}")

        #  Phase 1.5b: Todo title postcondition 
        # For any todo.add steps that reported success (with a title in the obs),
        # verify the exact title appears in todo.list. Catches cases where the model
        # used a wrong or truncated title, or the add silently failed.
        if not _postcond_warning:
            _added_titles: list = []
            for _cs in completed:
                if _cs.get("tool") == "todo.add" and _cs["status"] == "done":
                    _tm = re.search(r"title=['\"]([^'\"]+)['\"]", str(_cs["result"]))
                    if _tm:
                        _added_titles.append(_tm.group(1).strip().lower())
            if _added_titles:
                try:
                    _tlr = await asyncio.to_thread(self._safe_execute, "todo.list", {})
                    _existing = [
                        t.get("title", "").lower()
                        for t in (_tlr.get("tasks", []) if isinstance(_tlr, dict) else [])
                        if isinstance(t, dict)
                    ]
                    _missing_titles = [
                        t for t in _added_titles
                        if not any(t in ex or ex in t for ex in _existing)
                    ]
                    if _missing_titles:
                        _postcond_warning = (
                            f"\n\u26a0\ufe0f POSTCONDITION FAILURE: {len(_missing_titles)} todo title(s) "
                            f"reported as added but not found in todo.list: {_missing_titles[:5]}. "
                            "Report this discrepancy  the items may not have been saved.\n"
                        )
                        print(f"[AGENT] \u274c Todo title postcondition: missing {_missing_titles}")
                    else:
                        print(f"[AGENT] \u2705 Todo title postcondition: all {len(_added_titles)} title(s) confirmed")
                except Exception as _tlpe:
                    print(f"[AGENT] Todo title postcondition error: {_tlpe}")

        #  Phase 1.5c: Media-send postcondition for PDF/document delivery 
        # If task asks to send a PDF/document/file in WhatsApp context, ensure
        # whatsapp.send_media succeeded with type=document and a valid file path.
        if not _postcond_warning:
            _task_l = (text or "").lower()
            _wants_file_send = bool(
                re.search(r'\b(send|share|forward)\b', _task_l)
                and re.search(r'\b(pdf|document|doc|file)\b', _task_l)
            )
            _had_whatsapp = any(
                any(str(c.get("tool", "")).startswith("whatsapp.") and c.get("status") == "ok"
                    for c in (s.get("calls", []) if isinstance(s, dict) else []))
                for s in completed
            )
            if _wants_file_send and ("whatsapp" in _task_l or _had_whatsapp):
                # Check ToolRecordStore for send_media calls with correct args
                _media_records = [
                    r for r in self._context_compiler.tool_records._records
                    if r.tool_name == "whatsapp.send_media" and r.status == "ok"
                ]
                _sent_media_ok = len(_media_records) > 0
                # Deeper check: at least one call must have type=document and
                # a file path containing a PDF-like extension
                _sent_document_ok = any(
                    r.args.get("type") == "document"
                    and any(
                        ext in str(r.args.get("media_path", "") or r.args.get("path", "")).lower()
                        for ext in (".pdf", ".doc", ".docx", ".xlsx", ".csv")
                    )
                    for r in _media_records
                )
                if not _sent_media_ok:
                    _postcond_warning = (
                        "\n POSTCONDITION FAILURE: task requested sending a PDF/document/file, "
                        "but no successful whatsapp.send_media call was recorded. "
                        "Do not claim file delivery succeeded.\n"
                    )
                    print("[AGENT]  Media postcondition: missing successful whatsapp.send_media")
                elif not _sent_document_ok:
                    # send_media was called but with wrong type (e.g. type=image)
                    _actual_types = [r.args.get("type", "unknown") for r in _media_records]
                    _actual_paths = [
                        str(r.args.get("media_path", "") or r.args.get("path", ""))[:60]
                        for r in _media_records
                    ]
                    _postcond_warning = (
                        "\n POSTCONDITION WARNING: whatsapp.send_media succeeded but "
                        f"used type={_actual_types} instead of type='document', "
                        f"or the file path does not look like a PDF/document: {_actual_paths}. "
                        "Verify the correct file was sent as a document attachment.\n"
                    )
                    print(f"[AGENT]  Media postcondition: wrong type/path  "
                          f"types={_actual_types}, paths={_actual_paths}")
                else:
                    print("[AGENT]  Media postcondition: whatsapp.send_media with type=document recorded")

        # -- Phase 2: Synthesise --
        if not final_response:
            #  Hard-block: if any critical side-effect step failed, bypass the LLM
            # entirely to prevent 4B model from hallucinating success. 
            _CRITICAL_VERBS = ("send", "write", "create", "add", "update", "delete",
                               "move", "patch", "post", "upload")
            failed_critical = [
                s for s in completed
                if s["status"] == "failed"
                and any(v in s["step"].lower() for v in _CRITICAL_VERBS)
            ]
            if failed_critical:
                fail_lines = "\n".join(
                    f"  - Step '{s['step'][:80]}': {s['result'][:120]}"
                    for s in failed_critical
                )
                ok_lines = "\n".join(
                    f"  - Step '{s['step'][:80]}': {s['result'][:120]}"
                    for s in completed if s["status"] == "done"
                ) or "  (none)"
                final_response = (
                    f" Task incomplete  the following required actions failed:\n{fail_lines}\n\n"
                    f"What did succeed:\n{ok_lines}\n\n"
                    f"Please correct the issue and try again."
                )
                final_response = self._maybe_stage_workflow_suggestion(
                    text, completed, final_response, workflow_was_auto_used=bool(_workflow_steps_preloaded)
                )
                yield {"type": "final", "content": final_response}
                self.history.append((text, final_response))
                # Finalize metrics
                _run_metrics = self._context_compiler.metrics.finalize()
                print(self._context_compiler.metrics.render_metrics_block())
                return
            if self._should_skip_synthesis(completed) and not _postcond_warning:
                final_response = self._render_simple_completion(completed)
                final_response = self._maybe_stage_workflow_suggestion(
                    text, completed, final_response, workflow_was_auto_used=bool(_workflow_steps_preloaded)
                )
                _run_metrics = self._context_compiler.metrics.finalize()
                print(self._context_compiler.metrics.render_metrics_block())
                yield {"type": "final", "content": final_response}
                self.history.append((text, final_response))
                return
            #  Synthesizer bypass (BYPASS_SYNTHESIZER=True) 
            if (not final_response) and BYPASS_SYNTHESIZER:
                _ok  = [s for s in completed if s["status"] == "done"]
                _bad = [s for s in completed if s["status"] != "done"]
                _parts = []
                for s in _ok:
                    _parts.append(f" {s['step']}\n   {s['result']}")
                for s in _bad:
                    _parts.append(f" {s['step']}  FAILED: {s['result']}")
                final_response = "\n\n".join(_parts) if _parts else "All steps completed."
                final_response = self._maybe_stage_workflow_suggestion(
                    text, completed, final_response, workflow_was_auto_used=bool(_workflow_steps_preloaded)
                )
                print("[STREAM] Synthesizer bypassed  using raw step results.")
                _run_metrics = self._context_compiler.metrics.finalize()
                print(self._context_compiler.metrics.render_metrics_block())
                yield {"type": "final", "content": final_response}
                self.history.append((text, final_response))
                return
            # 

            # Use deterministic compaction for completed steps trajectory
            _compacted_trajectory = self._context_compiler.compact_trajectory(completed)
            # Also include structured tool call log from ToolRecordStore
            _tool_call_log = self._context_compiler.tool_records.render_compact_summary()

            # Build a step summary that distinguishes  success from  failure
            done_lines = "\n".join(
                f"  [{'ok' if s['status']=='done' else 'FAILED'}] "
                f"Step {j+1}: {s['step']} -> {s['result']}"
                for j, s in enumerate(completed)
            )
            # Check if any write/mutation step failed  used to warn the model
            write_keywords = ("update", "create", "add", "delete", "send", "write", "set", "move", "patch")
            failed_writes = [
                s for s in completed
                if s["status"] == "failed" and any(w in s["step"].lower() for w in write_keywords)
            ]
            failed_writes_note = ""
            if failed_writes:
                failed_writes_note = (
                    " IMPORTANT: The following write/update steps FAILED:\n"
                    + "\n".join(f"  - {s['step']}: {s['result']}" for s in failed_writes)
                    + "\nDo NOT claim these actions succeeded. Report the failure honestly.\n\n"
                )
            # Use ContextCompiler for synthesis with adaptive budget
            _synth_sections = self._context_compiler.compile(
                step_type=StepType.SYNTHESIZE,
                user_message=text,
                profile_context=_profile_context_exec_cached,
                memory_context=_memory_context_exec_cached,
                history_text=history_text,
                done_block=f"Execution results:\n{done_lines}\n\n{_compacted_trajectory}{_tool_call_log}",
                task_contract="",
                working_state="",
            )
            _synth_sections, _synth_budget_note, _synth_diag = self._context_compiler.apply_budget(
                _synth_sections, step_type=StepType.SYNTHESIZE
            )
            synth_prompt = self._context_compiler.render_prompt(
                _synth_sections,
                step_type=StepType.SYNTHESIZE,
                budget_note=_synth_budget_note,
                output_format=(
                    f"{failed_writes_note}"
                    f"{_postcond_warning}"
                    "CRITICAL RULES for your answer:\n"
                    "1. Only claim an action succeeded if the step summary shows [ok]. A step marked [FAILED] did NOT succeed.\n"
                    "2. If a write/send step is [FAILED], say so clearly and do NOT invent success.\n"
                    "3. Base your answer solely on the execution results above, not on the original plan.\n"
                    "MIRA: FINAL ANSWER:"
                ),
            )
            # Use structured messages when provider supports it
            _synth_output_format = (
                f"{failed_writes_note}"
                f"{_postcond_warning}"
                "CRITICAL RULES for your answer:\n"
                "1. Only claim an action succeeded if the step summary shows [ok]. A step marked [FAILED] did NOT succeed.\n"
                "2. If a write/send step is [FAILED], say so clearly and do NOT invent success.\n"
                "3. Base your answer solely on the execution results above, not on the original plan.\n"
                "MIRA: FINAL ANSWER:"
            )
            _synth_flat, _synth_msgs = self._context_compiler.prepare_for_generation(
                _synth_sections,
                step_type=StepType.SYNTHESIZE,
                budget_note=_synth_budget_note,
                output_format=_synth_output_format,
            )
            if _synth_msgs is not None:
                synth_raw = await asyncio.to_thread(generate_with_messages, _synth_msgs, model=model, temperature=0.5)
            else:
                synth_raw = await asyncio.to_thread(generate, _synth_flat or synth_prompt, model=model, temperature=0.5)
            print(f"\n{'='*70}")
            print("[STREAM] Phase 2  SYNTHESIS")
            print(f"{'-'*70}")
            print("[STREAM][PROMPT LLM]")
            print(synth_prompt)
            print(f"{'-'*70}")
            print("[STREAM][LLM REPLY]")
            print(synth_raw)
            print(f"{'='*70}")
            final_response = synth_raw.strip()
            if "FINAL ANSWER:" in final_response:
                final_response = final_response.split("FINAL ANSWER:")[-1].strip()
            if self._extract_tool_call(final_response):
                final_response = completed[-1]["result"] if completed else "Task completed."
            final_response = self._maybe_stage_workflow_suggestion(
                text, completed, final_response, workflow_was_auto_used=bool(_workflow_steps_preloaded)
            )

        #  Finalize context quality metrics 
        _run_metrics = self._context_compiler.metrics.finalize()
        print(self._context_compiler.metrics.render_metrics_block())

        yield {"type": "final", "content": final_response}
        self.history.append((text, final_response))

    def _safe_execute(self, tool_name: str, args: dict):
        """Internal helper to execute a tool with argument aliasing and safety."""

        #  Runtime allow/deny gate 
        # Constraints are set by handle_streaming() from user prompt patterns:
        #   "Use ONLY: filesystem.*, todo.*"   self._allowed_tools = {"filesystem", "todo"}
        #   "Do NOT use: web/email"            self._denied_tools  = {"web", "email"}
        # Buffer/reasoning tools are always permitted regardless of constraints.
        _GATE_BUILTINS = ("buffer.", "memory.", "reasoning.", "think")
        _allowed_tools = getattr(self, '_allowed_tools', set())
        _denied_tools  = getattr(self, '_denied_tools',  set())
        if not any(tool_name.startswith(b) for b in _GATE_BUILTINS):
            for _deny_tok in _denied_tools:
                if (
                    tool_name.lower() == _deny_tok or
                    tool_name.lower().startswith(_deny_tok + '.') or
                    (len(_deny_tok) > 3 and tool_name.lower().startswith(_deny_tok))
                ):
                    return {
                        "status": "error",
                        "error": (
                            f"Tool '{tool_name}' is blocked by a runtime constraint "
                            f"(Do NOT use: '{_deny_tok}'). Use an allowed alternative."
                        )
                    }
            if _allowed_tools:
                _is_allowed = any(
                    tool_name.lower() == a or tool_name.lower().startswith(a + '.')
                    for a in _allowed_tools
                )
                if not _is_allowed:
                    return {
                        "status": "error",
                        "error": (
                            f"Tool '{tool_name}' is outside the allowed scope for this task. "
                            f"Allowed prefixes: {sorted(_allowed_tools)}. "
                            "Use only tools from the allowed list."
                        )
                    }

        #  Step-category execution gate 
        # Block tools whose registered category is not in the current step's
        # selected categories. Prevents model from hallucinating cross-step
        # tools (e.g. calling whatsapp.send_media during a "retrieve contact" step).
        _step_tools = getattr(self, "_current_step_tool_names", None)
        if _step_tools and not any(tool_name.startswith(b) for b in _GATE_BUILTINS):
            if tool_name not in _step_tools:
                return {
                    "status": "error",
                    "error": (
                        f"Tool '{tool_name}' is outside this step's predicted tools: {sorted(_step_tools)}. "
                        "Use one of the listed step tools, or output DONE if this step is complete."
                    ),
                }
        _step_cats = getattr(self, '_current_step_cats', None)
        if _step_cats and not any(tool_name.startswith(b) for b in _GATE_BUILTINS):
            from mcp_server.tools import get_tool_metadata
            _tmeta = get_tool_metadata(tool_name)
            if _tmeta:
                _tool_cat = (_tmeta.get("category") or "Other").strip().lower()
                if _tool_cat not in _step_cats:
                    print(f"[AGENT]  Category gate: {tool_name} (cat={_tool_cat}) "
                          f"blocked  step allows: {sorted(_step_cats)}")
                    return {
                        "status": "error",
                        "error": (
                            f"Tool '{tool_name}' (category: {_tool_cat}) is not relevant "
                            f"for this step. Allowed categories: {sorted(_step_cats)}. "
                            "Choose a tool from the listed categories, or output DONE "
                            "if this step is complete."
                        )
                    }

        # --- BUFFER TOOLS (handled internally, no registered tool needed) ---
        if tool_name == "buffer.search":
            buf_id        = int(args.get("id", 0))
            query         = args.get("query", "")
            extract_prompt = args.get("prompt", "")   # agent-provided extraction instruction
            buf_entry = self.buffer.get(buf_id, {})
            data      = buf_entry.get("data", "")
            if not data:
                return {
                    "status": "error",
                    "error": f"Buffer {buf_id} not found. Available: {list(self.buffer.keys())}",
                }
            lines = data.split('\n')

            #  Fast path: word-level OR keyword matching 
            query_words = [w for w in query.lower().split() if len(w) > 3]
            matches, seen_ranges = [], set()
            for i, line in enumerate(lines):
                if any(w in line.lower() for w in query_words):
                    start, end = max(0, i - 2), min(len(lines), i + 3)
                    if (start, end) not in seen_ranges:
                        seen_ranges.add((start, end))
                        matches.append('\n'.join(f"L{j+1}: {lines[j]}" for j in range(start, end)))
            if matches:
                return {"result": f"Found {len(matches)} matches:\n\n" + "\n---\n".join(matches[:15])}

            #  Slow path: LLM extraction if agent provided a prompt 
            if extract_prompt:
                truncated = data[:3500]
                llm_prompt = (
                    f"{extract_prompt}\n\nData:\n{truncated}\n\n"
                    f"Respond with ONLY the extracted information, concisely (max 300 chars)."
                )
                model = getattr(self, "_exec_model", "qwen3:4b-instruct")
                result_text = generate(llm_prompt, model=model)
                return {"result": result_text.strip()[:400]}

            #  Fallback: small sample + hint 
            sample = '\n'.join(l for l in lines if l.strip())[:600]
            return {
                "status": "error",
                "error": f"No keyword match in buffer {buf_id}. Pass prompt= for semantic extraction. Sample:\n{sample}",
            }

        if tool_name in ("memory.write", "buffer.write"):
            raw_content = (
                args.get("content")
                or args.get("text")
                or args.get("note")
                or args.get("memory")
            )
            if raw_content is None:
                return {
                    "status": "error",
                    "error": "memory.write requires content (use content/text/note/memory).",
                }
            content = raw_content if isinstance(raw_content, str) else json.dumps(raw_content, default=str)
            content = content.strip()
            if not content:
                return {"status": "error", "error": "memory.write content is empty."}

            tag = str(args.get("tag", "")).strip()
            clean = " ".join(content.splitlines()).strip()
            path = self._memory_file_path()
            existing = self._read_memory_file().rstrip()
            note_line = f"- [{tag}] {clean}" if tag else f"- {clean}"
            new_text = (existing + "\n" if existing else "") + note_line + "\n"
            path.write_text(new_text, encoding="utf-8")
            note_count = len([ln for ln in new_text.splitlines() if ln.strip().startswith("-")])
            return {
                "result": {
                    "memory_file": MEMORY_FILE,
                    "memory_note_id": note_count,
                    "stored_chars": len(content),
                    "tag": tag,
                    "auto_injected": True,
                }
            }

        if tool_name in ("user.write", "soul.write", "profile.write"):
            raw_content = (
                args.get("content")
                or args.get("text")
                or args.get("note")
                or args.get("memory")
            )
            if raw_content is None:
                return {
                    "status": "error",
                    "error": f"{tool_name} requires content (use content/text/note/memory).",
                }
            content = raw_content if isinstance(raw_content, str) else json.dumps(raw_content, default=str)
            content = content.strip()
            if not content:
                return {"status": "error", "error": f"{tool_name} content is empty."}

            file_arg = str(args.get("file", "")).strip().lower()
            if tool_name.startswith("user."):
                target_file = USER_FILE
            elif tool_name.startswith("soul."):
                target_file = SOUL_FILE
            else:
                if file_arg in ("user", "user.md", USER_FILE.lower()):
                    target_file = USER_FILE
                elif file_arg in ("soul", "soul.md", SOUL_FILE.lower()):
                    target_file = SOUL_FILE
                else:
                    target_file = USER_FILE

            tag = str(args.get("tag", "")).strip()
            clean = " ".join(content.splitlines()).strip()
            path = self._profile_file_path(target_file)
            existing = self._read_profile_file(target_file).strip()
            if not existing:
                existing = f"# {Path(target_file).stem}\n"
            note_line = f"- [{tag}] {clean}" if tag else f"- {clean}"
            new_text = existing.rstrip() + "\n" + note_line + "\n"
            path.write_text(new_text, encoding="utf-8")
            note_count = len([ln for ln in new_text.splitlines() if ln.strip().startswith("-")])
            return {
                "result": {
                    "profile_file": target_file,
                    "profile_note_id": note_count,
                    "stored_chars": len(content),
                    "tag": tag,
                    "auto_injected": True,
                }
            }

        if tool_name in ("memory.update", "buffer.update"):
            raw_content = (
                args.get("content")
                or args.get("text")
                or args.get("note")
                or args.get("memory")
            )
            if raw_content is None:
                return {
                    "status": "error",
                    "error": "memory.update requires content (use content/text/note/memory).",
                }
            content = raw_content if isinstance(raw_content, str) else json.dumps(raw_content, default=str)
            content = content.strip()
            if not content:
                return {"status": "error", "error": "memory.update content is empty."}

            mode = str(args.get("mode", "replace")).strip().lower() or "replace"
            if mode not in ("replace", "append"):
                return {
                    "status": "error",
                    "error": "memory.update mode must be 'replace' or 'append'.",
                }

            target_id = args.get("id", None)
            target_tag = str(args.get("tag", "")).strip()
            sep = str(args.get("separator", " "))
            clean = " ".join(content.splitlines()).strip()

            path = self._memory_file_path()
            lines = self._read_memory_file().splitlines()
            note_line_indexes = [idx for idx, ln in enumerate(lines) if ln.strip().startswith("-")]

            def _build_line(note_text: str, note_tag: str) -> str:
                return f"- [{note_tag}] {note_text}" if note_tag else f"- {note_text}"

            def _parse_line(line: str) -> tuple[str, str]:
                m = re.match(r'^\s*-\s*(?:\[([^\]]+)\]\s*)?(.*)$', line)
                if not m:
                    return "", line.strip()
                return (m.group(1) or "").strip(), (m.group(2) or "").strip()

            selected_line_idx = None
            selected_note_id = None

            if target_id is not None:
                try:
                    target_note_id = int(target_id)
                except (TypeError, ValueError):
                    return {"status": "error", "error": f"Invalid memory note id: {target_id}"}
                if target_note_id < 1 or target_note_id > len(note_line_indexes):
                    return {
                        "status": "error",
                        "error": f"Memory note id {target_note_id} not found in {MEMORY_FILE}.",
                    }
                selected_note_id = target_note_id
                selected_line_idx = note_line_indexes[target_note_id - 1]
            elif target_tag:
                for rev_idx, line_idx in enumerate(reversed(note_line_indexes), start=1):
                    line = lines[line_idx]
                    line_tag, _ = _parse_line(line)
                    if line_tag == target_tag:
                        selected_line_idx = line_idx
                        selected_note_id = len(note_line_indexes) - (rev_idx - 1)
                        break
            else:
                if note_line_indexes:
                    selected_line_idx = note_line_indexes[-1]
                    selected_note_id = len(note_line_indexes)

            if selected_line_idx is None:
                new_line = _build_line(clean, target_tag)
                if lines and lines[-1].strip() != "":
                    lines.append("")
                lines.append(new_line)
                new_text = "\n".join(lines).rstrip() + "\n"
                path.write_text(new_text, encoding="utf-8")
                new_note_id = len([ln for ln in lines if ln.strip().startswith("-")])
                return {
                    "result": {
                        "memory_file": MEMORY_FILE,
                        "memory_note_id": new_note_id,
                        "mode": "append",
                        "stored_chars": len(clean),
                        "tag": target_tag,
                        "auto_injected": True,
                    }
                }

            line_tag, line_text = _parse_line(lines[selected_line_idx])
            final_tag = target_tag or line_tag
            if mode == "append":
                merged = f"{line_text}{sep}{clean}" if line_text else clean
            else:
                merged = clean
            lines[selected_line_idx] = _build_line(merged, final_tag)
            new_text = "\n".join(lines).rstrip() + "\n"
            path.write_text(new_text, encoding="utf-8")

            return {
                "result": {
                    "memory_file": MEMORY_FILE,
                    "memory_note_id": selected_note_id,
                    "mode": mode,
                    "stored_chars": len(merged),
                    "tag": final_tag,
                    "auto_injected": True,
                }
            }

        if tool_name in ("user.update", "soul.update", "profile.update"):
            raw_content = (
                args.get("content")
                or args.get("text")
                or args.get("note")
                or args.get("memory")
            )
            if raw_content is None:
                return {
                    "status": "error",
                    "error": f"{tool_name} requires content (use content/text/note/memory).",
                }
            content = raw_content if isinstance(raw_content, str) else json.dumps(raw_content, default=str)
            content = content.strip()
            if not content:
                return {"status": "error", "error": f"{tool_name} content is empty."}

            mode = str(args.get("mode", "replace")).strip().lower() or "replace"
            if mode not in ("replace", "append"):
                return {
                    "status": "error",
                    "error": f"{tool_name} mode must be 'replace' or 'append'.",
                }

            file_arg = str(args.get("file", "")).strip().lower()
            if tool_name.startswith("user."):
                target_file = USER_FILE
            elif tool_name.startswith("soul."):
                target_file = SOUL_FILE
            else:
                if file_arg in ("user", "user.md", USER_FILE.lower()):
                    target_file = USER_FILE
                elif file_arg in ("soul", "soul.md", SOUL_FILE.lower()):
                    target_file = SOUL_FILE
                else:
                    target_file = USER_FILE

            target_id = args.get("id", None)
            target_tag = str(args.get("tag", "")).strip()
            sep = str(args.get("separator", " "))
            clean = " ".join(content.splitlines()).strip()

            path = self._profile_file_path(target_file)
            existing = self._read_profile_file(target_file).strip()
            if not existing:
                existing = f"# {Path(target_file).stem}\n"
            lines = existing.splitlines()
            note_line_indexes = [idx for idx, ln in enumerate(lines) if ln.strip().startswith("-")]

            def _build_line(note_text: str, note_tag: str) -> str:
                return f"- [{note_tag}] {note_text}" if note_tag else f"- {note_text}"

            def _parse_line(line: str) -> tuple[str, str]:
                m = re.match(r'^\s*-\s*(?:\[([^\]]+)\]\s*)?(.*)$', line)
                if not m:
                    return "", line.strip()
                return (m.group(1) or "").strip(), (m.group(2) or "").strip()

            selected_line_idx = None
            selected_note_id = None

            if target_id is not None:
                try:
                    target_note_id = int(target_id)
                except (TypeError, ValueError):
                    return {"status": "error", "error": f"Invalid profile note id: {target_id}"}
                if target_note_id < 1 or target_note_id > len(note_line_indexes):
                    return {
                        "status": "error",
                        "error": f"Profile note id {target_note_id} not found in {target_file}.",
                    }
                selected_note_id = target_note_id
                selected_line_idx = note_line_indexes[target_note_id - 1]
            elif target_tag:
                for rev_idx, line_idx in enumerate(reversed(note_line_indexes), start=1):
                    line = lines[line_idx]
                    line_tag, _ = _parse_line(line)
                    if line_tag == target_tag:
                        selected_line_idx = line_idx
                        selected_note_id = len(note_line_indexes) - (rev_idx - 1)
                        break
            else:
                if note_line_indexes:
                    selected_line_idx = note_line_indexes[-1]
                    selected_note_id = len(note_line_indexes)

            if selected_line_idx is None:
                new_line = _build_line(clean, target_tag)
                if lines and lines[-1].strip() != "":
                    lines.append("")
                lines.append(new_line)
                new_text = "\n".join(lines).rstrip() + "\n"
                path.write_text(new_text, encoding="utf-8")
                new_note_id = len([ln for ln in lines if ln.strip().startswith("-")])
                return {
                    "result": {
                        "profile_file": target_file,
                        "profile_note_id": new_note_id,
                        "mode": "append",
                        "stored_chars": len(clean),
                        "tag": target_tag,
                        "auto_injected": True,
                    }
                }

            line_tag, line_text = _parse_line(lines[selected_line_idx])
            final_tag = target_tag or line_tag
            if mode == "append":
                merged = f"{line_text}{sep}{clean}" if line_text else clean
            else:
                merged = clean
            lines[selected_line_idx] = _build_line(merged, final_tag)
            new_text = "\n".join(lines).rstrip() + "\n"
            path.write_text(new_text, encoding="utf-8")

            return {
                "result": {
                    "profile_file": target_file,
                    "profile_note_id": selected_note_id,
                    "mode": mode,
                    "stored_chars": len(merged),
                    "tag": final_tag,
                    "auto_injected": True,
                }
            }
        
        if tool_name == "buffer.extract":
            buf_id = int(args.get("id", 0))
            buf_entry = self.buffer.get(buf_id, {})
            data = buf_entry.get("data", "")
            if not data:
                return {
                    "status": "error",
                    "error": f"Buffer {buf_id} not found. Available: {list(self.buffer.keys())}",
                }
            pattern_name = str(args.get("pattern", "")).strip()
            if not pattern_name:
                return {
                    "status": "error",
                    "error": "buffer.extract requires a non-empty pattern. Use built-ins like email/url/phone/number, a regex, or use buffer.search with prompt for semantic extraction.",
                }
            PATTERNS = {
                "email": r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}',
                "url":   r'https?://[^\s<>"\'\)\]]+',
                "phone": r'[\+]?[(]?[0-9]{1,4}[)]?[\-\s./0-9]{7,15}',
                "number": r'\b\d+(?:\.\d+)?\b',
            }
            regex = PATTERNS.get(pattern_name, pattern_name)  # Built-in or custom regex
            try:
                found = list(set(re.findall(regex, data)))
            except re.error as e:
                return {"status": "error", "error": f"Invalid regex pattern: {e}"}
            return {"result": {"pattern": pattern_name, "count": len(found), "matches": found[:100]}}
        
        if tool_name == "buffer.pipe":
            buf_id = int(args.get("id", 0))
            filename = args.get("filename", "")
            buf_entry = self.buffer.get(buf_id, {})
            data = buf_entry.get("data", "")
            if not data:
                return {"status": "error", "error": f"Buffer {buf_id} not found."}
            if not filename:
                return {"status": "error", "error": "filename argument is required."}
            try:
                # SANDBOX enforcement: buffer.pipe must write inside SANDBOX only
                from pathlib import Path as _P
                _sandbox = (_P(__file__).parent.parent / "SANDBOX").resolve()
                _stripped = filename.replace("SANDBOX/", "", 1).replace("SANDBOX\\", "", 1)
                _candidate = _P(_stripped)
                _resolved = (_sandbox / _stripped).resolve() if not _candidate.is_absolute() else _candidate.resolve()
                try:
                    _resolved.relative_to(_sandbox)
                except ValueError:
                    return {"status": "error", "error": f"Security: cannot write outside SANDBOX. Got: {filename}"}
                filepath = _resolved
                filepath.parent.mkdir(parents=True, exist_ok=True)
                filepath.write_text(data, encoding='utf-8')
                return {"result": f"Wrote {len(data)} chars ({len(data.split(chr(10)))} lines) to SANDBOX/{_P(filename).name}"}
            except Exception as e:
                return {"status": "error", "error": f"Failed to write: {e}"}
        
        if tool_name == "buffer.slice":
            buf_id = int(args.get("id", 0))
            buf_entry = self.buffer.get(buf_id, {})
            data = buf_entry.get("data", "")
            if not data:
                return {"status": "error", "error": f"Buffer {buf_id} not found."}
            lines = data.split('\n')
            start = int(args.get("start", 0))
            end = int(args.get("end", 50))
            end = min(end, len(lines))  # Clamp to actual line count
            if start < 0:
                start = 0
            if end <= start:
                return {
                    "status": "error",
                    "error": f"Invalid/empty slice range start={start}, end={end} for buffer {buf_id} with {len(lines)} lines.",
                }
            sliced = '\n'.join(lines[start:end])
            if not sliced.strip():
                return {
                    "status": "error",
                    "error": f"Empty slice for buffer {buf_id} in range {start}-{end}. Adjust range.",
                }
            return {"result": f"Lines {start}-{end} of buffer {buf_id} ({len(lines)} total lines):\n{sliced}"}
        
        # --- REGULAR TOOL EXECUTION ---
        # think: inject buffer content and/or inline context into the prompt
        # before dispatching to the reasoning connector (which has no buffer access).
        if tool_name == "think":
            _base_prompt = args.get("prompt", "")
            _injections = []
            # 1) buffer=<id>  OR  buffer_id=<id>   pull from agent buffer
            _raw_buf_id = args.pop("buffer", None) or args.pop("buffer_id", None)
            if _raw_buf_id is not None:
                try:
                    _buf_id = int(_raw_buf_id)
                    _buf_entry = self.buffer.get(_buf_id, {})
                    _buf_data  = _buf_entry.get("data", "")
                    if _buf_data:
                        _injections.append(f"--- BUFFER {_buf_id} ({len(_buf_data)} chars) ---\n{_buf_data}\n--- END BUFFER ---")
                    else:
                        _injections.append(f"[Buffer {_buf_id} not found. Available: {list(self.buffer.keys())}]")
                except (ValueError, TypeError):
                    _injections.append(f"[Invalid buffer id: {_raw_buf_id}]")
            # 2) context=<inline text>  OR  data=<inline text>
            _inline = args.pop("context", None) or args.pop("data", None)
            if _inline:
                _injections.append(f"--- CONTEXT ---\n{_inline}\n--- END CONTEXT ---")
            if _injections:
                args["prompt"] = _base_prompt + "\n\n" + "\n\n".join(_injections)
        # Common hallucinations for argument names
        if "markdown_to_pdf" in tool_name or "markdown_to_html" in tool_name:
            if "input_markdown" in args: args["markdown_content"] = args.pop("input_markdown")
            if "inp"   in args and "markdown_content" not in args: args["markdown_content"] = args.pop("inp")
            if "text"  in args and "markdown_content" not in args: args["markdown_content"] = args.pop("text")
            if "markdown" in args and "markdown_content" not in args: args["markdown_content"] = args.pop("markdown")
            if "content" in args and "markdown_content" not in args: args["markdown_content"] = args.pop("content")
            if "input"  in args and "markdown_content" not in args: args["markdown_content"] = args.pop("input")
            if "output" in args and "output_path" not in args: args["output_path"] = args.pop("output")
        if "html_to_pdf" in tool_name:
            if "html"    in args and "html_content" not in args: args["html_content"] = args.pop("html")
            if "content" in args and "html_content" not in args: args["html_content"] = args.pop("content")
            if "output"  in args and "output_path" not in args: args["output_path"] = args.pop("output")
        # whatsapp.status takes zero args  strip any hallucinated args (jid, to, number, ...)
        if tool_name == "whatsapp.status":
            args = {}
        # whatsapp.send pre-flight: require text payload or auto-fill from handoff payload.
        if tool_name == "whatsapp.send":
            if "body" in args and "message" not in args:
                args["message"] = args.get("body")
            if "text" in args and "message" not in args:
                args["message"] = args.get("text")
            if "content" in args and "message" not in args:
                args["message"] = args.get("content")
            _msg = args.get("message")
            if _msg is None:
                _msg = ""
            if not isinstance(_msg, str):
                _msg = json.dumps(_msg, default=str)
            _msg = _msg.strip()
            if not _msg:
                _fallback = str(getattr(self, "_handoff_payload", "") or "").strip()
                if _fallback and _fallback.lower() not in ("ok", "no result", "none", "null"):
                    args["message"] = _fallback[:1800]
                    print("[AGENT] whatsapp.send auto-filled message from prior step payload")
                else:
                    return {
                        "status": "error",
                        "error": (
                            "whatsapp.send requires non-empty message/body/text/content. "
                            "Use prior step results to compose the message text before sending."
                        ),
                    }
        # whatsapp.send_media pre-flight: verify file exists in SANDBOX.
        # Accept both SANDBOX-relative paths and absolute paths that point inside SANDBOX.
        if tool_name == "whatsapp.send_media":
            _url_arg = str(args.get("url", "")).strip()
            from pathlib import Path as _PF, PureWindowsPath as _PWP
            _sandbox_root = (_PF(__file__).parent.parent / "SANDBOX").resolve()

            # Normalize obvious absolute Windows path forms to filename first.
            # This avoids false negatives when runtime path semantics are POSIX-like.
            _candidate_rel = _url_arg.replace("SANDBOX/", "", 1).replace("SANDBOX\\", "", 1)
            if re.match(r"^[A-Za-z]:[\\/]", _url_arg):
                _candidate_rel = _PWP(_url_arg).name

            _abs = (_sandbox_root / _candidate_rel).resolve()

            # If the user provided an absolute path and it resolves under SANDBOX, trust it.
            _provided_abs_ok = False
            try:
                _provided_abs = _PF(_url_arg).resolve()
                _provided_abs_ok = str(_provided_abs).startswith(str(_sandbox_root)) and _provided_abs.exists()
            except Exception:
                _provided_abs_ok = False

            if not _abs.exists() and not _provided_abs_ok:
                return {
                    "status": "error",
                    "error": f"send_media pre-flight: '{_url_arg}' not found in SANDBOX. Create/verify the file before sending.",
                }

            # Canonicalize outbound path to SANDBOX-relative form for connector consistency.
            if _abs.exists():
                args["url"] = f"SANDBOX/{_abs.name}"

        if "pdf.create" in tool_name or "word.create" in tool_name:
            if "text" in args and "content" not in args: args["content"] = args.pop("text")

        # Guardrail: job-application tool must only run for explicit hiring/career intent.
        # Smaller models may incorrectly pick it for generic outreach tasks.
        if tool_name == "email.send_job_application":
            _task_l = str(getattr(self, "_current_task_text", "") or "").lower()
            _is_job_intent = bool(re.search(r"\b(job|career|apply|application|resume|cv|hiring|recruit)\b", _task_l))
            if not _is_job_intent:
                _to = str(args.get("email") or args.get("to") or "").strip()
                _company = str(args.get("company_name") or "").strip()
                _subject = str(args.get("subject") or "").strip() or (
                    f"Partnership outreach - {_company}" if _company else "Partnership outreach"
                )
                _body = str(args.get("body") or "").strip() or (
                    f"Hello{(' ' + _company) if _company else ''},\n\n"
                    "I came across your work and wanted to connect regarding potential collaboration."
                )
                tool_name = "email.create_draft"
                args = {"to": _to, "subject": _subject, "body": _body, "html": False}
                print("[AGENT] Guardrail: remapped email.send_job_application -> email.create_draft (non-job intent)")

        # Email recipient pre-flight: resolve "myself" and block placeholder domains.
        if tool_name in ("email.send", "email.create_draft"):
            _to_raw = str(args.get("to") or args.get("email") or "").strip()
            if "email" in args and "to" not in args:
                args["to"] = args.pop("email")
                _to_raw = str(args.get("to") or "").strip()

            _to_l = _to_raw.lower()
            _is_valid_to = bool(re.fullmatch(EMAIL_REGEX, _to_l))
            _is_placeholder = _to_l.endswith("@example.com")
            _self_aliases = {"me", "myself", "self", "my email", "my email address", "ankit"}
            _is_self_alias = _to_l in _self_aliases

            if (not _is_valid_to) or _is_placeholder or _is_self_alias:
                _resolved_email = self._get_preferred_user_email()
                if _resolved_email:
                    args["to"] = _resolved_email
                    print(f"[AGENT] Email recipient resolved to profile email: {_resolved_email}")
                else:
                    return {
                        "status": "error",
                        "error": (
                            "No trusted recipient email found for self-send. "
                            "Provide your real email address (not example.com), then store it with "
                            "user.update(content='Email: you@domain.com', mode='append', tag='contact')."
                        ),
                    }

        # Profile/memory files are managed by dedicated tools, not filesystem.*
        if tool_name in ("filesystem.write", "filesystem.append", "filesystem.read"):
            _path_arg = str(args.get("path") or args.get("file") or "").replace("\\", "/").lower()
            _is_memory = _path_arg.endswith("/memory.md") or _path_arg == "memory.md"
            _is_user = _path_arg.endswith("/user.md") or _path_arg == "user.md"
            _is_soul = _path_arg.endswith("/soul.md") or _path_arg == "soul.md"
            if _is_memory or _is_user or _is_soul:
                _advice = "Use memory.write/memory.update for MEMORY.md"
                if _is_user:
                    _advice = "Use user.write/user.update for USER.md"
                elif _is_soul:
                    _advice = "Use soul.write/soul.update for SOUL.md"
                return {
                    "status": "error",
                    "error": f"Do not use {tool_name} for profile memory files. {_advice}."
                }

        # --- JIT TOOL DOCS + ARG STRIPPING ---
        # Strip hallucinated kwargs BEFORE calling, using real param names from metadata.
        # e.g. system.time(format="iso")  system.time()   succeeds first try, no loop.
        # SKIP stripping when function accepts **kwargs (has_var_keyword=True)  those
        # functions are intentionally open-ended and should receive all args.
        try:
            from mcp_server.tools import get_tool_metadata as _gm
            _meta = _gm(tool_name)
            if _meta and not _meta.get("has_var_keyword", False):
                _known = set(_meta.get("parameters", {}).keys())
                if _known:
                    _bad = set(args) - _known
                    if _bad:
                        print(f"[JIT] Stripped hallucinated args for {tool_name}: {_bad}")
                        args = {k: v for k, v in args.items() if k in _known}
        except Exception:
            pass

        # --- Unknown tool correction helper ---
        _available_tools = set(self.tools or [])
        if tool_name not in _available_tools:
            _alias_map = {
                "whatsapp.send_message": "whatsapp.send",
                "whatsapp.message": "whatsapp.send",
                "send_whatsapp": "whatsapp.send",
                "whatsapp.send_file": "whatsapp.send_media",
                "calendar.list": "calendar.list_events",
            }
            _suggested = _alias_map.get(tool_name, "")
            if not _suggested:
                import difflib
                _close = difflib.get_close_matches(tool_name, sorted(_available_tools), n=3, cutoff=0.55)
                _suggested = _close[0] if _close else ""

            if _suggested:
                _msg = (
                    f"Tool '{tool_name}' not found. Use '{_suggested}'.\n"
                    f"Correct call format: TOOL: {_suggested} | arg=value"
                )
                try:
                    from mcp_server.tools import get_tool_metadata as _gm2
                    _meta2 = _gm2(_suggested)
                    if _meta2:
                        _ex = _meta2.get("example")
                        _params = ", ".join(_meta2.get("parameters", {}).keys())
                        if _params:
                            _msg += f"\nExpected args: {_params}"
                        if _ex:
                            _msg += f"\nExample: {_ex}"
                except Exception:
                    pass
                return {"status": "error", "error": _msg}

            return {
                "status": "error",
                "error": (
                    f"Tool '{tool_name}' not found. "
                    f"Use an exact registered tool name. "
                    f"Call format: TOOL: <valid_tool> | arg=value"
                )
            }

        raw = execute_tool(tool_name, **args)
        try:
            from mcp_server.tools import get_tool_metadata
            meta = get_tool_metadata(tool_name)
            if meta and isinstance(raw, dict) and raw.get("status") == "error":
                err = str(raw.get("error", ""))
                err_l = err.lower()
                if re.search(r"missing required field|is required|cannot be empty|required", err_l):
                    params = meta.get("parameters", {}) or {}
                    required_keys = [
                        k for k, p in params.items()
                        if isinstance(p, dict) and p.get("required")
                    ]

                    def _placeholder_for(_ptype: str):
                        t = (_ptype or "").lower()
                        if "int" in t or "float" in t or "number" in t:
                            return 0
                        if "bool" in t:
                            return False
                        if "list" in t or "array" in t:
                            return []
                        if "dict" in t or "object" in t or "map" in t:
                            return {}
                        return "<value>"

                    required_template = {
                        k: _placeholder_for((params.get(k) or {}).get("type", ""))
                        for k in required_keys
                    }
                    if tool_name == "calendar.add_event":
                        required_template = {
                            "title": "Meeting title",
                            "start": "2026-02-26 18:00",
                            "end": "2026-02-26 19:00",
                        }

                    example = meta.get("example")
                    fmt_msg = (
                        f"{err}\n"
                        f"Correct call format: TOOL: {tool_name} | args={json.dumps(required_template, ensure_ascii=False)}"
                    )
                    if required_keys:
                        fmt_msg += f"\nRequired args: {', '.join(required_keys)}"
                    if example:
                        fmt_msg += f"\nExample: {example}"
                    return {"status": "error", "error": fmt_msg}
        except Exception:
            pass  # Never let doc enrichment break the actual tool result
        return raw

    def _extract_tool_call(self, s: str):
        """
        Extract a tool call JSON from the LLM's output.
        Returns (tool_name, tool_args, pre_text) or None if no valid tool call.
        
        Uses brace-matching instead of brute-force O(n) scan.
        Handles: markdown code fences, nested braces, string escapes.
        """
        import re
        
        s = s.strip()
        
        # Strip markdown code fences if present (```json ... ```)
        cleaned = re.sub(r'```(?:json)?\s*', '', s)
        cleaned = cleaned.replace('```', '')
        
        # Find all top-level JSON objects using brace matching
        candidates = []
        i = 0
        while i < len(cleaned):
            if cleaned[i] == '{':
                # Track brace-matching from this position
                depth = 0
                in_string = False
                escape_next = False
                start = i
                
                for j in range(i, len(cleaned)):
                    ch = cleaned[j]
                    
                    if escape_next:
                        escape_next = False
                        continue
                    
                    if ch == '\\' and in_string:
                        escape_next = True
                        continue
                    
                    if ch == '"' and not escape_next:
                        in_string = not in_string
                        continue
                    
                    if in_string:
                        continue
                    
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            candidate = cleaned[start:j + 1]
                            try:
                                data = json.loads(candidate)
                                if isinstance(data, dict) and "tool" in data:
                                    pre_text = s[:s.find('{')].strip()
                                    return data["tool"], data.get("args", {}), pre_text
                                candidates.append(data)
                            except json.JSONDecodeError:
                                pass
                            break
                i += 1
            else:
                i += 1
        
        # Fallback: try regex for {"tool": "...", "args": {...}}
        pattern = r'\{\s*"tool"\s*:\s*"([^"]+)"\s*,\s*"args"\s*:\s*(\{[^}]*\})\s*\}'
        match = re.search(pattern, s)
        if match:
            tool_name = match.group(1)
            try:
                args = json.loads(match.group(2))
            except json.JSONDecodeError:
                args = {}
            pre_text = s[:match.start()].strip()
            return tool_name, args, pre_text
        
        return None







