"""
agent/run_controller.py

OpenClaw-style deterministic run lifecycle controller.

Guarantees
----------
- Per-session write lock   : only one run mutates a session at a time
- Explicit RunHandle        : run_id, session_id, task_id, status, cancel_event
- Command queue per run     : side-effect writes (events, DB, snapshots) FIFO
- Terminal states           : completed | failed | aborted | iteration_limit | timeout
- abort_run(run_id | task_id | session_id) → signals cancel_event immediately
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, Optional

# ── Terminal state set (no transitions after reaching these) ─────────────────

TERMINAL_STATES: frozenset[str] = frozenset(
    {"completed", "failed", "aborted", "iteration_limit", "timeout"}
)


# ── FIFO command queue (serialises side effects within one run) ───────────────

class CommandQueue:
    """
    A per-run FIFO worker that drains coroutines one at a time.
    Callers push coroutines (DB writes, event persists, snapshot saves …)
    via enqueue(); the worker executes them strictly in order so no two
    side effects from the same run interleave.
    """

    def __init__(self) -> None:
        self._q: asyncio.Queue[Optional[Coroutine[Any, Any, Any]]] = asyncio.Queue()
        self._task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        """Start the drain worker task.  Must be called inside an event loop."""
        self._task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        while True:
            coro = await self._q.get()
            if coro is None:          # None is the shutdown sentinel
                break
            try:
                await coro
            except Exception as exc:
                print(f"[CMD_QUEUE] Side-effect error: {exc}")
            finally:
                self._q.task_done()

    def enqueue(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Thread-safe enqueue (callable from sync or async context)."""
        self._q.put_nowait(coro)

    async def drain_and_stop(self) -> None:
        """Wait for all queued work to finish, then stop the drain worker."""
        self._q.put_nowait(None)
        if self._task:
            await self._task


# ── RunHandle ─────────────────────────────────────────────────────────────────

@dataclass
class RunHandle:
    """
    Immutable identity + mutable state for one agent execution.

    Fields
    ------
    run_id       : UUID string, globally unique per run
    session_id   : identifies the *agent session* (survives WS reconnects)
    task_id      : optional DB task id (from task_store.create_task)
    status       : pending → running → <terminal>
    cancel_event : set() to request abort; polled by the agent loop
    started_at   : wall-clock timestamp of registration
    cmd_queue    : FIFO queue for all state-writing coroutines
    """

    run_id:       str
    session_id:   str
    task_id:      Optional[int]
    status:       str             = "pending"
    cancel_event: asyncio.Event   = field(default_factory=asyncio.Event)
    started_at:   datetime        = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    cmd_queue:    CommandQueue    = field(default_factory=CommandQueue)
    # Per-run queue for NEED_INPUT pause/resume.  The agent awaits this when
    # it needs clarifying information from the user mid-execution.  The server
    # puts an answer string here when the client sends action="agent_answer".
    input_queue:  asyncio.Queue   = field(default_factory=asyncio.Queue)

    # ── Convenience helpers ───────────────────────────────────────────

    def is_cancelled(self) -> bool:
        """Return True if abort has been requested for this run."""
        return self.cancel_event.is_set()

    def enqueue(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Route a side-effect write through this run's command queue."""
        self.cmd_queue.enqueue(coro)

    def answer_input(self, answer: str) -> None:
        """Deliver a user answer to a paused NEED_INPUT run."""
        self.input_queue.put_nowait(answer)


# ── RunController (module-level singleton) ────────────────────────────────────

class RunController:
    """
    Central registry for all agent runs.

    Responsibilities
    ----------------
    1. Per-session asyncio.Lock — only one active run per session at a time.
    2. Three lookup indices:  session_id → RunHandle
                              run_id     → RunHandle
                              task_id    → RunHandle
    3. abort_run() — signal cancel_event + set status to 'aborted'.
    4. finalize_run() — set terminal status + free the session slot.
    5. run_under_lock() — acquire the session lock, then await caller's coro.
    """

    def __init__(self) -> None:
        self._session_locks: Dict[str, asyncio.Lock]  = {}
        self._active_runs:   Dict[str, RunHandle]     = {}   # session_id → handle
        self._run_by_id:     Dict[str, RunHandle]     = {}   # run_id     → handle
        self._run_by_task:   Dict[int,  RunHandle]    = {}   # task_id    → handle

    # ── Session lock ──────────────────────────────────────────────────

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        return self._session_locks[session_id]

    # ── Registration ──────────────────────────────────────────────────

    def register_run(
        self,
        session_id: str,
        task_id: Optional[int] = None,
    ) -> RunHandle:
        """
        .. deprecated::
            Use pre_register_run() + activate_run() instead.

        This method is kept for backward compatibility ONLY.  It is intentionally
        identical to pre_register_run() — it does NOT write _active_runs so it
        cannot introduce the race it historically caused.  Callers that previously
        relied on the old behaviour (implicit active-slot update) must explicitly
        call activate_run() from inside run_under_lock().
        """
        import warnings
        warnings.warn(
            "register_run() is deprecated. Use pre_register_run() + activate_run() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.pre_register_run(session_id=session_id, task_id=task_id)

    def pre_register_run(
        self,
        session_id: str,
        task_id: Optional[int] = None,
    ) -> RunHandle:
        """
        Phase-1 registration: allocate run_id and populate the run_id / task_id
        lookup indices so that abort-by-run_id and abort-by-task_id work immediately,
        but do NOT overwrite _active_runs[session_id] yet.

        Call this BEFORE acquiring the session lock (so the client ack can carry
        a real run_id).  Then call activate_run() from inside run_under_lock() to
        promote the handle to the active slot.

        This fixes the critical race where two concurrent WS messages for the same
        session both call register_run() before either holds the lock, causing the
        first handle to be silently overwritten in _active_runs.
        """
        run_id = str(uuid.uuid4())
        handle = RunHandle(run_id=run_id, session_id=session_id, task_id=task_id)
        self._run_by_id[run_id] = handle
        if task_id is not None:
            self._run_by_task[task_id] = handle
        print(f"[RUNCTL] Pre-registered run run_id={run_id} session={session_id} task={task_id}")
        return handle

    def activate_run(self, handle: RunHandle) -> None:
        """
        Phase-2 registration: promote a pre-registered handle to the active slot.
        Must be called from inside run_under_lock() so the session lock is held.
        Overwrites any previous _active_runs entry for the session — the caller
        must ensure the previous run is already finalized.
        """
        self._active_runs[handle.session_id] = handle
        print(f"[RUNCTL] Activated run run_id={handle.run_id} session={handle.session_id}")

    def discard_pre_registered(self, handle: RunHandle) -> None:
        """
        Remove a pre-registered run that was never activated (e.g. client disconnected
        between pre_register_run() and run_under_lock()). Evicts the run from the
        _run_by_id and _run_by_task indices so they don’t leak.

        Safe to call even if the handle was already finalized — pop() is a no-op
        when the key is absent.
        """
        self._run_by_id.pop(handle.run_id, None)
        if handle.task_id is not None:
            self._run_by_task.pop(handle.task_id, None)
        # _active_runs was never set for this handle, so nothing to clean there.
        print(f"[RUNCTL] Discarded pre-registered (never activated) run_id={handle.run_id}")

    def get_active_run(self, session_id: str) -> Optional[RunHandle]:
        """Return the currently active RunHandle for a session, or None."""
        return self._active_runs.get(session_id)

    def get_run(self, run_id: str) -> Optional[RunHandle]:
        """Look up by run_id."""
        return self._run_by_id.get(run_id)

    def get_run_by_task(self, task_id: int) -> Optional[RunHandle]:
        """Look up by task_id."""
        return self._run_by_task.get(task_id)

    # ── Abort ─────────────────────────────────────────────────────────

    def abort_run(
        self,
        *,
        run_id:     Optional[str] = None,
        task_id:    Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> bool:
        """
        Signal the target run to abort.  Accepts any of the three identifiers.
        Returns True if a non-terminal run was found and signalled.
        """
        handle: Optional[RunHandle] = None
        if run_id is not None:
            handle = self._run_by_id.get(run_id)
        elif task_id is not None:
            handle = self._run_by_task.get(task_id)
        elif session_id is not None:
            handle = self._active_runs.get(session_id)

        if handle is None:
            print(f"[RUNCTL] abort_run: no handle found "
                  f"(run_id={run_id} task_id={task_id} session_id={session_id})")
            return False

        if handle.status in TERMINAL_STATES:
            print(f"[RUNCTL] abort_run: run {handle.run_id} already in terminal state {handle.status}")
            return False

        handle.cancel_event.set()
        handle.status = "aborted"
        print(f"[RUNCTL] Aborted run_id={handle.run_id} (task={handle.task_id})")
        return True

    # ── Finalization ──────────────────────────────────────────────────

    def finalize_run(self, handle: RunHandle, status: str) -> None:
        """
        Mark a run as terminal and free the session slot so the next run
        can acquire the per-session lock.
        """
        if status not in TERMINAL_STATES:
            raise ValueError(f"finalize_run: '{status}' is not a terminal state")
        # Only update if not already in a terminal state (abort wins over completed)
        if handle.status not in TERMINAL_STATES:
            handle.status = status
        # Free active-run slot
        if self._active_runs.get(handle.session_id) is handle:
            del self._active_runs[handle.session_id]
        # Clean up lookup indices to prevent unbounded memory growth
        self._run_by_id.pop(handle.run_id, None)
        if handle.task_id is not None:
            self._run_by_task.pop(handle.task_id, None)
        print(f"[RUNCTL] Finalized run_id={handle.run_id} → {handle.status}")

    # ── Run under per-session lock ─────────────────────────────────────

    async def run_under_lock(
        self,
        session_id: str,
        coro_factory: Callable[[], Coroutine[Any, Any, Any]],
    ) -> Any:
        """
        Acquire the exclusive per-session write lock, then await coro_factory().
        Serialises all mutations for the same session_id.
        """
        lock = self._get_lock(session_id)
        async with lock:
            return await coro_factory()


# ── Module-level singleton ─────────────────────────────────────────────────────

_controller = RunController()


def get_controller() -> RunController:
    """Return the process-wide RunController singleton."""
    return _controller
