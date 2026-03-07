"""
Structured tool call/output records with stable IDs and provenance.

Replaces flat "recent outputs" list with typed, queryable records that
support relevance-ranked retrieval, dependency tracking, and deterministic
compaction.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(slots=True)
class ToolRecord:
    """Single tool invocation record with full provenance."""

    call_id: str                     # stable unique ID: f"{step_idx}_{attempt}_{seq}"
    tool_name: str
    args: dict[str, Any]
    args_hash: str                   # MD5[:12] of canonical JSON args
    status: str                      # "ok" | "error" | "blocked" | "buffered"
    result_digest: str               # compressed 1-line summary (≤220 chars)
    raw_result: str                  # full JSON result (may be large)
    buffer_id: Optional[int] = None  # if result was buffered
    artifacts: dict = field(default_factory=lambda: {"buffers": [], "files": [], "phones": []})
    step_idx: int = 0
    attempt: int = 0
    timestamp: float = field(default_factory=time.time)
    elapsed_ms: float = 0.0
    depends_on: list[str] = field(default_factory=list)  # call_ids this record consumed
    tags: list[str] = field(default_factory=list)         # semantic tags for retrieval

    @staticmethod
    def make_args_hash(args: dict) -> str:
        raw = json.dumps(args, sort_keys=True, default=str)
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    @staticmethod
    def make_call_id(step_idx: int, attempt: int, seq: int) -> str:
        return f"s{step_idx}_a{attempt}_c{seq}"


class ToolRecordStore:
    """
    Append-only store of ToolRecords for a single agent run.
    Supports indexed lookups, dependency tracking, and relevance queries.
    """

    def __init__(self):
        self._records: list[ToolRecord] = []
        self._by_id: dict[str, ToolRecord] = {}
        self._by_tool: dict[str, list[ToolRecord]] = {}
        self._by_step: dict[int, list[ToolRecord]] = {}
        self._seq_counter: int = 0

    def add(
        self,
        tool_name: str,
        args: dict,
        status: str,
        result_digest: str,
        raw_result: str,
        step_idx: int = 0,
        attempt: int = 0,
        buffer_id: int | None = None,
        artifacts: dict | None = None,
        elapsed_ms: float = 0.0,
        depends_on: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> ToolRecord:
        """Add a new tool record and return it."""
        self._seq_counter += 1
        call_id = ToolRecord.make_call_id(step_idx, attempt, self._seq_counter)
        args_hash = ToolRecord.make_args_hash(args)

        rec = ToolRecord(
            call_id=call_id,
            tool_name=tool_name,
            args=args,
            args_hash=args_hash,
            status=status,
            result_digest=result_digest,
            raw_result=raw_result,
            buffer_id=buffer_id,
            artifacts=artifacts or {"buffers": [], "files": [], "phones": []},
            step_idx=step_idx,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            depends_on=depends_on or [],
            tags=tags or [],
        )

        self._records.append(rec)
        self._by_id[call_id] = rec
        self._by_tool.setdefault(tool_name, []).append(rec)
        self._by_step.setdefault(step_idx, []).append(rec)
        return rec

    @property
    def all_records(self) -> list[ToolRecord]:
        return list(self._records)

    def get(self, call_id: str) -> ToolRecord | None:
        return self._by_id.get(call_id)

    def for_tool(self, tool_name: str) -> list[ToolRecord]:
        return self._by_tool.get(tool_name, [])

    def for_step(self, step_idx: int) -> list[ToolRecord]:
        return self._by_step.get(step_idx, [])

    def successful(self) -> list[ToolRecord]:
        return [r for r in self._records if r.status == "ok"]

    def errors(self) -> list[ToolRecord]:
        return [r for r in self._records if r.status == "error"]

    # ── Relevance-ranked retrieval ────────────────────────────────────────

    def relevant_for_step(
        self,
        current_step_desc: str,
        current_step_idx: int,
        max_records: int = 8,
        current_tool_deps: list[str] | None = None,
    ) -> list[ToolRecord]:
        """
        Return records ranked by relevance to the current step.

        Scoring:
          +3  explicit dependency (call_id in current_tool_deps)
          +2  same step (e.g. retry context)
          +1  adjacent step (step_idx == current - 1)
          +1  keyword overlap between step desc and tool name/digest
          +0.5 successful result
          -1  old step (> 3 steps ago)
          -2  error result from distant step
        """
        if not self._records:
            return []

        deps = set(current_tool_deps or [])
        step_words = set(current_step_desc.lower().split())

        scored: list[tuple[float, ToolRecord]] = []
        for rec in self._records:
            score = 0.0

            # Explicit dependency
            if rec.call_id in deps:
                score += 3.0

            # Same step context
            if rec.step_idx == current_step_idx:
                score += 2.0
            elif rec.step_idx == current_step_idx - 1:
                score += 1.0

            # Keyword overlap
            tool_words = set(rec.tool_name.replace(".", " ").lower().split())
            digest_words = set(rec.result_digest.lower().split())
            overlap = step_words & (tool_words | digest_words)
            score += len(overlap) * 0.3

            # Status bonus/penalty
            if rec.status == "ok":
                score += 0.5
            elif rec.status == "error" and abs(rec.step_idx - current_step_idx) > 2:
                score -= 2.0

            # Recency decay
            step_distance = abs(current_step_idx - rec.step_idx)
            if step_distance > 3:
                score -= 1.0

            scored.append((score, rec))

        # Sort descending by score, then by recency (latest first)
        scored.sort(key=lambda x: (-x[0], -x[1].timestamp))
        return [rec for _, rec in scored[:max_records]]

    # ── Rendering ─────────────────────────────────────────────────────────

    def render_recent_context(
        self,
        current_step_desc: str = "",
        current_step_idx: int = 0,
        max_records: int = 6,
        max_raw_chars: int = 800,
    ) -> str:
        """
        Render relevant tool records as structured context for the LLM prompt.
        Uses relevance ranking instead of fixed "last N" truncation.
        """
        records = self.relevant_for_step(
            current_step_desc, current_step_idx, max_records
        )
        if not records:
            return ""

        lines = ["TOOL CALL HISTORY (relevance-ranked, most relevant first):"]
        for rec in records:
            status_icon = "✓" if rec.status == "ok" else "✗"
            buf_note = f" [BUFFER:{rec.buffer_id}]" if rec.buffer_id else ""
            lines.append(
                f"  [{rec.call_id}] {status_icon} {rec.tool_name} "
                f"(step {rec.step_idx + 1}){buf_note}"
            )
            lines.append(f"    Digest: {rec.result_digest[:220]}")

            # Include truncated raw for high-relevance or recent records
            if rec.step_idx >= current_step_idx - 1 and rec.raw_result:
                raw_preview = rec.raw_result[:max_raw_chars]
                if len(rec.raw_result) > max_raw_chars:
                    raw_preview += "\n    ...[truncated]"
                lines.append(f"    Raw: {raw_preview}")

            if rec.artifacts.get("buffers") or rec.artifacts.get("files"):
                art_parts = []
                if rec.artifacts.get("buffers"):
                    art_parts.append(f"buffers={','.join(str(b) for b in rec.artifacts['buffers'][:3])}")
                if rec.artifacts.get("files"):
                    art_parts.append(f"files={','.join(rec.artifacts['files'][:3])}")
                lines.append(f"    Artifacts: {' | '.join(art_parts)}")

        return "\n".join(lines) + "\n\n"

    def render_compact_summary(self) -> str:
        """One-line-per-record summary for compaction/synthesis phases."""
        if not self._records:
            return ""
        lines = ["TOOL CALL LOG:"]
        for rec in self._records:
            icon = "✓" if rec.status == "ok" else "✗"
            lines.append(
                f"  [{icon}] s{rec.step_idx + 1} {rec.tool_name}: "
                f"{rec.result_digest[:140]}"
            )
        return "\n".join(lines) + "\n\n"

    # ── Metrics ───────────────────────────────────────────────────────────

    def compute_metrics(self) -> dict:
        """Return context quality metrics for the run."""
        total = len(self._records)
        if total == 0:
            return {"total_calls": 0}

        errors = sum(1 for r in self._records if r.status == "error")
        blocked = sum(1 for r in self._records if r.status == "blocked")
        ok = sum(1 for r in self._records if r.status == "ok")

        # Retry rate: errors / total
        retry_rate = errors / total if total else 0

        # Wrong-tool rate: approximate by counting consecutive errors on same step
        wrong_tool_count = 0
        for step_recs in self._by_step.values():
            consecutive_errors = 0
            for r in step_recs:
                if r.status == "error":
                    consecutive_errors += 1
                else:
                    if consecutive_errors >= 2:
                        wrong_tool_count += consecutive_errors - 1
                    consecutive_errors = 0

        # Duplicate rate
        dup_rate = blocked / total if total else 0

        return {
            "total_calls": total,
            "success_count": ok,
            "error_count": errors,
            "blocked_count": blocked,
            "retry_rate": round(retry_rate, 3),
            "wrong_tool_estimates": wrong_tool_count,
            "duplicate_rate": round(dup_rate, 3),
            "unique_tools_used": len(self._by_tool),
            "steps_with_calls": len(self._by_step),
        }
