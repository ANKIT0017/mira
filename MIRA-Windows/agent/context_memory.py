"""
Two-level memory system for the agent.

Level 1: Short-term checkpoint memory (per-run)
  - Captures intermediate state, open loops, partial results
  - Volatile — cleared between runs
  - Used for multi-step reasoning continuity

Level 2: Long-term distilled facts (persistent)
  - Extracted from MEMORY.md with provenance tracking
  - Each fact has a source (tool call, user input, inference)
  - Supports relevance-scored retrieval
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass(slots=True)
class Checkpoint:
    """Short-term memory checkpoint for a single run."""
    checkpoint_id: str
    step_idx: int
    label: str
    data: dict[str, Any]      # arbitrary structured state
    timestamp: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DistilledFact:
    """Long-term fact with provenance and confidence."""
    fact_id: str
    content: str
    source: str                # "tool:<tool_name>:<call_id>" | "user_input" | "inference"
    confidence: float = 1.0   # 0.0 to 1.0
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0
    tags: list[str] = field(default_factory=list)
    superseded_by: Optional[str] = None  # fact_id if this fact was updated


class ShortTermMemory:
    """
    Per-run volatile memory for tracking intermediate state.
    
    Captures:
    - Open loops (tasks started but not completed)
    - Key decisions made during planning
    - Intermediate data dependencies between steps
    - User clarifications received mid-run
    """

    def __init__(self):
        self._checkpoints: list[Checkpoint] = []
        self._open_loops: dict[str, dict] = {}  # loop_id -> {description, step_idx, status}
        self._decisions: list[dict] = []
        self._user_inputs: list[dict] = []

    def checkpoint(self, step_idx: int, label: str, data: dict) -> Checkpoint:
        """Save a state checkpoint at the current step."""
        cp_id = f"cp_{step_idx}_{len(self._checkpoints)}"
        cp = Checkpoint(
            checkpoint_id=cp_id,
            step_idx=step_idx,
            label=label,
            data=data,
        )
        self._checkpoints.append(cp)
        return cp

    def open_loop(self, loop_id: str, description: str, step_idx: int):
        """Register an open loop (task started, awaiting completion)."""
        self._open_loops[loop_id] = {
            "description": description,
            "step_idx": step_idx,
            "status": "open",
            "opened_at": time.time(),
        }

    def close_loop(self, loop_id: str, result: str = ""):
        """Close a previously opened loop."""
        if loop_id in self._open_loops:
            self._open_loops[loop_id]["status"] = "closed"
            self._open_loops[loop_id]["result"] = result
            self._open_loops[loop_id]["closed_at"] = time.time()

    def record_decision(self, step_idx: int, decision: str, rationale: str = ""):
        """Record a key decision made during execution."""
        self._decisions.append({
            "step_idx": step_idx,
            "decision": decision,
            "rationale": rationale,
            "timestamp": time.time(),
        })

    def record_user_input(self, step_idx: int, question: str, answer: str):
        """Record user clarification received during execution."""
        self._user_inputs.append({
            "step_idx": step_idx,
            "question": question,
            "answer": answer,
            "timestamp": time.time(),
        })

    def get_open_loops(self) -> list[dict]:
        """Return currently open loops."""
        return [
            {"id": k, **v}
            for k, v in self._open_loops.items()
            if v["status"] == "open"
        ]

    def render_context(self, current_step_idx: int = 0) -> str:
        """Render short-term memory as context for the LLM."""
        blocks = []

        # Open loops
        open_loops = self.get_open_loops()
        if open_loops:
            lines = ["OPEN LOOPS (started but not yet completed):"]
            for loop in open_loops:
                lines.append(f"  - [{loop['id']}] (step {loop['step_idx'] + 1}): {loop['description']}")
            blocks.append("\n".join(lines))

        # Recent decisions
        recent_decisions = [d for d in self._decisions if d["step_idx"] >= current_step_idx - 3]
        if recent_decisions:
            lines = ["KEY DECISIONS:"]
            for d in recent_decisions[-5:]:
                lines.append(f"  - Step {d['step_idx'] + 1}: {d['decision']}")
            blocks.append("\n".join(lines))

        # User inputs
        if self._user_inputs:
            lines = ["USER CLARIFICATIONS:"]
            for ui in self._user_inputs[-3:]:
                lines.append(f"  - Q: {ui['question'][:100]} → A: {ui['answer'][:100]}")
            blocks.append("\n".join(lines))

        # Recent checkpoints (just labels)
        recent_cps = [cp for cp in self._checkpoints if cp.step_idx >= current_step_idx - 2]
        if recent_cps:
            lines = ["CHECKPOINTS:"]
            for cp in recent_cps[-3:]:
                lines.append(f"  - [{cp.checkpoint_id}] Step {cp.step_idx + 1}: {cp.label}")
            blocks.append("\n".join(lines))

        if not blocks:
            return ""
        return "\n".join(blocks) + "\n\n"

    def reset(self):
        """Clear all short-term memory."""
        self._checkpoints.clear()
        self._open_loops.clear()
        self._decisions.clear()
        self._user_inputs.clear()


class LongTermMemory:
    """
    Persistent fact store backed by MEMORY.md with provenance tracking.

    Enhances the existing MEMORY.md system with:
    - Structured fact IDs for stable references
    - Source provenance for each fact
    - Relevance scoring based on recency + access frequency
    - Deduplication and supersession tracking
    """

    def __init__(self, memory_file: Path):
        self._file = memory_file
        self._facts: dict[str, DistilledFact] = {}
        self._last_mtime: float = 0.0  # track file modification time
        self._load_facts()

    def _load_facts(self):
        """Parse existing MEMORY.md into structured facts."""
        if not self._file.exists():
            return

        self._last_mtime = self._file.stat().st_mtime
        text = self._file.read_text(encoding="utf-8")
        note_id = 0
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("-"):
                continue
            content = stripped[1:].strip()
            if not content:
                continue

            note_id += 1
            # Check for existing tagged format: [tag] content
            tag_match = re.match(r'\[([^\]]+)\]\s*(.*)', content)
            source = "legacy"
            if tag_match:
                source = tag_match.group(1)
                content = tag_match.group(2)

            fact_id = f"mem_{note_id}"
            fact_hash = hashlib.md5(content.encode()).hexdigest()[:8]

            self._facts[fact_id] = DistilledFact(
                fact_id=fact_id,
                content=content,
                source=source,
                tags=[source] if source != "legacy" else [],
            )

    def add_fact(
        self,
        content: str,
        source: str = "inference",
        confidence: float = 1.0,
        tags: list[str] | None = None,
    ) -> DistilledFact:
        """Add a new fact, deduplicating against existing content."""
        # Check for near-duplicate
        content_lower = content.lower().strip()
        for existing in self._facts.values():
            if existing.content.lower().strip() == content_lower:
                existing.last_accessed = time.time()
                existing.access_count += 1
                return existing

        fact_id = f"mem_{len(self._facts) + 1}"
        fact = DistilledFact(
            fact_id=fact_id,
            content=content,
            source=source,
            confidence=confidence,
            tags=tags or [],
        )
        self._facts[fact_id] = fact
        return fact

    def get_relevant_facts(
        self,
        query: str,
        max_facts: int = 20,
    ) -> list[DistilledFact]:
        """
        Return facts ranked by relevance to the query.
        Uses keyword overlap + recency + access frequency.
        """
        query_words = set(query.lower().split())

        scored: list[tuple[float, DistilledFact]] = []
        for fact in self._facts.values():
            if fact.superseded_by:
                continue

            score = 0.0
            fact_words = set(fact.content.lower().split())

            # Keyword overlap
            overlap = query_words & fact_words
            score += len(overlap) * 0.5

            # Recency bonus (facts accessed recently score higher)
            age_hours = (time.time() - fact.last_accessed) / 3600
            if age_hours < 1:
                score += 1.0
            elif age_hours < 24:
                score += 0.5

            # Access frequency bonus
            score += min(fact.access_count * 0.1, 1.0)

            # Confidence weight
            score *= fact.confidence

            scored.append((score, fact))

        scored.sort(key=lambda x: -x[0])
        return [f for _, f in scored[:max_facts]]

    def render_context(self, query: str = "", max_chars: int = 6000) -> str:
        """Render memory facts as annotated context."""
        # Reload if the backing file has been mutated since last load
        self.reload_if_changed()

        if not self._facts:
            return ""

        facts = self.get_relevant_facts(query) if query else list(self._facts.values())

        lines = ["PERSISTENT MEMORY (with provenance):"]
        total_chars = 0
        for fact in facts:
            prov = f"[{fact.source}]" if fact.source != "legacy" else ""
            conf = f" (conf:{fact.confidence:.1f})" if fact.confidence < 1.0 else ""
            line = f"  - [{fact.fact_id}] {prov} {fact.content}{conf}"
            if total_chars + len(line) > max_chars:
                lines.append("  ...[truncated for budget]")
                break
            lines.append(line)
            total_chars += len(line)

        return "\n".join(lines) + "\n\n"

    @property
    def fact_count(self) -> int:
        return len(self._facts)

    def reload_if_changed(self):
        """Reload facts from disk if the backing file has been modified."""
        if not self._file.exists():
            return
        try:
            current_mtime = self._file.stat().st_mtime
        except OSError:
            return
        if current_mtime > self._last_mtime:
            self._facts.clear()
            self._load_facts()
