"""
Context quality metrics tracker.

Instruments the agent run to capture signals about context quality:
- Retry rate (errors / total tool calls)
- Wrong-tool rate (consecutive errors suggesting wrong tool selection)
- Missing-argument rate (arg-related errors)
- Stale-context failures (tool tried on already-processed data)
- Duplicate call rate (idempotency blocks)
- Compaction loss (how much context was trimmed)

These metrics feed back into context selection tuning and are
exposed via the UI for observability.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StepMetrics:
    """Per-step quality metrics."""
    step_idx: int
    step_type: str = "act"        # plan | act | replan | synthesize
    attempts: int = 0
    tool_calls: int = 0
    errors: int = 0
    retries: int = 0
    blocked_duplicates: int = 0
    tokens_used: int = 0
    tokens_budget: int = 0
    trimmed_sections: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    success: bool = False


@dataclass
class RunMetrics:
    """Aggregate metrics for an entire agent run."""
    run_id: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    model: str = ""
    task_text: str = ""

    # Aggregate counters
    total_steps: int = 0
    successful_steps: int = 0
    failed_steps: int = 0
    total_tool_calls: int = 0
    total_errors: int = 0
    total_retries: int = 0
    total_blocked: int = 0
    plan_revisions: int = 0
    need_input_pauses: int = 0

    # Token accounting
    total_prompt_tokens: int = 0
    total_tokens_trimmed: int = 0

    # Context quality signals
    wrong_tool_estimates: int = 0
    missing_arg_errors: int = 0
    stale_context_errors: int = 0

    # Per-step breakdown
    step_metrics: list[StepMetrics] = field(default_factory=list)

    @property
    def elapsed_seconds(self) -> float:
        end = self.finished_at or time.time()
        return round(end - self.started_at, 2)

    @property
    def retry_rate(self) -> float:
        if self.total_tool_calls == 0:
            return 0.0
        return round(self.total_retries / self.total_tool_calls, 3)

    @property
    def error_rate(self) -> float:
        if self.total_tool_calls == 0:
            return 0.0
        return round(self.total_errors / self.total_tool_calls, 3)

    @property
    def success_rate(self) -> float:
        if self.total_steps == 0:
            return 0.0
        return round(self.successful_steps / self.total_steps, 3)

    @property
    def duplicate_rate(self) -> float:
        total = self.total_tool_calls + self.total_blocked
        if total == 0:
            return 0.0
        return round(self.total_blocked / total, 3)


class ContextMetricsTracker:
    """
    Tracks context quality metrics throughout an agent run.

    Usage:
        tracker = ContextMetricsTracker(run_id="r1", model="groq:llama-3.3-70b")
        tracker.start_step(0, "act")
        tracker.record_tool_call("todo.add", success=True)
        tracker.record_tool_call("contacts.search", success=False, error_type="missing_arg")
        tracker.end_step(0, success=True)
        metrics = tracker.finalize()
    """

    def __init__(self, run_id: str = "", model: str = "", task_text: str = ""):
        self._run = RunMetrics(
            run_id=run_id,
            model=model,
            task_text=task_text[:200],
        )
        self._current_step: StepMetrics | None = None
        self._step_map: dict[int, StepMetrics] = {}
        self._finalized_steps: set[int] = set()  # guard against double end_step calls

    def start_step(self, step_idx: int, step_type: str = "act"):
        """Begin tracking a new step. Idempotent for the same step_idx."""
        # If this step_idx was already started, reuse it (prevents retry inflation)
        if step_idx in self._step_map:
            self._current_step = self._step_map[step_idx]
            self._current_step.attempts += 1
            return
        sm = StepMetrics(step_idx=step_idx, step_type=step_type, attempts=1)
        self._current_step = sm
        self._step_map[step_idx] = sm
        self._run.total_steps += 1

    def record_tool_call(
        self,
        tool_name: str,
        success: bool = True,
        error_type: str = "",
        is_retry: bool = False,
        is_blocked: bool = False,
    ):
        """Record a tool call outcome."""
        self._run.total_tool_calls += 1

        if self._current_step:
            self._current_step.tool_calls += 1

        if not success:
            self._run.total_errors += 1
            if self._current_step:
                self._current_step.errors += 1

            # Classify error type
            if error_type == "missing_arg":
                self._run.missing_arg_errors += 1
            elif error_type == "stale_context":
                self._run.stale_context_errors += 1
            elif error_type == "wrong_tool":
                self._run.wrong_tool_estimates += 1

        if is_retry:
            self._run.total_retries += 1
            if self._current_step:
                self._current_step.retries += 1

        if is_blocked:
            self._run.total_blocked += 1
            if self._current_step:
                self._current_step.blocked_duplicates += 1

    def record_budget_trim(self, sections_trimmed: list[str], tokens_trimmed: int = 0):
        """Record a context budget trimming event."""
        self._run.total_tokens_trimmed += tokens_trimmed
        if self._current_step:
            self._current_step.trimmed_sections.extend(sections_trimmed)

    def record_plan_revision(self):
        """Record a plan revision/replan event."""
        self._run.plan_revisions += 1

    def record_need_input(self):
        """Record a NEED_INPUT pause."""
        self._run.need_input_pauses += 1

    def record_prompt_tokens(self, tokens: int):
        """Record tokens used in a prompt."""
        self._run.total_prompt_tokens += tokens
        if self._current_step:
            self._current_step.tokens_used += tokens

    def end_step(self, step_idx: int, success: bool = True):
        """End tracking for a step. Idempotent — repeated calls do not double-count."""
        # Clear current pointer regardless so later calls don't get stale state
        if self._current_step and self._current_step.step_idx == step_idx:
            self._current_step = None
        # Guard: only record counters once per step even across retries/rewinds
        if step_idx in self._finalized_steps:
            return
        self._finalized_steps.add(step_idx)
        sm = self._step_map.get(step_idx)
        if sm:
            sm.success = success
            self._run.step_metrics.append(sm)
            if success:
                self._run.successful_steps += 1
            else:
                self._run.failed_steps += 1

    def finalize(self) -> RunMetrics:
        """Finalize and return the run metrics."""
        self._run.finished_at = time.time()
        return self._run

    def get_summary(self) -> dict:
        """Return a concise summary dict for logging/UI."""
        m = self._run
        return {
            "run_id": m.run_id,
            "model": m.model,
            "elapsed_s": m.elapsed_seconds,
            "steps": f"{m.successful_steps}/{m.total_steps}",
            "tool_calls": m.total_tool_calls,
            "error_rate": f"{m.error_rate:.1%}",
            "retry_rate": f"{m.retry_rate:.1%}",
            "duplicate_rate": f"{m.duplicate_rate:.1%}",
            "plan_revisions": m.plan_revisions,
            "need_input_pauses": m.need_input_pauses,
            "wrong_tool_est": m.wrong_tool_estimates,
            "missing_arg_errors": m.missing_arg_errors,
            "total_prompt_tokens": m.total_prompt_tokens,
            "tokens_trimmed": m.total_tokens_trimmed,
        }

    def render_metrics_block(self) -> str:
        """Render metrics as a diagnostic block (for logging, not prompt injection)."""
        s = self.get_summary()
        lines = [
            "═══ CONTEXT QUALITY METRICS ═══",
            f"  Run: {s['run_id']}  Model: {s['model']}  Elapsed: {s['elapsed_s']}s",
            f"  Steps: {s['steps']}  Tool calls: {s['tool_calls']}",
            f"  Error rate: {s['error_rate']}  Retry rate: {s['retry_rate']}  Dup rate: {s['duplicate_rate']}",
            f"  Plan revisions: {s['plan_revisions']}  Need-input pauses: {s['need_input_pauses']}",
            f"  Wrong-tool est: {s['wrong_tool_est']}  Missing-arg: {s['missing_arg_errors']}",
            f"  Prompt tokens used: {s['total_prompt_tokens']}  Trimmed: {s['tokens_trimmed']}",
            "═══════════════════════════════",
        ]
        return "\n".join(lines)
