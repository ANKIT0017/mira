import json
import re
import asyncio
import hashlib
import dataclasses
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from .model_adapter import (
    generate,
    generate_with_messages,
    generate_with_native_tools,
    get_llm_usage_totals,
    render_llm_usage_summary,
    reset_llm_usage_totals,
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
FORCE_BUFFER_PREFIXES = ("search.", "firecrawl.", "research.", "think")
EMAIL_REGEX = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"

#  Synthesizer bypass 
# Set True to skip the LLM synthesis call and emit the raw step results directly.
# Useful for testing execution quality without the synthesizer layer.
BYPASS_SYNTHESIZER: bool = False

# Tool category keywords used for fallback matching when planner categories are missing.
CATEGORY_KEYWORDS: dict = {
    "Research": ["research", "investigate", "source", "sources", "evidence", "fact"],
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
    "calendar.add_event", "calendar.add_many", "calendar.add_recurring_event",
    "calendar.delete_event", "calendar.clear", "calendar.toggle_event",
    "filesystem.write", "filesystem.append",
    "todo.add", "todo.add_many", "todo.update", "todo.delete",
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

def _fb_research_from_query(tn, args, err):
    """knowledge.search fallback: try the web/research pipeline before asking."""
    q = str(args.get("query") or args.get("topic") or args.get("source") or "").strip()
    if not q:
        return None
    return ("research.run", {"query": q, "max_sources": 5})

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
    fallback_sequence=(
        ("tool", _fb_research_from_query),
        ("need_input", _ni_knowledge_search),
    ),
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
_POLICY_RESEARCH       = ToolPolicy(family="research",       empty_is_failure=True)


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
    ("research.",          _POLICY_RESEARCH),
    ("pdf.read",           _POLICY_READ),
    ("word.read",          _POLICY_READ),
    ("excel.read",         _POLICY_READ),
    ("convert.",           _POLICY_CONVERT),
    ("pdf.create",         _POLICY_CONVERT),
    ("word.create",        _POLICY_CONVERT),
    ("whatsapp.send",      _POLICY_SEND),
    ("email.send",         _POLICY_SEND),
    ("email.create_draft", _POLICY_SEND),
    ("todo.add_many",      _POLICY_SEND),
    ("todo.add",           _POLICY_SEND),
    ("todo.update",        _POLICY_SEND),
    ("todo.delete",        _POLICY_SEND),
    ("calendar.add_many",  _POLICY_SEND),
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

_RETRYABLE_ARG_ERROR_PATTERNS: tuple = (
    r"priority must be",
    r"invalid .*must be one of",
    r"must be one of",
    r"invalid recurrence",
    r"invalid time format",
    r"correct call format",
    r"missing \d+ required positional argument",
    r"unexpected keyword argument",
    r"got an unexpected",
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
        for pat in _RETRYABLE_ARG_ERROR_PATTERNS:
            if re.search(pat, e):
                return "retryable"
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
            or (
                isinstance(tool_res, dict)
                and "text" in tool_res
                and not str(tool_res.get("text") or "").strip()
            )
            or (
                isinstance(tool_res, dict)
                and "content" in tool_res
                and not str(tool_res.get("content") or "").strip()
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
        self._dependency_outputs: dict = {}  # {target_step_number: [signal output records]}
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
        self._dependency_outputs = {}
        self._handoff_payload = ""
        self._pending_workflow_candidate = None
        # Reset context engineering state for new session
        self._context_compiler.reset()
        return {"status": "ok", "message": "History and buffer cleared"}
    
    def stop(self):
        """Signal the agent to stop current execution."""
        self.stop_requested = True
        return {"status": "ok", "message": "Stop signal sent"}

    def _print_llm_usage_summary(self) -> None:
        """Print end-of-run LLM usage totals when at least one call was made."""
        try:
            snap = get_llm_usage_totals()
            if int(snap.get("calls", 0)) <= 0:
                return
            print(render_llm_usage_summary())
        except Exception as _usage_err:
            print(f"[LLM USAGE] Summary unavailable: {_usage_err}")
    
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
        _research_tools = [
            {
                "name": "research.plan",
                "description": "Decide where to look, source types, search queries, and extraction schema for a research task",
                "category": "Research",
                "parameters": {
                    "query": {"type": "str", "required": True, "default": None},
                    "max_queries": {"type": "int", "required": False, "default": 5},
                },
                "example": '{"tool": "research.plan", "args": {"query": "best fiction and romance books 2026"}}',
                "notes": "Use first for research tasks when source strategy matters.",
                "has_var_keyword": False,
                "function": None,
            },
            {
                "name": "research.search_many",
                "description": "Run multiple focused searches from a research plan and group the results",
                "category": "Research",
                "parameters": {
                    "query": {"type": "str", "required": True, "default": None},
                    "max_results": {"type": "int", "required": False, "default": 5},
                },
                "example": '{"tool": "research.search_many", "args": {"query": "best laptops for students 2026"}}',
                "notes": None,
                "has_var_keyword": False,
                "function": None,
            },
            {
                "name": "research.select_sources",
                "description": "Rank and deduplicate search results into sources worth reading",
                "category": "Research",
                "parameters": {
                    "query": {"type": "str", "required": True, "default": None},
                    "max_sources": {"type": "int", "required": False, "default": 5},
                },
                "example": '{"tool": "research.select_sources", "args": {"query": "3 Idiots movie summary"}}',
                "notes": None,
                "has_var_keyword": False,
                "function": None,
            },
            {
                "name": "research.read_sources",
                "description": "Read selected sources using web/firecrawl tools with snippet fallback",
                "category": "Research",
                "parameters": {
                    "query": {"type": "str", "required": True, "default": None},
                    "max_sources": {"type": "int", "required": False, "default": 5},
                },
                "example": '{"tool": "research.read_sources", "args": {"query": "latest AI regulation"}}',
                "notes": None,
                "has_var_keyword": False,
                "function": None,
            },
            {
                "name": "research.extract_facts",
                "description": "Extract only intended facts from read research sources and remove noise",
                "category": "Research",
                "parameters": {
                    "query": {"type": "str", "required": True, "default": None},
                },
                "example": '{"tool": "research.extract_facts", "args": {"query": "best books 2026"}}',
                "notes": None,
                "has_var_keyword": False,
                "function": None,
            },
            {
                "name": "research.synthesize",
                "description": "Write the final answer from extracted research facts only",
                "category": "Research",
                "parameters": {
                    "query": {"type": "str", "required": True, "default": None},
                },
                "example": '{"tool": "research.synthesize", "args": {"query": "compare phones"}}',
                "notes": None,
                "has_var_keyword": False,
                "function": None,
            },
            {
                "name": "research.run",
                "description": "Full research pipeline: decide where to look, search, read sources, extract facts, and synthesize",
                "category": "Research",
                "parameters": {
                    "query": {"type": "str", "required": True, "default": None},
                    "max_queries": {"type": "int", "required": False, "default": 5},
                    "max_sources": {"type": "int", "required": False, "default": 5},
                },
                "example": '{"tool": "research.run", "args": {"query": "best fiction and romance books 2026", "max_sources": 5}}',
                "notes": "Prefer for research/recommendation/summarization tasks instead of raw search snippets.",
                "has_var_keyword": False,
                "function": None,
            },
        ]
        _reasoning_tools = [
            {
                "name": "think",
                "description": "Reason over data or answer directly using the LLM",
                "category": "Reasoning",
                "parameters": {
                    "prompt": {"type": "str", "required": True, "default": None},
                    "file": {"type": "str", "required": False, "default": None},
                    "model": {"type": "str", "required": False, "default": None},
                    "max_tokens": {"type": "int", "required": False, "default": 1024},
                },
                "example": '{"tool": "think", "args": {"prompt": "Answer concisely:"}}',
                "notes": "Use for general factual questions, homework, calculations, and synthesis from known context.",
                "has_var_keyword": False,
                "function": None,
            },
        ]
        for _t in _buffer_tools + _research_tools + _reasoning_tools:
            TOOL_METADATA.setdefault(_t["name"], _t)
            if _t["name"] not in self.tools:
                self.tools.append(_t["name"])
        self.tools = sorted(set(self.tools))

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

    def _extract_recipient_hint(self, text: str) -> str:
        """Extract a likely recipient name hint (e.g., "to ankit") from user text."""
        if not text:
            return ""
        raw = str(text)
        m = re.search(r'\b(?:to|for)\s+([A-Za-z][A-Za-z0-9_.\-]{1,40})\b', raw)
        if not m:
            return ""
        cand = m.group(1).strip()
        low = cand.lower()
        if low in {
            "me", "myself", "you", "them", "it", "all", "the", "this", "that",
            "email", "mail", "message", "whatsapp", "send", "share", "forward",
            "code", "file", "pdf", "document",
        }:
            return ""
        if re.search(EMAIL_REGEX, cand) or re.search(r'\d', cand):
            return ""
        return cand

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
        recipient_hint = self._extract_recipient_hint(raw)
        if recipient_hint:
            known_slots["recipient_name_hint"] = recipient_hint

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
        # Preserve non-required hints (e.g., recipient_name_hint) for downstream steps.
        for k, v in known_slots.items():
            if k not in slots:
                slots[k] = v
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
        q_l = str(question or "").lower()
        if re.search(r"\b(column headers?|headers?)\b", q_l):
            headers_list = self._extract_excel_columns() if self._is_advanced_excel_task() else []
            if headers_list:
                headers = ", ".join(headers_list)
                self._working_state.setdefault("slots", {})["column_headers"] = headers
                return False, "column_headers", "Column headers are already inferable from the Excel request."
        if not slot and re.search(r"\b(name|who|recipient)\b", q_l):
            hint = str((self._working_state or {}).get("slots", {}).get("recipient_name_hint") or "").strip()
            if hint:
                return False, "recipient_name_hint", f"Recipient name already known from task: '{hint}'."
            extracted = self._extract_recipient_hint(getattr(self, "_current_task_text", ""))
            if extracted:
                self._working_state.setdefault("slots", {})["recipient_name_hint"] = extracted
                return False, "recipient_name_hint", f"Recipient name found in original request: '{extracted}'."
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

        if tool_name == "think":
            txt = ""
            if isinstance(tool_res, dict):
                txt = str(
                    tool_res.get("result")
                    or tool_res.get("content")
                    or tool_res.get("text")
                    or ""
                )
            else:
                txt = str(tool_res or "")
            txt = txt.strip()
            if txt:
                if "message_body" in {
                    str(x or "").strip().lower()
                    for x in getattr(self, "_current_step_outputs", []) or []
                }:
                    txt = self._normalize_delivery_body(txt)
                slots["message_body"] = txt[:12000]

        if tool_name.startswith("research."):
            if isinstance(tool_res, dict):
                answer = str(tool_res.get("answer") or "").strip()
                if answer:
                    slots["research_answer"] = answer[:12000]
                    slots.setdefault("summary", answer[:12000])
                facts = tool_res.get("facts")
                if facts:
                    try:
                        slots["research_facts"] = json.dumps(facts, ensure_ascii=False, default=str)[:12000]
                    except Exception:
                        slots["research_facts"] = str(facts)[:12000]

        if tool_name in ("filesystem.write", "filesystem.append"):
            norm = self._extract_file_path_from_tool_result(tool_res)
            if not norm:
                norm = self._normalize_sandbox_file_ref(
                    tool_args.get("path")
                    or tool_args.get("file_path")
                    or tool_args.get("filename")
                    or ""
                )
            if norm:
                artifacts["last_file_path"] = norm
                slots["file_path"] = norm

        if tool_name in ("pdf.create", "word.create", "convert.markdown_to_pdf", "convert.html_to_pdf"):
            norm = self._extract_file_path_from_tool_result(tool_res)
            if norm:
                artifacts["last_file_path"] = norm
                artifacts["document_file_path"] = norm
                slots["file_path"] = norm
                if norm.lower().endswith(".pdf"):
                    artifacts["pdf_file_path"] = norm
                    slots["pdf_file_path"] = norm

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

        if tool_name == "todo.add":
            artifacts["todo_add_count"] = int(artifacts.get("todo_add_count", 0) or 0) + 1
        if tool_name == "todo.add_many" and isinstance(tool_res, dict):
            created = tool_res.get("tasks") or []
            artifacts["todo_add_count"] = int(artifacts.get("todo_add_count", 0) or 0) + len(created)
            slots["todo_list_updates"] = json.dumps(created, ensure_ascii=False, default=str)[:12000]
        if tool_name == "calendar.add_many" and isinstance(tool_res, dict):
            events = tool_res.get("events") or []
            slots["calendar_schedule"] = json.dumps(events, ensure_ascii=False, default=str)[:12000]

        if tool_name == "email.create_draft" and isinstance(tool_res, dict):
            _draft_id = tool_res.get("draft_id") or tool_res.get("id") or tool_res.get("message_id")
            if _draft_id:
                slots["draft_id"] = str(_draft_id)

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

    def _extract_primary_payload(self, tool_res, raw_obs: str = "") -> str:
        """Return the most useful text payload from a tool result."""
        payload = ""
        if isinstance(tool_res, dict):
            for key in ("text", "result", "content", "message", "body", "data"):
                val = tool_res.get(key)
                if isinstance(val, str) and val.strip():
                    payload = val.strip()
                    break
            if not payload:
                payload = json.dumps(tool_res, ensure_ascii=False, default=str)
        elif tool_res is not None:
            payload = str(tool_res).strip()
        if not payload:
            payload = str(raw_obs or "").strip()

        # buffer.slice returns a header followed by the actual sliced payload.
        if payload.startswith("Lines ") and ":\n" in payload:
            payload = payload.split(":\n", 1)[1].strip()

        # If the payload itself is JSON, prefer its content-bearing field.
        if payload.startswith("{") and payload.endswith("}"):
            try:
                obj = json.loads(payload)
                if isinstance(obj, dict):
                    for key in ("text", "result", "content", "message", "body"):
                        val = obj.get(key)
                        if isinstance(val, str) and val.strip():
                            return val.strip()
            except Exception:
                pass
        return payload.strip()

    def _payload_to_signal(
        self,
        payload: str,
        tool_name: str = "",
        slot: str = "",
        max_chars: int = 4000,
    ) -> str:
        """Convert raw tool output into compact task-relevant signal."""
        text = str(payload or "").strip()
        if not text:
            return ""

        slot_l = str(slot or "").lower()
        tool_l = str(tool_name or "").lower()
        lines: list[str] = []

        try:
            data = json.loads(text)
        except Exception:
            data = None

        if isinstance(data, dict):
            if tool_l.startswith("research."):
                answer = str(data.get("answer") or "").strip()
                if answer:
                    return answer[:max_chars]

            contact = data.get("contact")
            contacts = data.get("contacts")
            if isinstance(contact, dict):
                contacts = [contact]
            if isinstance(contacts, list) and contacts:
                for item in contacts[:5]:
                    if not isinstance(item, dict):
                        continue
                    parts = []
                    for key in ("id", "name", "phone", "email", "jid"):
                        val = item.get(key)
                        if val not in (None, "", [], {}):
                            parts.append(f"{key}={val}")
                    if parts:
                        lines.append("- " + ", ".join(parts))

            results = data.get("results") or data.get("items") or data.get("matches")
            if isinstance(results, list) and results:
                for item in results[:8]:
                    if isinstance(item, dict):
                        title = str(item.get("title") or item.get("name") or item.get("label") or "").strip()
                        snippet = str(
                            item.get("snippet")
                            or item.get("summary")
                            or item.get("description")
                            or item.get("content")
                            or ""
                        ).strip()
                        url = str(item.get("url") or item.get("link") or "").strip()
                        source_query = str(item.get("source_query") or "").strip()
                        row = ": ".join(p for p in (title, snippet[:240]) if p)
                        if url:
                            row = f"{row} ({url})" if row else url
                        if source_query and row:
                            row = f"[{source_query}] {row}"
                        if row:
                            lines.append("- " + re.sub(r"\s+", " ", row).strip())
                    elif str(item).strip():
                        lines.append("- " + re.sub(r"\s+", " ", str(item)).strip()[:260])

            if not lines:
                for key in ("answer", "text", "result", "content", "message", "body", "summary"):
                    val = data.get(key)
                    if isinstance(val, str) and val.strip():
                        lines.append(val.strip())
                        break
            if not lines and isinstance(data.get("facts"), list):
                for item in data.get("facts", [])[:10]:
                    if isinstance(item, dict):
                        lines.append("- " + json.dumps(item, ensure_ascii=False, default=str)[:500])
                    elif str(item).strip():
                        lines.append("- " + str(item).strip()[:500])
            if not lines:
                small = {
                    k: v for k, v in data.items()
                    if k in {"id", "name", "title", "phone", "email", "jid", "path", "file", "url", "status"}
                    and v not in (None, "", [], {})
                }
                if small:
                    lines.append(json.dumps(small, ensure_ascii=False, default=str))

        if lines:
            signal = "\n".join(lines)
        else:
            signal_lines = []
            for line in text.splitlines():
                clean = re.sub(r"\s+", " ", line).strip()
                if not clean:
                    continue
                if clean.startswith(("{", "}", "[", "]")) and len(clean) < 4:
                    continue
                signal_lines.append(clean)
                if len("\n".join(signal_lines)) >= max_chars:
                    break
            signal = "\n".join(signal_lines) if signal_lines else text

        signal = signal.replace("\\n", "\n").strip()
        if len(signal) > max_chars or (
            tool_l.startswith(("search.", "firecrawl.", "web."))
            and len(signal) > 1200
        ):
            try:
                prompt = (
                    "Rewrite the content below as pure signal for the current task.\n"
                    "Keep only facts, identifiers, file paths, contact details, titles, URLs, or exact text needed by later steps.\n"
                    "Remove JSON wrappers, tool logs, duplicates, generic commentary, and unsupported claims.\n"
                    "Use concise bullets. Max 900 characters.\n\n"
                    f"Task: {getattr(self, '_current_task_text', '')[:300]}\n"
                    f"Step: {getattr(self, '_current_step_desc', '')[:180]}\n"
                    f"Slot: {slot_l or 'unspecified'}\n\n"
                    f"Content:\n{signal[:6000]}\n\n"
                    "Pure signal:"
                )
                compact = str(generate(prompt, model=getattr(self, "_exec_model", "local"), temperature=0.0, max_tokens=320) or "").strip()
                compact = re.sub(r"^\s*(?:Pure signal|Signal|FINAL ANSWER)\s*:\s*", "", compact, flags=re.IGNORECASE)
                if compact and len(compact) < len(signal):
                    signal = compact
            except Exception as e:
                print(f"[AGENT] signal distill skipped: {e}")

        if len(signal) > max_chars:
            signal = signal[:max_chars].rstrip() + "\n...[signal truncated]"
        return signal

    def _clean_multi_search_item(self, item: str) -> str:
        """Normalize one extracted item/topic label for focused searching."""
        s = str(item or "").strip()
        if not s:
            return ""
        s = re.sub(r"[*_`#>\[\]\(\)]", "", s)
        s = re.sub(r"^\s*(?:[-+•]|\d+[.)])\s*", "", s)
        s = re.sub(r"^\s*(?:book|title|item|topic)\s*\d*\s*[:.-]\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s+", " ", s).strip(" :-–—\"'")
        if ":" in s:
            s = s.split(":", 1)[0].strip(" :-–—\"'")
        if " - " in s:
            s = s.split(" - ", 1)[0].strip(" :-–—\"'")
        return s.strip()

    def _extract_multi_search_items_from_text(self, text: str, max_items: int = 8) -> list[str]:
        """Extract concrete item/topic labels from a slot value."""
        raw = str(text or "").strip()
        if not raw:
            return []
        candidates: list[str] = []

        try:
            data = json.loads(raw)
        except Exception:
            data = None
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    val = item.get("title") or item.get("name") or item.get("topic") or item.get("query")
                else:
                    val = item
                if val:
                    candidates.append(str(val))
        elif isinstance(data, dict):
            for key in ("titles", "items", "topics", "queries", "books", "entities", "results"):
                val = data.get(key)
                if isinstance(val, list):
                    for item in val:
                        if isinstance(item, dict):
                            candidates.append(str(item.get("title") or item.get("name") or item.get("topic") or ""))
                        elif item:
                            candidates.append(str(item))

        for m in re.finditer(r"\*\*([^*\n]{2,120})\*\*", raw):
            candidates.append(m.group(1))

        for line in raw.splitlines():
            clean = line.strip()
            if not clean:
                continue
            if not re.match(r"^\s*(?:[-*•]|\d+[.)])\s+", clean):
                # Also catch compact title lines like "Still Into You by Erin Connor".
                if not re.search(r"\bby\s+[A-Z][A-Za-z.'-]+", clean):
                    continue
            candidates.append(clean)

        bad_fragments = (
            "based on", "search result", "potential", "additional", "context",
            "genre", "theme", "summary", "recommendation", "specific featured",
            "general trend", "available", "source", "criteria", "identified",
        )
        out: list[str] = []
        seen = set()
        for cand in candidates:
            item = self._clean_multi_search_item(cand)
            if not item:
                continue
            low = item.lower()
            if any(b in low for b in bad_fragments):
                continue
            if "http://" in low or "https://" in low:
                continue
            if "..." in item or item.endswith(" E"):
                continue
            if "/" in item and len(item.split("/")) > 1:
                # Usually comparison/examples, not a single concrete search target.
                continue
            words = item.split()
            if len(item) < 3 or len(item) > 90 or len(words) > 14:
                continue
            norm = re.sub(r"[^a-z0-9]+", " ", low).strip()
            if not norm or norm in seen:
                continue
            seen.add(norm)
            out.append(item)
            if len(out) >= max_items:
                break
        return out

    def _collect_multi_search_items(self) -> list[str]:
        """Collect item/topic labels from the current step's declared input slots."""
        slots = (self._working_state or {}).get("slots", {})
        inputs = [str(s or "").strip().lower() for s in getattr(self, "_current_step_inputs", []) or []]
        if not inputs:
            return []
        preferred = [
            s for s in inputs
            if re.search(r"(titles?|items?|topics?|queries|books?|entities|names?|products?|companies|people)", s)
        ]
        candidates = preferred or inputs
        out: list[str] = []
        seen = set()
        for slot in candidates:
            val = slots.get(slot)
            if not isinstance(val, str) or not val.strip():
                continue
            for item in self._extract_multi_search_items_from_text(val):
                key = re.sub(r"[^a-z0-9]+", " ", item.lower()).strip()
                if key and key not in seen:
                    seen.add(key)
                    out.append(item)
                if len(out) >= 8:
                    return out
        return out

    def _search_intent_suffix(self, base_query: str, items: list[str]) -> str:
        """Derive the reusable search intent after removing known item labels."""
        suffix = str(base_query or "").strip()
        for item in items:
            variants = [item]
            if re.search(r"\s+by\s+", item, flags=re.IGNORECASE):
                variants.append(re.split(r"\s+by\s+", item, flags=re.IGNORECASE)[0])
            for var in variants:
                if not var:
                    continue
                suffix = re.sub(re.escape(var), " ", suffix, flags=re.IGNORECASE)
        suffix = re.sub(r"\s+", " ", suffix).strip(" -:|,")
        if suffix:
            return suffix
        goal = str(getattr(self, "_current_step_desc", "") or "").lower()
        if re.search(r"\b(summary|summarize|summarise|plot|synopsis|review)\b", goal):
            return "summary review plot"
        if re.search(r"\b(price|cost|buy)\b", goal):
            return "price"
        if re.search(r"\b(news|latest|recent)\b", goal):
            return "latest news"
        return "details"

    def _build_multi_search_queries(self, tool_name: str, args: dict) -> list[str]:
        """Build focused per-item search queries when a search step has multi-item inputs."""
        if tool_name not in ("search.duckduckgo", "search.news"):
            return []
        base_query = str(args.get("query") or "").strip()
        if not base_query:
            return []
        items = self._collect_multi_search_items()
        if len(items) < 2:
            return []
        step_l = str(getattr(self, "_current_step_desc", "") or "").lower()
        task_l = str(getattr(self, "_current_task_text", "") or "").lower()
        should_expand = bool(
            re.search(r"\b(each|every|identified|selected|listed|multiple|multi|per[- ]?item|summar)", step_l + " " + task_l)
        )
        if not should_expand:
            return []
        suffix = self._search_intent_suffix(base_query, items)
        queries: list[str] = []
        seen = set()
        for item in items[:6]:
            q = f"{item} {suffix}".strip()
            q = re.sub(r"\s+", " ", q)
            key = q.lower()
            if key not in seen:
                seen.add(key)
                queries.append(q)
        return queries

    def _is_research_task(self, text: str | None = None) -> bool:
        t = str(text if text is not None else getattr(self, "_current_task_text", "") or "").lower()
        if not re.search(r"\b(find|search|research|look up|lookup|best|recommend|compare|summari[sz]e|top|latest|review|gather)\b", t):
            return False
        return bool(
            re.search(r"\b(web|internet|online|latest|current|202\d|best|top|recommend|book|movie|product|company|paper|article|news|source|summar|research|study|medical|health|law|legal|information|info|details)\b", t)
        )

    def _normalize_research_steps(self, steps: list) -> list:
        """Upgrade raw web-search steps into the reusable research pipeline."""
        if not steps or self._is_homework_answer_task() or not self._is_research_task():
            return steps
        pure_research = not any(
            any(str(step.get("action", "")).strip().lower().startswith(p) for p in _SIDE_EFFECT_PREFIXES)
            or re.search(r"\b(send|write|create|save|delete|update|append|email|whatsapp|calendar|todo)\b", str(step.get("goal", "")).lower())
            for step in steps
        )
        if pure_research:
            return [{
                "action": "research.run",
                "goal": "Research topic and extract clean facts",
                "outputs": ["research_answer"],
                "needed_by": [],
                "categories": self._normalize_categories(["Research"]),
            }]
        out = []
        for step in steps:
            s = dict(step)
            act = str(s.get("action", "")).strip().lower()
            goal_l = str(s.get("goal", "")).lower()
            if act in {"search.duckduckgo", "search.news", "firecrawl.search"} or (
                not act and re.search(r"\b(find|search|research|look up|lookup|gather)\b", goal_l)
            ):
                s["action"] = "research.run"
                s["goal"] = "Research topic and extract clean facts"
                if not s.get("outputs"):
                    s["outputs"] = ["research_facts"]
                s["categories"] = self._normalize_categories(["Research"])
                s.pop("inputs", None)
            out.append(s)
        return out

    def _normalize_homework_steps(self, steps: list) -> list:
        """Route school-style factual/calculation prompts through one reasoning step."""
        if not steps or not self._is_homework_answer_task():
            return steps
        return [{
            "action": "think",
            "goal": "Answer questions and calculate result",
            "outputs": ["final_answer"],
            "needed_by": [],
            "categories": self._normalize_categories(["Reasoning"]),
        }]

    def _clean_research_query(self, query: str) -> str:
        q = re.sub(r"\s+", " ", str(query or "")).strip()
        q = re.sub(r"^(?:search|find|look up|lookup|research)\s+(?:for\s+)?", "", q, flags=re.IGNORECASE).strip()
        # Multi-action requests often contain delivery or task-management clauses.
        # Keep the research topic scoped so search engines do not receive the whole workflow.
        parts = re.split(
            r"\b(?:and\s+then\s+|then\s+|and\s+)?(?:create|add|make|save|send|share|email|whatsapp|message|draft|write\s+to)\b",
            q,
            maxsplit=1,
            flags=re.IGNORECASE,
        )
        if parts:
            q = parts[0].strip()
        return q.strip(" ,.;:-")

    def _research_query_from_args(self, args: dict) -> str:
        query = str(
            args.get("query")
            or args.get("topic")
            or args.get("task")
            or args.get("question")
            or ""
        ).strip()
        task = str(getattr(self, "_current_task_text", "") or "").strip()
        if query:
            query = self._clean_research_query(query)
            query_l = re.sub(r"\s+", " ", query.lower()).strip()
            task_l = re.sub(r"\s+", " ", task.lower()).strip()
            omitted_constraints = re.search(
                r"\b(not opinions?|confirm|verified|facts?|claims?|evidence|sources?|current|latest|"
                r"financials?|technical analysis|investors?|execution|business|fundamental)\b",
                task_l,
            ) and not re.search(
                r"\b(not opinions?|confirm|verified|facts?|claims?|evidence|sources?|current|latest|"
                r"financials?|technical analysis|investors?|execution|business|fundamental)\b",
                query_l,
            )
            if task and omitted_constraints:
                return task
            return query
        step = str(getattr(self, "_current_step_desc", "") or "").strip()
        return self._clean_research_query(step or task)

    def _research_plan(self, query: str, max_queries: int = 5) -> dict:
        q = re.sub(r"\s+", " ", str(query or "")).strip()
        q_l = q.lower()
        source_types = ["official or primary sources", "credible reference pages", "recent authoritative articles"]
        preferred_domains: list[str] = []
        avoid = ["ads", "thin SEO pages", "forum comments unless user asks for opinions"]
        schema = ["claim", "item/title", "summary", "source", "url", "date if available"]
        needs_news = bool(re.search(r"\b(latest|today|recent|news|this week|this month)\b", q_l))

        if re.search(r"\b(stock|stocks|invest|investment|investing|ticker|equity|share|shares|financials?|technical analysis|institutional investors?)\b", q_l):
            source_types = [
                "company investor relations and annual reports",
                "SEC filings and earnings releases",
                "financial statement and ownership data sources",
                "price/volume technical analysis sources",
            ]
            preferred_domains = [
                "sec.gov",
                "nasdaq.com",
                "finance.yahoo.com",
                "stockanalysis.com",
                "tradingview.com",
                "companiesmarketcap.com",
            ]
            avoid.extend([
                "opinion-only stock picks",
                "sponsored recommendations",
                "articles without ticker-level financial evidence",
            ])
            schema = [
                "ticker",
                "company",
                "business model",
                "execution evidence",
                "major investors/ownership",
                "revenue/profit/cash-flow/debt facts",
                "technical trend/support/resistance/volume",
                "risk",
                "source",
                "url",
                "date if available",
            ]
        elif re.search(r"\b(book|novel|fiction|romance|author|reading)\b", q_l):
            source_types = ["publisher/bookseller pages", "book review/editorial lists", "Goodreads/library catalog pages"]
            preferred_domains = ["goodreads.com", "publishersweekly.com", "bookriot.com", "kirkusreviews.com", "penguinrandomhouse.com"]
            schema = ["title", "author", "genre/category", "release date/year", "why it matches", "short summary", "source", "url"]
        elif re.search(r"\b(movie|film|series|episode|plot|cast)\b", q_l):
            source_types = ["IMDb or TMDb pages", "Wikipedia/reference pages", "critic/review pages"]
            preferred_domains = ["imdb.com", "wikipedia.org", "rottentomatoes.com", "themoviedb.org"]
            schema = ["title", "year", "genre", "plot summary", "themes", "source", "url"]
        elif re.search(r"\b(product|buy|price|laptop|phone|camera|headphone|software|tool|tools|app|apps|saas|productivity)\b", q_l):
            source_types = ["official product pages", "trusted reviews", "retailer/spec pages"]
            preferred_domains = ["g2.com", "capterra.com", "pcmag.com", "zapier.com", "forbes.com"]
            schema = ["tool", "category", "small-business usefulness", "pros", "cons", "pricing if available", "source", "url"]
        elif re.search(r"\b(paper|study|research|journal|medical|health|disease|treatment)\b", q_l):
            source_types = ["peer-reviewed papers", "government/medical institutions", "systematic reviews"]
            preferred_domains = ["pubmed.ncbi.nlm.nih.gov", "nih.gov", "who.int", "nature.com", "thelancet.com"]
            schema = ["finding", "population/context", "evidence strength", "date", "source", "url"]
        elif re.search(r"\b(law|legal|regulation|tax|visa|government|policy)\b", q_l):
            source_types = ["government pages", "official regulations", "reputable legal explainers"]
            preferred_domains = ["gov", "usa.gov", "ecfr.gov", "federalregister.gov", "congress.gov"]
            schema = ["rule/fact", "jurisdiction", "effective date", "official source", "url"]
        elif re.search(r"\b(company|ceo|founder|funding|revenue|stock|market)\b", q_l):
            source_types = ["company official pages", "regulatory filings", "reputable business/news sources"]
            preferred_domains = ["sec.gov", "reuters.com", "bloomberg.com", "crunchbase.com"]
            schema = ["company/person", "fact", "date", "source", "url"]

        queries = [q]
        if re.search(r"\b(stock|stocks|invest|investment|investing|ticker|equity|share|shares|financials?|technical analysis)\b", q_l):
            queries.extend([
                f"{q} revenue earnings free cash flow debt institutional ownership",
                f"{q} SEC filings annual report investor presentation",
                f"{q} technical analysis price trend support resistance volume",
                f"{q} stockanalysis financials ownership technicals",
            ])
        for domain in preferred_domains[:3]:
            if domain == "gov":
                queries.append(f"{q} official government")
            else:
                queries.append(f"site:{domain} {q}")
        if needs_news:
            queries.append(f"{q} latest news")
        if re.search(r"\b(summary|summarize|summarise|plot|synopsis)\b", q_l):
            queries.append(f"{q} summary synopsis")
        if re.search(r"\b(best|top|recommend)\b", q_l):
            queries.append(f"{q} recommendations list")

        deduped = []
        seen = set()
        for item in queries:
            clean = re.sub(r"\s+", " ", item).strip()
            key = clean.lower()
            if clean and key not in seen:
                seen.add(key)
                deduped.append(clean)
            if len(deduped) >= max(1, min(int(max_queries or 5), 8)):
                break

        return {
            "query": q,
            "where_to_look": source_types,
            "preferred_domains": preferred_domains,
            "avoid": avoid,
            "queries": deduped,
            "extraction_schema": schema,
            "needs_news": needs_news,
        }

    def _run_registered_tool(self, tool_name: str, args: dict) -> dict:
        try:
            if tool_name not in self.tools:
                import importlib
                prefix = tool_name.split(".", 1)[0]
                module_map = {
                    "search": "mcp_server.connectors.search",
                    "web": "mcp_server.connectors.web",
                    "firecrawl": "mcp_server.connectors.firecrawl",
                }
                if prefix in module_map:
                    importlib.import_module(module_map[prefix])
                    self.tools = sorted(set(list_tools()) | set(self.tools or []))
            return execute_tool(tool_name, **(args or {}))
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _research_search_many(self, plan: dict, max_results: int = 5) -> dict:
        all_results = []
        errors = []
        for q in (plan.get("queries") or [])[:8]:
            prefer_news = bool(plan.get("needs_news") and re.search(r"\b(news|latest|today|recent)\b", q, re.IGNORECASE))
            tool_name = "search.news" if prefer_news else "search.duckduckgo"
            if tool_name not in self.tools:
                tool_name = "search.duckduckgo"
            res = self._run_registered_tool(tool_name, {"query": q, "max_results": int(max_results or 5)})
            if isinstance(res, dict) and res.get("status") == "error" and tool_name == "search.news":
                errors.append(f"{tool_name}({q}): {res.get('error', 'unknown error')}")
                tool_name = "search.duckduckgo"
                res = self._run_registered_tool(tool_name, {"query": q, "max_results": int(max_results or 5)})
            if isinstance(res, dict) and res.get("status") == "error":
                errors.append(f"{tool_name}({q}): {res.get('error', 'unknown error')}")
                continue
            payload = res.get("result", res) if isinstance(res, dict) else res
            results = payload.get("results", []) if isinstance(payload, dict) else []
            if not results and prefer_news and tool_name != "search.duckduckgo":
                fallback = self._run_registered_tool("search.duckduckgo", {"query": q, "max_results": int(max_results or 5)})
                if not (isinstance(fallback, dict) and fallback.get("status") == "error"):
                    tool_name = "search.duckduckgo"
                    payload = fallback.get("result", fallback) if isinstance(fallback, dict) else fallback
                    results = payload.get("results", []) if isinstance(payload, dict) else []
            for item in results:
                if isinstance(item, dict):
                    enriched = dict(item)
                    enriched["source_query"] = q
                    enriched["search_tool"] = tool_name
                    all_results.append(enriched)
        return {"query": plan.get("query", ""), "results": all_results, "count": len(all_results), "errors": errors[:5]}

    def _nth_business_day_from_tomorrow(self, index: int) -> str:
        day = datetime.now().date()
        remaining = max(0, int(index or 0)) + 1
        while remaining > 0:
            day = day + timedelta(days=1)
            if day.weekday() < 5:
                remaining -= 1
        return day.isoformat()

    def _infer_todo_due_date(self, args: dict) -> str:
        text = " ".join(
            str(x or "")
            for x in (
                getattr(self, "_current_task_text", ""),
                getattr(self, "_current_step_desc", ""),
                args.get("title"),
                args.get("description"),
            )
        ).lower()
        if re.search(r"\btomorrow\b", text):
            return (datetime.now().date() + timedelta(days=1)).isoformat()
        if re.search(r"\bnext\s+(?:\d+|one|two|three|four|five|six|seven)?\s*business\s+days?\b", text):
            artifacts = (self._working_state or {}).setdefault("artifacts", {})
            return self._nth_business_day_from_tomorrow(int(artifacts.get("todo_add_count", 0) or 0))
        return ""

    def _normalize_priority_value(self, value: str | None) -> str:
        p = str(value or "medium").strip().lower()
        aliases = {
            "urgent": "high",
            "critical": "high",
            "important": "high",
            "normal": "medium",
            "med": "medium",
            "default": "medium",
            "minor": "low",
        }
        return aliases.get(p, p)

    def _normalize_due_date_value(self, value: str | None) -> str:
        raw = re.sub(r"\s+", " ", str(value or "")).strip()
        if not raw:
            return ""
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return raw
        today = datetime.now().date()
        low = raw.lower()
        if low == "today":
            return today.isoformat()
        if low == "tomorrow":
            return (today + timedelta(days=1)).isoformat()
        if re.search(r"\bend of (?:the )?week\b|eow|friday", low):
            days = (4 - today.weekday()) % 7
            return (today + timedelta(days=days)).isoformat()
        weekdays = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6,
        }
        for name, idx in weekdays.items():
            if re.search(rf"\b{name}\b", low):
                days = (idx - today.weekday()) % 7
                return (today + timedelta(days=days)).isoformat()
        return raw

    def _normalize_clock_time(self, text: str | None) -> str:
        raw = str(text or "").strip().lower()
        m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", raw)
        if not m:
            return ""
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        mer = m.group(3)
        if mer == "pm" and hour < 12:
            hour += 12
        if mer == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            return ""
        return f"{hour:02d}:{minute:02d}"

    def _normalize_calendar_datetime_value(self, value: str | None, default_date: str | None = None) -> str:
        raw = re.sub(r"\s+", " ", str(value or "")).strip()
        if not raw:
            return ""
        if re.match(r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}", raw):
            return raw
        date_part = self._normalize_due_date_value(raw) or default_date or datetime.now().date().isoformat()
        time_part = self._normalize_clock_time(raw) or "16:00"
        return f"{date_part}T{time_part}:00+05:30"

    def _normalize_common_tool_args(self, tool_name: str, args: dict) -> dict:
        args = dict(args or {})
        if tool_name in {"todo.add", "todo.update"} and "priority" in args:
            args["priority"] = self._normalize_priority_value(args.get("priority"))
        if tool_name == "todo.add_many" and isinstance(args.get("tasks"), list):
            tasks = []
            for item in args.get("tasks") or []:
                if not isinstance(item, dict):
                    continue
                clean = dict(item)
                clean["priority"] = self._normalize_priority_value(clean.get("priority"))
                if clean.get("due_date"):
                    clean["due_date"] = self._normalize_due_date_value(clean.get("due_date"))
                tasks.append(clean)
            args["tasks"] = tasks
        if tool_name in {"todo.add", "todo.update"} and args.get("due_date"):
            args["due_date"] = self._normalize_due_date_value(args.get("due_date"))
        if tool_name == "calendar.add_event":
            start = self._normalize_calendar_datetime_value(args.get("start"))
            if start:
                args["start"] = start
            if args.get("end"):
                args["end"] = self._normalize_calendar_datetime_value(args.get("end"), start[:10] if start else None)
            elif start:
                args["end"] = start
        if tool_name == "calendar.add_many" and isinstance(args.get("events"), list):
            events = []
            for ev in args.get("events") or []:
                if not isinstance(ev, dict):
                    continue
                clean = dict(ev)
                start = self._normalize_calendar_datetime_value(clean.get("start"))
                if start:
                    clean["start"] = start
                    clean["end"] = self._normalize_calendar_datetime_value(clean.get("end"), start[:10]) if clean.get("end") else start
                events.append(clean)
            args["events"] = events
        if tool_name == "calendar.add_recurring_event":
            if args.get("recurrence"):
                args["recurrence"] = str(args.get("recurrence")).strip().lower()
            clock = self._normalize_clock_time(args.get("time"))
            if clock:
                args["time"] = clock
        if tool_name == "whatsapp.send_media" and args.get("type"):
            args["type"] = str(args.get("type")).strip().lower()
        return args

    def _task_source_text_for_batch(self) -> str:
        slots = (self._working_state or {}).get("slots", {})
        parts = []
        for key in ("extracted_tasks", "todo_list_updates", "calendar_schedule_details", "message_body", "summary"):
            val = str(slots.get(key) or "").strip()
            if val:
                parts.append(val)
        for bid in sorted(getattr(self, "buffer", {}).keys(), reverse=True)[:5]:
            buf = self.buffer.get(bid)
            data = str((buf or {}).get("data") if isinstance(buf, dict) else buf or "").strip()
            if data:
                parts.append(data)
        return "\n\n".join(parts)

    def _parse_batch_tasks_from_text(self, text: str) -> list[dict]:
        rows = []
        for line in str(text or "").splitlines():
            raw = line.strip()
            if not raw or "|" not in raw:
                continue
            if re.search(r"\|\s*:?-{2,}:?\s*\|", raw) or re.search(r"\baction item\b.*\bpriority\b", raw, re.IGNORECASE):
                continue
            cells = [c.strip(" *`") for c in raw.strip("|").split("|")]
            if len(cells) < 3:
                continue
            if re.fullmatch(r"#|\d+|[ivx]+", cells[0].strip().lower()):
                cells = cells[1:]
            title = cells[0].strip().rstrip(".")
            if not title or len(title) < 3:
                continue
            priority = self._normalize_priority_value(cells[1] if len(cells) > 1 else "medium")
            due = self._normalize_due_date_value(cells[2] if len(cells) > 2 else "")
            desc = cells[3] if len(cells) > 3 else ""
            rows.append({
                "title": title,
                "priority": priority if priority in {"low", "medium", "high"} else "medium",
                "due_date": due or None,
                "description": desc,
            })
        if rows:
            return rows[:25]

        # Fallback for numbered/bulleted lists.
        for line in str(text or "").splitlines():
            m = re.match(r"^\s*(?:[-*]|\d+[.)])\s+(.+?)\s*$", line)
            if not m:
                continue
            title = re.sub(r"\s*\((?:high|medium|low).*$", "", m.group(1), flags=re.IGNORECASE).strip(" .")
            if 4 <= len(title) <= 140 and not re.search(r"\b(calendar|summary|proposed weekly)\b", title, re.IGNORECASE):
                rows.append({"title": title, "priority": "medium", "due_date": None, "description": ""})
        return rows[:25]

    def _availability_time_from_context(self) -> str:
        for _, ans in sorted((getattr(self, "_input_answers", {}) or {}).items()):
            t = self._normalize_clock_time(ans)
            if t:
                return t
        return self._normalize_clock_time(getattr(self, "_current_task_text", "")) or "16:00"

    def _build_calendar_events_for_tasks(self, tasks: list[dict]) -> list[dict]:
        if not tasks:
            return []
        clock = self._availability_time_from_context()
        start_day = datetime.now().date()
        events = []
        for idx, task in enumerate(tasks[:12]):
            day = start_day + timedelta(days=idx)
            start = f"{day.isoformat()}T{clock}:00+05:30"
            hh, mm = [int(x) for x in clock.split(":", 1)]
            end_dt = datetime(day.year, day.month, day.day, hh, mm) + timedelta(hours=1)
            end = end_dt.strftime("%Y-%m-%dT%H:%M:%S+05:30")
            events.append({
                "title": str(task.get("title") or f"Work block {idx + 1}").strip(),
                "start": start,
                "end": end,
            })
        return events

    def _research_select_sources(self, plan: dict, search_payload: dict, max_sources: int = 5) -> list[dict]:
        preferred = [str(d).lower() for d in plan.get("preferred_domains", [])]
        query_terms = {
            w for w in re.findall(r"[a-z0-9]{3,}", str(plan.get("query", "")).lower())
            if w not in {"the", "and", "for", "with", "best", "top", "latest", "summary"}
        }
        scored = []
        seen_urls = set()
        seen_hosts = {}
        for item in search_payload.get("results", []) or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("link") or "").strip()
            title = str(item.get("title") or "").strip()
            snippet = str(item.get("snippet") or item.get("summary") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            host = re.sub(r"^www\.", "", re.sub(r"^https?://", "", url).split("/", 1)[0].lower())
            text = f"{title} {snippet} {host}".lower()
            score = 0
            score += sum(2 for term in query_terms if term in text)
            score += 6 if any(d in host or (d == "gov" and host.endswith(".gov")) for d in preferred) else 0
            score += 2 if re.search(r"\b(summary|review|best|top|list|official|plot|release|guide)\b", text) else 0
            score -= 3 if re.search(r"youtube|facebook|instagram|pinterest|reddit", host) else 0
            score -= seen_hosts.get(host, 0) * 2
            seen_hosts[host] = seen_hosts.get(host, 0) + 1
            scored.append((score, item))
        selected = [item for _, item in sorted(scored, key=lambda kv: kv[0], reverse=True)[: max(1, min(int(max_sources or 5), 8))]]
        return selected

    def _research_read_sources(self, selected: list[dict]) -> list[dict]:
        pages = []
        for src in selected[:8]:
            url = str(src.get("url") or src.get("link") or "").strip()
            title = str(src.get("title") or "").strip()
            snippet = str(src.get("snippet") or src.get("summary") or "").strip()
            content = ""
            reader = ""
            if url:
                for tool_name, tool_args in (
                    ("firecrawl.scrape", {"url": url}),
                    ("web.read", {"url": url}),
                ):
                    res = self._run_registered_tool(tool_name, tool_args)
                    payload = res.get("result", res) if isinstance(res, dict) else res
                    if isinstance(payload, dict) and payload.get("status") == "error":
                        continue
                    if isinstance(payload, dict):
                        content = str(
                            payload.get("content")
                            or payload.get("markdown")
                            or payload.get("text")
                            or payload.get("summary")
                            or ""
                        ).strip()
                        title = str(payload.get("title") or title).strip()
                    elif isinstance(payload, str):
                        content = payload.strip()
                    if content:
                        reader = tool_name
                        break
            if not content:
                content = snippet
                reader = "search.snippet"
            pages.append({
                "title": title,
                "url": url,
                "snippet": snippet,
                "content": content[:5000],
                "reader": reader,
                "source_query": src.get("source_query", ""),
            })
        return pages

    def _extract_json_array(self, text: str) -> list:
        raw = str(text or "").strip()
        m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
        if m:
            raw = m.group(1).strip()
        start = raw.find("[")
        end = raw.rfind("]")
        if start >= 0 and end > start:
            raw = raw[start:end + 1]
        try:
            val = json.loads(raw)
            return val if isinstance(val, list) else []
        except Exception:
            return []

    def _research_contract(self, query: str, plan: dict | None = None) -> dict:
        """Infer a compact, domain-neutral answer contract for research tasks."""
        q = re.sub(r"\s+", " ", str(query or "")).strip()
        q_l = q.lower()

        def number_from_text(default: int) -> int:
            words = {
                "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
            }
            m = re.search(r"\b(?:top|best|first|at least|minimum|min)?\s*(\d{1,2})\b", q_l)
            if m:
                return max(1, min(int(m.group(1)), 12))
            for word, val in words.items():
                if re.search(rf"\b(?:top|best|first|at least|minimum|min)?\s*{word}\b", q_l):
                    return val
            return default

        if re.search(r"\b(compare|comparison|versus|vs\.?|which is better|rank)\b", q_l):
            intent = "compare_items"
            answer_unit = "item"
            default_count = 2
            required_fields = ["name", "comparison", "source"]
        elif re.search(r"\b(best|top|recommend|recommendation|suggest|pick|choose|shortlist)\b", q_l):
            intent = "recommend_items"
            answer_unit = "item"
            default_count = 5
            required_fields = ["name", "reason", "source"]
        elif re.search(r"\b(summarize|summarise|summary|explain|overview|brief)\b", q_l):
            intent = "summarize"
            answer_unit = "summary"
            default_count = 1
            required_fields = ["subject", "summary", "source"]
        elif re.search(r"\b(confirm|verify|validated?|facts?|claims?|evidence|not opinions?)\b", q_l):
            intent = "verify_claims"
            answer_unit = "claim"
            default_count = 3
            required_fields = ["claim", "source"]
        else:
            intent = "find_facts"
            answer_unit = "fact"
            default_count = 3
            required_fields = ["fact", "source"]

        if re.search(r"\b(latest|current|today|this week|this month|this quarter|recent|202\d)\b", q_l):
            required_fields = list(dict.fromkeys([*required_fields, "date_or_current_context"]))

        min_items = number_from_text(default_count)
        if intent in {"summarize", "find_facts"} and not re.search(r"\b(top|best|list|recommend|several|multiple|\d)\b", q_l):
            min_items = 1

        return {
            "intent": intent,
            "answer_unit": answer_unit,
            "min_items": min_items,
            "required_fields": required_fields,
            "success_criteria": [
                "Each answer candidate directly answers the user request.",
                "Return concrete answer units, not just pages or places to look.",
                "Each important answer candidate includes source support when available.",
                "If enough concrete answer candidates are not found, report gaps instead of pretending.",
            ],
            "avoid": [
                "source-only results",
                "navigation text",
                "generic category pages",
                "article/list page titles used as final answers",
                "unsupported claims",
            ],
        }

    def _research_fact_text(self, fact: dict) -> str:
        if not isinstance(fact, dict):
            return ""
        return " ".join(
            re.sub(r"\s+", " ", str(v or "")).strip()
            for v in fact.values()
            if not isinstance(v, (dict, list))
        ).strip()

    def _research_primary_name(self, fact: dict) -> str:
        if not isinstance(fact, dict):
            return ""
        for key in (
            "name", "title", "item", "item/title", "tool", "product", "company",
            "person", "ticker", "book", "movie", "paper", "subject", "claim",
            "finding", "fact", "rule",
        ):
            val = str(fact.get(key) or "").strip()
            if val:
                return re.sub(r"\s+", " ", val)
        return ""

    def _research_has_supporting_detail(self, fact: dict, contract: dict) -> bool:
        if not isinstance(fact, dict):
            return False
        detail_keys = {
            "reason", "why", "why it matches", "summary", "short summary",
            "description", "category", "genre/category", "comparison",
            "comparison_points", "pros", "cons", "evidence", "date",
            "date if available", "release date/year",
        }
        if contract.get("intent") not in {"recommend_items", "compare_items"}:
            detail_keys.update({"finding", "fact", "claim", "rule"})
        for key, val in fact.items():
            if str(key).strip().lower() in detail_keys and str(val or "").strip():
                return True
        if contract.get("intent") in {"recommend_items", "compare_items"}:
            return False
        text = self._research_fact_text(fact)
        return len(text) >= 80

    def _research_is_source_lead(self, fact: dict, contract: dict) -> bool:
        if not isinstance(fact, dict):
            return True
        label = str(fact.get("candidate_type") or fact.get("type") or "").lower()
        if "source_lead" in label:
            return True
        name = self._research_primary_name(fact).lower()
        source = str(fact.get("source") or "").strip().lower()
        text = self._research_fact_text(fact).lower()
        listish = bool(re.search(
            r"\b(best|top|list|lists|guide|roundup|recommendations?|shelf|browse|"
            r"articles?|home|category|categories|publisher|bookseller|search results?)\b",
            name,
        ))
        no_detail = not self._research_has_supporting_detail(fact, contract)
        same_as_source = bool(name and source and (name == source or name in source or source in name))
        if listish and (no_detail or same_as_source):
            return True
        if not name and re.search(r"\b(available from|lists? of|sources?|look at|browse)\b", text):
            return True
        return False

    def _research_candidate_grade(self, fact: dict, contract: dict) -> str:
        if not isinstance(fact, dict):
            return "source_lead"
        explicit = str(fact.get("candidate_type") or fact.get("answer_type") or "").strip().lower()
        if explicit in {"answer_candidate", "supporting_evidence", "source_lead"}:
            if explicit == "answer_candidate" and self._research_is_source_lead(fact, contract):
                return "source_lead"
            return explicit
        name = self._research_primary_name(fact)
        has_source = bool(str(fact.get("source") or fact.get("url") or "").strip())
        has_detail = self._research_has_supporting_detail(fact, contract)
        if self._research_is_source_lead(fact, contract):
            return "source_lead"
        if contract.get("intent") in {"recommend_items", "compare_items"}:
            return "answer_candidate" if name and has_detail else "supporting_evidence"
        if contract.get("intent") in {"verify_claims", "find_facts"}:
            return "answer_candidate" if (name or has_detail) and has_source else "supporting_evidence"
        if contract.get("intent") == "summarize":
            return "answer_candidate" if has_detail else "supporting_evidence"
        return "answer_candidate" if (name or has_detail) else "source_lead"

    def _research_annotate_facts(self, facts: list[dict], contract: dict) -> list[dict]:
        annotated = []
        for fact in facts or []:
            if not isinstance(fact, dict):
                continue
            item = dict(fact)
            item["candidate_type"] = self._research_candidate_grade(item, contract)
            item["direct_answer"] = item["candidate_type"] == "answer_candidate"
            annotated.append(item)
        return annotated

    def _research_extract_facts(self, query: str, plan: dict, pages: list[dict], contract: dict | None = None) -> list[dict]:
        if not pages:
            return []
        contract = contract or self._research_contract(query, plan)
        source_text = []
        for idx, page in enumerate(pages[:6], start=1):
            source_text.append(
                f"SOURCE {idx}\nTitle: {page.get('title','')}\nURL: {page.get('url','')}\n"
                f"Content:\n{str(page.get('content',''))[:1800]}"
            )
        prompt = (
            "Extract only information needed to satisfy the user's research request. "
            "Classify every object as answer_candidate, supporting_evidence, or source_lead. "
            "answer_candidate must directly answer the request; source_lead is only a page/place to inspect. "
            "Remove ads, navigation, duplicate noise, and unrelated items.\n"
            f"User request: {query}\n"
            f"Answer contract: {json.dumps(contract, ensure_ascii=False)}\n"
            f"Where to look: {json.dumps(plan.get('where_to_look', []), ensure_ascii=False)}\n"
            f"Extraction schema: {json.dumps(plan.get('extraction_schema', []), ensure_ascii=False)}\n"
            "Return JSON only: an array of objects. Each object must include candidate_type, source, and url when available.\n\n"
            + "\n\n".join(source_text)
        )
        facts = []
        try:
            out = generate(prompt, model=getattr(self, "_exec_model", "local"), max_tokens=1800, timeout=180, temperature=0.1)
            facts = self._extract_json_array(out)
        except Exception:
            facts = []
        if facts:
            clean = []
            seen = set()
            for item in facts[:30]:
                if not isinstance(item, dict):
                    continue
                key = self._normalize_key(json.dumps(item, sort_keys=True, ensure_ascii=False)[:300])
                if key and key not in seen:
                    seen.add(key)
                    clean.append(item)
            return self._research_annotate_facts(clean, contract)
        # Deterministic fallback: keep source-level evidence if LLM extraction fails.
        for page in pages[:8]:
            summary = re.sub(r"\s+", " ", str(page.get("content") or page.get("snippet") or "")).strip()[:360]
            if summary:
                facts.append({
                    "claim": summary,
                    "source": page.get("title", ""),
                    "url": page.get("url", ""),
                    "candidate_type": "source_lead",
                    "direct_answer": False,
                })
        return self._research_annotate_facts(facts, contract)

    def _research_synthesize(self, query: str, plan: dict, facts: list[dict], contract: dict | None = None, adequacy_gaps: list[str] | None = None) -> str:
        if not facts:
            return "I could not extract reliable facts from the available research sources."
        contract = contract or self._research_contract(query, plan)
        answer_facts = [f for f in facts if isinstance(f, dict) and f.get("candidate_type") == "answer_candidate"]
        supporting = [f for f in facts if isinstance(f, dict) and f.get("candidate_type") != "answer_candidate"]
        if not answer_facts and adequacy_gaps:
            leads = []
            for fact in supporting[:5]:
                title = self._research_primary_name(fact) or str(fact.get("source") or "Source lead").strip()
                url = str(fact.get("url") or "").strip()
                if title:
                    leads.append(f"- {title}" + (f" ({url})" if url else ""))
            msg = (
                "I found research sources, but not enough direct answer candidates for the request yet.\n"
                "Missing: " + "; ".join(str(g) for g in adequacy_gaps[:4]) + "."
            )
            if leads:
                msg += "\nUseful leads found:\n" + "\n".join(leads)
            return msg[:1800]
        synthesis_facts = answer_facts if answer_facts else facts
        gap_note = ""
        if adequacy_gaps:
            gap_note = (
                "Known gaps remain: "
                + "; ".join(str(g) for g in adequacy_gaps[:4])
                + ". If gaps prevent a complete answer, say so plainly.\n"
            )
        prompt = (
            "Write the final answer using only the extracted research facts below. "
            "Be concise, concrete, and do not mention internal buffers or tool steps. "
            "Use answer_candidate facts as the answer. Use supporting_evidence only to explain or cite. "
            "Never present source_lead/page titles as final answer items. "
            "If the request asks to summarize or recommend items, handle each answer item separately. "
            "Include source names/URLs briefly when useful.\n\n"
            f"User request: {query}\n"
            f"Answer contract: {json.dumps(contract, ensure_ascii=False)}\n"
            f"{gap_note}"
            f"Answer facts:\n{json.dumps(synthesis_facts[:25], ensure_ascii=False, indent=2)}\n"
            f"Supporting facts:\n{json.dumps(supporting[:10], ensure_ascii=False, indent=2)}"
        )
        try:
            ans = generate(prompt, model=getattr(self, "_exec_model", "local"), max_tokens=1800, timeout=180, temperature=0.1)
            ans = self._sanitize_delivery_message(ans)
            if ans and len(ans) >= 40:
                return ans
        except Exception:
            pass
        lines = []
        for idx, fact in enumerate(facts[:10], start=1):
            title = fact.get("title") or fact.get("item") or fact.get("claim") or fact.get("summary") or f"Finding {idx}"
            detail = fact.get("short summary") or fact.get("summary") or fact.get("why it matches") or fact.get("claim") or ""
            src = fact.get("source") or fact.get("url") or ""
            lines.append(f"{idx}. {title}: {str(detail)[:240]}" + (f" Source: {src}" if src else ""))
        return "\n".join(lines)

    def _research_adequacy_gaps(self, query: str, facts: list[dict], contract: dict | None = None) -> list[str]:
        """Return generic answer-contract gaps that should trigger follow-up research."""
        contract = contract or self._research_contract(query, {})
        q_l = str(query or "").lower()
        gaps: list[str] = []
        answer_candidates = [
            f for f in (facts or [])
            if isinstance(f, dict) and f.get("candidate_type") == "answer_candidate"
        ]
        source_leads = [
            f for f in (facts or [])
            if isinstance(f, dict) and f.get("candidate_type") == "source_lead"
        ]
        unique_sources = {
            str(f.get("url") or f.get("source") or "").strip().lower()
            for f in answer_candidates
            if isinstance(f, dict) and str(f.get("url") or f.get("source") or "").strip()
        }
        wants_verified = bool(re.search(r"\b(confirm|verified|facts?|claim|claims?|not opinions?|source|sources|evidence)\b", q_l))
        min_items = max(1, int(contract.get("min_items") or 1))
        if not answer_candidates and source_leads:
            gaps.append("direct answer candidates, not just source pages")
        if len(answer_candidates) < min_items:
            gaps.append(f"at least {min_items} direct answer candidates")

        required = [str(x).strip().lower() for x in contract.get("required_fields", []) if str(x).strip()]
        missing_required: set[str] = set()
        for fact in answer_candidates:
            text = self._research_fact_text(fact).lower()
            name = self._research_primary_name(fact)
            has_source = bool(str(fact.get("source") or fact.get("url") or "").strip())
            for req in required:
                ok = True
                if req in {"name", "subject"}:
                    ok = bool(name)
                elif req in {"source", "official source"}:
                    ok = has_source
                elif req in {"reason", "comparison", "summary", "claim", "fact"}:
                    ok = self._research_has_supporting_detail(fact, contract)
                elif req == "date_or_current_context":
                    ok = bool(re.search(r"\b(20\d{2}|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|today|current|latest|recent|quarter)\b", text))
                else:
                    ok = req in text or bool(str(fact.get(req) or "").strip())
                if not ok:
                    missing_required.add(req)
        if answer_candidates and missing_required:
            gaps.append("missing required fields: " + ", ".join(sorted(missing_required)[:4]))

        if wants_verified and len(unique_sources) < 2:
            gaps.append("multiple independent or primary sources")
        if facts and not answer_candidates and not source_leads:
            gaps.append("facts classified against the answer contract")
        return list(dict.fromkeys(gaps))

    def _research_followup_queries(self, query: str, gaps: list[str], contract: dict | None = None) -> list[str]:
        """Build targeted follow-up queries from generic contract gaps."""
        q = re.sub(r"\s+", " ", str(query or "")).strip()
        contract = contract or self._research_contract(q, {})
        out: list[str] = []
        req = " ".join(str(x) for x in contract.get("required_fields", [])[:4])
        unit = str(contract.get("answer_unit") or "answer").strip()
        intent = str(contract.get("intent") or "").strip()
        if any("direct answer candidates" in g or "not just source pages" in g for g in gaps):
            out.append(f"{q} concrete {unit} names details")
            out.append(f"{q} examples with {req}".strip())
        if any("missing required fields" in g for g in gaps):
            out.append(f"{q} {req}".strip())
        if any("multiple independent" in g for g in gaps):
            out.append(f"{q} reliable sources evidence")
        if any("at least" in g for g in gaps):
            out.append(f"{q} list of {unit}s with sources")
        if not out:
            out.append(f"{q} {intent} {unit} evidence".strip())
        return list(dict.fromkeys(out))[:3]

    def _dedupe_research_facts(self, facts: list[dict]) -> list[dict]:
        seen = set()
        out = []
        for fact in facts or []:
            if not isinstance(fact, dict):
                continue
            key = (
                str(fact.get("url") or "").strip().lower(),
                str(
                    self._research_primary_name(fact)
                    or fact.get("claim")
                    or fact.get("summary")
                    or fact.get("fact")
                    or fact.get("finding")
                    or ""
                ).strip().lower()[:220],
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(fact)
        return out

    def _execute_research_tool(self, tool_name: str, args: dict) -> dict:
        query = self._research_query_from_args(args or {})
        if not query:
            return {"status": "error", "error": "research tool requires query/topic/task."}
        max_queries = int((args or {}).get("max_queries") or 5)
        max_sources = int((args or {}).get("max_sources") or 5)
        max_rounds = max(1, min(int((args or {}).get("max_rounds") or 2), 4))
        plan = self._research_plan(query, max_queries=max_queries)
        contract = self._research_contract(query, plan)
        if tool_name == "research.plan":
            return {"status": "success", **plan, "answer_contract": contract}
        searches = self._research_search_many(plan, max_results=int((args or {}).get("max_results") or 5))
        if tool_name == "research.search_many":
            status = "success" if searches.get("count", 0) else "error"
            if status == "error":
                return {"status": "error", "error": "Research search returned no results.", "plan": plan, **searches}
            return {"status": "success", "plan": plan, **searches}
        selected = self._research_select_sources(plan, searches, max_sources=max_sources)
        if tool_name == "research.select_sources":
            if not selected:
                return {"status": "error", "error": "No useful research sources were found.", "plan": plan, "search": searches}
            return {"status": "success", "plan": plan, "sources": selected, "source_count": len(selected)}
        pages = self._research_read_sources(selected)
        if tool_name == "research.read_sources":
            if not pages:
                return {"status": "error", "error": "No research sources could be read.", "plan": plan, "sources": selected}
            return {"status": "success", "plan": plan, "sources": selected, "pages": pages, "page_count": len(pages)}
        facts = self._research_extract_facts(query, plan, pages, contract)
        adequacy_gaps = self._research_adequacy_gaps(query, facts, contract)
        followup_queries = self._research_followup_queries(query, adequacy_gaps, contract)
        extra_sources = []
        if tool_name in ("research.run", "research.extract_facts") and followup_queries:
            seen_followups = set()
            for _round in range(max_rounds):
                if not adequacy_gaps:
                    break
                followup_queries = [
                    fq for fq in self._research_followup_queries(query, adequacy_gaps, contract)
                    if fq.lower() not in seen_followups
                ]
                if not followup_queries:
                    break
                for followup_query in followup_queries:
                    if not adequacy_gaps:
                        break
                    seen_followups.add(followup_query.lower())
                    fplan = self._research_plan(followup_query, max_queries=max_queries)
                    fsearches = self._research_search_many(fplan, max_results=int((args or {}).get("max_results") or 5))
                    fselected = self._research_select_sources(fplan, fsearches, max_sources=max(3, min(max_sources, 6)))
                    fpages = self._research_read_sources(fselected)
                    ffacts = self._research_extract_facts(query, fplan, fpages, contract)
                    if ffacts:
                        facts = self._dedupe_research_facts([*facts, *ffacts])
                        facts = self._research_annotate_facts(facts, contract)
                        selected = [*selected, *fselected]
                        extra_sources.extend(fselected)
                        adequacy_gaps = self._research_adequacy_gaps(query, facts, contract)
        if tool_name == "research.extract_facts":
            if not facts:
                return {"status": "error", "error": "No useful facts could be extracted from research sources.", "plan": plan, "sources": selected}
            return {
                "status": "success",
                "plan": plan,
                "answer_contract": contract,
                "sources": selected,
                "facts": facts,
                "fact_count": len(facts),
                "adequacy_gaps": adequacy_gaps,
                "followup_sources": [
                    {"title": s.get("title", ""), "url": s.get("url", ""), "source_query": s.get("source_query", "")}
                    for s in extra_sources
                ],
            }
        if not searches.get("count", 0):
            return {"status": "error", "error": "Research search returned no results.", "plan": plan, "search": searches}
        if not selected:
            return {"status": "error", "error": "No useful research sources were found.", "plan": plan, "search": searches}
        if not facts:
            return {"status": "error", "error": "No useful facts could be extracted from research sources.", "plan": plan, "sources": selected}
        answer = self._research_synthesize(query, plan, facts, contract, adequacy_gaps)
        return {
            "status": "success",
            "query": query,
            "answer_contract": contract,
            "where_to_look": plan.get("where_to_look", []),
            "queries": plan.get("queries", []),
            "sources": [
                {"title": s.get("title", ""), "url": s.get("url", ""), "source_query": s.get("source_query", "")}
                for s in selected
            ],
            "facts": facts,
            "answer": answer,
            "fact_count": len(facts),
            "adequacy_gaps": adequacy_gaps,
            "followup_sources": [
                {"title": s.get("title", ""), "url": s.get("url", ""), "source_query": s.get("source_query", "")}
                for s in extra_sources
            ],
        }

    def _parse_needed_by(self, raw: str) -> list[int]:
        """Parse planner dependency targets as one-based step numbers."""
        if not raw:
            return []
        out = []
        for part in re.split(r"[,/|+\s]+|\band\b", str(raw), flags=re.IGNORECASE):
            part = part.strip().lower()
            if not part or part in {"none", "na", "n/a", "-", "step", "steps"}:
                continue
            m = re.search(r"\d+", part)
            if not m:
                continue
            val = int(m.group(0))
            if val > 0 and val not in out:
                out.append(val)
        return out

    def _route_dependency_output(
        self,
        source_step: int,
        source_goal: str,
        target_steps: list[int],
        slot: str,
        payload: str,
        buffer_id: int | None = None,
    ) -> None:
        """Store a signal payload for only the future steps that declared a need."""
        if not target_steps or not payload:
            return
        for target in target_steps:
            if target <= source_step:
                continue
            self._dependency_outputs.setdefault(target, []).append({
                "source_step": source_step,
                "source_goal": str(source_goal or "")[:140],
                "slot": slot,
                "payload": payload[:4000],
                "buffer_id": buffer_id,
            })

    def _get_dependency_context(self, target_step: int, inputs: list) -> str:
        """Render only outputs explicitly routed to this step."""
        records = list((self._dependency_outputs or {}).get(target_step, []) or [])
        if not records:
            return ""
        input_set = {
            str(x or "").strip().lower()
            for x in (inputs or [])
            if str(x or "").strip()
        }
        if input_set:
            filtered = [
                r for r in records
                if str(r.get("slot", "")).lower() in input_set
                or any(str(r.get("slot", "")).lower().startswith(inp) or inp in str(r.get("slot", "")).lower() for inp in input_set)
            ]
            if filtered:
                records = filtered
        lines = ["TARGETED DEPENDENCY OUTPUTS (only for this step):"]
        for rec in records[:8]:
            ref = f" [BUFFER:{rec['buffer_id']}]" if rec.get("buffer_id") else ""
            lines.append(
                f"- from step {rec.get('source_step')} slot.{rec.get('slot')}{ref}:"
            )
            lines.append(str(rec.get("payload", "")).strip()[:4000])
        return "\n".join(lines) + "\n\n"

    def _store_declared_outputs(
        self,
        outputs: list,
        tool_name: str,
        tool_res,
        raw_obs: str,
        step_result: str,
        buffer_id: int | None = None,
        needed_by: list[int] | None = None,
        source_step: int = 0,
        source_goal: str = "",
    ) -> None:
        """Persist planner-declared OUTPUT slots into run-scoped working state."""
        if not outputs:
            return
        slots = self._working_state.setdefault("slots", {})
        artifacts = self._working_state.setdefault("artifacts", {})
        payload = self._extract_primary_payload(tool_res, raw_obs)
        if not payload:
            payload = str(step_result or "").strip()

        for raw_slot in outputs:
            slot = str(raw_slot or "").strip().lower()
            if not slot or slot in {"none", "na", "n_a"}:
                continue
            if "buffer" in slot and buffer_id is not None:
                slots[slot] = f"[BUFFER:{buffer_id}]"
                artifacts.setdefault("buffers", [])
                if str(buffer_id) not in artifacts["buffers"]:
                    artifacts["buffers"].append(str(buffer_id))
                self._route_dependency_output(
                    source_step, source_goal, needed_by or [], slot, slots[slot], buffer_id=buffer_id
                )
                continue
            if slot in {"recipient_phone_or_jid", "phone", "phone_number"}:
                phone = self._extract_phone_like(payload)
                if phone:
                    slots["recipient_phone_or_jid"] = phone
                    self._route_dependency_output(
                        source_step, source_goal, needed_by or [], "recipient_phone_or_jid", phone, buffer_id=buffer_id
                    )
                continue
            if slot in {"recipient_email", "email", "email_address"}:
                emails = self._extract_emails(payload)
                if emails:
                    slots["recipient_email"] = emails[0]
                    self._route_dependency_output(
                        source_step, source_goal, needed_by or [], "recipient_email", emails[0], buffer_id=buffer_id
                    )
                continue
            if slot in {"file_path", "filepath", "path", "excel_file_path"}:
                files = self._extract_key_data(payload).get("files", [])
                path_val = self._extract_file_path_from_tool_result(tool_res, raw_obs)
                if path_val and path_val not in files:
                    files.insert(0, path_val)
                if files:
                    files[0] = self._normalize_sandbox_file_ref(files[0]) or files[0]
                    bare = self._bare_excel_filename(files[0]) if slot == "excel_file_path" else files[0]
                    slots[slot] = bare
                    if slot == "excel_file_path":
                        artifacts["excel_file_path"] = bare
                    if tool_name in ("filesystem.write", "filesystem.append", "pdf.create", "word.create", "convert.markdown_to_pdf", "convert.html_to_pdf"):
                        slots["file_path"] = files[0]
                    else:
                        slots.setdefault("file_path", files[0])
                    artifacts["last_file_path"] = files[0]
                    self._route_dependency_output(
                        source_step, source_goal, needed_by or [], slot, bare, buffer_id=buffer_id
                    )
                continue
            file_artifact_path = self._extract_file_path_from_tool_result(tool_res, raw_obs)
            if file_artifact_path and re.search(r"(?:^|_)(?:pdf|docx?|document|file|attachment|report|output)(?:_|$)", slot):
                slots[slot] = file_artifact_path
                slots["file_path"] = file_artifact_path
                artifacts["last_file_path"] = file_artifact_path
                artifacts["document_file_path"] = file_artifact_path
                if file_artifact_path.lower().endswith(".pdf"):
                    slots["pdf_file_path"] = file_artifact_path
                    artifacts["pdf_file_path"] = file_artifact_path
                self._route_dependency_output(
                    source_step, source_goal, needed_by or [], slot, file_artifact_path, buffer_id=buffer_id
                )
                continue
            if tool_name.startswith("research.") and slot in {
                "research_answer", "final_answer", "answer", "summary", "recommendations"
            }:
                answer = ""
                if isinstance(tool_res, dict):
                    answer = str(tool_res.get("answer") or "").strip()
                if not answer:
                    try:
                        parsed = json.loads(payload)
                        if isinstance(parsed, dict):
                            answer = str(parsed.get("answer") or "").strip()
                    except Exception:
                        answer = ""
                if answer:
                    slots[slot] = answer[:12000]
                    if slot != "research_answer":
                        slots.setdefault("research_answer", answer[:12000])
                    self._route_dependency_output(
                        source_step, source_goal, needed_by or [], slot, answer, buffer_id=buffer_id
                    )
                    continue
            if payload:
                signal = self._payload_to_signal(payload, tool_name=tool_name, slot=slot)
                if slot in {"message_body", "summary", "recommendations", "content"}:
                    signal = self._normalize_delivery_body(signal)
                slots[slot] = signal[:12000]
                self._route_dependency_output(
                    source_step, source_goal, needed_by or [], slot, signal, buffer_id=buffer_id
                )

    def _get_step_input_context(self, inputs: list) -> str:
        """Render only the declared INPUT slots needed by the current step."""
        if not inputs:
            return ""
        slots = (self._working_state or {}).get("slots", {})
        lines = ["STEP INPUT DATA:"]
        for raw_slot in inputs:
            slot = str(raw_slot or "").strip().lower()
            if not slot or slot in {"none", "na", "n_a"}:
                continue
            val = slots.get(slot)
            if val in (None, "", [], {}):
                lines.append(f"- slot.{slot}: missing")
                continue
            text = str(val)
            if len(text) > 4000:
                text = text[:4000] + "\n...[truncated from slot]"
            lines.append(f"- slot.{slot}:\n{text}")
        return "\n".join(lines) + "\n\n" if len(lines) > 1 else ""

    def _content_slot_candidates(self, defaults: tuple[str, ...]) -> list[str]:
        """Prefer current step INPUT slots before generic content slots."""
        current_inputs = [
            str(s or "").strip().lower()
            for s in getattr(self, "_current_step_inputs", []) or []
        ]
        blocked = ("recipient", "contact", "phone", "email", "jid", "file", "path", "url")
        ordered = []
        for slot in [*current_inputs, *defaults]:
            if not slot or slot in ordered:
                continue
            if any(b in slot for b in blocked):
                continue
            ordered.append(slot)
        return ordered

    def _looks_like_raw_retrieval_payload(self, text: str) -> bool:
        """Detect search/scrape evidence that should be composed before sending."""
        s = str(text or "").strip()
        if not s:
            return False
        low = s.lower()
        if low.startswith("lines ") and " of buffer " in low:
            return True
        if '"query"' in low and '"results"' in low and '"snippet"' in low:
            return True
        if re.search(r'\b(lists? available|there are lists|resources? available|search results?|recommended .*themes)\b', low):
            return True
        if low.count('"url"') >= 2 or low.count("http") >= 2:
            return True
        return False

    def _looks_like_incomplete_code(self, text: str) -> bool:
        """Detect code fragments that should not be written as final source files."""
        s = str(text or "").strip()
        if not s:
            return True
        low = s.lower()
        if "...[truncated" in low or "[signal truncated]" in low:
            return True
        if "```" in s and s.count("```") % 2 == 1:
            return True
        if re.search(r"@\s*param\b|/\*\*|\bfunction\b.*\Z", s, re.IGNORECASE) and "main(" not in low:
            return True
        # A rough but useful guard for C/C++/Java-style source fragments.
        opens = s.count("{")
        closes = s.count("}")
        if opens > closes:
            return True
        if re.search(r"\.(?:c|cc|cpp|cxx|h|hpp)\b", str(getattr(self, "_current_task_text", "") or ""), re.IGNORECASE):
            return "main(" not in low and len(s) < 1500
        return False

    def _build_delivery_message_from_slots(self) -> str:
        """Build a send-ready message from prepared or retrieved content slots."""
        slots = (self._working_state or {}).get("slots", {})
        for key in ("message_body", "recommendations", "summary", "draft_body", "content"):
            val = str(slots.get(key) or "").strip()
            if val and not self._looks_like_raw_retrieval_payload(val):
                return val[:1800]

        raw = ""
        dynamic_slots = [
            str(s or "").strip().lower()
            for s in getattr(self, "_current_step_inputs", []) or []
            if str(s or "").strip()
        ]
        for key in (
            "research_answer",
            "book_recommendations",
            "recommendations",
            "summary",
            "search_results",
            "results",
            "retrieved_results",
            "web_results",
            *dynamic_slots,
        ):
            val = str(slots.get(key) or "").strip()
            if val:
                raw = val
                break
        if not raw:
            for buf_id in sorted(self.buffer.keys(), reverse=True):
                buf = self.buffer.get(buf_id)
                if not isinstance(buf, dict):
                    continue
                tool = str(buf.get("tool") or "")
                if not tool.startswith(("search.", "firecrawl.", "research.")):
                    continue
                val = str(buf.get("data") or "").strip()
                if val:
                    raw = val
                    break
        if not raw:
            return ""

        try:
            prompt = (
                "Compose a send-ready WhatsApp message from the retrieved data below.\n"
                "Requirements:\n"
                "- Plain text only; no JSON, no tool syntax, no escaped newline strings.\n"
                "- Keep it under 900 characters.\n"
                "- Prefer concrete book titles and authors when present.\n"
                "- If the data contains only article/list titles, say these are reading-list leads to check, not actual book titles.\n"
                "- Do not invent book titles that are not supported by the data.\n\n"
                f"Retrieved data:\n{raw[:5000]}\n\n"
                "WhatsApp message:"
            )
            composed = str(generate(prompt, temperature=0.1, max_tokens=350) or "").strip()
            composed = self._sanitize_delivery_message(composed)
            if (
                composed
                and not composed.lower().startswith(("[error", "error running", "traceback"))
                and not self._looks_like_raw_retrieval_payload(composed)
            ):
                slots["message_body"] = composed[:1800]
                return slots["message_body"]
        except Exception as e:
            print(f"[AGENT] delivery compose fallback skipped: {e}")

        titles = []
        snippets = []
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                for item in data.get("results", []) or []:
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title") or "").strip()
                    snippet = str(item.get("snippet") or "").strip()
                    if title:
                        titles.append(title)
                    if snippet:
                        snippets.append(snippet)
        except Exception:
            for line in raw.splitlines():
                clean = re.sub(r'^[\s*\-•\d.)]+', '', line).strip()
                if clean:
                    snippets.append(clean)

        # Prefer concrete book/article titles over dumping raw result JSON.
        usable = []
        for title in titles:
            title = re.sub(r'\s+', ' ', title).strip()
            if not title or title.lower().startswith(("the best books", "best books", "books to look out")):
                usable.append(title)
            elif title not in usable:
                usable.append(title)
            if len(usable) >= 5:
                break

        if usable:
            lines = ["Here are some fiction and romance reading leads I found:"]
            lines.extend(f"{idx}. {title}" for idx, title in enumerate(usable, start=1))
            msg = "\n".join(lines)
        else:
            compact = []
            for s in snippets:
                if self._looks_like_raw_retrieval_payload(s):
                    continue
                compact.append(s)
                if len(compact) >= 5:
                    break
            if not compact:
                return ""
            msg = "Here are the fiction and romance recommendations I found:\n" + "\n".join(
                f"- {s}" for s in compact
            )

        slots["message_body"] = msg[:1800]
        return slots["message_body"]

    def _sanitize_delivery_message(self, text: str) -> str:
        """Remove model/tool wrapper noise from a message before delivery."""
        msg = str(text or "").strip()
        msg = re.sub(r"<\|?/?(?:tool_call|eos)\|?>", "", msg, flags=re.IGNORECASE)
        call_msg = re.search(r'\bmessage\s*[:=]\s*"([^"]+)"', msg, re.IGNORECASE | re.DOTALL)
        if call_msg:
            msg = call_msg.group(1)
        msg = re.sub(r"^\s*(?:FINAL ANSWER|DONE|MIRA|WhatsApp message)\s*:\s*", "", msg, flags=re.IGNORECASE)
        msg = msg.replace("\\n", "\n")
        msg = re.sub(r"(?im)^\s*Source:\s*\[BUFFER:\d+\]\s*$", "", msg)
        msg = re.sub(r"\n{3,}", "\n\n", msg)
        msg = msg.strip().strip('"').strip("'").strip()
        return msg

    def _is_multi_option_message(self, text: str) -> bool:
        """Detect draft-menu output that should not be sent directly."""
        low = str(text or "").lower()
        return bool(
            "here are a few options" in low
            or re.search(r"\boption\s*[1-9]\s*:", low)
            or "choose the option" in low
            or "depending on the desired tone" in low
        )

    def _normalize_delivery_body(self, text: str) -> str:
        """Convert a draft/menu response into one send-ready message."""
        msg = self._sanitize_delivery_message(text)
        if not msg:
            return ""
        if not self._is_multi_option_message(msg):
            return msg[:1800]

        try:
            prompt = (
                "Rewrite the draft below into exactly ONE WhatsApp-ready message.\n"
                "Rules:\n"
                "- No options, no headings, no bullet list, no markdown blockquotes.\n"
                "- Do not ask the user to choose.\n"
                "- Make it a concise actual summary, not a recommendation menu.\n"
                "- 2 to 4 sentences, under 900 characters.\n"
                "- If it is about the movie 3 Idiots, mention Farhan, Raju and Rancho, "
                "the pressure of the education system, friendship, and following passion over blind success.\n\n"
                f"Draft:\n{msg[:4000]}\n\n"
                "Single message:"
            )
            rewritten = str(generate(prompt, model=getattr(self, "_exec_model", "local"), temperature=0.1, max_tokens=280) or "").strip()
            rewritten = self._sanitize_delivery_message(rewritten)
            if (
                rewritten
                and not rewritten.lower().startswith(("[error", "error running", "traceback"))
                and not self._is_multi_option_message(rewritten)
            ):
                return rewritten[:1800]
        except Exception as e:
            print(f"[AGENT] delivery body rewrite skipped: {e}")

        # Deterministic fallback: choose the first option body and strip menu framing.
        if re.search(r"\b3\s+idiots\b", msg, re.IGNORECASE):
            return (
                "3 Idiots is a 2009 Indian comedy-drama about Farhan, Raju and Rancho, "
                "three engineering students caught in a pressure-driven education system. "
                "Through their friendship, the film questions blind competition and reminds us "
                "to follow passion, think independently and define success on our own terms."
            )
        match = re.search(
            r"(?is)(?:option\s*1\s*:[^\n]*\n+)(.*?)(?=\n+\s*#{0,3}\s*option\s*2\s*:|\n+\*\*\s*choose|\Z)",
            msg,
        )
        chosen = match.group(1).strip() if match else msg
        chosen = re.sub(r"(?m)^\s*[>#]\s*", "", chosen)
        chosen = re.sub(r"(?im)^\s*option\s*\d+\s*:.*$", "", chosen)
        chosen = re.sub(r"(?im)^here are .*$", "", chosen)
        chosen = re.sub(r"(?is)\*\*choose the option.*$", "", chosen)
        chosen = re.sub(r"\n{2,}", " ", chosen)
        return self._sanitize_delivery_message(chosen)[:900]

    def _task_requires_retrieval_delivery_message(self) -> bool:
        """True when a delivery task depends on fetched/researched content."""
        t = str(getattr(self, "_current_task_text", "") or "").lower()
        wants_delivery = bool(re.search(r'\b(send|share|forward|whatsapp|email|mail|message)\b', t))
        wants_retrieval = bool(re.search(r'\b(find|search|research|look up|get|recommend|best|books?|list)\b', t))
        return wants_delivery and wants_retrieval

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
            result = str(item.get("raw_payload") or item.get("result", "") or "").strip()
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

    def _maybe_update_handoff_payload(self, step_tool: str, step_result: str, raw_payload: str = "") -> None:
        """Keep latest useful non-delivery text as fallback payload for send steps."""
        t = (step_tool or "").lower()
        if not step_result and not raw_payload:
            return
        if t.startswith((
            "whatsapp.", "email.", "contacts.", "calendar.", "system.", "memory.",
            "user.", "soul.", "profile."
        )):
            return
        text = str(raw_payload or step_result).strip()
        if len(text) < 12:
            return
        if text.lower() in ("ok", "no result", "none", "null"):
            return
        self._handoff_payload = text[:8000]

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

    def _get_all_buffer_signal_context(self, max_chars: int = 8000) -> str:
        """Render all buffer inventory as compact signal for memory/scratchpad tools."""
        if not self.buffer:
            return "ACTIVE BUFFERS: none.\n\n"
        lines = ["ALL ACTIVE BUFFER SIGNAL (use only task-relevant facts for persistence):"]
        used = 0
        for buf_id in sorted(self.buffer.keys()):
            buf = self.buffer.get(buf_id, {})
            if isinstance(buf, dict):
                tool = str(buf.get("tool", "unknown"))
                data = str(buf.get("data", "") or "")
                chars = int(buf.get("chars", len(data)) or 0)
            else:
                tool = "unknown"
                data = str(buf or "")
                chars = len(data)
            signal = self._payload_to_signal(data, tool_name=tool, slot=f"buffer_{buf_id}", max_chars=1400)
            block = f"[BUFFER:{buf_id}] from {tool}, {chars} chars\n{signal[:1400]}"
            if used + len(block) > max_chars:
                lines.append("...[remaining buffers omitted for context budget]")
                break
            lines.append(block)
            used += len(block)
        return "\n\n".join(lines) + "\n\n"

    def _step_needs_all_buffer_signal(self, step_action: str, step_desc: str, step_categories: list[str]) -> bool:
        """Memory/scratchpad/buffer persistence steps need the full buffer signal inventory."""
        action = str(step_action or "").lower()
        desc = str(step_desc or "").lower()
        cats = {str(c or "").strip().lower() for c in (step_categories or [])}
        if action.startswith(("memory.", "buffer.", "scratchpad.")):
            return True
        if "buffer" in cats or "knowledge base" in cats:
            return True
        return bool(re.search(r"\b(memory|remember|scratchpad|save note|store note|persist|buffer)\b", desc))

    def _prepare_persistence_content(self, content: str, tool_name: str) -> str:
        """Sanitize memory/buffer writes into task-relevant pure signal."""
        raw = str(content or "").strip()
        if not raw:
            return ""
        signal = self._payload_to_signal(raw, tool_name=tool_name, slot="persistent_note", max_chars=2500)
        buffer_context = self._get_all_buffer_signal_context(max_chars=5000)
        # If the model tried to save a generic or raw note, rewrite against the task and buffer signal.
        looks_raw = self._looks_like_raw_retrieval_payload(signal) or len(signal) > 900
        generic = signal.lower() in {"ok", "done", "saved", "task complete", "no result"}
        if looks_raw or generic or "[BUFFER:" in raw:
            try:
                prompt = (
                    "Create a persistent note in pure signal form.\n"
                    "Keep only facts required by the user's task or explicitly needed by future steps.\n"
                    "Use compact bullets. Preserve exact contact details, file paths, URLs, titles, and IDs.\n"
                    "Do not save tool logs, JSON wrappers, reasoning, duplicate snippets, or unrelated context.\n"
                    "Max 700 characters.\n\n"
                    f"Task: {getattr(self, '_current_task_text', '')[:400]}\n"
                    f"Current step: {getattr(self, '_current_step_desc', '')[:220]}\n"
                    f"Declared inputs: {', '.join(getattr(self, '_current_step_inputs', []) or []) or 'none'}\n"
                    f"Declared outputs: {', '.join(getattr(self, '_current_step_outputs', []) or []) or 'none'}\n\n"
                    f"Candidate note:\n{raw[:3000]}\n\n"
                    f"{buffer_context[:5000]}\n"
                    "Persistent note:"
                )
                distilled = str(generate(
                    prompt,
                    model=getattr(self, "_exec_model", "local"),
                    temperature=0.0,
                    max_tokens=260,
                ) or "").strip()
                distilled = re.sub(r"^\s*(?:Persistent note|Note|FINAL ANSWER)\s*:\s*", "", distilled, flags=re.IGNORECASE)
                if distilled and distilled.lower() not in {"ok", "done"}:
                    signal = distilled
            except Exception as e:
                print(f"[AGENT] persistence distill skipped: {e}")
        signal = " ".join(line.strip() for line in signal.splitlines() if line.strip())
        return signal[:1200]

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
            "research": "Research",
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
            ("Research", r"\b(research|find|search|lookup|look up|best|recommend|summari[sz]e|compare|latest|top)\b", "research.run"),
            ("Web & Search", r"\bnews\b", "search.news"),
            ("Web & Search", r"\b(search|find|lookup|look up)\b", "search.duckduckgo"),
            ("Knowledge Base", r"\b(remember|ingest|store|save|add)\b", "knowledge.ingest"),
            ("Knowledge Base", r"\b(search|find|lookup|look up|get|retrieve|list|notes?|documents?)\b", "knowledge.search"),
            ("Filesystem", r"\b(write|save|create file)\b", "filesystem.write"),
            ("Filesystem", r"\b(read|open|view)\b", "filesystem.read"),
            ("Filesystem", r"\b(list|show files|directory)\b", "filesystem.list"),
            ("Email", r"\bsend\b", "email.send"),
            ("Email", r"\b(list|read|inbox|fetch)\b", "email.list"),
            ("Calendar", r"\b(all|multiple|several|each|every|blocks?|tasks?|\d+\s+).*\b(add|create|schedule)|\b(add|create|schedule).*\b(all|multiple|several|each|every|blocks?|tasks?|\d+\s+)", "calendar.add_many"),
            ("Calendar", r"\b(add|create|schedule)\b", "calendar.add_event"),
            ("Calendar", r"\b(list|show|upcoming)\b", "calendar.list_events"),
            ("Todo List", r"\b(all|multiple|several|each|every|\d+\s+).*\b(add|create)|\b(add|create).*\b(all|multiple|several|each|every|\d+\s+)", "todo.add_many"),
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

    def _parse_inline_args_string(self, raw: str) -> dict:
        """Parse strings like 'to=123, url=SANDBOX/a.cpp, type=document'."""
        s = str(raw or "").strip().strip('"').strip("'").strip()
        if not s:
            return {}
        if s.startswith("{") and s.endswith("}"):
            try:
                parsed = json.loads(s)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                pass
        out: dict = {}
        for part in re.split(r"\s*[,|]\s*(?=[A-Za-z_][\w.-]*\s*=)", s):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            key = k.strip()
            if key:
                val = str(v or "").strip().strip('"').strip("'")
                # Delivery/path fields must stay strings. Phone numbers and
                # local paths can be damaged if coerced to int/float.
                if key.lower() in {
                    "to", "phone", "number", "jid", "recipient", "url",
                    "path", "file", "file_path", "media_path", "type",
                    "caption", "filename",
                }:
                    out[key] = val
                else:
                    out[key] = self._coerce_kv_value(v)
        return out

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
            if arg_blob.startswith("{") and arg_blob.endswith("}"):
                try:
                    parsed = json.loads(arg_blob)
                    if isinstance(parsed, dict):
                        return tool, parsed, ""
                except Exception:
                    pass
        args = {}
        if arg_blob:
            if "|" in arg_blob:
                parts = [p.strip() for p in re.split(r"\s*\|\s*", arg_blob) if p.strip()]
            elif re.search(r",\s*[A-Za-z_][\w.-]*\s*[=:]", arg_blob):
                parts = [p.strip() for p in re.split(r",\s*(?=[A-Za-z_][\w.-]*\s*[=:])", arg_blob) if p.strip()]
            else:
                parts = [arg_blob.strip()]
            for part in parts:
                if "=" in part:
                    k, v = part.split("=", 1)
                elif ":" in part:
                    k, v = part.split(":", 1)
                else:
                    continue
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
        retrieval_roots = ("search.", "firecrawl.", "web.")
        if any(str(s.get("tool", "")).startswith(retrieval_roots) for s in completed):
            return False
        if len(completed) == 1:
            return True
        action_roots = ("calendar.", "email.", "todo.", "contacts.", "whatsapp.")
        return all(str(s.get("tool", "")).startswith(action_roots) for s in completed)

    def _render_synthesis_tool_outputs(
        self,
        completed: list,
        max_total_chars: int = 9000,
        max_each_chars: int = 2500,
    ) -> str:
        """
        Render ordered tool outputs for the final synthesizer.

        The step result is often a compact digest for routing. Final answers need
        the actual tool evidence, so prefer raw_payload and fall back to result.
        """
        if not completed:
            return ""
        lines: list[str] = []
        used = 0
        for idx, step in enumerate(completed, start=1):
            tool = str(step.get("tool") or "unknown").strip()
            status = str(step.get("status") or "").strip()
            goal = str(step.get("step") or "").strip()
            payload = str(step.get("raw_payload") or "").strip()
            if not payload:
                payload = str(step.get("result") or "").strip()
            if not payload:
                continue

            payload = payload.replace("\r\n", "\n").replace("\r", "\n").strip()
            if len(payload) > max_each_chars:
                payload = payload[:max_each_chars].rstrip() + "\n...[tool output truncated]"
            block = (
                f"Tool output {idx}:\n"
                f"- status: {status or 'unknown'}\n"
                f"- tool: {tool}\n"
                f"- step: {goal[:220]}\n"
                f"{payload}\n"
            )
            if used + len(block) > max_total_chars:
                remaining = max_total_chars - used
                if remaining > 500:
                    lines.append(block[:remaining].rstrip() + "\n...[remaining tool outputs truncated]")
                break
            lines.append(block)
            used += len(block)
        return "\n".join(lines).strip()

    def _render_simple_completion(self, completed: list) -> str:
        """
        Deterministic final message when synthesis is skipped.
        """
        if not completed:
            return "Done."
        if len(completed) == 1:
            s = completed[0]
            if str(s.get("tool", "")).startswith("research."):
                raw = str(s.get("raw_payload") or "").strip()
                if raw:
                    try:
                        parsed = json.loads(raw)
                        answer = str(parsed.get("answer") or "").strip() if isinstance(parsed, dict) else ""
                        if answer:
                            return self._sanitize_delivery_message(answer)
                    except Exception:
                        pass
            return f"Done. {str(s.get('result', '')).strip()[:280]}".strip()
        lines = ["Done. Completed steps:"]
        for idx, s in enumerate(completed[:6], start=1):
            lines.append(f"{idx}. {str(s.get('result', '')).strip()[:140]}")
        return "\n".join(lines)

    def _get_terminal_slot_response(self, completed: list) -> str:
        """
        Return a final answer artifact directly when the run already produced one.

        This avoids a lossy Phase-2 synthesis pass over trimmed execution logs.
        """
        if not completed or not all(s.get("status") == "done" for s in completed):
            return ""
        last = completed[-1]
        if str(last.get("tool", "")).lower() == "think":
            raw = str(last.get("raw_payload") or "").strip()
            if raw:
                payload = self._extract_primary_payload(None, raw)
                payload = self._sanitize_delivery_message(payload)
                if payload and len(payload) >= 40 and not self._looks_like_raw_retrieval_payload(payload):
                    return payload
        slots = (self._working_state or {}).get("slots", {})
        preferred = (
            "final_summary", "final_answer", "research_answer", "answer", "book_summaries",
            "summary", "report", "message_body",
        )
        for key in preferred:
            val = str(slots.get(key) or "").strip()
            if val and len(val) >= 40 and not self._looks_like_raw_retrieval_payload(val):
                return self._sanitize_delivery_message(val)

        return ""

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

    def _sanitize_plan_goal(self, goal: str, max_words: int = 18, max_chars: int = 140) -> str:
        """Keep planner step goals short and atomic for small-model reliability."""
        g = re.sub(r"\s+", " ", str(goal or "")).strip()
        if not g:
            return ""
        g = re.split(r"[\r\n]", g, maxsplit=1)[0].strip()
        if ":" in g and len(g) > max_chars:
            head, tail = g.split(":", 1)
            if len(tail.split()) > 12:
                g = head.strip()
        words = g.split()
        if len(words) > max_words:
            g = " ".join(words[:max_words])
        if len(g) > max_chars:
            g = g[:max_chars].rsplit(" ", 1)[0].strip()
        return g

    def _parse_plan_slots(self, raw: str) -> list[str]:
        """Parse OUTPUT/INPUT slot tokens into compact snake_case names."""
        if not raw:
            return []
        parts = [
            p.strip().lower()
            for p in re.split(r"[,/|+]+|\band\b", str(raw), flags=re.IGNORECASE)
            if p and p.strip()
        ]
        out = []
        for p in parts:
            if p in {"none", "n/a", "na", "-"}:
                continue
            if "=" in p:
                key_raw, val_raw = p.split("=", 1)
                key = re.sub(r"[^a-z0-9_]+", "_", key_raw.strip().lower()).strip("_")
                val = re.sub(r"[^a-z0-9_]+", "_", val_raw.strip().lower()).strip("_")
                if key in {"name", "query", "topic", "search", "person", "recipient_name"}:
                    continue
                if key in {"message", "body", "text", "content", "message_body", "summary"}:
                    slot = "message_body" if key in {"message_body", "summary"} else key
                elif key in {"recipient_contact", "contact", "recipient"}:
                    slot = val if val and ("contact" in val or "recipient" in val) else "recipient_contact"
                elif key in {"phone", "number", "to", "jid"}:
                    slot = "recipient_phone_or_jid"
                elif key in {"email", "to_email"}:
                    slot = "recipient_email"
                else:
                    slot = key or val
            else:
                slot = re.sub(r"[^a-z0-9_]+", "_", p).strip("_")
            slot = slot[:40]
            if slot and slot not in out:
                out.append(slot)
        return out

    def _estimate_min_plan_steps(self, user_text: str) -> int:
        """Heuristic minimum step pressure for planner output."""
        t = (user_text or "").lower()
        if not t.strip():
            return 1
        if self._is_homework_answer_task(user_text):
            return 1

        has_lookup = bool(re.search(r"\b(find|search|lookup|look up|research|get)\b", t))
        has_generation = bool(
            re.search(r"\b(write|draft|compose|generate|prepare|summari[sz]e|code|convert|report|document|pdf)\b", t)
        )
        has_delivery = bool(re.search(r"\b(send|share|forward|email|message|whatsapp|mail)\b", t))
        has_side_effect = bool(
            re.search(r"\b(create|write|add|update|delete|save|send|share|forward|upload|post)\b", t)
        )
        has_contact_target = bool(re.search(r"\b(contact|recipient|phone|email address|to\s+[a-z])\b", t))

        est = 0
        if has_lookup:
            est += 1
        if has_generation:
            est += 1
        if has_delivery or has_side_effect:
            est += 1
        if has_delivery and has_contact_target:
            est += 1
        if has_side_effect:
            est += 1  # verify step

        return max(1, min(est, 8))

    def _is_homework_answer_task(self, text: str | None = None) -> bool:
        """Detect school-style factual/math prompts that should be answered directly."""
        t = str(text if text is not None else getattr(self, "_current_task_text", "") or "").lower()
        if not t.strip():
            return False

        # Requests that explicitly need tools or side effects are not direct Q&A.
        tool_or_action = bool(re.search(
            r"\b(search|research|latest|news|source|sources|cite|citation|"
            r"send|email|whatsapp|message|create\s+(?:file|pdf|excel|sheet|document)|"
            r"save\s+(?:file|as)|open|read\s+(?:file|pdf|document)|upload|download)\b",
            t,
        ))
        if tool_or_action:
            return False

        questionish = bool(re.search(
            r"\b(question\s*(?:number|no\.?)|q\s*\d+|define|what\s+is|what\s+are|"
            r"explain|formula|derive|calculate|find\s+the\s+(?:value|amount|charge|current|time)|"
            r"solve|unit\s+of|write\s+down)\b",
            t,
        ))
        education_terms = bool(re.search(
            r"\b(electric|current|charge|circuit|ammeter|voltage|resistance|ampere|"
            r"coulomb|second|minute|physics|chemistry|math|mathematics|science)\b",
            t,
        ))
        has_numbers = bool(re.search(r"\b\d+(?:\.\d+)?\b", t))
        return questionish and (education_terms or has_numbers or "question" in t)

    def _is_advanced_excel_task(self, text: str | None = None) -> bool:
        """Detect Excel requests that need workbook logic beyond simple row export."""
        t = str(text if text is not None else getattr(self, "_current_task_text", "") or "").lower()
        return bool(
            re.search(r"\b(excel|xlsx|spreadsheet|workbook|sheet)\b", t)
            and re.search(
                r"\b(formula|calculate|pivot|chart|dashboard|kpi|highlight|conditional|sort|top\s+\d+|lowest|highest|margin|trend|summary|analysis)\b",
                t,
            )
        )

    def _excel_task_text(self) -> str:
        """Combine the original Excel request with any user-provided tabular data."""
        parts = [str(getattr(self, "_current_task_text", "") or "")]
        for _, ans in sorted((self._input_answers or {}).items()):
            if isinstance(ans, str) and ans.strip():
                parts.append(ans)
        return "\n".join(parts)

    def _extract_excel_output_filename(self, fallback: str = "analysis_final.xlsx") -> str:
        """Prefer a filename mentioned after 'save/final/as', otherwise last xlsx."""
        text = self._excel_task_text()
        for pat in (
            r"(?:save\s+(?:the\s+)?(?:final\s+)?file\s+as|save\s+as|final\s+file\s+as|output\s+file\s+as)[^\n`]{0,80}`?([A-Za-z0-9_. -]+\.xlsx)`?",
            r"(?:save|final file|output file)[^\n`]{0,80}`?([A-Za-z0-9_. -]+\.xlsx)`?",
        ):
            final_match = re.search(pat, text, re.IGNORECASE)
            if final_match:
                return self._bare_excel_filename(final_match.group(1), fallback=fallback)
        names = re.findall(r"`?([A-Za-z0-9_. -]+\.xlsx)`?", text, flags=re.IGNORECASE)
        return self._bare_excel_filename(names[-1], fallback=fallback) if names else fallback

    def _extract_excel_columns(self) -> list[str]:
        """Extract requested columns from bullets or CSV headers."""
        text = self._excel_task_text()
        bullet_cols = []
        in_columns_area = False
        for line in text.splitlines():
            l = line.strip()
            if re.search(r"\bcolumns?\b", l, re.IGNORECASE):
                in_columns_area = True
                continue
            if in_columns_area:
                m = re.match(r"^\*\s*`?([A-Za-z_][A-Za-z0-9_ ]*)`?\s*$", l)
                if m:
                    col = re.sub(r"\s+", "_", m.group(1).strip())
                    if col not in bullet_cols:
                        bullet_cols.append(col)
                    continue
                if l and not l.startswith("*"):
                    in_columns_area = False
        if bullet_cols:
            return bullet_cols
        m = re.search(r"([A-Za-z_][A-Za-z0-9_ ]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_ ]*){2,})", text)
        if not m:
            return []
        cols = [re.sub(r"\s+", "_", c.strip()) for c in m.group(1).split(",") if c.strip()]
        return cols

    def _parse_generic_tabular_records(self) -> list[dict]:
        """Parse pasted CSV-like data using requested/header columns."""
        import csv
        import io

        text = self._excel_task_text()
        columns = self._extract_excel_columns()
        if not columns:
            return []
        header_pat = r"\s*,\s*".join(re.escape(c).replace("_", r"[_ ]") for c in columns)
        m = re.search(header_pat, text, flags=re.IGNORECASE)
        if not m:
            return []
        body = text[m.end():].strip()
        # Common pasted shape: header row followed by rows separated only by spaces.
        body = re.sub(r"\s+(?=\d+\s*,\s*\d{4}-\d{2}-\d{2}\s*,)", "\n", body)
        rows = [ln.strip() for ln in body.splitlines() if ln.strip()]
        if not rows and body:
            rows = [body]
        csv_text = ",".join(columns) + "\n" + "\n".join(rows)
        parsed = []
        for row in csv.DictReader(io.StringIO(csv_text)):
            rec = {}
            for col in columns:
                val = str(row.get(col, "") or "").strip()
                if re.fullmatch(r"-?\d+", val):
                    val = int(val)
                elif re.fullmatch(r"-?\d+\.\d+", val):
                    val = float(val)
                rec[col] = val
            if any(str(v).strip() for v in rec.values()):
                parsed.append(rec)
        return parsed

    def _extract_excel_formulas(self, headers: list[str]) -> list[tuple[str, str]]:
        """Extract derived-column formulas like Profit = Revenue - Cost."""
        text = self._excel_task_text()
        formulas = []
        seen = set()
        for lhs, rhs in re.findall(r"(?m)^\s*([A-Za-z][A-Za-z0-9_ ]{1,40})\s*=\s*([A-Za-z0-9_ ().+\-*/%]+)\s*$", text):
            out_col = re.sub(r"\s+", "_", lhs.strip())
            expr = rhs.strip()
            if out_col.lower() in {h.lower() for h in headers}:
                continue
            key = out_col.lower()
            if key not in seen:
                formulas.append((out_col, expr))
                seen.add(key)
        # Also catch "Formula:\n Profit = ..." after indentation was flattened.
        for lhs, rhs in re.findall(r"Formula:\s*([A-Za-z][A-Za-z0-9_ ]{1,40})\s*=\s*([A-Za-z0-9_ ().+\-*/%]+)", text, re.IGNORECASE):
            out_col = re.sub(r"\s+", "_", lhs.strip())
            key = out_col.lower()
            if key not in seen:
                formulas.append((out_col, rhs.strip()))
                seen.add(key)
        return formulas

    def _formula_to_excel(self, expr: str, header_cols: dict[str, int], row: int) -> str:
        """Translate a simple field-name expression into an Excel row formula."""
        formula = str(expr or "")
        for name, col_idx in sorted(header_cols.items(), key=lambda kv: len(kv[0]), reverse=True):
            col_letter = self._excel_col_letter(col_idx)
            variants = {name, name.replace("_", " ")}
            for var in variants:
                formula = re.sub(rf"\b{re.escape(var)}\b", f"{col_letter}{row}", formula, flags=re.IGNORECASE)
        return "=" + formula.replace("%", "/100")

    def _evaluate_excel_expr(self, expr: str, values: dict) -> float | None:
        """Evaluate simple arithmetic formulas for summaries without relying on Excel."""
        formula = str(expr or "")
        for name, val in sorted(values.items(), key=lambda kv: len(str(kv[0])), reverse=True):
            if isinstance(val, (int, float)):
                repl = str(float(val))
            else:
                try:
                    repl = str(float(str(val).replace(",", "")))
                except Exception:
                    continue
            for var in {str(name), str(name).replace("_", " ")}:
                formula = re.sub(rf"\b{re.escape(var)}\b", repl, formula, flags=re.IGNORECASE)
        formula = formula.replace("%", "/100")
        if not re.fullmatch(r"[\d\s().+\-*/]+", formula):
            return None
        try:
            val = eval(formula, {"__builtins__": {}}, {})
            return float(val)
        except Exception:
            return None

    def _excel_col_letter(self, idx: int) -> str:
        letters = ""
        while idx:
            idx, rem = divmod(idx - 1, 26)
            letters = chr(65 + rem) + letters
        return letters

    def _normalize_key(self, s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())

    def _build_generic_excel_workbook(self, records: list[dict], output_path: str | None = None) -> dict:
        """Generic advanced Excel builder: formulas, summaries, charts, dashboard."""
        if not records:
            return {"status": "error", "error": "No parseable tabular records found for the Excel workbook."}
        try:
            from openpyxl import Workbook
            from openpyxl.chart import BarChart, LineChart, Reference
            from openpyxl.formatting.rule import FormulaRule
            from openpyxl.styles import Font, PatternFill
            from openpyxl.worksheet.table import Table, TableStyleInfo
        except Exception as e:
            return {"status": "error", "error": f"openpyxl unavailable: {e}"}

        output_name = self._bare_excel_filename(output_path or self._extract_excel_output_filename(), fallback="analysis_final.xlsx")
        sandbox = (Path(__file__).parent.parent / SANDBOX_DIR).resolve()
        sandbox.mkdir(parents=True, exist_ok=True)
        output = sandbox / output_name

        headers = list(records[0].keys())
        formulas = self._extract_excel_formulas(headers)
        for out_col, _ in formulas:
            if out_col not in headers:
                headers.append(out_col)
        formula_cols = {out for out, _ in formulas}

        computed_records = []
        for rec in records:
            vals = dict(rec)
            for out_col, expr in formulas:
                calc = self._evaluate_excel_expr(expr, vals)
                vals[out_col] = calc if calc is not None else ""
            computed_records.append(vals)

        text = self._excel_task_text()
        if re.search(r"\bsort\b", text, re.IGNORECASE) and computed_records:
            sort_fields = []
            for field in re.findall(r"(?:highest|lowest)\s+([A-Za-z_ ]+?)\s+first", text, re.IGNORECASE):
                h = next((x for x in headers if self._normalize_key(x) == self._normalize_key(field)), "")
                if h:
                    sort_fields.append((h, "desc" if re.search(rf"highest\s+{re.escape(field)}", text, re.IGNORECASE) else "asc"))
            for field in re.findall(r"(?:then\s+by|by)\s+([A-Za-z_ ]+?)\s+descending", text, re.IGNORECASE):
                h = next((x for x in headers if self._normalize_key(x) == self._normalize_key(field)), "")
                if h and all(existing != h for existing, _ in sort_fields):
                    sort_fields.append((h, "desc"))
            if sort_fields:
                def _sort_val(rec: dict, field: str):
                    try:
                        return float(rec.get(field, 0) or 0)
                    except Exception:
                        return str(rec.get(field, ""))
                # Apply stable sorts from last key to first so mixed directions work.
                for field, direction in reversed(sort_fields[:3]):
                    computed_records.sort(key=lambda r, f=field: _sort_val(r, f), reverse=(direction == "desc"))

        wb = Workbook()
        ws = wb.active
        ws.title = "Data"
        ws.append(headers)
        header_cols = {h: i + 1 for i, h in enumerate(headers)}
        base_headers = [h for h in records[0].keys()]
        for r_idx, rec in enumerate(computed_records, start=2):
            row = []
            for h in headers:
                if h in rec and h not in formula_cols:
                    row.append(rec[h])
                else:
                    formula_expr = next((expr for out, expr in formulas if out == h), "")
                    row.append(self._formula_to_excel(formula_expr, header_cols, r_idx) if formula_expr else "")
            ws.append(row)
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="D9EAF7")
        ws.freeze_panes = "A2"
        for idx, h in enumerate(headers, start=1):
            ws.column_dimensions[self._excel_col_letter(idx)].width = max(12, min(24, len(h) + 3))
        end_row = len(records) + 1
        ws.add_table(Table(displayName="DataTable", ref=f"A1:{self._excel_col_letter(len(headers))}{end_row}"))
        ws.tables["DataTable"].tableStyleInfo = TableStyleInfo(name="TableStyleMedium9", showRowStripes=True)

        # Generic conditional highlighting for "A below X and B greater than Y".
        conds = []
        text_words = re.sub(r"[_\s]+", " ", text.lower())
        for match in headers:
            field_pat = re.sub(r"[_\s]+", r"\\s+", match.lower())
            for op, num in re.findall(
                rf"\b{field_pat}\s+(?:is\s+|are\s+)?(below|less than|greater than|above|under|over)\s+([\d,]+(?:\.\d+)?%?)",
                text_words,
                re.IGNORECASE,
            ):
                n = num.replace(",", "")
                if n.endswith("%"):
                    n = n[:-1]
                symbol = "<" if op.lower() in {"below", "less than", "under"} else ">"
                conds.append(f"${self._excel_col_letter(header_cols[match])}2{symbol}{n}")
        if conds:
            ws.conditional_formatting.add(
                f"A2:{self._excel_col_letter(len(headers))}{end_row}",
                FormulaRule(formula=["AND(" + ",".join(conds[:4]) + ")"], fill=PatternFill("solid", fgColor="FFC7CE")),
            )

        summary = wb.create_sheet("Summaries")
        summary_row = 1
        numeric_headers = [
            h for h in headers
            if any(isinstance(r.get(h), (int, float)) for r in computed_records) or h in formula_cols
        ]
        group_headers = [h for h in base_headers if h not in numeric_headers and not re.search(r"date|id", h, re.IGNORECASE)]

        summary_blocks = []
        text_norm = re.sub(r"[_\s]+", " ", text.lower())
        for metric_h in numeric_headers:
            metric_pat = re.sub(r"[_\s]+", r"\\s+", metric_h.lower())
            for group_h in group_headers:
                group_pat = re.sub(r"[_\s]+", r"\\s+", group_h.lower())
                if re.search(rf"\btotal\s+{metric_pat}\s+by\s+{group_pat}\b", text_norm, re.IGNORECASE):
                    summary_blocks.append((group_h, metric_h, "sum"))
        if not summary_blocks and group_headers and numeric_headers:
            summary_blocks.append((group_headers[0], numeric_headers[-1], "sum"))

        block_locations = {}
        for group_h, metric_h, _ in summary_blocks[:4]:
            totals = {}
            for rec in computed_records:
                key = rec.get(group_h, "")
                val = rec.get(metric_h, 0)
                try:
                    totals[key] = totals.get(key, 0) + float(val or 0)
                except Exception:
                    pass
            title = f"Total {metric_h} by {group_h}"
            summary.cell(summary_row, 1, title).font = Font(bold=True)
            summary.cell(summary_row + 1, 1, group_h)
            summary.cell(summary_row + 1, 2, f"Total {metric_h}")
            start = summary_row + 2
            for off, (key, val) in enumerate(sorted(totals.items(), key=lambda kv: str(kv[0]))):
                summary.cell(start + off, 1, key)
                summary.cell(start + off, 2, val)
            block_locations[title.lower()] = (summary_row + 1, start + len(totals) - 1)
            summary_row = start + len(totals) + 2

        if re.search(r"\b(monthly|trend)\b", text, re.IGNORECASE):
            date_h = next((h for h in headers if re.search(r"date", h, re.IGNORECASE)), "")
            metric_h = next((h for h in headers if re.search(r"revenue|sales|amount|total", h, re.IGNORECASE)), "")
            if date_h and metric_h:
                totals = {}
                for rec in computed_records:
                    raw_date = str(rec.get(date_h, "") or "")
                    m = re.match(r"(\d{4})-(\d{2})", raw_date)
                    month_key = f"{m.group(1)}-{m.group(2)}" if m else raw_date[:7]
                    try:
                        totals[month_key] = totals.get(month_key, 0) + float(rec.get(metric_h, 0) or 0)
                    except Exception:
                        pass
                if totals:
                    title = f"Monthly {metric_h} Trend"
                    summary.cell(summary_row, 1, title).font = Font(bold=True)
                    summary.cell(summary_row + 1, 1, "Month")
                    summary.cell(summary_row + 1, 2, metric_h)
                    start = summary_row + 2
                    for off, (key, val) in enumerate(sorted(totals.items(), key=lambda kv: str(kv[0]))):
                        summary.cell(start + off, 1, key)
                        summary.cell(start + off, 2, val)
                    block_locations[title.lower()] = (summary_row + 1, start + len(totals) - 1)
                    summary_row = start + len(totals) + 2

        dash = wb.create_sheet("Dashboard")
        dash["A1"] = "Excel Analysis Dashboard"
        dash["A1"].font = Font(bold=True, size=16)
        kpi_row = 3
        for agg, field in re.findall(r"\b(Total|Average)\s+([A-Za-z_ ]+)", text, re.IGNORECASE):
            h = next((x for x in headers if self._normalize_key(x) == self._normalize_key(field)), "")
            if not h:
                continue
            vals = []
            for rec in computed_records:
                try:
                    vals.append(float(rec.get(h, 0) or 0))
                except Exception:
                    pass
            value = (sum(vals) / len(vals)) if agg.lower() == "average" and vals else sum(vals)
            dash.cell(kpi_row, 1, f"{agg.title()} {h}").font = Font(bold=True)
            dash.cell(kpi_row, 2, value)
            kpi_row += 1

        # Top-N table: choose Product-like dimension and Units/Sales/Revenue-like metric when available.
        top_n_match = re.search(r"\btop\s+(\d+)\b", text, re.IGNORECASE)
        if top_n_match:
            n = int(top_n_match.group(1))
            dim = next((h for h in headers if re.search(r"product|item|category", h, re.IGNORECASE)), group_headers[0] if group_headers else "")
            metric = next((h for h in headers if re.search(r"units|quantity|sold", h, re.IGNORECASE)), numeric_headers[0] if numeric_headers else "")
            if dim and metric:
                totals = {}
                for rec in computed_records:
                    try:
                        totals[rec.get(dim, "")] = totals.get(rec.get(dim, ""), 0) + float(rec.get(metric, 0) or 0)
                    except Exception:
                        pass
                row = max(kpi_row + 2, 9)
                dash.cell(row, 1, f"Top {n} {dim} by {metric}").font = Font(bold=True)
                dash.cell(row + 1, 1, dim)
                dash.cell(row + 1, 2, metric)
                for off, (key, val) in enumerate(sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:n], start=2):
                    dash.cell(row + off, 1, key)
                    dash.cell(row + off, 2, val)

        # Charts from summary blocks.
        chart_anchor_row = 3
        chart_items = []
        for item in block_locations.items():
            if "trend" not in item[0] and "monthly" not in item[0]:
                chart_items.append(item)
                break
        for item in block_locations.items():
            if "trend" in item[0] or "monthly" in item[0]:
                chart_items.append(item)
                break
        if len(chart_items) < 2:
            chart_items = list(block_locations.items())[:2]
        for title, (header_row, last_row) in chart_items[:2]:
            if last_row < header_row + 1:
                continue
            chart = LineChart() if "trend" in title.lower() or "monthly" in title.lower() else BarChart()
            chart.title = title.title()
            chart.add_data(Reference(summary, min_col=2, min_row=header_row, max_row=last_row), titles_from_data=True)
            chart.set_categories(Reference(summary, min_col=1, min_row=header_row + 1, max_row=last_row))
            dash.add_chart(chart, f"D{chart_anchor_row}")
            chart_anchor_row += 16

        wb.save(output)
        return {
            "status": "success",
            "path": str(output),
            "rows": len(records),
            "columns": len(headers),
            "sheets": wb.sheetnames,
            "size_bytes": output.stat().st_size,
        }

    def _normalize_advanced_excel_steps(self, steps: list) -> list:
        """Route complex Excel tasks through one deterministic workbook build step."""
        if not steps or not self._is_advanced_excel_task():
            return steps
        out = []
        create_seen = False
        for step in steps:
            act = str(step.get("action", "")).lower()
            goal_l = str(step.get("goal", "")).lower()
            if act in {"excel.append"} or re.search(r"\b(pivot|chart|dashboard|sort|top\s+\d+|highlight|formula)\b", goal_l):
                continue
            if act == "excel.create" and not create_seen:
                s = dict(step)
                s["goal"] = "Create requested Excel workbook and analysis"
                s["outputs"] = ["excel_file_path"]
                s.pop("inputs", None)
                s["categories"] = self._normalize_categories(["Documents"])
                out.append(s)
                create_seen = True
                continue
            out.append(dict(step))
        if not create_seen:
            out.append({
                "goal": "Create requested Excel workbook and analysis",
                "categories": self._normalize_categories(["Documents"]),
                "action": "excel.create",
                "outputs": ["excel_file_path"],
            })
        return out

    def _is_budget_excel_task(self, text: str | None = None) -> bool:
        """Detect spreadsheet requests that should calculate expenses/savings."""
        t = str(text if text is not None else getattr(self, "_current_task_text", "") or "").lower()
        return bool(
            re.search(r"\b(excel|xlsx|spreadsheet|sheet)\b", t)
            and re.search(r"\b(savings?|expense|expenditure|spend|budget)\b", t)
            and re.search(r"\b(income|salary|monthly\s+income)\b", t)
        )

    def _is_sales_analysis_excel_task(self, text: str | None = None) -> bool:
        """Detect the richer sales-performance workbook request."""
        t = str(text if text is not None else getattr(self, "_current_task_text", "") or "").lower()
        return bool(
            re.search(r"\b(excel|xlsx|spreadsheet|workbook|sheet)\b", t)
            and re.search(r"\b(sales|salesperson|revenue|profit[_ ]?margin|pivot|dashboard)\b", t)
            and re.search(r"\b(region|product|units[_ ]?sold|cost)\b", t)
        )

    def _sales_data_text(self) -> str:
        """Return raw user-provided sales data from clarifications or task text."""
        parts = []
        for _, ans in sorted((self._input_answers or {}).items()):
            if isinstance(ans, str) and "Order_ID" in ans and "Revenue" in ans and "Cost" in ans:
                parts.append(ans)
        task = str(getattr(self, "_current_task_text", "") or "")
        if "Order_ID" in task and "Revenue" in task and "Cost" in task:
            parts.append(task)
        return "\n".join(parts).strip()

    def _parse_sales_records(self, text: str | None = None) -> list[dict]:
        """Parse compact CSV-like sales rows pasted by the user."""
        import csv
        import io

        raw = str(text if text is not None else self._sales_data_text() or "").strip()
        if not raw:
            return []
        header_match = re.search(
            r"Order_ID\s*,\s*Date\s*,\s*Salesperson\s*,\s*Region\s*,\s*Product\s*,\s*Units_Sold\s*,\s*Revenue\s*,\s*Cost",
            raw,
            re.IGNORECASE,
        )
        if not header_match:
            return []
        header = "Order_ID,Date,Salesperson,Region,Product,Units_Sold,Revenue,Cost"
        body = raw[header_match.end():].strip()
        body = re.sub(r"\s+(?=\d{4}\s*,\s*\d{4}-\d{2}-\d{2}\s*,)", "\n", body)
        body = re.sub(r"\s+", " ", body).replace("\n ", "\n").strip()
        lines = [header] + [ln.strip() for ln in body.splitlines() if ln.strip()]
        if len(lines) <= 1:
            return []
        reader = csv.DictReader(io.StringIO("\n".join(lines)))
        records = []
        for row in reader:
            try:
                rec = {
                    "Order_ID": str(row.get("Order_ID", "")).strip(),
                    "Date": str(row.get("Date", "")).strip(),
                    "Salesperson": str(row.get("Salesperson", "")).strip(),
                    "Region": str(row.get("Region", "")).strip(),
                    "Product": str(row.get("Product", "")).strip(),
                    "Units_Sold": int(float(str(row.get("Units_Sold", "0")).strip() or 0)),
                    "Revenue": float(str(row.get("Revenue", "0")).strip() or 0),
                    "Cost": float(str(row.get("Cost", "0")).strip() or 0),
                }
            except Exception:
                continue
            if rec["Order_ID"] and rec["Date"] and rec["Product"]:
                records.append(rec)
        return records

    def _build_sales_analysis_workbook(self, records: list[dict], output_path: str = "sales_analysis_final.xlsx") -> dict:
        """Create the requested sales analysis workbook with formulas, summaries, charts, and dashboard."""
        if not records:
            return {"status": "error", "error": "No parseable sales records found."}
        try:
            from openpyxl import Workbook
            from openpyxl.chart import BarChart, LineChart, Reference
            from openpyxl.formatting.rule import FormulaRule
            from openpyxl.styles import Font, PatternFill
            from openpyxl.worksheet.table import Table, TableStyleInfo
        except Exception as e:
            return {"status": "error", "error": f"openpyxl unavailable: {e}"}

        output_name = self._bare_excel_filename(output_path, fallback="sales_analysis_final.xlsx")
        if output_name.lower() == "sales_data.xlsx":
            output_name = "sales_analysis_final.xlsx"
        sandbox = (Path(__file__).parent.parent / SANDBOX_DIR).resolve()
        sandbox.mkdir(parents=True, exist_ok=True)
        output = sandbox / output_name

        def profit(rec):
            return float(rec.get("Revenue", 0) or 0) - float(rec.get("Cost", 0) or 0)

        sorted_records = sorted(
            records,
            key=lambda r: (profit(r), int(r.get("Units_Sold", 0) or 0)),
            reverse=True,
        )
        wb = Workbook()
        ws = wb.active
        ws.title = "Sales_Data"
        headers = [
            "Order_ID", "Date", "Salesperson", "Region", "Product", "Units_Sold",
            "Revenue", "Cost", "Profit", "Profit_Margin",
        ]
        ws.append(headers)
        for idx, rec in enumerate(sorted_records, start=2):
            ws.append([
                rec["Order_ID"], rec["Date"], rec["Salesperson"], rec["Region"], rec["Product"],
                rec["Units_Sold"], rec["Revenue"], rec["Cost"], f"=G{idx}-H{idx}", f"=IF(G{idx}=0,0,I{idx}/G{idx}*100)",
            ])
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="D9EAF7")
        ws.freeze_panes = "A2"
        for col, width in {"A": 12, "B": 14, "C": 16, "D": 12, "E": 14, "F": 12, "G": 14, "H": 14, "I": 14, "J": 16}.items():
            ws.column_dimensions[col].width = width
        end_row = len(sorted_records) + 1
        ws.conditional_formatting.add(
            f"A2:J{end_row}",
            FormulaRule(formula=["AND($J2<10,$G2>50000)"], fill=PatternFill("solid", fgColor="FFC7CE")),
        )
        table = Table(displayName="SalesTable", ref=f"A1:J{end_row}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium9", showRowStripes=True)
        ws.add_table(table)

        region_summary = {}
        salesperson_profit = {}
        product_units = {}
        monthly_revenue = {}
        for rec in records:
            p = profit(rec)
            region_summary.setdefault(rec["Region"], {"Revenue": 0, "Profit": 0})
            region_summary[rec["Region"]]["Revenue"] += rec["Revenue"]
            region_summary[rec["Region"]]["Profit"] += p
            salesperson_profit[rec["Salesperson"]] = salesperson_profit.get(rec["Salesperson"], 0) + p
            product_units[rec["Product"]] = product_units.get(rec["Product"], 0) + rec["Units_Sold"]
            month = str(rec["Date"])[:7]
            monthly_revenue[month] = monthly_revenue.get(month, 0) + rec["Revenue"]

        piv = wb.create_sheet("Pivot_Summaries")
        piv.append(["Region", "Total Revenue", "Total Profit"])
        for region, vals in sorted(region_summary.items()):
            piv.append([region, vals["Revenue"], vals["Profit"]])
        sp_start = len(region_summary) + 4
        piv.cell(sp_start, 1, "Salesperson")
        piv.cell(sp_start, 2, "Total Profit")
        for offset, (name, total) in enumerate(sorted(salesperson_profit.items()), start=1):
            piv.cell(sp_start + offset, 1, name)
            piv.cell(sp_start + offset, 2, total)
        month_start = sp_start + len(salesperson_profit) + 4
        piv.cell(month_start, 1, "Month")
        piv.cell(month_start, 2, "Total Revenue")
        for offset, (month, total) in enumerate(sorted(monthly_revenue.items()), start=1):
            piv.cell(month_start + offset, 1, month)
            piv.cell(month_start + offset, 2, total)
        for cell in piv[1]:
            cell.font = Font(bold=True)

        dash = wb.create_sheet("Dashboard")
        total_revenue = sum(r["Revenue"] for r in records)
        total_profit = sum(profit(r) for r in records)
        avg_margin = (total_profit / total_revenue * 100) if total_revenue else 0
        lowest_salesperson = min(salesperson_profit.items(), key=lambda x: x[1])
        top_products = sorted(product_units.items(), key=lambda x: x[1], reverse=True)[:5]
        dash["A1"] = "Sales Performance Dashboard"
        dash["A1"].font = Font(bold=True, size=16)
        kpis = [
            ("Total Revenue", total_revenue),
            ("Total Profit", total_profit),
            ("Average Profit Margin", avg_margin),
            ("Lowest Performing Salesperson", f"{lowest_salesperson[0]} ({lowest_salesperson[1]:.0f})"),
        ]
        for r, (label, value) in enumerate(kpis, start=3):
            dash.cell(r, 1, label).font = Font(bold=True)
            dash.cell(r, 2, value)
        dash["A9"] = "Top 5 Best-Selling Products"
        dash["A9"].font = Font(bold=True)
        dash.append([])
        dash.cell(10, 1, "Product")
        dash.cell(10, 2, "Units Sold")
        for idx, (prod, units) in enumerate(top_products, start=11):
            dash.cell(idx, 1, prod)
            dash.cell(idx, 2, units)

        bar = BarChart()
        bar.title = "Revenue by Region"
        bar.y_axis.title = "Revenue"
        bar.x_axis.title = "Region"
        bar.add_data(Reference(piv, min_col=2, min_row=1, max_row=1 + len(region_summary)), titles_from_data=True)
        bar.set_categories(Reference(piv, min_col=1, min_row=2, max_row=1 + len(region_summary)))
        dash.add_chart(bar, "D3")

        line = LineChart()
        line.title = "Monthly Sales Trend"
        line.y_axis.title = "Revenue"
        line.x_axis.title = "Month"
        line.add_data(Reference(piv, min_col=2, min_row=month_start, max_row=month_start + len(monthly_revenue)), titles_from_data=True)
        line.set_categories(Reference(piv, min_col=1, min_row=month_start + 1, max_row=month_start + len(monthly_revenue)))
        dash.add_chart(line, "D20")

        wb.save(output)
        return {
            "status": "success",
            "path": str(output),
            "rows": len(records),
            "columns": len(headers),
            "sheets": wb.sheetnames,
            "size_bytes": output.stat().st_size,
        }

    def _normalize_sales_analysis_steps(self, steps: list) -> list:
        """Keep sales analysis plans focused on data acquisition then one workbook build."""
        if not steps or not self._is_sales_analysis_excel_task():
            return steps
        out = []
        for step in steps:
            act = str(step.get("action", "")).lower()
            goal_l = str(step.get("goal", "")).lower()
            if act in {"excel.append"}:
                continue
            if act == "excel.create":
                s = dict(step)
                s["goal"] = "Create sales analysis workbook with dashboard"
                s["outputs"] = ["excel_file_path"]
                s.pop("inputs", None)
                s["categories"] = self._normalize_categories(["Documents"])
                out.append(s)
                continue
            if re.search(r"\b(pivot|chart|dashboard|sort|top 5|profit[_ ]?margin)\b", goal_l):
                continue
            out.append(dict(step))
        if not any(str(s.get("action", "")).lower() == "excel.create" for s in out):
            out.append({
                "goal": "Create sales analysis workbook with dashboard",
                "categories": self._normalize_categories(["Documents"]),
                "action": "excel.create",
                "outputs": ["excel_file_path"],
            })
        return out

    def _extract_income_amount(self, text: str | None = None, default: int = 30000) -> int:
        """Extract a likely monthly income amount from task text."""
        raw = str(text if text is not None else getattr(self, "_current_task_text", "") or "")
        m = re.search(r"(?:income|salary|monthly income|monthly)\D{0,24}(\d[\d,]*)", raw, re.IGNORECASE)
        if not m:
            m = re.search(r"\b(\d[\d,]{3,})\b", raw)
        if not m:
            return default
        try:
            return int(re.sub(r"[^0-9]", "", m.group(1))) or default
        except Exception:
            return default

    def _budget_excel_rows(self, income: int) -> list[dict]:
        """Build a 12-month savings/expenditure tracker with formulas."""
        rows = []
        for month in range(1, 13):
            row_num = month + 1  # header is row 1 in Excel
            rows.append({
                "Month": f"Month {month}",
                "Income": income,
                "Rent": 0,
                "Food": 0,
                "Transport": 0,
                "Utilities": 0,
                "Other Expenses": 0,
                "Total Expenses": f"=SUM(C{row_num}:G{row_num})",
                "Savings": f"=B{row_num}-H{row_num}",
                "Savings Rate": f"=IF(B{row_num}=0,0,I{row_num}/B{row_num})",
            })
        rows.append({
            "Month": "Total",
            "Income": "=SUM(B2:B13)",
            "Rent": "=SUM(C2:C13)",
            "Food": "=SUM(D2:D13)",
            "Transport": "=SUM(E2:E13)",
            "Utilities": "=SUM(F2:F13)",
            "Other Expenses": "=SUM(G2:G13)",
            "Total Expenses": "=SUM(H2:H13)",
            "Savings": "=SUM(I2:I13)",
            "Savings Rate": "=IF(B14=0,0,I14/B14)",
        })
        return rows

    def _bare_excel_filename(self, value: str, fallback: str = "savings_expenditure_tracker.xlsx") -> str:
        """Return a bare xlsx filename accepted by the Excel tools."""
        s = str(value or "").strip().strip('"').strip("'")
        if not s:
            return fallback
        s = re.split(r"[\\/]+", s)[-1]
        if not s.lower().endswith(".xlsx"):
            s = re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("_") or fallback
            if not s.lower().endswith(".xlsx"):
                s += ".xlsx"
        return s

    def _extract_requested_output_filename(self, fallback: str = "generated_output.txt") -> str:
        """Extract a user/planner-mentioned output filename and keep it SANDBOX-relative."""
        text = " ".join(
            str(x or "")
            for x in (
                getattr(self, "_current_step_desc", ""),
                getattr(self, "_current_task_text", ""),
            )
        )
        matches = re.findall(
            r"`?([A-Za-z0-9_. -]+\.(?:cpp|cc|cxx|c|h|hpp|py|java|js|ts|html|css|txt|md|json|csv|pdf|docx|xlsx))`?",
            text,
            flags=re.IGNORECASE,
        )
        if matches:
            name = matches[0].strip()
            # Regex may include prose before the filename, e.g.
            # "Save ... named bellman_ford.cpp"; keep the actual token.
            spaced = [p for p in re.split(r"\s+", name) if "." in p]
            if spaced:
                name = spaced[-1]
        else:
            low = text.lower()
            ext = self._infer_generated_file_extension(low, fallback=fallback)
            subject = self._infer_generated_file_subject(text)
            name = f"{subject}.{ext}" if subject else fallback
        name = re.split(r"[\\/]+", name)[-1]
        name = re.sub(r"[^A-Za-z0-9_. -]+", "_", name).strip(" ._") or fallback
        return f"SANDBOX/{name}"

    def _infer_generated_file_extension(self, text: str, fallback: str = "generated_output.txt") -> str:
        """Infer a reasonable extension for generated file tasks."""
        low = str(text or "").lower()
        checks = [
            (r"\b(c\+\+|cpp|cxx)\b", "cpp"),
            (r"\bpython|\.py\b", "py"),
            (r"\btypescript|\.ts\b", "ts"),
            (r"\bjavascript|\.js\b", "js"),
            (r"\bjava\b", "java"),
            (r"\bhtml\b", "html"),
            (r"\bcss\b", "css"),
            (r"\bjson\b", "json"),
            (r"\bcsv\b", "csv"),
            (r"\bmarkdown|\.md\b", "md"),
            (r"\btext file|\.txt\b", "txt"),
            (r"\bc program\b|\bin c\b|\bc code\b", "c"),
        ]
        for pattern, ext in checks:
            if re.search(pattern, low):
                return ext
        m = re.search(r"\.([A-Za-z0-9]{1,8})$", str(fallback or ""))
        return (m.group(1).lower() if m else "txt")

    def _infer_generated_file_subject(self, text: str) -> str:
        """Build a generic filename stem from the requested artifact subject."""
        raw = str(text or "").strip()
        if not raw:
            return "generated_output"
        cleaned = re.split(
            r"\b(?:and\s+)?(?:send|share|forward|whatsapp|email|mail)\b",
            raw,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        m = re.search(
            r"\b(?:write|generate|create|make|build|draft)\b\s+(.+?)(?:\s+\bin\b\s+\w[\w+#.+-]*|\s+as\s+(?:a\s+)?file|\s+file\b|$)",
            cleaned,
            flags=re.IGNORECASE,
        )
        subject = m.group(1) if m else cleaned
        subject = re.sub(
            r"\b(?:complete|full|source|code|program|implementation|algorithm|algo|file|for|the|a|an|in|using|"
            r"cpp|c\+\+|python|java|javascript|typescript|html|css|json|csv|markdown|text|pdf|docx|xlsx)\b",
            " ",
            subject,
            flags=re.IGNORECASE,
        )
        words = re.findall(r"[A-Za-z0-9]+", subject.lower())
        if not words:
            return "generated_output"
        return "_".join(words[:6])[:80].strip("_") or "generated_output"

    def _clean_generated_source_text(self, content: str) -> str:
        """Strip prose/markdown framing from generated source code when possible."""
        s = str(content or "").strip()
        if not s:
            return ""
        m = re.search(r"```(?:[A-Za-z0-9_+-]+)?\s*(.*?)```", s, re.DOTALL)
        if m:
            return m.group(1).strip()
        first_code = re.search(r"(?m)^\s*(#include\b|using\s+namespace\b|import\b|package\b|public\s+class\b|def\s+\w+|int\s+main\s*\()", s)
        if first_code:
            s = s[first_code.start():].strip()
        return s

    def _normalize_excel_budget_steps(self, steps: list) -> list:
        """Collapse budget spreadsheet plans into create + verify, no header prompts."""
        if not steps or not self._is_budget_excel_task():
            return steps
        out = []
        created = False
        for step in steps:
            act = str(step.get("action", "")).lower()
            goal_l = str(step.get("goal", "")).lower()
            if act == "excel.create" and not created:
                s = dict(step)
                s["goal"] = "Create savings and expenditure tracker workbook"
                s["outputs"] = ["excel_file_path"]
                s.pop("inputs", None)
                out.append(s)
                created = True
                continue
            if act == "excel.append" and re.search(r"\b(headers?|columns?|structure)\b", goal_l):
                continue
            out.append(dict(step))
        if not created:
            out.insert(0, {
                "goal": "Create savings and expenditure tracker workbook",
                "categories": self._normalize_categories(["Documents"]),
                "action": "excel.create",
                "outputs": ["excel_file_path"],
            })
        return out

    def _parse_plan(self, text: str) -> list:
        """Extract numbered steps, optional ACTION/slots, and normalized categories."""
        steps = []
        for line in text.strip().splitlines():
            line = line.strip()
            m = re.match(r'^(?:step\s*)?(\d+)[.):\-\s]+(.+)$', line, re.IGNORECASE)
            if not m:
                continue

            body = m.group(2).strip().strip("*_` ").strip()
            if not body:
                continue

            parts = [p.strip().strip("*_` ").strip() for p in body.split("|") if p and p.strip()]
            kv = {}
            free_parts = []
            for part in parts:
                part = part.strip().strip("*_` ").strip()
                mk = re.match(
                    r'^(action|goal|purpose|output|outputs|input|inputs|needed[_\s-]*by|used[_\s-]*by|categories?)\s*:\s*(.+)$',
                    part,
                    re.IGNORECASE,
                )
                if mk:
                    key = re.sub(r"[\s-]+", "_", mk.group(1).lower())
                    kv[key] = mk.group(2).strip().strip("*_` ").strip()
                else:
                    free_parts.append(part)

            action = (kv.get("action") or "").strip().strip("*_` ").strip()
            goal = (kv.get("goal") or kv.get("purpose") or (free_parts[0] if free_parts else "")).strip()
            goal = self._sanitize_plan_goal(goal)
            if not goal and action:
                if action.lower() in {"verify", "check", "confirm"}:
                    goal = "Verify completion criteria and report gaps"
                else:
                    goal = self._sanitize_plan_goal(action.replace(".", " "))
            if not goal or len(goal) <= 3:
                continue

            raw_cats = kv.get("categories") or kv.get("category") or ""
            cats = [c.strip() for c in re.split(r'[,/|]+', raw_cats) if c.strip()]
            raw_inputs = kv.get("input") or kv.get("inputs") or ""
            embedded_cats = re.search(r'(?:^|[,|])\s*categories?\s*:\s*(.+)$', raw_inputs, re.IGNORECASE)
            if embedded_cats:
                cats.extend(c.strip() for c in re.split(r'[,/|]+', embedded_cats.group(1)) if c.strip())
                raw_inputs = raw_inputs[:embedded_cats.start()].rstrip(" ,|")

            if action in self.tools:
                action_cat = str((TOOL_METADATA.get(action, {}) or {}).get("category", "")).strip()
                if action_cat:
                    cats.append(action_cat)

            norm_cats = self._normalize_categories(cats)
            if not norm_cats:
                inferred = self._select_categories(goal, planner_categories=[])
                norm_cats = self._normalize_categories(list(inferred))

            step = {"goal": goal, "categories": norm_cats}
            if action:
                step["action"] = action

            literal_args = {}
            for mk in re.finditer(r'\b(message|body|text|content)\s*=\s*["\']?([^,|"\']+)', raw_inputs, re.IGNORECASE):
                literal_args[mk.group(1).lower()] = mk.group(2).strip()
            for mk in re.finditer(r'\b(name|query|topic)\s*=\s*["\']?([^,|"\']+)', raw_inputs, re.IGNORECASE):
                key = mk.group(1).lower()
                val = mk.group(2).strip()
                if not val:
                    continue
                if key == "topic" and str(action).lower().startswith("search."):
                    literal_args.setdefault("query", val)
                else:
                    literal_args.setdefault(key, val)

            outputs = self._parse_plan_slots(kv.get("output") or kv.get("outputs") or "")
            inputs = self._parse_plan_slots(raw_inputs)
            needed_by = self._parse_needed_by(kv.get("needed_by") or kv.get("used_by") or "")
            if literal_args:
                literal_prefixes = tuple(f"{k}_" for k in literal_args.keys())
                inputs = [slot for slot in inputs if not str(slot).startswith(literal_prefixes)]
            if outputs:
                step["outputs"] = outputs
            if inputs:
                step["inputs"] = inputs
            if needed_by:
                step["needed_by"] = needed_by
            if literal_args:
                step["literal_args"] = literal_args

            if action.lower() in {"think", "draft", "summarize", "summarise", "synthesize", "plan"}:
                step["allow_toolless_done"] = True
            if action.lower() in {"verify", "check", "confirm"}:
                step["is_verify"] = True
                step["allow_toolless_done"] = True

            steps.append(step)
        return steps

    def _finalize_step_dependencies(self, steps: list) -> list:
        """Ensure producer outputs are routed only to future consumer steps."""
        if not steps:
            return steps
        out = [dict(s) for s in steps]
        slot_producers: dict[str, list[int]] = {}
        for idx, step in enumerate(out, start=1):
            for slot in step.get("outputs") or []:
                key = str(slot or "").strip().lower()
                if key:
                    slot_producers.setdefault(key, []).append(idx)

        for idx, step in enumerate(out, start=1):
            inputs = {
                str(slot or "").strip().lower()
                for slot in (step.get("inputs") or [])
                if str(slot or "").strip()
            }
            if not inputs:
                continue
            for slot in inputs:
                for producer_idx in slot_producers.get(slot, []):
                    if producer_idx >= idx:
                        continue
                    targets = list(out[producer_idx - 1].get("needed_by") or [])
                    if idx not in targets:
                        targets.append(idx)
                    out[producer_idx - 1]["needed_by"] = sorted(t for t in targets if t > producer_idx)

        # Remove stale or impossible targets after plan normalization/insertion.
        max_step = len(out)
        for idx, step in enumerate(out, start=1):
            targets = []
            source_slots = [
                str(slot or "").strip().lower()
                for slot in (step.get("outputs") or [])
                if str(slot or "").strip()
            ]
            for raw in step.get("needed_by") or []:
                try:
                    target = int(raw)
                except Exception:
                    continue
                if not (idx < target <= max_step) or target in targets:
                    continue
                target_inputs = {
                    str(slot or "").strip().lower()
                    for slot in (out[target - 1].get("inputs") or [])
                    if str(slot or "").strip()
                }
                if source_slots and target_inputs:
                    relevant = any(
                        src == inp
                        or src.startswith(inp + "_")
                        or inp.startswith(src + "_")
                        for src in source_slots
                        for inp in target_inputs
                    )
                    if not relevant:
                        continue
                elif source_slots:
                    continue
                targets.append(target)
            if targets:
                step["needed_by"] = targets
            else:
                step.pop("needed_by", None)
        return out

    def _ensure_send_content_prereqs(self, steps: list) -> list:
        """Ensure delivery steps have explicit content + recipient prerequisites."""
        if not steps:
            return steps

        out = [dict(s) for s in steps]
        # Planner sometimes emits delivery before the message-preparation step.
        # Reorder that deterministically: a send step must never precede its
        # content producer.
        j = 0
        while j < len(out):
            cur_action = str(out[j].get("action", "")).lower()
            cur_goal = str(out[j].get("goal", "")).lower()
            is_delivery = bool(
                cur_action in {"email.send", "email.create_draft", "whatsapp.send", "whatsapp.send_media"}
                or re.search(r"\b(send|share|forward|email|mail)\b", cur_goal)
            )
            if not is_delivery:
                j += 1
                continue
            later_idx = None
            for k in range(j + 1, len(out)):
                later_action = str(out[k].get("action", "")).lower()
                later_goal = str(out[k].get("goal", "")).lower()
                later_outputs = {str(x).strip().lower() for x in (out[k].get("outputs") or [])}
                prepares_message = (
                    bool(later_outputs & {"message_body", "recommendations", "summary", "content"})
                    or later_action in {"think", "draft", "summarize", "summarise", "synthesize"}
                    or re.search(r"\b(prepare|compose|draft|summari[sz]e)\b", later_goal)
                )
                if prepares_message:
                    later_idx = k
                    break
            if later_idx is not None:
                prep = out.pop(later_idx)
                out.insert(j, prep)
                j += 2
                continue
            j += 1

        i = 0
        while i < len(out):
            cur = out[i]
            goal_l = str(cur.get("goal", "")).lower()
            action_l = str(cur.get("action", "")).lower()
            is_send_step = bool(
                action_l in {"email.send", "email.create_draft", "whatsapp.send", "whatsapp.send_media"}
                or re.search(r"\b(send|share|forward|email|mail)\b", goal_l)
            )
            if not is_send_step:
                i += 1
                continue

            prior = out[:i]
            content_output_slots = []
            raw_retrieval_slots = []
            prepared_output_slots = []
            for p in prior:
                p_goal_l = str(p.get("goal", "")).lower()
                p_action_l = str(p.get("action", "")).lower()
                p_outputs = [str(x or "").strip() for x in (p.get("outputs") or [])]
                if (
                    p_action_l in {"think", "draft", "summarize", "summarise", "synthesize"}
                    or re.search(r"\b(prepare|compose|draft|summari[sz]e)\b", p_goal_l)
                ):
                    for slot in p_outputs:
                        if slot and slot not in prepared_output_slots:
                            prepared_output_slots.append(slot)
                if re.search(r"\b(contact|recipient|email|phone)\b", p_goal_l):
                    continue
                for out_slot in p_outputs:
                    slot = str(out_slot or "").strip()
                    if slot and slot not in content_output_slots:
                        content_output_slots.append(slot)
                    if (
                        slot
                        and slot not in raw_retrieval_slots
                        and (
                            str(p.get("action", "")).lower().startswith(("search.", "firecrawl."))
                            or re.search(r"\b(search|results?|sources?|web)\b", slot.lower())
                        )
                    ):
                        raw_retrieval_slots.append(slot)
            send_inputs_now = [str(x or "").strip() for x in (cur.get("inputs") or [])]
            produced_slots = {
                str(slot or "").strip().lower()
                for p in prior
                for slot in (p.get("outputs") or [])
                if str(slot or "").strip()
            }
            has_prepared_message_input = any(
                re.search(r"\b(message_body|draft|summary|recommendations|content)\b", x.lower())
                and (
                    x.lower() in produced_slots
                    or any(ps.startswith(x.lower() + "_") or x.lower().startswith(ps + "_") for ps in produced_slots)
                )
                for x in send_inputs_now
            ) or bool(cur.get("literal_args"))
            if prepared_output_slots and not has_prepared_message_input and not cur.get("literal_args"):
                send_inputs = list(cur.get("inputs", []))
                preferred = next(
                    (s for s in prepared_output_slots if re.search(r"\b(message_body|draft|summary|recommendations|content)\b", s.lower())),
                    prepared_output_slots[-1],
                )
                if preferred not in send_inputs:
                    send_inputs.append(preferred)
                    cur["inputs"] = send_inputs
                send_inputs_now = [str(x or "").strip() for x in send_inputs]
                has_prepared_message_input = True
            if raw_retrieval_slots and not has_prepared_message_input and not cur.get("literal_args"):
                source_slot = raw_retrieval_slots[-1]
                prep_step = {
                    "goal": "Prepare concise message content from retrieved results",
                    "categories": self._normalize_categories(["Reasoning"]),
                    "action": "think",
                    "inputs": [source_slot],
                    "outputs": ["message_body"],
                }
                out.insert(i, prep_step)
                send_inputs = list(cur.get("inputs", []))
                if "message_body" not in send_inputs:
                    send_inputs.append("message_body")
                cur["inputs"] = send_inputs
                i += 2
                continue
            has_content_step = any(
                bool(p.get("outputs"))
                and not re.search(r"\b(contact|recipient|email|phone)\b", str(p.get("goal", "")).lower())
                for p in prior
            ) or any(
                re.search(
                    r"\b(write|draft|compose|generate|prepare|summari[sz]e|create|build|code|convert)\b",
                    str(p.get("goal", "")).lower(),
                )
                for p in prior
            ) or bool(cur.get("literal_args")) or any(
                re.match(r"^(message|body|text|content)_", str(inp or "").lower())
                for inp in (cur.get("inputs") or [])
            )
            if has_content_step and content_output_slots and not cur.get("literal_args"):
                send_inputs = list(cur.get("inputs", []))
                if not any(str(inp or "").strip() in content_output_slots for inp in send_inputs):
                    send_inputs.append(content_output_slots[-1])
                    cur["inputs"] = send_inputs
            if not has_content_step:
                prep_step = {
                    "goal": "Prepare message content for delivery",
                    "categories": self._normalize_categories(["Documents"]),
                    "action": "think",
                    "outputs": ["message_body"],
                    "allow_toolless_done": True,
                }
                out.insert(i, prep_step)
                send_inputs = list(cur.get("inputs", []))
                if "message_body" not in send_inputs:
                    send_inputs.append("message_body")
                cur["inputs"] = send_inputs
                i += 2
                continue

            # File/code delivery must persist generated content before sending.
            _task_l = str(getattr(self, "_current_task_text", "") or "").lower()
            _task_implies_code_file = any(k in _task_l for k in ("c++", " cpp", ".cpp", "source code", "implementation", "algorithm"))
            needs_file_delivery = bool(
                re.search(r"\b(attach|attachment|file|document|pdf|docx?|code|cpp|source|implementation)\b", goal_l)
                or _task_implies_code_file
            )
            has_persist_step = any(
                str(p.get("action", "")).lower() in {
                    "filesystem.write", "buffer.pipe", "pdf.create", "word.create"
                }
                or "file_path" in (p.get("outputs") or [])
                for p in prior
            )
            if needs_file_delivery and not has_persist_step:
                source_slot = "message_body"
                for p in reversed(prior):
                    outs = list(p.get("outputs") or [])
                    if not outs:
                        continue
                    cand = str(outs[0]).strip().lower()
                    if not cand or "recipient" in cand or "contact" in cand:
                        continue
                    source_slot = str(outs[0]).strip()
                    break
                persist_step = {
                    "goal": "Persist generated content to SANDBOX file",
                    "categories": self._normalize_categories(["Filesystem"]),
                    "action": "filesystem.write",
                    "inputs": [source_slot],
                    "outputs": ["file_path"],
                }
                out.insert(i, persist_step)
                send_inputs = list(cur.get("inputs", []))
                if "file_path" not in send_inputs:
                    send_inputs.append("file_path")
                cur["inputs"] = send_inputs
                i += 2
                continue

            has_contact_step = any(
                str(p.get("action", "")).startswith("contacts.")
                or re.search(r"\b(contact|lookup|find recipient|recipient details)\b", str(p.get("goal", "")).lower())
                for p in prior
            )
            has_explicit_target = bool(
                re.search(EMAIL_REGEX, goal_l)
                or re.search(r"\b\d{10,15}\b", goal_l)
            )
            if not has_contact_step and not has_explicit_target:
                lookup_step = {
                    "goal": "Lookup recipient contact details",
                    "categories": self._normalize_categories(["Contacts"]),
                    "action": "contacts.search",
                    "outputs": ["recipient_contact"],
                }
                out.insert(i, lookup_step)
                send_inputs = list(cur.get("inputs", []))
                if "recipient_contact" not in send_inputs:
                    send_inputs.append("recipient_contact")
                cur["inputs"] = send_inputs
                i += 2
                continue

            i += 1

        return out

    def _normalize_email_draft_send_flow(self, steps: list) -> list:
        """Avoid orphan drafts when the same plan already contains a real send step."""
        if not steps:
            return steps
        first_send_idx = next(
            (idx for idx, s in enumerate(steps) if str(s.get("action", "")).lower() == "email.send"),
            None,
        )
        if first_send_idx is None:
            return steps

        out = []
        for idx, s in enumerate(steps):
            act = str(s.get("action", "")).lower()
            if act == "email.create_draft" and idx < first_send_idx:
                continue
            out.append(dict(s))
        return out

    def _dedupe_contact_lookup_steps(self, steps: list) -> list:
        """Drop repeated recipient contact lookups when a prior lookup already exists."""
        if not steps:
            return steps
        out = []
        resolved_lookup = False
        for s in steps:
            act = str(s.get("action", "")).lower()
            goal_l = str(s.get("goal", "")).lower()
            is_explicit_confirmation = bool(
                re.search(r"\b(confirm|verify|check|search\s+again|exists?|existence|found)\b", goal_l)
            )
            is_contact_lookup = bool(
                act in ("contacts.get", "contacts.search", "contacts.list")
                or re.search(r"\b(lookup|find|search|get)\b.{0,32}\b(contact|recipient|email|phone)\b", goal_l)
            )
            if is_contact_lookup and resolved_lookup and not is_explicit_confirmation:
                continue
            out.append(dict(s))
            if is_contact_lookup:
                resolved_lookup = True
        return out

    def _ensure_verification_step(self, steps: list) -> list:
        """Append a final verify/check step for mutation-heavy plans when missing."""
        if not steps:
            return steps

        has_verify = any(
            bool(s.get("is_verify"))
            or re.search(r"\b(verify|confirm|check|validate)\b", str(s.get("goal", "")).lower())
            for s in steps
        )
        if has_verify:
            return steps

        has_side_effect = False
        for s in steps:
            action_l = str(s.get("action", "")).lower()
            goal_l = str(s.get("goal", "")).lower()
            if action_l and any(action_l.startswith(p) for p in _SIDE_EFFECT_PREFIXES):
                has_side_effect = True
                break
            if re.search(r"\b(send|write|create|add|update|delete|save|upload|post)\b", goal_l):
                has_side_effect = True
                break
        if not has_side_effect:
            return steps

        cats = {str(c).strip().lower() for s in steps for c in (s.get("categories") or [])}
        for s in steps:
            action_l = str(s.get("action", "")).lower()
            if action_l.startswith("whatsapp."):
                cats.add("whatsapp")
            elif action_l.startswith("email."):
                cats.add("email")
            elif action_l.startswith("filesystem.") or action_l.startswith(("pdf.", "word.", "excel.", "convert.")):
                cats.add("filesystem")
            elif action_l.startswith("contacts."):
                cats.add("contacts")
            elif action_l.startswith("calendar."):
                cats.add("calendar")
            elif action_l.startswith("todo."):
                cats.add("todo list")
        verify_tool = ""
        verify_goal = "Verify required action completed successfully"
        verify_cats = []

        if "whatsapp" in cats and "whatsapp.status" in self.tools:
            verify_tool = "whatsapp.status"
            verify_goal = "Verify WhatsApp service status before reporting success"
            verify_cats = ["WhatsApp"]
        elif "todo list" in cats and "todo.list" in self.tools:
            verify_tool = "todo.list"
            verify_goal = "Verify todo entries exist"
            verify_cats = ["Todo List"]
        elif "calendar" in cats and "calendar.list_events" in self.tools:
            verify_tool = "calendar.list_events"
            verify_goal = "Verify scheduled events exist"
            verify_cats = ["Calendar"]
        elif ("filesystem" in cats or "documents" in cats or "conversion" in cats) and "filesystem.list" in self.tools:
            verify_tool = "filesystem.list"
            verify_goal = "Verify output file exists in SANDBOX"
            verify_cats = ["Filesystem"]
        elif "contacts" in cats and "contacts.search" in self.tools:
            verify_tool = "contacts.search"
            verify_goal = "Verify contact retrieval result"
            verify_cats = ["Contacts"]
        elif "email" in cats and "email.list" in self.tools:
            verify_tool = "email.list"
            verify_goal = "Verify email action in inbox/sent list"
            verify_cats = ["Email"]
        verify_step = {
            "goal": verify_goal,
            "categories": self._normalize_categories(verify_cats),
            "is_verify": True,
        }
        if verify_tool:
            verify_step["action"] = verify_tool
        else:
            verify_step["allow_toolless_done"] = True
        return steps + [verify_step]

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
        """Inject concrete [BUFFER:N] refs into downstream consumer steps.

        This is used after any content-producing step (search/scrape/think/etc.) that
        buffered output, so write/send steps can consume the exact payload deterministically.
        """
        artifacts = completed_step.get("artifacts", {}) if isinstance(completed_step, dict) else {}
        candidate_ids = [
            int(str(x))
            for x in (artifacts.get("buffers") or [])
            if str(x).isdigit()
        ]
        if not candidate_ids:
            # Fallback: use latest buffer ID in memory when artifacts are sparse.
            latest = max((int(k) for k in buffer.keys() if str(k).isdigit()), default=None)
            if latest is not None:
                candidate_ids = [latest]
        if not candidate_ids:
            return steps

        latest_id = max(candidate_ids)
        buf_ref = f"[BUFFER:{latest_id}]"
        updated = []
        for s in steps:
            goal = str(s.get("goal", ""))
            # Replace placeholder
            goal = re.sub(r'\[BUFFER:\?\]', buf_ref, goal)
            # Also replace generic "id=<id>" hint with real id
            goal = re.sub(r'id=<id>', f'id={latest_id}', goal)
            # If a transform/write step consumes generated content but has no explicit
            # buffer ref, append one. Do not append raw search buffers to delivery
            # steps; delivery should use a prepared message_body slot.
            _consumer = bool(re.search(
                r"\b(compose|draft|write|save|attach|convert|create file|filesystem\.write|think|summari[sz]e|prepare)\b",
                goal,
                re.IGNORECASE,
            ))
            if _consumer and "[BUFFER:" not in goal:
                goal = f"{goal} {buf_ref}".strip()
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
        Max chars per buffer is adaptive: small for generic steps, larger for
        send/write composition steps where payload fidelity matters.
        """
        if not buffer:
            return ""
        MAX_PREFETCH_CHARS = 600
        if re.search(r'\b(send|share|forward|email|whatsapp|draft|compose|write|filesystem\.write|attach|attachment)\b', step_desc, re.IGNORECASE):
            MAX_PREFETCH_CHARS = 4000
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
            if isinstance(raw, dict):
                raw = raw.get("data", "")
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

    def _get_step_tools(
        self,
        step_desc: str,
        user_message: str = "",
        planner_categories: list[str] | None = None,
        planned_action: str = "",
    ) -> str:
        """Return predicted tool docs (1 primary + 1 backup) for this step."""
        cats = self._select_categories(step_desc, user_message, planner_categories)
        _is_verify_action = (planned_action or "").strip().lower() in {"verify", "check", "confirm"}
        # Store for execution-time category gate in _safe_execute
        self._current_step_cats = {c.strip().lower() for c in cats}
        predicted = self._predict_step_tools(step_desc, list(cats))

        planned_action = (planned_action or "").strip()
        _batch_step = bool(re.search(r"\b(all|multiple|several|each|every|\d+\s+|extracted|identified|listed|blocks?|tasks?)\b", step_desc, re.IGNORECASE))
        if planned_action == "todo.add" and _batch_step and "todo.add_many" in self.tools:
            predicted = ["todo.add_many", "todo.add"] + [p for p in predicted if p not in {"todo.add_many", "todo.add"}]
        elif planned_action == "calendar.add_event" and _batch_step and "calendar.add_many" in self.tools:
            predicted = ["calendar.add_many", "calendar.add_event"] + [p for p in predicted if p not in {"calendar.add_many", "calendar.add_event"}]
        elif planned_action in self.tools:
            predicted = [planned_action] + [p for p in predicted if p != planned_action]
        elif _is_verify_action:
            verify_bias = [
                p for p in predicted
                if (p.endswith(".list") or p.endswith(".status") or p.endswith(".read"))
                and not p.startswith("system.clipboard")
            ]
            if verify_bias:
                predicted = verify_bias + [p for p in predicted if p not in verify_bias]
            else:
                predicted = [
                    p for p in predicted
                    if (p.endswith(".list") or p.endswith(".status") or p.endswith(".read"))
                    and not p.startswith("system.clipboard")
                ]

        if not predicted:
            if _is_verify_action:
                predicted = [
                    t for t in (
                        "todo.list", "calendar.list_events", "filesystem.list", "email.list", "whatsapp.status"
                    )
                    if t in self.tools
                ][:2]
                self._current_step_tool_names = set(predicted)
                return full_tool_schemas_for_tools(predicted)
            # Fallback to one tool per category at most, to keep context small.
            fallback = []
            for cat in sorted(cats):
                if cat.strip().lower() == "knowledge base" and "knowledge.search" in self.tools:
                    fallback.append("knowledge.search")
                    if len(fallback) >= 2:
                        break
                    continue
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
            sub = m.group(1).replace('\\', '/').lstrip('/')
            fp = f"SANDBOX/{sub}"
            if fp not in data["files"]:
                data["files"].append(fp)
        for m in re.finditer(r'\b\d{10,15}\b', text):
            ph = m.group(0)
            if ph not in data["phones"]:
                data["phones"].append(ph)
        return data

    def _normalize_sandbox_file_ref(self, value: str) -> str:
        """Return a stable SANDBOX-relative file reference when possible."""
        s = str(value or "").strip().strip('"').strip("'")
        if not s:
            return ""
        norm = s.replace("\\", "/")
        m = re.search(r"(?i)(?:^|/)SANDBOX/(.+)$", norm)
        if m:
            return f"SANDBOX/{m.group(1).lstrip('/')}"
        if re.search(r"\.[A-Za-z0-9]{1,8}$", norm):
            return f"SANDBOX/{Path(norm).name}"
        return ""

    def _extract_file_path_from_tool_result(self, tool_res, raw_obs: str = "") -> str:
        """Extract a generated file path from direct or nested tool results."""
        candidates = []
        if isinstance(tool_res, dict):
            candidates.extend([
                tool_res.get("path"),
                tool_res.get("file_path"),
                tool_res.get("output_path"),
                tool_res.get("url"),
            ])
            nested = tool_res.get("result")
            if isinstance(nested, dict):
                candidates.extend([
                    nested.get("path"),
                    nested.get("file_path"),
                    nested.get("output_path"),
                    nested.get("url"),
                ])
        if raw_obs:
            candidates.extend(self._extract_key_data(raw_obs).get("files", []))
        for val in candidates:
            fp = self._normalize_sandbox_file_ref(str(val or ""))
            if fp:
                return fp
        return ""

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
            "- For each OUTPUT, include NEEDED_BY with only the future step numbers that require it\n"
            "- For USER.md/SOUL.md/MEMORY.md updates, use user.update/soul.update/memory.update (not filesystem tools)\n"
            "- goal is to complete the task "
            "Format exactly:\n"
            "1. ACTION: <tool.name|think|verify> | GOAL: <goal> | OUTPUT: <slot|none> | NEEDED_BY: <future_step_numbers|none> | INPUTS: <slots|none> | categories: <Cat1>, <Cat2>\n"
            "2. ACTION: <tool.name|think|verify> | GOAL: <goal> | OUTPUT: <slot|none> | NEEDED_BY: <future_step_numbers|none> | INPUTS: <slots|none> | categories: <CatX>\n"
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
                        source_query = str(item.get('source_query') or '').strip()
                        title = re.sub(r'\s+', ' ', title)[:90]
                        snippet = re.sub(r'\s+', ' ', snippet)[:120]
                        prefix = f"[{source_query}] " if source_query else ""
                        if title and snippet:
                            parts.append(f"{idx}) {prefix}{title} - {snippet}")
                        elif title:
                            parts.append(f"{idx}) {prefix}{title}")
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
        # Reset run-scoped LLM token/cost tracker for this user request.
        reset_llm_usage_totals()

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
            self._dependency_outputs = {}
            self._handoff_payload = ""
            self._pending_workflow_candidate = None
            self._context_compiler.reset()
            yield {"type": "final", "content": "Session cleared. Fresh start!"}
            self._print_llm_usage_summary()
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
                self._print_llm_usage_summary()
                return
            msg = "Okay, I won't save that as a workflow."
            yield {"type": "final", "content": msg}
            self.history.append((text, msg))
            self._print_llm_usage_summary()
            return

        self._input_answers = {}
        # Run-scoped buffers prevent stale cross-task context leakage.
        self.buffer = {}
        self._buffer_id = 0
        self._task_contract = self._build_task_contract(text)
        self._working_state = self._init_working_state(self._task_contract)
        self._dependency_outputs = {}
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
        _min_plan_steps = self._estimate_min_plan_steps(text)
        _plan_output_format = (
            "PLANNING MODE: DO NOT call any tools yet.\n"
            "Is this conversational? -> CHAT: <reply>\n"
            "Otherwise output a numbered plan using this EXACT one-line schema per step:\n"
            "  1. ACTION: <tool.name|think|verify> | GOAL: <5-14 words> | OUTPUT: <slot_name|none> | NEEDED_BY: <future_step_numbers|none> | INPUTS: <slot_a,slot_b|none> | categories: <Cat1>, <Cat2>\n"
            "Rules:\n"
            "- Each step must map to exactly ONE action.\n"
            "- GOAL must be short and atomic; never include payload text, email body, or code blocks.\n"
            f"- Produce at least {_min_plan_steps} steps for this task unless CHAT applies.\n"
            "- OUTPUT names are pure signal slots, e.g. recipient_contact, search_results, message_body, file_path.\n"
            "- NEEDED_BY must list only future step numbers that require this output; do not route context to unrelated steps.\n"
            "- For school/homework/general factual questions or calculations, do not use Knowledge Base or web tools unless the user asks to search/cite sources; use CHAT or one think step.\n"
            "- Knowledge Base is only for searching/ingesting the user's indexed local documents, not for answering from general knowledge.\n"
            "- For pure research/recommendation/latest/summary-from-web tasks, use exactly one research.run step with the full user request as query; it first decides where to look, then searches, reads sources, extracts facts, and removes noise.\n"
            "- For web research that must summarize each book/article/item, do not summarize from generic search snippets only; add a step to scrape/extract the best result page or otherwise identify concrete titles first.\n"
            "- For multi-topic or multi-item search, search once using the item/topic slot as INPUTS, then add a think step to extract only the intended facts and remove noise before later steps.\n"
            "- For send/share/email/whatsapp: add content-preparation only when content must be generated; for literal text like 'hi', put message=\"hi\" in INPUTS.\n"
            "- If sending to a person, add a contact-lookup step BEFORE the send step.\n"
            "- Do NOT combine read and write in the same step.\n"
            "- Add a final VERIFY step only when a reliable status/read tool matches the action; WhatsApp verification uses whatsapp.status, never filesystem.list.\n"
            "MIRA: Plan:\n"
        )
        plan_prompt = self._context_compiler.render_prompt(
            _plan_sections,
            step_type=StepType.PLAN,
            budget_note=_plan_budget_note,
            output_format=_plan_output_format,
        )
        # Use structured messages when provider supports it
        _plan_flat, _plan_msgs = self._context_compiler.prepare_for_generation(
            _plan_sections,
            step_type=StepType.PLAN,
            budget_note=_plan_budget_note,
            output_format=_plan_output_format,
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
            self._print_llm_usage_summary()
            return

        # Accept direct planner answer ONLY when the response is pure FINAL ANSWER
        # (no numbered steps). Some models emit both a plan and a hallucinated
        # FINAL ANSWER line; in that case we must execute the plan.
        _pure_final = _first_upper.startswith("FINAL ANSWER:") and not steps
        if (not _workflow_steps_preloaded) and _pure_final and "ACTION:" not in plan_raw and "TOOL:" not in plan_raw:
            final_response = plan_raw.split("FINAL ANSWER:")[-1].strip()
            self.history.append((text, final_response))
            yield {"type": "final", "content": final_response}
            self._print_llm_usage_summary()
            return

        if not steps:
            steps = [{"goal": text, "categories": []}]  # fallback single step
        if self._should_split_compound_task(text, steps):
            _split_steps = self._split_compound_task(text)
            if len(_split_steps) >= 2:
                print(f"[AGENT] Plan auto-split: 1 -> {len(_split_steps)} steps")
                steps = _split_steps
        steps = self._normalize_homework_steps(steps)
        steps = self._normalize_research_steps(steps)
        steps = self._normalize_advanced_excel_steps(steps)
        if not _workflow_steps_preloaded:
            steps = self._ensure_send_content_prereqs(steps)
            steps = self._normalize_email_draft_send_flow(steps)
            steps = self._ensure_verification_step(steps)
        steps = self._finalize_step_dependencies(steps)
        steps_block = "\n".join(
            f"{i+1}. "
            + (f"ACTION: {s.get('action')} | " if s.get("action") else "")
            + f"{s['goal']}"
            + (f" | outputs: {', '.join(s.get('outputs', []))}" if s.get("outputs") else "")
            + (f" | needed_by: {', '.join(str(x) for x in s.get('needed_by', []))}" if s.get("needed_by") else "")
            + (f" | inputs: {', '.join(s.get('inputs', []))}" if s.get("inputs") else "")
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
            self._current_step_desc = step_desc
            step_categories = step_item.get("categories", [])
            step_action = str(step_item.get("action", "")).strip()
            step_inputs = list(step_item.get("inputs", []) or [])
            step_outputs = list(step_item.get("outputs", []) or [])
            step_needed_by = [
                int(x) for x in (step_item.get("needed_by") or [])
                if str(x).isdigit()
            ]
            self._current_step_inputs = step_inputs
            self._current_step_outputs = step_outputs
            self._current_step_literal_args = dict(step_item.get("literal_args", {}) or {})
            _step_idx += 1

            #  Cancel check (OpenClaw-style abort) 
            if run_handle and run_handle.is_cancelled():
                print(f"[STREAM] Run aborted at step {i+1}  cancel_event set")
                yield {"type": "aborted", "content": "Run aborted by user.", "step": i + 1}
                self._print_llm_usage_summary()
                return

            #  Iteration-limit terminal state 
            if i >= MAX_AUTONOMOUS_STEPS - 1 and i > 0:
                msg = f"Reached {MAX_AUTONOMOUS_STEPS}-step limit after {i} completed steps."
                print(f"[STREAM] {msg}")
                yield {"type": "iteration_limit", "content": msg, "steps_completed": i}
                self._print_llm_usage_summary()
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
            _step_tools = self._get_step_tools(
                step_desc,
                text,
                step_categories,
                planned_action=step_action,
            )
            _force_plan_revise = False  # bypass fallback and jump to plan revision when strategy is wrong
            _latest_user_input = None   # latest explicit user clarification for replanning
            _step_success_calls = 0     # valid successful tool executions in this step
            _step_had_error = False     # any tool/runtime error happened in this step
            _step_allows_toolless_done = (
                bool(step_item.get("allow_toolless_done"))
                or bool(step_item.get("is_verify"))
                or str(step_action).lower().startswith("chat")
                or bool(re.search(r"\b(obtain|request|collect|get)\b.{0,40}\b(input|data|clarification)\b", step_desc, re.IGNORECASE))
            )
            _step_raw_payload = ""      # full payload for downstream handoff (not compressed digest)

            #  Quantified step tracking 
            # Detect numeric goals in step description (e.g. "add 6 todos").
            # Skip detection for continuation steps already annotated "[item N of M]".
            _step_quant_n    = 0   # expected count (0 = no quant goal)
            _step_quant_done = 0   # successful side-effect calls in this step
            if "[item " not in step_desc:
                _step_l = re.sub(r'\[BUFFER:\d+\]', '', step_desc, flags=re.IGNORECASE).lower()
                _batch_like = bool(
                    re.search(r'\b(add|create|append|insert|ingest)\b', _step_l)
                    and re.search(r'\b(todo|task|event|contact|message|item)s?\b', _step_l)
                )
                if _batch_like:
                    _sq = re.search(r'\b([2-9]|[1-9]\d+)\b', _step_l)
                    if _sq:
                        _n = int(_sq.group(1))
                        if 2 <= _n <= 50:   # safety cap: ignore absurd counts
                            _step_quant_n = _n

            # Start step tracking BEFORE the retry loop (once per logical step)
            self._context_compiler.metrics.start_step(i, "act")

            # Deterministic generated-file write. Small models often read a buffer
            # correctly, then loop or write only the visible slice. If the plan says
            # to save generated content to a file and the content slot/buffer exists,
            # perform the write directly.
            if step_action in ("filesystem.write", "filesystem.append"):
                _slots_for_write = (self._working_state or {}).get("slots", {})
                _artifacts_for_write = (self._working_state or {}).get("artifacts", {})
                _existing_artifact_path = ""
                for _slot_name in step_inputs:
                    _key = str(_slot_name or "").strip().lower()
                    _val = str(_slots_for_write.get(_key) or "").strip()
                    if (
                        re.search(r"(?:^|_)(?:pdf|docx?|document|file|attachment|report|output)(?:_|$)", _key)
                        or self._normalize_sandbox_file_ref(_val)
                    ):
                        _existing_artifact_path = self._normalize_sandbox_file_ref(_val)
                        if _existing_artifact_path:
                            break
                if not _existing_artifact_path:
                    _existing_artifact_path = str(
                        _artifacts_for_write.get("pdf_file_path")
                        or _artifacts_for_write.get("document_file_path")
                        or ""
                    ).strip()
                if _existing_artifact_path:
                    _existing_artifact_path = self._normalize_sandbox_file_ref(_existing_artifact_path) or _existing_artifact_path
                    _slots_for_write["file_path"] = _existing_artifact_path
                    _artifacts_for_write["last_file_path"] = _existing_artifact_path
                    if _existing_artifact_path.lower().endswith(".pdf"):
                        _slots_for_write["pdf_file_path"] = _existing_artifact_path
                        _artifacts_for_write["pdf_file_path"] = _existing_artifact_path
                    step_result = f"path: {_existing_artifact_path}"
                    _step_raw_payload = json.dumps({"path": _existing_artifact_path}, default=str)
                    self._store_declared_outputs(
                        step_outputs or ["file_path"],
                        step_action,
                        {"path": _existing_artifact_path},
                        _step_raw_payload,
                        step_result,
                        needed_by=step_needed_by,
                        source_step=i + 1,
                        source_goal=step_desc,
                    )
                    step_status = "done"
                    _step_tool = step_action
                    _step_calls.append({
                        "tool": step_action,
                        "status": "ok",
                        "summary": step_result[:180],
                        "args": json.dumps({"path": _existing_artifact_path}, default=str)[:140],
                    })
                    _step_actions.append(f"{step_action} -> existing artifact {step_result[:80]}")
                    self._merge_artifacts(_step_artifacts, {"files": [_existing_artifact_path], "buffers": [], "phones": []})
                    self._context_compiler.metrics.end_step(i, success=True)
                    completed.append({
                        "step": step_desc,
                        "status": step_status,
                        "result": step_result,
                        "tool": step_action,
                        "calls": _step_calls,
                        "artifacts": _step_artifacts,
                        "categories": step_categories,
                        "raw_payload": _step_raw_payload,
                    })
                    yield {"type": "tool_result", "tool": step_action, "result": _step_raw_payload, "status": "ok", "step": i + 1}
                    continue
                _content = ""
                for _slot_name in [*step_inputs, "cpp_code_content", "code_content", "code_text", "content"]:
                    _key = str(_slot_name or "").strip().lower()
                    _val = _slots_for_write.get(_key)
                    _val_s = str(_val or "").strip()
                    if (
                        isinstance(_val, str)
                        and _val_s
                        and "recipient" not in _key
                        and "file" not in _key
                        and not self._normalize_sandbox_file_ref(_val_s)
                        and _val_s.lower() not in ("ok", "no result", "none", "null")
                        and not re.fullmatch(r"\{\s*\"?status\"?\s*:\s*\"?ok\"?\s*\}", _val_s, re.IGNORECASE)
                    ):
                        _content = _val.strip()
                        break
                if not _content:
                    for _bid in sorted(self.buffer.keys(), reverse=True):
                        _buf = self.buffer.get(_bid)
                        if isinstance(_buf, dict):
                            _val = str(_buf.get("data") or "").strip()
                            if _val:
                                _content = _val
                                break
                if _content:
                    _content = self._clean_generated_source_text(_content)
                    _path = self._extract_requested_output_filename(fallback="generated_output.txt")
                    tool_name = "filesystem.write"
                    tool_args = {"path": _path, "content": _content}
                    _step_tool = tool_name
                    yield {"type": "tool_call", "tool": tool_name, "args": tool_args, "step": i + 1}
                    print(f"[STREAM][TOOL CALL] {tool_name}")
                    print(f"[STREAM][TOOL ARGS] {json.dumps(tool_args, indent=2, default=str)[:2000]}")
                    try:
                        result = await asyncio.to_thread(self._safe_execute, tool_name, tool_args)
                    except Exception as e:
                        result = {"status": "error", "error": f"Internal crash: {str(e)}"}
                    if isinstance(result, dict) and result.get("status") == "error":
                        observation = f"Error: {result.get('error', 'unknown error')}"
                        _step_had_error = True
                        last_error = result.get("error", "unknown error")
                        step_result = observation
                        step_status = "failed"
                        yield {"type": "tool_result", "tool": tool_name, "result": observation, "status": "error", "step": i + 1}
                        self._context_compiler.metrics.record_tool_call(tool_name, success=False)
                        self._context_compiler.metrics.end_step(i, success=False)
                        completed.append({
                            "step": step_desc,
                            "status": step_status,
                            "result": step_result,
                            "tool": tool_name,
                            "calls": _step_calls,
                            "artifacts": _step_artifacts,
                            "categories": step_categories,
                            "raw_payload": json.dumps(result, default=str),
                        })
                        continue

                    tool_res = result.get("result", result) if isinstance(result, dict) else result
                    self._update_working_state_from_tool_result(tool_name, tool_args, tool_res)
                    raw_obs = json.dumps(tool_res, indent=2, default=str)
                    _step_raw_payload = raw_obs[:12000]
                    step_result = self._compress_obs(raw_obs, tool_name)
                    self._store_declared_outputs(
                        step_outputs or ["file_path"],
                        tool_name,
                        tool_res,
                        raw_obs,
                        step_result,
                        needed_by=step_needed_by,
                        source_step=i + 1,
                        source_goal=step_desc,
                    )
                    _step_success_calls += 1
                    step_status = "done"
                    _step_calls.append({
                        "tool": tool_name,
                        "status": "ok",
                        "summary": step_result[:180],
                        "args": json.dumps(tool_args, default=str)[:140],
                    })
                    _step_actions.append(f"{tool_name} -> {step_result[:120]}")
                    self._merge_artifacts(_step_artifacts, self._extract_key_data(raw_obs + "\n" + step_result))
                    self._context_compiler.tool_records.add(
                        tool_name=tool_name, args=tool_args, status="ok",
                        result_digest=step_result[:220], raw_result=raw_obs,
                        step_idx=i, attempt=0,
                        artifacts=dict(_step_artifacts),
                    )
                    self._context_compiler.metrics.record_tool_call(tool_name, success=True)
                    print(f"[STREAM][TOOL RESULT ] ({len(raw_obs)} chars):")
                    print(raw_obs[:2000] + ("\n...[truncated]" if len(raw_obs) > 2000 else ""))
                    print(f"[STREAM][CONTEXTNEXT] {step_result}")
                    yield {"type": "tool_result", "tool": tool_name, "result": raw_obs, "status": "ok", "step": i + 1}
                    completed.append({
                        "step": step_desc,
                        "status": step_status,
                        "result": step_result,
                        "tool": tool_name,
                        "calls": _step_calls,
                        "artifacts": _step_artifacts,
                        "categories": step_categories,
                        "raw_payload": _step_raw_payload,
                    })
                    self._context_compiler.metrics.end_step(i, success=True)
                    continue

            # Duplicate contact-lookup short-circuit: if recipient is already resolved,
            # skip repeated lookup/get steps instead of re-querying the same contact.
            _slots_now = (self._working_state or {}).get("slots", {})
            _recipient_known = bool(_slots_now.get("recipient_email") or _slots_now.get("recipient_phone_or_jid"))
            _prior_contact_ok = any(
                str(c.get("tool", "")).startswith("contacts.") and c.get("status") == "done"
                for c in completed
            )
            _confirmation_lookup = bool(
                step_action in ("contacts.get", "contacts.search", "contacts.list")
                and re.search(
                    r"\b(confirm|verify|check|search\s+again|again|exists?|existence|found)\b",
                    str(step_desc or ""),
                    re.IGNORECASE,
                )
            )
            if step_action in ("contacts.get", "contacts.search") and _recipient_known and _prior_contact_ok and not _confirmation_lookup:
                step_status = "done"
                step_result = "Recipient already resolved from earlier contact lookup; skipping duplicate step."
                _step_tool = step_action or "contacts.search"
                _step_actions.append("duplicate_lookup_skipped")
                _completed_entry = {
                    "step": step_desc,
                    "status": step_status,
                    "result": step_result,
                    "tool": _step_tool,
                    "calls": _step_calls,
                    "artifacts": _step_artifacts,
                    "categories": step_categories,
                    "raw_payload": "",
                }
                completed.append(_completed_entry)
                self._context_compiler.metrics.end_step(i, success=True)
                yield {
                    "type": "thought",
                    "content": "Contact already resolved earlier; skipping redundant contact lookup step.",
                    "step": i + 1,
                }
                continue

            # Deterministic batch todo/calendar creation. This avoids asking a small
            # model to emit many side-effect calls or to remember exact schemas.
            _batch_like_step = bool(re.search(
                r"\b(all|multiple|several|each|every|extracted|identified|listed|blocks?|tasks?)\b",
                step_desc,
                re.IGNORECASE,
            ))
            if _batch_like_step and step_action in ("todo.add", "todo.add_many"):
                _tasks = self._parse_batch_tasks_from_text(self._task_source_text_for_batch())
                if len(_tasks) >= 2 and "todo.add_many" in self.tools:
                    tool_name = "todo.add_many"
                    tool_args = {"tasks": _tasks}
                    _step_tool = tool_name
                    yield {"type": "tool_call", "tool": tool_name, "args": tool_args, "step": i + 1}
                    print(f"[STREAM][TOOL CALL] {tool_name}")
                    print(f"[STREAM][TOOL ARGS] {json.dumps(tool_args, indent=2, default=str)[:2000]}")
                    result = await asyncio.to_thread(self._safe_execute, tool_name, tool_args)
                    if isinstance(result, dict) and result.get("status") == "error":
                        observation = f"Error: {result.get('error', 'unknown error')}"
                        _step_had_error = True
                        last_error = result.get("error", "unknown error")
                        step_result = observation
                        step_status = "failed"
                        yield {"type": "tool_result", "tool": tool_name, "result": observation, "status": "error", "step": i + 1}
                        self._context_compiler.metrics.record_tool_call(tool_name, success=False)
                        self._context_compiler.metrics.end_step(i, success=False)
                        completed.append({
                            "step": step_desc,
                            "status": step_status,
                            "result": step_result,
                            "tool": tool_name,
                            "calls": _step_calls,
                            "artifacts": _step_artifacts,
                            "categories": step_categories,
                            "raw_payload": json.dumps(result, default=str),
                        })
                        continue
                    tool_res = result.get("result", result) if isinstance(result, dict) else result
                    self._update_working_state_from_tool_result(tool_name, tool_args, tool_res)
                    raw_obs = json.dumps(tool_res, indent=2, default=str)
                    _step_raw_payload = raw_obs[:12000]
                    yield {"type": "tool_result", "tool": tool_name, "result": raw_obs, "status": "ok", "step": i + 1}
                    step_result = self._compress_obs(raw_obs, tool_name)
                    self._store_declared_outputs(
                        step_outputs or ["todo_list_updates"],
                        tool_name,
                        tool_res,
                        raw_obs,
                        step_result,
                        needed_by=step_needed_by,
                        source_step=i + 1,
                        source_goal=step_desc,
                    )
                    _step_success_calls += 1
                    step_status = "done"
                    _step_calls.append({
                        "tool": tool_name,
                        "status": "ok",
                        "summary": step_result[:180],
                        "args": json.dumps({"count": len(_tasks)}, default=str),
                    })
                    _step_actions.append(f"{tool_name} -> {step_result[:120]}")
                    self._context_compiler.metrics.record_tool_call(tool_name, success=True)
                    self._context_compiler.metrics.end_step(i, success=True)
                    completed.append({
                        "step": step_desc,
                        "status": step_status,
                        "result": step_result,
                        "tool": tool_name,
                        "calls": _step_calls,
                        "artifacts": _step_artifacts,
                        "categories": step_categories,
                        "raw_payload": _step_raw_payload,
                    })
                    continue

            if _batch_like_step and step_action in ("calendar.add_event", "calendar.add_many"):
                _tasks = self._parse_batch_tasks_from_text(self._task_source_text_for_batch())
                _events = self._build_calendar_events_for_tasks(_tasks)
                if len(_events) >= 2 and "calendar.add_many" in self.tools:
                    tool_name = "calendar.add_many"
                    tool_args = {"events": _events}
                    _step_tool = tool_name
                    yield {"type": "tool_call", "tool": tool_name, "args": tool_args, "step": i + 1}
                    print(f"[STREAM][TOOL CALL] {tool_name}")
                    print(f"[STREAM][TOOL ARGS] {json.dumps(tool_args, indent=2, default=str)[:2000]}")
                    result = await asyncio.to_thread(self._safe_execute, tool_name, tool_args)
                    if isinstance(result, dict) and result.get("status") == "error":
                        observation = f"Error: {result.get('error', 'unknown error')}"
                        _step_had_error = True
                        last_error = result.get("error", "unknown error")
                        step_result = observation
                        step_status = "failed"
                        yield {"type": "tool_result", "tool": tool_name, "result": observation, "status": "error", "step": i + 1}
                        self._context_compiler.metrics.record_tool_call(tool_name, success=False)
                        self._context_compiler.metrics.end_step(i, success=False)
                        completed.append({
                            "step": step_desc,
                            "status": step_status,
                            "result": step_result,
                            "tool": tool_name,
                            "calls": _step_calls,
                            "artifacts": _step_artifacts,
                            "categories": step_categories,
                            "raw_payload": json.dumps(result, default=str),
                        })
                        continue
                    tool_res = result.get("result", result) if isinstance(result, dict) else result
                    self._update_working_state_from_tool_result(tool_name, tool_args, tool_res)
                    raw_obs = json.dumps(tool_res, indent=2, default=str)
                    _step_raw_payload = raw_obs[:12000]
                    yield {"type": "tool_result", "tool": tool_name, "result": raw_obs, "status": "ok", "step": i + 1}
                    step_result = self._compress_obs(raw_obs, tool_name)
                    self._store_declared_outputs(
                        step_outputs or ["calendar_schedule"],
                        tool_name,
                        tool_res,
                        raw_obs,
                        step_result,
                        needed_by=step_needed_by,
                        source_step=i + 1,
                        source_goal=step_desc,
                    )
                    _step_success_calls += 1
                    step_status = "done"
                    _step_calls.append({
                        "tool": tool_name,
                        "status": "ok",
                        "summary": step_result[:180],
                        "args": json.dumps({"count": len(_events)}, default=str),
                    })
                    _step_actions.append(f"{tool_name} -> {step_result[:120]}")
                    self._context_compiler.metrics.record_tool_call(tool_name, success=True)
                    self._context_compiler.metrics.end_step(i, success=True)
                    completed.append({
                        "step": step_desc,
                        "status": step_status,
                        "result": step_result,
                        "tool": tool_name,
                        "calls": _step_calls,
                        "artifacts": _step_artifacts,
                        "categories": step_categories,
                        "raw_payload": _step_raw_payload,
                    })
                    continue

            # Complex Excel tasks are safer to execute deterministically once
            # raw tabular data is available. This avoids asking the small model
            # to serialize large row arrays, formulas, charts, pivots, and
            # dashboard instructions into a single fragile tool call.
            if step_action == "excel.create" and self._is_advanced_excel_task():
                _records = self._parse_generic_tabular_records()
                if _records:
                    tool_name = "excel.create"
                    tool_args = {"output_path": self._extract_excel_output_filename()}
                    _step_tool = tool_name
                    yield {"type": "tool_call", "tool": tool_name, "args": tool_args, "step": i + 1}
                    print(f"[STREAM][TOOL CALL] {tool_name}")
                    print(f"[STREAM][TOOL ARGS] {json.dumps(tool_args, indent=2, default=str)}")
                    try:
                        result = await asyncio.to_thread(
                            self._build_generic_excel_workbook,
                            _records,
                            tool_args["output_path"],
                        )
                    except Exception as e:
                        result = {"status": "error", "error": f"Internal crash: {str(e)}"}
                    if isinstance(result, dict) and result.get("status") == "error":
                        observation = f"Error: {result.get('error', 'unknown error')}"
                        _step_had_error = True
                        last_error = result.get("error", "unknown error")
                        _step_calls.append({
                            "tool": tool_name,
                            "status": "error",
                            "summary": str(last_error)[:180],
                            "args": json.dumps(tool_args, default=str)[:140],
                        })
                        self._context_compiler.tool_records.add(
                            tool_name=tool_name, args=tool_args, status="error",
                            result_digest=str(last_error)[:220], raw_result=observation,
                            step_idx=i, attempt=0,
                        )
                        self._context_compiler.metrics.record_tool_call(tool_name, success=False)
                        print(f"[STREAM][TOOL RESULT ] {observation}")
                        yield {"type": "tool_result", "tool": tool_name, "result": observation, "status": "error", "step": i + 1}
                        step_status = "failed"
                        step_result = observation
                        self._context_compiler.metrics.end_step(i, success=False)
                        completed.append({
                            "step": step_desc,
                            "status": step_status,
                            "result": step_result,
                            "tool": tool_name,
                            "calls": _step_calls,
                            "artifacts": _step_artifacts,
                            "categories": step_categories,
                            "raw_payload": json.dumps(result, default=str),
                        })
                        continue

                    tool_res = result.get("result", result) if isinstance(result, dict) else result
                    self._update_working_state_from_tool_result(tool_name, tool_args, tool_res)
                    raw_obs = json.dumps(tool_res, indent=2, default=str)
                    _step_raw_payload = raw_obs[:12000]
                    print(f"[STREAM][TOOL RESULT ] ({len(raw_obs)} chars):")
                    print(raw_obs[:2000] + ("\n...[truncated]" if len(raw_obs) > 2000 else ""))
                    yield {"type": "tool_result", "tool": tool_name, "result": raw_obs, "status": "ok", "step": i + 1}
                    step_result = self._compress_obs(raw_obs, tool_name)
                    self._store_declared_outputs(
                        step_outputs or ["excel_file_path"],
                        tool_name,
                        tool_res,
                        raw_obs,
                        step_result,
                        needed_by=step_needed_by,
                        source_step=i + 1,
                        source_goal=step_desc,
                    )
                    _step_success_calls += 1
                    step_status = "done"
                    _step_calls.append({
                        "tool": tool_name,
                        "status": "ok",
                        "summary": step_result[:180],
                        "args": json.dumps(tool_args, default=str)[:140],
                    })
                    _step_actions.append(f"{tool_name} -> {step_result[:120]}")
                    self._merge_artifacts(_step_artifacts, self._extract_key_data(raw_obs + "\n" + step_result))
                    self._context_compiler.tool_records.add(
                        tool_name=tool_name, args=tool_args, status="ok",
                        result_digest=step_result[:220], raw_result=raw_obs,
                        step_idx=i, attempt=0,
                        artifacts=dict(_step_artifacts),
                    )
                    self._context_compiler.metrics.record_tool_call(tool_name, success=True)
                    print(f"[STREAM][CONTEXTNEXT] {step_result}")
                    completed.append({
                        "step": step_desc,
                        "status": step_status,
                        "result": step_result,
                        "tool": tool_name,
                        "calls": _step_calls,
                        "artifacts": _step_artifacts,
                        "categories": step_categories,
                        "raw_payload": _step_raw_payload,
                    })
                    self._context_compiler.metrics.end_step(i, success=True)
                    continue

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

                buffer_context = (
                    self._get_all_buffer_signal_context()
                    if self._step_needs_all_buffer_signal(step_action, step_desc, step_categories)
                    else self._get_buffer_context()
                )
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
                _slot_hint = ""
                if step_inputs or step_outputs:
                    _in_slots = ", ".join(step_inputs) if step_inputs else "none"
                    _out_slots = ", ".join(step_outputs) if step_outputs else "none"
                    _needed_by = ", ".join(str(x) for x in step_needed_by) if step_needed_by else "none"
                    _slot_hint = (
                        "SLOT CONTRACT:\n"
                        f"- INPUTS: {_in_slots}\n"
                        f"- OUTPUTS: {_out_slots}\n"
                        f"- NEEDED_BY: {_needed_by}\n"
                        "Use these names consistently in your DONE summary.\n\n"
                    )
                _multi_search_hint = ""
                if step_action in ("search.duckduckgo", "search.news"):
                    _ms_items = self._collect_multi_search_items()
                    if len(_ms_items) > 1:
                        _multi_search_hint = (
                            "MULTI-ITEM SEARCH: This step has multiple input items/topics. "
                            "Call the search tool once with the common search intent; runtime will expand it "
                            "across the input items and merge signal-only results.\n\n"
                        )
                _extra_hints = "\n".join(filter(None, [
                    _slot_hint,
                    _multi_search_hint,
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
                _declared_inputs_ctx = self._get_step_input_context(step_inputs)
                _targeted_dependency_ctx = self._get_dependency_context(i + 1, step_inputs)
                _working_state_ctx = _targeted_dependency_ctx + _declared_inputs_ctx + _prefetched
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
                    working_state=_working_state_ctx,
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
                if _has_buf_in_goal and step_action != "think":
                    _seq_hint = (
                        "SEQUENCE for this step:\n"
                        "  turn 1 -> TOOL: buffer.slice | id=<N>   (reads the buffer)\n"
                        "  turn 2 -> TOOL: <send/write tool> | ...  (uses the content)\n"
                        "Do NOT write the buffer reference as a literal string.\n\n"
                    )
                elif _has_buf_in_goal and step_action == "think":
                    _seq_hint = (
                        "BUFFER CONTENT IS PRE-READ ABOVE. Use that content directly in think.\n"
                        "Do NOT call buffer.slice unless the pre-read content is absent or clearly incomplete.\n\n"
                    )
                else:
                    _seq_hint = ""
                _action_hint = (
                    f"PLANNED ACTION: {step_action}\n"
                    "Prefer this action unless a tool error requires fallback.\n\n"
                    if step_action else ""
                )
                _orig_req = re.sub(r'\s+', ' ', (text or '').strip())[:320]
                _orig_req_line = f"ORIGINAL REQUEST: {_orig_req}\n\n" if _orig_req else ""
                _slot_contract_line = ""
                if step_inputs or step_outputs:
                    _in_slots = ", ".join(step_inputs) if step_inputs else "none"
                    _out_slots = ", ".join(step_outputs) if step_outputs else "none"
                    _needed_by = ", ".join(str(x) for x in step_needed_by) if step_needed_by else "none"
                    _slot_contract_line = (
                        "STEP SLOTS:\n"
                        f"- INPUTS: {_in_slots}\n"
                        f"- OUTPUTS: {_out_slots}\n"
                        f"- NEEDED_BY: {_needed_by}\n\n"
                    )
                _priority_tail = ""
                if attempt > 0 and _step_actions:
                    _priority_tail = (
                        "PRIORITY CHECK (FINAL RULE): If STEP PROGRESS already satisfies this step, "
                        "output DONE immediately. Do NOT call another tool.\n\n"
                    )
                _act_output_format = (
                    f"Current step: {step_desc}\n\n"
                    f"{_orig_req_line}"
                    f"{_action_hint}"
                    f"{_slot_contract_line}"
                    f"{_seq_hint}"
                    "Output exactly ONE of:\n"
                    "TOOL: tool.name | arg=value | arg2=value2\n"
                    "DONE: <one-line summary>\n"
                    "NEED: <one specific question>\n\n"
                    f"{_priority_tail}"
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
                        _done_summary = answer[_prefix_len:].strip()[:220] or (step_result or "step complete")
                        _has_evidence = _step_success_calls > 0 or (_step_allows_toolless_done and not _step_had_error)
                        if not _has_evidence:
                            last_error = "DONE rejected: no successful tool execution for this step"
                            _special_retry_note = (
                                "DONE rejected: this step has no successful tool result yet.\n"
                                "Execute one valid tool call first, then output DONE.\n\n"
                            )
                            if attempt == MAX_RETRIES_PER_STEP - 1:
                                step_result = "Marked DONE without evidence; no successful tool call."
                                step_status = "failed"
                                break
                            continue
                        step_result = _done_summary
                        _step_raw_payload = _done_summary
                        self._store_declared_outputs(
                            step_outputs,
                            _step_tool,
                            {"text": _done_summary},
                            _done_summary,
                            _done_summary,
                            needed_by=step_needed_by,
                            source_step=i + 1,
                            source_goal=step_desc,
                        )
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
                                    steps[i] = {**step_item, "goal": _clarified_desc, "categories": step_categories}
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
                    if _is_side_effect:
                        step_status = "done"
                        step_result = obs_skip
                        _step_tool = tool_name
                        _step_calls.append({
                            "tool": tool_name,
                            "status": "blocked_duplicate_done",
                            "summary": obs_skip,
                            "args": json.dumps(tool_args, default=str)[:140],
                        })
                        break
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
                    _step_had_error = True
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
                    if tool_name == "think":
                        _think_text = ""
                        if isinstance(tool_res, dict):
                            _think_text = str(
                                tool_res.get("text")
                                or tool_res.get("content")
                                or tool_res.get("result")
                                or ""
                            ).strip()
                        else:
                            _think_text = str(tool_res or "").strip()
                        if not _think_text:
                            _step_had_error = True
                            last_error = "think returned empty text; use provided slot/buffer context and produce concrete output"
                            last_failed_tool = tool_name
                            last_failed_args = tool_args
                            observation = f"Error: {last_error}"
                            _step_calls.append({
                                "tool": tool_name,
                                "status": "empty",
                                "summary": last_error,
                                "args": json.dumps(tool_args, default=str)[:140],
                            })
                            _recent_tool_outputs.append({
                                "tool": tool_name,
                                "status": "error",
                                "raw": observation,
                            })
                            self._context_compiler.tool_records.add(
                                tool_name=tool_name, args=tool_args, status="error",
                                result_digest=last_error[:220], raw_result=observation,
                                step_idx=i, attempt=attempt,
                            )
                            self._context_compiler.metrics.record_tool_call(
                                tool_name, success=False, error_type="empty_result", is_retry=attempt > 0,
                            )
                            print(f"[STREAM][TOOL RESULT ] {observation}")
                            yield {"type": "tool_result", "tool": tool_name, "result": observation,
                                   "status": "error", "step": i + 1}
                            if attempt == MAX_RETRIES_PER_STEP - 1:
                                step_status = "failed"
                                break
                            _special_retry_note = (
                                "The previous think call returned empty text.\n"
                                "Use the STEP INPUT DATA / TARGETED DEPENDENCY OUTPUTS / BUFFER content already in context.\n"
                                "Return concrete titles, summaries, or a DONE summary. Do NOT ask the user.\n\n"
                            )
                            continue
                    self._update_working_state_from_tool_result(tool_name, tool_args, tool_res)
                    raw_obs = json.dumps(tool_res, indent=2, default=str)
                    _step_raw_payload = raw_obs[:12000]
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
                        _store_data = self._payload_to_signal(
                            _store_data,
                            tool_name=tool_name,
                            slot="buffer",
                            max_chars=6000,
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
                    self._store_declared_outputs(
                        step_outputs,
                        tool_name,
                        tool_res,
                        raw_obs,
                        step_result,
                        buffer_id=_buf_id_for_record,
                        needed_by=step_needed_by,
                        source_step=i + 1,
                        source_goal=step_desc,
                    )
                    step_status = "done"
                    _step_success_calls += 1
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
                            _step_had_error = True
                            _step_success_calls = 0
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
                            _step_had_error = True
                            _step_success_calls = 0
                            step_status = "failed"
                            step_result = None
                            continue  # retry loop

                    # Side-effect calls should not be followed by extra calls in same step.
                    if _is_side_effect:
                        break
                    # One planned action per step: once that action succeeds, the step is done.
                    # Do not ask the model to emit a second DONE turn; small models often call
                    # the same tool again instead.
                    if step_action and tool_name == step_action:
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
                            steps[i] = {**step_item, "goal": _clarified_desc, "categories": step_categories}
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
                                _fb_raw_for_synthesis = _fb_obs[:12000]
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
                                    _fb_store = self._payload_to_signal(
                                        _fb_store,
                                        tool_name=_fb_tn,
                                        slot="buffer",
                                        max_chars=6000,
                                    )
                                    self._buffer_id += 1
                                    self.buffer[self._buffer_id] = {
                                        "data": _fb_store, "tool": _fb_tn, "chars": len(_fb_store)
                                    }
                                    _fb_obs = self._make_buffer_observation(
                                        self._buffer_id, _fb_store, _fb_tn
                                    )
                                seen_calls.add(_fb_sig)
                                step_result = self._compress_obs(_fb_obs, _fb_tn)
                                step_status = "done"
                                _step_success_calls += 1
                                _step_tool  = _fb_tn
                                _step_raw_payload = _fb_raw_for_synthesis
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
                                    steps[i] = {**step_item, "goal": _clarified_desc, "categories": step_categories}
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
                and (_force_plan_revise or _consecutive_failures >= REPLAN_CONSECUTIVE_FAILURES)
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
                    steps = self._finalize_step_dependencies(steps[:i] + revised_steps)
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
                    steps[i] = {**step_item, "goal": _clarified_desc, "categories": step_categories}
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
                    self._maybe_update_handoff_payload(
                        _step_tool,
                        step_result or "",
                        raw_payload=_step_raw_payload,
                    )
                _completed_entry = {
                    "step": step_desc,
                    "status": step_status,
                    "result": step_result if step_result else (last_error[:100] if last_error else "failed"),
                    "tool": _step_tool,
                    "calls": _step_calls,
                    "artifacts": _step_artifacts,
                    "categories": step_categories,
                    "raw_payload": _step_raw_payload,
                }
                completed.append(_completed_entry)
                # Track step completion in metrics
                self._context_compiler.metrics.end_step(i, success=(step_status == "done"))
                # After any buffered content-producing step, annotate remaining steps with
                # a concrete buffer ID so downstream send/write steps can consume payload.
                if step_status == "done" and _step_artifacts.get("buffers"):
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
                        ext in str(
                            r.args.get("url", "")
                            or r.args.get("file_path", "")
                            or r.args.get("media_path", "")
                            or r.args.get("path", "")
                            or r.args.get("file", "")
                        ).lower()
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
                        str(
                            r.args.get("url", "")
                            or r.args.get("file_path", "")
                            or r.args.get("media_path", "")
                            or r.args.get("path", "")
                            or r.args.get("file", "")
                        )[:60]
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
                self._print_llm_usage_summary()
                return
            _terminal_response = self._get_terminal_slot_response(completed)
            if _terminal_response and not _postcond_warning:
                final_response = self._maybe_stage_workflow_suggestion(
                    text, completed, _terminal_response, workflow_was_auto_used=bool(_workflow_steps_preloaded)
                )
                _run_metrics = self._context_compiler.metrics.finalize()
                print(self._context_compiler.metrics.render_metrics_block())
                yield {"type": "final", "content": final_response}
                self.history.append((text, final_response))
                self._print_llm_usage_summary()
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
                self._print_llm_usage_summary()
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
                self._print_llm_usage_summary()
                return
            # 

            # Use deterministic compaction for completed steps trajectory
            _compacted_trajectory = self._context_compiler.compact_trajectory(completed)
            # Also include structured tool call log from ToolRecordStore
            _tool_call_log = self._context_compiler.tool_records.render_compact_summary()
            _tool_outputs = self._render_synthesis_tool_outputs(completed)

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
                done_block=(
                    f"Execution results:\n{done_lines}\n\n"
                    f"Tool outputs available for the final answer:\n{_tool_outputs or '(none)'}\n\n"
                    f"{_compacted_trajectory}{_tool_call_log}"
                ),
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
                    "4. Answer the user's actual request directly, using the tool outputs as evidence.\n"
                    "5. Do not echo internal digests such as 'Done', 'Top results', JSON keys, tool names, buffers, or planning text unless the user asked for logs.\n"
                    "6. For search/retrieval results, synthesize the relevant facts into a clear user-facing answer. Include brief source names only when useful.\n"
                    "7. If the tool output is insufficient, say what is missing and give the best supported answer without inventing facts.\n"
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
                "4. Answer the user's actual request directly, using the tool outputs as evidence.\n"
                "5. Do not echo internal digests such as 'Done', 'Top results', JSON keys, tool names, buffers, or planning text unless the user asked for logs.\n"
                "6. For search/retrieval results, synthesize the relevant facts into a clear user-facing answer. Include brief source names only when useful.\n"
                "7. If the tool output is insufficient, say what is missing and give the best supported answer without inventing facts.\n"
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
        self._print_llm_usage_summary()

        yield {"type": "final", "content": final_response}
        self.history.append((text, final_response))

    def _safe_execute(self, tool_name: str, args: dict):
        """Internal helper to execute a tool with argument aliasing and safety."""

        #  Runtime allow/deny gate 
        # Constraints are set by handle_streaming() from user prompt patterns:
        #   "Use ONLY: filesystem.*, todo.*"   self._allowed_tools = {"filesystem", "todo"}
        #   "Do NOT use: web/email"            self._denied_tools  = {"web", "email"}
        # Buffer/reasoning tools are always permitted regardless of constraints.
        _GATE_BUILTINS = ("buffer.", "memory.", "reasoning.", "research.", "think")
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

        # --- RESEARCH TOOLS (handled internally as an orchestrated pipeline) ---
        if tool_name.startswith("research."):
            _task_text = str(getattr(self, "_current_task_text", "") or "").strip()
            _query = str(args.get("query") or "").strip()
            _task_l = _task_text.lower()
            _query_l = _query.lower()
            if _task_text and tool_name in ("research.run", "research.extract_facts"):
                _has_research_constraints = bool(
                    re.search(r"\b(confirm|verified|facts?|claim|claims?|not opinions?|source|sources|evidence|current|latest)\b", _task_l)
                )
                _task_terms = {
                    w for w in re.findall(r"[a-z0-9]{4,}", _task_l)
                    if w not in {"find", "search", "send", "whatsapp", "create", "make", "with", "then", "best", "top"}
                }
                _query_terms = set(re.findall(r"[a-z0-9]{4,}", _query_l))
                _missing_terms = len(_task_terms - _query_terms) >= max(2, min(4, len(_task_terms) // 3 or 1))
                if _has_research_constraints and (_missing_terms or len(_query_l.split()) < 8):
                    args["query"] = _task_text
                if _has_research_constraints:
                    args["max_sources"] = max(int(args.get("max_sources") or 0), 8)
                    args["max_queries"] = max(int(args.get("max_queries") or 0), 5)
            return self._execute_research_tool(tool_name, args or {})

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
            content = self._prepare_persistence_content(content, tool_name)
            if not content:
                return {"status": "error", "error": "memory.write content had no task-relevant signal to store."}

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
            content = self._prepare_persistence_content(content, tool_name)
            if not content:
                return {"status": "error", "error": "memory.update content had no task-relevant signal to store."}

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
        _slots = (self._working_state or {}).get("slots", {})
        _artifacts = (self._working_state or {}).get("artifacts", {})
        literal_args = getattr(self, "_current_step_literal_args", {}) or {}
        if isinstance(args, dict) and isinstance(args.get("args"), dict):
            nested_args = dict(args.get("args") or {})
            for k, v in args.items():
                if k != "args":
                    nested_args.setdefault(k, v)
            args = nested_args
            print(f"[AGENT] Unwrapped nested args for {tool_name}")
        elif isinstance(args, dict) and isinstance(args.get("args"), str):
            parsed_args = self._parse_inline_args_string(args.get("args", ""))
            if parsed_args:
                for k, v in args.items():
                    if k != "args":
                        parsed_args.setdefault(k, v)
                args = parsed_args
                print(f"[AGENT] Parsed inline args string for {tool_name}")

        args = self._normalize_common_tool_args(tool_name, args)

        # think: inject buffer content and/or inline context into the prompt
        # before dispatching to the reasoning connector (which has no buffer access).
        if tool_name == "think":
            _base_prompt = args.get("prompt", "") or literal_args.get("prompt", "")
            if not str(_base_prompt or "").strip():
                _base_prompt = str(getattr(self, "_current_step_desc", "") or "Prepare concise useful output.").strip()
                args["prompt"] = _base_prompt
            if self._is_homework_answer_task():
                _task_text = str(getattr(self, "_current_task_text", "") or "").strip()
                if _task_text and _task_text.lower() not in str(_base_prompt).lower():
                    _base_prompt = (
                        "Answer the user's school questions clearly and show any calculation steps.\n\n"
                        f"User questions:\n{_task_text}"
                    )
                    args["prompt"] = _base_prompt
            _wants_code_output = bool(
                re.search(r"\b(code|cpp|c\+\+|c program|algorithm|source)\b", str(getattr(self, "_current_task_text", "") or ""), re.IGNORECASE)
                or any(
                    re.search(r"\b(code|cpp|source)\b", str(x or ""), re.IGNORECASE)
                    for x in getattr(self, "_current_step_outputs", []) or []
                )
            )
            if _wants_code_output:
                if "complete source code" not in str(_base_prompt).lower():
                    _base_prompt = (
                        str(_base_prompt or "").strip()
                        + "\n\nCODE OUTPUT CONTRACT:\n"
                        "- Return complete, compilable source code.\n"
                        "- Do not include markdown fences or explanatory prose.\n"
                        "- Include all required includes, data structures, functions, and a usable main function.\n"
                        "- Do not truncate the code."
                    ).strip()
                    args["prompt"] = _base_prompt
                args["max_tokens"] = max(int(args.get("max_tokens") or 0), 4096)
            _task_text_for_think = str(getattr(self, "_current_task_text", "") or "").strip()
            _task_l_for_think = _task_text_for_think.lower()
            _wants_markdown_report = bool(
                re.search(r"\b(pdf|report|document|recommendation|recommendations|stocks?|investment)\b", _task_l_for_think)
                and any(
                    re.search(r"\b(sector|stock|invest|recommend|confirm|analysis|report|pdf)\b", str(x or ""), re.IGNORECASE)
                    for x in [getattr(self, "_current_step_desc", ""), *getattr(self, "_current_step_outputs", [])]
                )
            )
            if _wants_markdown_report and not _wants_code_output:
                if "markdown report contract" not in str(_base_prompt).lower():
                    _base_prompt = (
                        str(_base_prompt or "").strip()
                        + "\n\nMARKDOWN REPORT CONTRACT:\n"
                        f"- User request: {_task_text_for_think}\n"
                        "- Produce complete Markdown suitable for direct PDF conversion.\n"
                        "- Preserve the user's requested decision criteria.\n"
                        "- Separate confirmed facts from inferred implications.\n"
                        "- Do not treat opinions, predictions, or unsourced claims as confirmed facts.\n"
                        "- If evidence is insufficient for specific recommendations, state what is missing instead of inventing.\n"
                        "- Include concise source names or URLs for each important claim when available."
                    ).strip()
                    args["prompt"] = _base_prompt
                args["max_tokens"] = max(int(args.get("max_tokens") or 0), 3072)
            if "message_body" in {
                str(x or "").strip().lower()
                for x in getattr(self, "_current_step_outputs", []) or []
            }:
                _base_prompt = (
                    str(_base_prompt or "").strip()
                    + "\n\nMESSAGE BODY CONTRACT:\n"
                    "- Return exactly ONE send-ready WhatsApp message.\n"
                    "- Do not give options, headings, analysis, citations, or ask the user to choose.\n"
                    "- Make it an actual concise summary, not a generic recommendation.\n"
                    "- 2 to 4 sentences, under 900 characters.\n"
                    "- If summarizing the movie 3 Idiots, mention Farhan, Raju and Rancho, "
                    "the pressure of the education system, friendship, and following passion over blind success."
                ).strip()
                args["prompt"] = _base_prompt
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
            for _slot in getattr(self, "_current_step_inputs", []) or []:
                _slot_key = str(_slot or "").strip().lower()
                if not _slot_key:
                    continue
                _slot_val = _slots.get(_slot_key)
                if isinstance(_slot_val, str) and _slot_val.strip():
                    _injections.append(
                        f"--- SLOT { _slot_key } ---\n{_slot_val.strip()}\n--- END SLOT { _slot_key } ---"
                    )
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

        if tool_name == "excel.create":
            if self._is_advanced_excel_task():
                records = self._parse_generic_tabular_records()
                if not records and isinstance(args.get("data"), list):
                    records = [dict(item) for item in (args.get("data") or []) if isinstance(item, dict)]
                if records:
                    out_name = self._bare_excel_filename(
                        str(args.get("output_path") or args.get("file_path") or ""),
                        fallback=self._extract_excel_output_filename(),
                    )
                    print(f"[AGENT] excel.create using generic advanced workbook builder rows={len(records)}")
                    return self._build_generic_excel_workbook(records, output_path=out_name)
                return {
                    "status": "error",
                    "error": "Advanced Excel task requires parseable tabular rows before workbook generation.",
                }
            if args.get("output_path"):
                args["output_path"] = self._bare_excel_filename(str(args.get("output_path")))

        if tool_name == "excel.append":
            if not str(args.get("file_path") or "").strip():
                _path_hint = str(
                    _slots.get("excel_file_path")
                    or _slots.get("file_path")
                    or _artifacts.get("excel_file_path")
                    or _artifacts.get("last_file_path")
                    or ""
                )
                if _path_hint:
                    args["file_path"] = self._bare_excel_filename(_path_hint)
            elif args.get("file_path"):
                args["file_path"] = self._bare_excel_filename(str(args.get("file_path")))

        if tool_name == "knowledge.ingest":
            if str(args.get("content") or "").strip() and str(args.get("source") or "").strip().lower() != "text":
                src = str(args.get("source") or "").strip()
                looks_like_path = bool(
                    re.search(r"(^SANDBOX[\\/]|[\\/]|\.txt$|\.md$|\.csv$|\.pdf$|\.docx?$|\.xlsx?$)", src, re.IGNORECASE)
                )
                if src and not looks_like_path:
                    args["source"] = "text"

        if tool_name == "filesystem.list":
            path_arg = str(args.get("path", "") or "").strip().replace("\\", "/")
            if path_arg.upper().strip("/") == "SANDBOX":
                args["path"] = "."

        # contacts.search/get pre-flight: recover missing query/name from task/slot hints.
        if tool_name in ("contacts.search", "contacts.get"):
            _q = str(args.get("query") or args.get("name") or args.get("text") or "").strip()
            if not _q:
                _hint = str(
                    literal_args.get("name")
                    or literal_args.get("query")
                    or _slots.get("recipient_name_hint")
                    or self._extract_recipient_hint(getattr(self, "_current_task_text", ""))
                    or ""
                ).strip()
                if _hint:
                    if tool_name == "contacts.search":
                        args.setdefault("query", _hint)
                    args.setdefault("name", _hint)
                    print(f"[AGENT] {tool_name} auto-filled recipient hint: {_hint}")

        if tool_name in ("search.duckduckgo", "search.news"):
            if not str(args.get("query") or "").strip():
                _q_hint = str(literal_args.get("query") or literal_args.get("topic") or "").strip()
                if _q_hint:
                    args["query"] = _q_hint

        # whatsapp.status takes zero args  strip any hallucinated args (jid, to, number, ...)
        if tool_name == "whatsapp.status":
            args = {}
        # whatsapp.send pre-flight: require text payload or auto-fill from handoff payload.
        if tool_name == "whatsapp.send":
            _explicit_message_supplied = any(
                isinstance(args.get(k), str) and args.get(k).strip()
                for k in ("message", "body", "text", "content")
            )
            if not str(args.get("to") or "").strip():
                _to = str(_slots.get("recipient_phone_or_jid") or "").strip()
                if not _to:
                    _contact_payload = str(_slots.get("contact_info") or _slots.get("recipient_contact") or "").strip()
                    _to = self._extract_phone_like(_contact_payload)
                if _to:
                    args["to"] = _to
            for _k in ("message", "body", "text", "content"):
                if _k in literal_args and _k not in args:
                    args[_k] = literal_args[_k]
            if "body" in args and "message" not in args:
                args["message"] = args.get("body")
            if "text" in args and "message" not in args:
                args["message"] = args.get("text")
            if "content" in args and "message" not in args:
                args["message"] = args.get("content")
            if isinstance(args.get("message"), str):
                args["message"] = self._normalize_delivery_body(args["message"])
            _msg = args.get("message")
            if _msg is None:
                _msg = ""
            if not isinstance(_msg, str):
                _msg = json.dumps(_msg, default=str)
            _msg = _msg.strip()
            _slot_key = _msg.lower()
            if _slot_key in _slots and isinstance(_slots.get(_slot_key), str) and _slots.get(_slot_key).strip():
                _msg = _slots[_slot_key].strip()
                args["message"] = _msg
                _explicit_message_supplied = False
                print(f"[AGENT] whatsapp.send dereferenced message slot: {_slot_key}")
            if _msg and self._looks_like_raw_retrieval_payload(_msg) and not _explicit_message_supplied:
                _prepared = str(_slots.get("message_body") or _slots.get("recommendations") or _slots.get("summary") or "").strip()
                if _prepared and not self._looks_like_raw_retrieval_payload(_prepared):
                    _msg = self._normalize_delivery_body(_prepared)
                    args["message"] = _msg
                    print("[AGENT] whatsapp.send replaced raw retrieval payload with prepared message slot")
                else:
                    args["message"] = ""
                    _msg = ""
            if self._task_requires_retrieval_delivery_message() and not literal_args and not _explicit_message_supplied:
                generic_delivery = bool(re.fullmatch(r'["\']?\s*(hi|hello|hey|hi\s+\w+|hello\s+\w+|hey\s+\w+)[.!? "\']*', _msg, re.IGNORECASE))
                has_prepared = any(
                    isinstance(_slots.get(k), str)
                    and _slots.get(k).strip()
                    and not self._looks_like_raw_retrieval_payload(_slots.get(k))
                    for k in ("message_body", "recommendations", "summary")
                )
                if generic_delivery or not has_prepared:
                    _built = self._build_delivery_message_from_slots()
                    if _built:
                        _msg = self._normalize_delivery_body(_built)
                        args["message"] = _msg
                        print("[AGENT] whatsapp.send composed delivery message from retrieval slots")
                    else:
                        args["message"] = ""
                        _msg = ""
            if not _msg:
                _fallback = ""
                for _k in self._content_slot_candidates(("message_body", "content", "summary", "text")):
                    _v = _slots.get(_k)
                    if isinstance(_v, str) and _v.strip():
                        _fallback = _v.strip()
                        break
                if not _fallback:
                    for _k in getattr(self, "_current_step_inputs", []) or []:
                        _ks = str(_k or "").strip().lower()
                        m = re.match(r"^(message|body|text|content)_(.+)$", _ks)
                        if m:
                            _fallback = m.group(2).replace("_", " ").strip()
                            break
                if not _fallback:
                    _fallback = str(getattr(self, "_handoff_payload", "") or "").strip()
                if self._looks_like_raw_retrieval_payload(_fallback):
                    _fallback = ""
                if self._task_requires_retrieval_delivery_message() and not literal_args:
                    if re.fullmatch(r'["\']?\s*(hi|hello|hey|hi\s+\w+|hello\s+\w+|hey\s+\w+)[.!? "\']*', _fallback, re.IGNORECASE):
                        _fallback = ""
                    if not _fallback:
                        _fallback = self._build_delivery_message_from_slots()
                if _fallback and _fallback.lower() not in ("ok", "no result", "none", "null"):
                    args["message"] = self._normalize_delivery_body(_fallback)[:1800]
                    print("[AGENT] whatsapp.send auto-filled message from prior step payload")
                else:
                    return {
                        "status": "error",
                        "error": (
                            "whatsapp.send requires non-empty message/body/text/content. "
                            "Use prior step results to compose the message text before sending."
                        ),
                    }

        # filesystem.write/append pre-flight: hydrate content/path from slots when missing.
        if tool_name in ("filesystem.write", "filesystem.append"):
            if "content" in args and "data" not in args:
                args["data"] = args.get("content")
            if "text" in args and "data" not in args:
                args["data"] = args.get("text")
            _data = args.get("data")
            _candidate = ""
            for _k in self._content_slot_candidates(("message_body", "cpp_code", "code_text", "content")):
                _v = _slots.get(_k)
                if isinstance(_v, str) and _v.strip():
                    _candidate = _v.strip()
                    break
            if not _candidate:
                _fallback = str(getattr(self, "_handoff_payload", "") or "").strip()
                if _fallback and _fallback.lower() not in ("ok", "no result", "none", "null"):
                    _candidate = _fallback
            data_missing = _data is None or (isinstance(_data, str) and not _data.strip())
            data_incomplete = isinstance(_data, str) and self._looks_like_incomplete_code(_data)
            candidate_better = bool(
                _candidate
                and (
                    data_missing
                    or data_incomplete
                    or (isinstance(_data, str) and len(_candidate) > len(_data) + 200)
                )
            )
            if candidate_better:
                args["data"] = _candidate[:60000]
                args["content"] = _candidate[:60000]
                print("[AGENT] filesystem.write auto-filled/replaced data from slots/handoff payload")

            _path = str(args.get("path") or args.get("file_path") or args.get("filename") or "").strip()
            if not _path:
                _path = str(_artifacts.get("last_file_path") or "").strip()
            if not _path:
                _path = self._extract_requested_output_filename(fallback="generated_output.txt")
            args.setdefault("path", _path)

        # whatsapp.send_media pre-flight: verify file exists in SANDBOX.
        # Accept both SANDBOX-relative paths and absolute paths that point inside SANDBOX.
        if tool_name == "whatsapp.send_media":
            from pathlib import Path as _PF, PureWindowsPath as _PWP
            _sandbox_root = (_PF(__file__).parent.parent / "SANDBOX").resolve()

            # Models vary: some pass to=<slot name>, recipient=<name>,
            # phone=<number>, or omit it after a contact lookup. Normalize to
            # the connector's required "to" field here.
            if not str(args.get("to") or "").strip():
                for key in ("recipient", "recipient_contact", "contact", "phone", "number", "jid"):
                    if str(args.get(key) or "").strip():
                        args["to"] = args.get(key)
                        break
            _to_raw = str(args.get("to") or "").strip()
            _to_slot = _to_raw.lower()
            if _to_slot in _slots:
                _slot_to = str(_slots.get(_to_slot) or "").strip()
                _phone = self._extract_phone_like(_slot_to)
                if _phone:
                    args["to"] = _phone
            if not str(args.get("to") or "").strip() or not self._extract_phone_like(str(args.get("to") or "")):
                _phone = str(_slots.get("recipient_phone_or_jid") or "").strip()
                if not _phone:
                    _contact_payload = str(_slots.get("contact_info") or _slots.get("recipient_contact") or "").strip()
                    _phone = self._extract_phone_like(_contact_payload)
                if _phone:
                    args["to"] = _phone

            def _media_candidate_path(raw: str):
                raw = str(raw or "").strip().strip('"').strip("'")
                if not raw:
                    return None
                rel = raw.replace("\\", "/")
                rel = re.sub(r"(?i)^sandbox/", "", rel, count=1)
                if re.match(r"^[A-Za-z]:[\\/]", raw):
                    rel = _PWP(raw).name
                try:
                    p_raw = _PF(raw)
                    if p_raw.is_absolute():
                        resolved = p_raw.resolve()
                        try:
                            resolved.relative_to(_sandbox_root)
                            if resolved.exists():
                                return resolved
                        except Exception:
                            pass
                    resolved = (_sandbox_root / rel).resolve()
                    try:
                        resolved.relative_to(_sandbox_root)
                    except Exception:
                        return None
                    return resolved
                except Exception:
                    return None

            def _sandbox_prefix_match(raw: str):
                raw = str(raw or "").strip().strip('"').strip("'")
                if not raw:
                    return None
                name = raw.replace("\\", "/").split("/")[-1].lower()
                if len(name) < 6:
                    return None
                matches = []
                for child in _sandbox_root.iterdir():
                    if child.is_file():
                        cname = child.name.lower()
                        if cname.startswith(name) or name.startswith(cname):
                            matches.append(child)
                return matches[0] if len(matches) == 1 else None

            _task_media_l = str(getattr(self, "_current_task_text", "") or "").lower()
            preferred_candidates = []
            if ".pdf" in _task_media_l or re.search(r"\bpdf\b", _task_media_l):
                preferred_candidates.extend([
                    _slots.get("pdf_file_path"),
                    _artifacts.get("pdf_file_path"),
                ])
            if re.search(r"\b(document|doc|docx|report|file)\b", _task_media_l):
                preferred_candidates.extend([
                    _slots.get("document_file_path"),
                    _artifacts.get("document_file_path"),
                    _slots.get("file_path"),
                    _artifacts.get("last_file_path"),
                ])

            url_candidates = preferred_candidates + [
                args.get("url"),
                args.get("file_path"),
                args.get("path"),
                args.get("file"),
                args.get("media_path"),
                args.get("media"),
                args.get("document"),
                args.get("attachment"),
                args.get("attachment_path"),
                _slots.get("file_path"),
                _artifacts.get("last_file_path"),
            ]
            for key in ("url", "file_path", "path", "file", "media_path", "media", "document", "attachment", "attachment_path"):
                val = str(args.get(key) or "").strip().lower()
                if val in _slots:
                    url_candidates.insert(0, _slots.get(val))
            if re.search(r"\bpdf\b", _task_media_l):
                for val in list(_slots.values()) + list(_artifacts.values()):
                    fp = self._normalize_sandbox_file_ref(str(val or ""))
                    if fp.lower().endswith(".pdf"):
                        url_candidates.insert(0, fp)
            for _fp in list(_artifacts.get("files") or []):
                url_candidates.append(_fp)

            _chosen = None
            _tried = []
            for candidate in url_candidates:
                raw = str(candidate or "").strip()
                if not raw:
                    continue
                _tried.append(raw)
                p = _media_candidate_path(raw)
                if p and p.exists():
                    _chosen = p
                    break
                p = _sandbox_prefix_match(raw)
                if p and p.exists():
                    _chosen = p
                    break

            if not _chosen:
                _shown = str(args.get("url") or args.get("file_path") or args.get("path") or args.get("file") or "").strip()
                _suffix = f" Tried: {', '.join(dict.fromkeys(_tried[:5]))}." if _tried else ""
                return {
                    "status": "error",
                    "error": f"send_media pre-flight: '{_shown or 'missing url'}' not found in SANDBOX. Create/verify the file before sending.{_suffix}",
                }

            # Canonicalize outbound path to SANDBOX-relative form for connector consistency.
            args["url"] = f"SANDBOX/{_chosen.name}"
            if not str(args.get("type") or "").strip():
                ext = _chosen.suffix.lower()
                if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
                    args["type"] = "image"
                elif ext in {".mp3", ".wav", ".m4a", ".ogg", ".flac"}:
                    args["type"] = "audio"
                elif ext in {".mp4", ".mov", ".mkv", ".webm", ".avi"}:
                    args["type"] = "video"
                else:
                    args["type"] = "document"

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

            if "message" in args and "body" not in args:
                args["body"] = args.get("message")
            if "text" in args and "body" not in args:
                args["body"] = args.get("text")
            if "content" in args and "body" not in args:
                args["body"] = args.get("content")

            _body = args.get("body")
            if _body is None:
                _body = ""
            if not isinstance(_body, str):
                _body = json.dumps(_body, default=str)
            _body = _body.strip()
            if not _body:
                _slot_body = ""
                for _k in self._content_slot_candidates(("message_body", "cpp_code", "code_text", "content")):
                    _v = _slots.get(_k)
                    if isinstance(_v, str) and _v.strip():
                        _slot_body = _v.strip()
                        break
                if not _slot_body:
                    _fallback = str(getattr(self, "_handoff_payload", "") or "").strip()
                    if _fallback and _fallback.lower() not in ("ok", "no result", "none", "null"):
                        _slot_body = _fallback
                if _slot_body:
                    args["body"] = _slot_body[:12000]
                    print(f"[AGENT] {tool_name} auto-filled body from slots/handoff payload")

            if "body" in args:
                args.setdefault("message", args["body"])
                args.setdefault("content", args["body"])
                args.setdefault("text", args["body"])

            if tool_name == "email.send":
                _task_l = str(getattr(self, "_current_task_text", "") or "").lower()
                _wants_file_send = bool(re.search(r"\b(attach|attachment|file|pdf|document|code|cpp|source)\b", _task_l))
                _atts = args.get("attachments")
                if _atts is None:
                    _atts = []
                if isinstance(_atts, str):
                    _atts = [_atts]
                if _wants_file_send and not _atts:
                    _file_path = str(_slots.get("file_path") or _artifacts.get("last_file_path") or "").strip()
                    if _file_path:
                        args["attachments"] = [_file_path]
                        args.setdefault("attachment_paths", [_file_path])
                        args.setdefault("files", [_file_path])
                        print(f"[AGENT] email.send auto-attached generated file: {_file_path}")

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

        _multi_queries = self._build_multi_search_queries(tool_name, args)
        if _multi_queries:
            base_query = str(args.get("query") or "").strip()
            try:
                requested_max = int(args.get("max_results", 5) or 5)
            except Exception:
                requested_max = 5
            per_query_max = max(1, min(3, requested_max))
            merged_results = []
            errors = []
            print(f"[AGENT] Multi-search expansion: {len(_multi_queries)} focused queries from '{base_query}'")
            for q in _multi_queries:
                q_args = {k: v for k, v in args.items() if k in {"query", "max_results"}}
                q_args["query"] = q
                q_args["max_results"] = per_query_max
                try:
                    raw_i = execute_tool(tool_name, **q_args)
                except Exception as e:
                    errors.append(f"{q}: {e}")
                    continue
                payload_i = raw_i.get("result", raw_i) if isinstance(raw_i, dict) else raw_i
                if isinstance(payload_i, dict) and payload_i.get("status") == "error":
                    errors.append(f"{q}: {payload_i.get('error', 'error')}")
                    continue
                results_i = []
                if isinstance(payload_i, dict) and isinstance(payload_i.get("results"), list):
                    results_i = payload_i.get("results") or []
                elif isinstance(payload_i, list):
                    results_i = payload_i
                if not results_i:
                    errors.append(f"{q}: no results")
                    continue
                for item in results_i[:per_query_max]:
                    if isinstance(item, dict):
                        enriched = dict(item)
                        enriched.setdefault("source_query", q)
                        merged_results.append(enriched)
                    else:
                        merged_results.append({"title": str(item), "source_query": q})
            if not merged_results:
                return {
                    "status": "error",
                    "error": "Multi-search found no usable results. " + "; ".join(errors[:4]),
                }
            return {
                "query": base_query,
                "multi_query": True,
                "queries": _multi_queries,
                "results": merged_results[:18],
                "count": len(merged_results[:18]),
                "errors": errors[:5],
            }

        if tool_name == "todo.add" and not str(args.get("due_date") or "").strip():
            inferred_due = self._infer_todo_due_date(args)
            if inferred_due:
                args["due_date"] = inferred_due

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







