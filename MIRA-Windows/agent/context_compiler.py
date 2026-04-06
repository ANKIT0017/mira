"""
ContextCompiler — the core context engineering module.

Implements the full context lifecycle:
  compile → cache → compact → retrieve → verify

Replaces the flat prompt-string concatenation in agent.py with a structured,
typed-section pipeline that is provider-aware and step-type-adaptive.

Architecture:
  ┌─────────────┐     ┌──────────────┐     ┌─────────────┐
  │ Typed Sections│────▶│ TokenBudget  │────▶│  Provider   │
  │  (compile)   │     │  (compact)   │     │  State Mgr  │
  └──────┬───────┘     └──────────────┘     └──────┬──────┘
         │                                          │
  ┌──────▼───────┐     ┌──────────────┐     ┌──────▼──────┐
  │ ToolRecords  │     │ ShortTerm    │     │  Structured │
  │  (retrieve)  │     │   Memory     │     │   Output    │
  └──────────────┘     └──────────────┘     └─────────────┘
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .token_budget import TokenBudget, count_tokens
from .tool_records import ToolRecordStore
from .context_memory import ShortTermMemory, LongTermMemory
from .provider_state import ProviderStateManager
from .context_metrics import ContextMetricsTracker


# ── Typed context sections ─────────────────────────────────────────────

@dataclass
class ContextSections:
    """
    Structured context container. Each section has a stable type ID
    and can be independently budgeted, cached, or omitted.
    """
    system: str = ""           # core system prompt
    step_tools: str = ""       # filtered tool docs for current step
    profile: str = ""          # SOUL.md + USER.md
    memory: str = ""           # MEMORY.md (long-term facts)
    short_term: str = ""       # per-run volatile memory (open loops, decisions)
    buffer: str = ""           # active buffer inventory
    tool_history: str = ""     # relevance-ranked tool call records
    history: str = ""          # conversation history turns
    done_block: str = ""       # completed steps block
    goal: str = ""             # current task/step goal
    task_contract: str = ""    # immutable user task contract (high-priority)
    working_state: str = ""    # mutable slots/artifacts for current run
    constraints: str = ""      # runtime constraints (retry notes, format, etc.)

    def to_dict(self) -> dict[str, str]:
        return {
            "system": self.system,
            "step_tools": self.step_tools,
            "profile": self.profile,
            "memory": self.memory,
            "short_term": self.short_term,
            "buffer": self.buffer,
            "tool_history": self.tool_history,
            "history": self.history,
            "done_block": self.done_block,
            "goal": self.goal,
            "task_contract": self.task_contract,
            "working_state": self.working_state,
            "constraints": self.constraints,
        }

    def total_tokens(self) -> int:
        return sum(count_tokens(v) for v in self.to_dict().values())

    @staticmethod
    def from_dict(d: dict[str, str]) -> "ContextSections":
        return ContextSections(**{k: v for k, v in d.items() if hasattr(ContextSections, k)})


# ── Step type definitions ──────────────────────────────────────────────

class StepType:
    PLAN = "plan"
    ACT = "act"
    REPLAN = "replan"
    SYNTHESIZE = "synthesize"


# ── Main ContextCompiler ──────────────────────────────────────────────

class ContextCompiler:
    """
    Builds typed, budgeted context for each LLM call.

    Lifecycle per call:
    1. compile()  — Assemble ContextSections from all sources
    2. budget()   — Fit into token budget with priority-ordered compaction
    3. render()   — Produce final prompt string (or structured messages)
    4. verify()   — Post-call, check if context was sufficient

    The compiler is stateful within a run — it tracks the ToolRecordStore,
    ShortTermMemory, and metrics across steps.
    """

    def __init__(
        self,
        system_prompt: str,
        model: str = "",
        memory_file: Path | None = None,
    ):
        self.system_prompt = system_prompt
        self.model = model

        # Sub-systems
        self.tool_records = ToolRecordStore()
        self.short_term = ShortTermMemory()
        self.long_term = LongTermMemory(memory_file) if memory_file else None
        self.provider_state = ProviderStateManager()
        self.metrics = ContextMetricsTracker(model=model)
        self.budget = TokenBudget(model=model)

        # Caches for stable content
        self._profile_cache: str = ""
        self._profile_hash: str = ""
        self._tool_summary_cache: str = ""

    def reset(self):
        """Reset per-run state (keep long-term memory)."""
        self.tool_records = ToolRecordStore()
        self.short_term = ShortTermMemory()
        self.metrics = ContextMetricsTracker(model=self.model)

    # ── Phase 1: Compile ───────────────────────────────────────────────

    def compile(
        self,
        step_type: str,
        user_message: str = "",
        step_desc: str = "",
        step_idx: int = 0,
        step_tools: str = "",
        profile_context: str = "",
        memory_context: str | None = None,
        buffer_context: str = "",
        history_text: str = "",
        done_block: str = "",
        task_contract: str = "",
        working_state: str = "",
        retry_note: str = "",
        progress_block: str = "",
        extra_hints: str = "",
    ) -> ContextSections:
        """
        Assemble all context sections for a single LLM call.

        This replaces the manual string concatenation in agent.py with
        typed sections that can be independently budgeted and cached.
        """
        sections = ContextSections()
        sections.system = self.system_prompt

        # Keep system prompt stable; avoid injecting per-call temporal instructions.

        # Tool docs (act/replan get full tool docs, plan gets category summary)
        if step_type in (StepType.ACT, StepType.REPLAN, StepType.PLAN):
            sections.step_tools = step_tools

        # Profile (cacheable — rarely changes within a run)
        sections.profile = profile_context

        # Memory (long-term persistent facts)
        # If caller provided memory_context explicitly (including ""), use it as-is.
        if memory_context is not None:
            sections.memory = memory_context
        elif self.long_term and step_type in (StepType.PLAN, StepType.REPLAN):
            sections.memory = self.long_term.render_context(
                query=f"{user_message} {step_desc}"
            )
        else:
            sections.memory = ""

        # Short-term memory (per-run volatile state)
        if step_type in (StepType.ACT, StepType.REPLAN):
            sections.short_term = self.short_term.render_context(step_idx)

        # Buffer inventory
        sections.buffer = buffer_context

        # Tool history (relevance-ranked instead of fixed "last 3")
        if step_type in (StepType.ACT, StepType.REPLAN):
            sections.tool_history = self.tool_records.render_recent_context(
                current_step_desc=step_desc,
                current_step_idx=step_idx,
                max_records=6,
                max_raw_chars=800,
            )
        # NOTE: For SYNTHESIZE, tool_history is intentionally omitted here.
        # The caller already includes render_compact_summary() inside done_block
        # to avoid duplicating the tool call log in the synthesis prompt.

        # Conversation history
        sections.history = history_text

        # Completed steps
        sections.done_block = done_block

        # Goal + constraints
        # Executor (ACT) only needs the current step — the full user message adds
        # noise and wastes tokens without guiding tool selection.
        goal_parts = []
        if step_type != StepType.ACT:
            goal_parts.append(f"User: {user_message}")
        if step_desc and step_type in (StepType.ACT, StepType.REPLAN):
            goal_parts.append(f"Current step: {step_desc}")
        sections.goal = "\n".join(goal_parts)
        sections.task_contract = task_contract
        sections.working_state = working_state

        constraint_parts = []
        if retry_note:
            constraint_parts.append(retry_note)
        if progress_block:
            constraint_parts.append(progress_block)
        if extra_hints:
            constraint_parts.append(extra_hints)
        sections.constraints = "\n".join(constraint_parts)

        return sections

    # ── Phase 2: Budget ────────────────────────────────────────────────

    def apply_budget(
        self,
        sections: ContextSections,
        step_type: str = "act",
    ) -> tuple[ContextSections, str, dict]:
        """
        Fit sections into token budget with adaptive allocation.

        Returns:
            (trimmed_sections, budget_note, diagnostics)
        """
        trimmed, note, diagnostics = self.budget.allocate(
            sections.to_dict(),
            step_type=step_type,
        )

        # Track metrics
        if diagnostics.get("trimmed"):
            before = diagnostics.get("total_tokens_before", 0)
            after = diagnostics.get("total_tokens_after", 0)
            # Only report sections that were actually trimmed (token count reduced)
            sections_before = diagnostics.get("sections_before", {})
            sections_after_counts = diagnostics.get("sections_after", {})
            actually_trimmed = [
                k for k in sections_after_counts
                if sections_before.get(k, 0) > sections_after_counts.get(k, 0)
            ]
            self.metrics.record_budget_trim(
                sections_trimmed=actually_trimmed,
                tokens_trimmed=before - after,
            )

        self.metrics.record_prompt_tokens(
            diagnostics.get("total_tokens_after", diagnostics.get("total_tokens_before", 0))
        )

        return ContextSections.from_dict(trimmed), note, diagnostics

    # ── Phase 3: Render ────────────────────────────────────────────────

    def render_prompt(
        self,
        sections: ContextSections,
        step_type: str,
        budget_note: str = "",
        output_format: str = "",
    ) -> str:
        """
        Render sections into a final prompt string.

        Order is optimized per step type:
        - Plan: system → profile → memory → history → goal → task_contract → state → tools_summary → instructions
        - Act:  system → tools → profile → memory → short_term → buffer →
                tool_history → history → done → goal → task_contract → state → constraints → format
        - Replan: system → profile → memory → history → done → goal → task_contract → state → constraints
        - Synthesize: system → profile → memory → history → done → goal → task_contract → state → constraints
        """
        parts = []

        if step_type == StepType.PLAN:
            parts = [
                sections.system,
                budget_note,
                sections.profile,
                sections.memory,
                sections.history,
                sections.goal,
                sections.task_contract,
                sections.working_state,
                sections.step_tools,   # tool categories for plan
                sections.constraints,
                output_format,
            ]
        elif step_type == StepType.ACT:
            parts = [
                sections.system,
                sections.step_tools,
                budget_note,
                sections.profile,
                sections.memory,
                sections.short_term,
                sections.buffer,
                sections.tool_history,
                sections.history,
                sections.done_block,
                sections.goal,
                sections.task_contract,
                sections.working_state,
                sections.constraints,
                output_format,
            ]
        elif step_type == StepType.REPLAN:
            parts = [
                sections.system,
                budget_note,
                sections.profile,
                sections.memory,
                sections.history,
                sections.done_block,
                sections.tool_history,
                sections.goal,
                sections.task_contract,
                sections.working_state,
                sections.constraints,
                output_format,
            ]
        elif step_type == StepType.SYNTHESIZE:
            parts = [
                sections.system,
                budget_note,
                sections.profile,
                sections.memory,
                sections.history,
                sections.done_block,
                sections.tool_history,
                sections.goal,
                sections.task_contract,
                sections.working_state,
                sections.constraints,
                output_format,
            ]

        # Filter empty parts and join
        return "\n\n".join(p for p in parts if p and p.strip())

    def render_structured(
        self,
        sections: ContextSections,
        step_type: str,
        budget_note: str = "",
        output_format: str = "",
    ) -> dict | list | str:
        """
        Render as provider-native structured messages when supported.

        Returns either:
        - A list of messages (OpenAI-compatible providers)
        - A dict with systemInstruction (Gemini)
        - A flat string (Ollama/local)
        """
        provider = self.provider_state.detect_provider(self.model)

        if self.provider_state.supports_system_role(provider):
            stable_prefix = f"{sections.system}\n\n{sections.step_tools}\n\n{sections.profile}"
            dynamic = "\n\n".join(filter(None, [
                budget_note,
                sections.memory,
                sections.short_term,
                sections.buffer,
                sections.tool_history,
                sections.history,
                sections.done_block,
                sections.task_contract,
                sections.working_state,
            ]))
            user_msg = "\n\n".join(filter(None, [
                sections.goal,
                sections.constraints,
                output_format,
            ]))

            self.provider_state.cache_stable_prefix(provider, stable_prefix)

            return self.provider_state.build_openai_messages(
                system_prompt=sections.system,
                tool_docs=sections.step_tools,
                profile_context=sections.profile,
                memory_context="",  # already included in dynamic
                dynamic_context=dynamic,
                user_message=user_msg,
            )

        elif self.provider_state.supports_system_instruction(provider):
            dynamic = "\n\n".join(filter(None, [
                budget_note,
                sections.memory,
                sections.short_term,
                sections.buffer,
                sections.tool_history,
                sections.history,
                sections.done_block,
                sections.goal,
                sections.task_contract,
                sections.working_state,
                sections.constraints,
                output_format,
            ]))
            return self.provider_state.build_gemini_content(
                system_prompt=sections.system,
                tool_docs=sections.step_tools,
                profile_context=sections.profile,
                dynamic_context=dynamic,
                user_message=sections.goal,
            )

        # Fallback: flat prompt string (Ollama, etc.)
        return self.render_prompt(sections, step_type, budget_note, output_format)

    # ── Phase 4: Verify ────────────────────────────────────────────────

    def verify_context_quality(
        self,
        llm_reply: str,
        step_idx: int,
        step_type: str,
    ) -> dict:
        """
        Post-call verification: check if the LLM reply suggests context issues.

        Returns diagnostic signals:
        - hallucinated_tool: LLM referenced a tool not in step_tools
        - missing_args: LLM omitted required args (suggests insufficient tool docs)
        - stale_reference: LLM referenced a buffer/file that doesn't exist
        - repeated_call: LLM tried to repeat an already-done call
        """
        signals = {
            "hallucinated_tool": False,
            "missing_args": False,
            "stale_reference": False,
            "repeated_call": False,
        }

        if not llm_reply:
            return signals

        reply_lower = llm_reply.lower()

        # Check for empty/missing args pattern (signal only — actual tool
        # error accounting happens in the agent's tool-execution path)
        if '"args": {}' in llm_reply or '"args":{}' in llm_reply:
            signals["missing_args"] = True

        # Check for stale buffer references
        buf_refs = re.findall(r'BUFFER:(\d+)', llm_reply)
        for ref in buf_refs:
            record = self.tool_records.get(f"buf_{ref}")
            # This is a heuristic — actual buffer existence check is in agent.py
            if int(ref) > self.tool_records._seq_counter + 10:
                signals["stale_reference"] = True

        return signals

    # ── Deterministic compaction ──────────────────────────────────────

    def compact_trajectory(self, completed_steps: list[dict]) -> str:
        """
        Deterministic compaction of the execution trajectory.

        Produces a schema-based summary instead of lossy text truncation.
        Keeps raw provenance pointers (call_ids, buffer_ids) so nothing
        is truly "lost" — just compressed.
        """
        if not completed_steps:
            return ""

        lines = ["EXECUTION TRAJECTORY (compacted):"]
        for idx, step in enumerate(completed_steps):
            icon = "✓" if step.get("status") == "done" else "✗"
            step_desc = step.get("step", "")[:120]
            result = step.get("result", "")[:180]

            lines.append(f"  [{icon}] Step {idx + 1}: {step_desc}")
            lines.append(f"    Result: {result}")

            # Include artifact pointers
            artifacts = step.get("artifacts", {})
            pointers = []
            if artifacts.get("buffers"):
                pointers.append(f"buf={','.join(str(b) for b in artifacts['buffers'][:3])}")
            if artifacts.get("files"):
                pointers.append(f"files={','.join(artifacts['files'][:3])}")
            if artifacts.get("phones"):
                pointers.append(f"phones={','.join(artifacts['phones'][:2])}")
            if pointers:
                lines.append(f"    Refs: {' | '.join(pointers)}")

            # Include call_ids for provenance
            calls = step.get("calls", [])
            if calls:
                call_summary = ", ".join(
                    f"{c.get('tool', '?')}[{c.get('status', '?')}]"
                    for c in calls[-3:]
                )
                lines.append(f"    Calls: {call_summary}")

        return "\n".join(lines) + "\n\n"

    # ── Convenience: backward-compatible dict interface ────────────────

    def fit_context_budget_compat(
        self,
        sections: dict[str, str],
        step_type: str = "act",
    ) -> tuple[dict[str, str], str]:
        """
        Drop-in replacement for the old _fit_context_budget().

        Returns (trimmed_dict, budget_note) — same interface as before,
        but using accurate token counting and adaptive allocation.
        """
        trimmed, note, _ = self.budget.allocate(sections, step_type=step_type)
        return trimmed, note

    # ── Smart generate dispatch ───────────────────────────────────────

    def prepare_for_generation(
        self,
        sections: ContextSections,
        step_type: str,
        budget_note: str = "",
        output_format: str = "",
    ) -> tuple[str | None, list[dict] | None]:
        """
        Prepare context for LLM generation, choosing the best format.

        Returns:
            (flat_prompt, messages) — exactly ONE will be non-None.
            If the provider supports structured messages, messages is set.
            Otherwise flat_prompt is set.
        """
        provider = self.provider_state.detect_provider(self.model)

        if self.provider_state.supports_system_role(provider):
            # Provider supports OpenAI-compatible messages
            structured = self.render_structured(
                sections, step_type, budget_note, output_format
            )
            if isinstance(structured, list):
                return None, structured

        if self.provider_state.supports_system_instruction(provider):
            # Gemini — returns a dict with systemInstruction
            structured = self.render_structured(
                sections, step_type, budget_note, output_format
            )
            if isinstance(structured, dict):
                return None, structured  # type: ignore[return-value]

        # Flat prompt fallback
        prompt = self.render_prompt(sections, step_type, budget_note, output_format)
        return prompt, None
