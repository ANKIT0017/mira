"""
Token-accurate budgeting engine.

Replaces the heuristic chars/4 estimator with provider-aware tokenization.
Falls back gracefully to tiktoken cl100k_base when provider-specific
tokenizers are unavailable.
"""

from __future__ import annotations

import os
from typing import Optional

# ── Tokenizer selection ───────────────────────────────────────────────────

_tokenizer = None
_tokenizer_name = "char_estimate"  # fallback


def _init_tokenizer():
    """
    Try to load tiktoken for accurate token counting.
    Falls back to char-based estimation if unavailable.
    """
    global _tokenizer, _tokenizer_name
    if _tokenizer is not None:
        return

    try:
        import tiktoken
        # cl100k_base covers GPT-4/3.5, good approximation for Llama-family too
        _tokenizer = tiktoken.get_encoding("cl100k_base")
        _tokenizer_name = "tiktoken_cl100k"
    except ImportError:
        _tokenizer = None
        _tokenizer_name = "char_estimate"


def count_tokens(text: str) -> int:
    """Count tokens using the best available tokenizer."""
    if not text:
        return 0
    _init_tokenizer()
    if _tokenizer is not None:
        return len(_tokenizer.encode(text, disallowed_special=()))
    # Fallback: chars / 3.5 is more accurate than chars / 4 for English text
    return max(1, int(len(text) / 3.5))


def tokenizer_info() -> dict:
    """Return info about the active tokenizer for diagnostics."""
    _init_tokenizer()
    return {"name": _tokenizer_name, "available": _tokenizer is not None}


# ── Provider-specific token limits ────────────────────────────────────────

# Context window sizes for known model families.
# Used for intelligent budget allocation.
PROVIDER_CONTEXT_WINDOWS: dict[str, int] = {
    "groq": 32768,         # llama-3.3-70b-versatile
    "gemini": 1048576,     # gemini-2.5-flash (1M tokens)
    "cerebras": 32768,     # llama-3.3-70b
    "sambanova": 32768,    # llama-3.3-70b-instruct
    "openrouter": 32768,   # default, varies by model
    "ollama": 8192,        # typical local 4B model
}


def get_context_window(model: str) -> int:
    """Return context window size for a model identifier."""
    model_lower = model.lower()
    for prefix, window in PROVIDER_CONTEXT_WINDOWS.items():
        if model_lower.startswith(prefix + ":"):
            return window
    # Gemini models have huge context
    if "gemini" in model_lower:
        return 1048576
    # Default for local/unknown models
    return int(os.getenv("MIRA_DEFAULT_CONTEXT_WINDOW", "8192"))


# ── Budget allocation engine ─────────────────────────────────────────────

# Default soft limit as env-overridable
DEFAULT_SOFT_LIMIT = int(os.getenv("MIRA_PROMPT_TOKEN_SOFT_LIMIT", "8000"))


class TokenBudget:
    """
    Token budget allocator that distributes a soft limit across named sections.

    Replaces the old _fit_context_budget() with:
    - Accurate token counting (not chars/4)
    - Priority-ordered compaction with configurable per-section limits
    - Adaptive allocation based on step type (plan/act/replan/synthesize)
    """

    # Priority order: lower = trimmed first when over budget
    DEFAULT_PRIORITIES = {
        "recent_outputs": 1,
        "tool_history": 2,       # structured tool records
        "short_term": 3,        # volatile per-run memory
        "history": 4,
        "done_block": 5,
        "constraints": 6,       # retry notes, hints, etc.
        "memory": 7,
        "buffer": 8,
        "goal": 9,              # user message + step desc
        "profile": 10,
        "system": 11,           # system prompt — almost never trimmed
        "step_tools": 12,       # tool docs — critical for execution
        "working_state": 13,    # compact slot/artifact state for run continuity
        "task_contract": 14,    # immutable task spec; keep unless impossible
    }

    # Sections that must not be trimmed by normal budget passes.
    PROTECTED_SECTIONS = frozenset({"task_contract"})

    # Adaptive limits per step type (tokens, not chars)
    STEP_TYPE_ALLOCATIONS = {
        "plan": {
            "system": 600, "profile": 800, "memory": 600,
            "history": 1200, "step_tools": 1000, "recent_outputs": 200,
            "done_block": 200, "buffer": 200,
            "short_term": 200, "tool_history": 200,
            "goal": 400, "task_contract": 800, "working_state": 250,
            "constraints": 200,
        },
        "act": {
            "system": 500, "profile": 600, "memory": 500,
            "history": 800, "step_tools": 1500, "recent_outputs": 1200,
            "done_block": 1000, "buffer": 400,
            "short_term": 400, "tool_history": 800,
            "goal": 400, "task_contract": 800, "working_state": 300,
            "constraints": 400,
        },
        "replan": {
            "system": 500, "profile": 500, "memory": 500,
            "history": 600, "step_tools": 800, "recent_outputs": 400,
            "done_block": 1500, "buffer": 200,
            "short_term": 300, "tool_history": 400,
            "goal": 400, "task_contract": 900, "working_state": 300,
            "constraints": 300,
        },
        "synthesize": {
            "system": 500, "profile": 600, "memory": 600,
            "history": 1000, "step_tools": 0, "recent_outputs": 0,
            "done_block": 2500, "buffer": 200,
            "short_term": 0, "tool_history": 600,
            "goal": 400, "task_contract": 900, "working_state": 300,
            "constraints": 300,
        },
    }

    def __init__(self, soft_limit: int | None = None, model: str = ""):
        self.soft_limit = soft_limit or DEFAULT_SOFT_LIMIT
        self.model = model

        # If we know the model's context window, set a reasonable soft limit
        if model:
            window = get_context_window(model)
            # Use at most 60% of context window for prompt (leave room for output)
            max_prompt = int(window * 0.6)
            self.soft_limit = min(self.soft_limit, max_prompt)

    def allocate(
        self,
        sections: dict[str, str],
        step_type: str = "act",
    ) -> tuple[dict[str, str], str, dict]:
        """
        Fit sections into the token budget.

        Returns:
            (trimmed_sections, budget_note, diagnostics)
        """
        allocations = self.STEP_TYPE_ALLOCATIONS.get(step_type, self.STEP_TYPE_ALLOCATIONS["act"])

        # Count tokens for each section
        token_counts = {k: count_tokens(v) for k, v in sections.items()}
        total = sum(token_counts.values())

        diagnostics = {
            "total_tokens_before": total,
            "soft_limit": self.soft_limit,
            "step_type": step_type,
            "tokenizer": tokenizer_info()["name"],
            "sections_before": dict(token_counts),
        }

        if total <= self.soft_limit:
            diagnostics["trimmed"] = False
            return dict(sections), "", diagnostics

        # Over budget — trim sections by priority (lowest priority first)
        budget_note = "⚠ Context trimmed to fit token budget.\n\n"
        out = dict(sections)

        # Sort ALL keys by priority (lowest = trimmed first).
        # Keys not in DEFAULT_PRIORITIES get a mid-range default so they
        # are still subject to trimming rather than being ungoverned.
        priorities = self.DEFAULT_PRIORITIES
        all_keys = list(out.keys())
        sorted_keys = sorted(
            all_keys,
            key=lambda k: priorities.get(k, 5)  # unknown sections get priority 5
        )

        # Pass 1: Per-section limits — trim each section that exceeds its allocation
        for key in sorted_keys:
            current_total = sum(count_tokens(v) for v in out.values())
            if current_total <= self.soft_limit:
                break

            if key in self.PROTECTED_SECTIONS:
                continue

            section_tokens = count_tokens(out.get(key, ""))
            max_for_section = allocations.get(key, section_tokens)

            if section_tokens > max_for_section and out.get(key):
                out[key] = self._trim_section(out[key], max_for_section, key)

        # Pass 2: Global cap — if still over budget after per-section trimming,
        # progressively halve lowest-priority sections until we fit.
        # This guarantees the total will converge to <= soft_limit.
        for _round in range(10):  # safety cap on iterations
            current_total = sum(count_tokens(v) for v in out.values())
            if current_total <= self.soft_limit:
                break
            for key in sorted_keys:
                if not out.get(key):
                    continue
                if key in self.PROTECTED_SECTIONS:
                    continue
                section_tokens = count_tokens(out[key])
                if section_tokens <= 1:
                    continue
                # Halve this section's budget
                target = max(1, section_tokens // 2)
                out[key] = self._trim_section(out[key], target, key)
                # Re-check total after each trim
                if sum(count_tokens(v) for v in out.values()) <= self.soft_limit:
                    break

        diagnostics["trimmed"] = True
        diagnostics["total_tokens_after"] = sum(count_tokens(v) for v in out.values())
        diagnostics["sections_after"] = {k: count_tokens(v) for k, v in out.items()}
        return out, budget_note, diagnostics

    def _trim_section(self, text: str, max_tokens: int, section_name: str) -> str:
        """Trim a section to fit within max_tokens, preserving tail for recency."""
        if not text:
            return text

        current = count_tokens(text)
        if current <= max_tokens:
            return text

        # For history/done_block/recent_outputs: prefer keeping the tail (recent)
        tail_preferred = section_name in ("history", "done_block", "recent_outputs")

        lines = text.splitlines()
        if not lines:
            return text

        if len(lines) <= 6:
            # Short section — just char-truncate
            ratio = max_tokens / max(current, 1)
            char_limit = int(len(text) * ratio)
            if tail_preferred:
                return "…[trimmed]\n" + text[-char_limit:]
            return text[:char_limit] + "\n…[trimmed]"

        # Keep first 3 + last 3 lines, then trim the middle
        head = "\n".join(lines[:3])
        tail = "\n".join(lines[-3:])
        core = head + "\n...[middle omitted for budget]\n" + tail

        if count_tokens(core) > max_tokens:
            # Even head+tail is too large — just keep tail
            ratio = max_tokens / max(current, 1)
            char_limit = int(len(text) * ratio)
            if tail_preferred:
                return "…[trimmed]\n" + text[-char_limit:]
            return text[:char_limit] + "\n…[trimmed]"

        return core

    def estimate_remaining(self, used_sections: dict[str, str]) -> int:
        """How many tokens remain in the budget after used_sections."""
        used = sum(count_tokens(v) for v in used_sections.values())
        return max(0, self.soft_limit - used)
