"""
Patch agent.py - run from MIRA-Windows directory.
python patch_agent.py
"""
from pathlib import Path
import re, sys

AGENT = Path("agent/agent.py")
if not AGENT.exists():
    sys.exit(f"ERROR: {AGENT.resolve()} not found. Run from MIRA-Windows/")

text = AGENT.read_text(encoding="utf-8")
orig = len(text)
ok, miss = [], []

def patch(name, old, new):
    global text
    if old in text:
        text = text.replace(old, new, 1)
        ok.append(name)
    else:
        miss.append(name)

# ─── P1: tiny system prompt ───────────────────────────────────────────────────
patch("P1:system_prompt",
    '    def _build_system_prompt(self):\r\n'
    '        """Build compact prompt optimized for accurate local tool-calling."""\r\n'
    '        self.system_prompt = f"""{SOUL_FILE}, {USER_FILE}, {MEMORY_FILE}\r\n'
    '\r\n'
    'Core behavior:\r\n'
    '1) Complete tasks end-to-end and skip steps already satisfied by completed results.\r\n'
    '2) Be truthful: only claim success from successful tool outputs.\r\n'
    '3) If information is missing from user, ask NEED_INPUT with one precise question.\r\n'
    '4) Keep memory concise; use memory.write/memory.update for durable facts.\r\n'
    '\r\n'
    '\r\n'
    'Output format (exactly one):\r\n'
    '- ACTION: {{"tool":"...","args":{{}}}}\r\n'
    '- FINAL ANSWER: <concise completion summary>\r\n'
    '- NEED_INPUT: <specific missing info>\r\n'
    '\r\n'
    '"""',
    '    def _build_system_prompt(self):\r\n'
    '        """Tiny stable system prompt - fits small-model (4B) context budgets."""\r\n'
    '        self.system_prompt = (\r\n'
    '            "You are MIRA, a local AI assistant.\\n"\r\n'
    '            "Complete tasks using the available tools. One tool call per turn.\\n"\r\n'
    '            "Only claim success when the tool output confirms it.\\n"\r\n'
    '            "Ask the user only when you cannot proceed without their input."\r\n'
    '        )'
)

# ─── P2: _get_relevant_memory method ─────────────────────────────────────────
INSERTION_ANCHOR = (
    '        return (\r\n'
    '            f"PERSISTENT MEMORY ({MEMORY_FILE}, auto-injected each turn):\\n"\r\n'
    '            f"{memory_view}\\n\\n"\r\n'
    '        )\r\n'
    '\r\n'
    '    def _get_profile_context'
)
NEW_RELEVANT = (
    '        return (\r\n'
    '            f"PERSISTENT MEMORY ({MEMORY_FILE}, auto-injected each turn):\\n"\r\n'
    '            f"{memory_view}\\n\\n"\r\n'
    '        )\r\n'
    '\r\n'
    '    def _get_relevant_memory(self, task: str, max_items: int = 5) -> str:\r\n'
    '        """Return only memory items relevant to the current task (keyword overlap).\r\n'
    '        Keeps ACT prompts tight for small 4B models. Instead of injecting the full\r\n'
    '        MEMORY.md (which grows over months), injects only the top-N relevant notes.\r\n'
    '        """\r\n'
    '        memory_text = self._read_memory_file().strip()\r\n'
    '        if not memory_text:\r\n'
    '            return ""\r\n'
    '        note_lines = [\r\n'
    '            ln.strip() for ln in memory_text.splitlines()\r\n'
    '            if ln.strip().startswith("-")\r\n'
    '        ]\r\n'
    '        if not note_lines:\r\n'
    '            return ""\r\n'
    "        task_words = set(re.sub(r'[^\\w\\s]', ' ', task.lower()).split())\r\n"
    '        scored = []\r\n'
    '        for ln in note_lines:\r\n'
    "            ln_words = set(re.sub(r'[^\\w\\s]', ' ', ln.lower()).split())\r\n"
    '            scored.append((len(task_words & ln_words), ln))\r\n'
    '        scored.sort(key=lambda x: x[0], reverse=True)\r\n'
    '        top = [ln for _, ln in scored[:max_items]]\r\n'
    '        recent = note_lines[-2:]\r\n'
    '        combined = list(dict.fromkeys(top + recent))[:max_items]\r\n'
    '        if not combined:\r\n'
    '            return ""\r\n'
    '        return "MEMORY (relevant):\\n" + "\\n".join(combined) + "\\n\\n"\r\n'
    '\r\n'
    '    def _get_profile_context'
)
patch("P2:_get_relevant_memory", INSERTION_ANCHOR, NEW_RELEVANT)

# ─── P3: _predict_tool + focused _get_step_tools ─────────────────────────────
OLD_STEP_TOOLS = (
    '    def _get_step_tools(self, step_desc: str, user_message: str = "", planner_categories: list[str] | None = None) -> str:\r\n'
    '        """Return full tool schemas filtered to categories the planner assigned to this step.\r\n'
    '        The planner already pre-selected which categories each step needs, so we give the\r\n'
    '        executor the complete arg/type/example card for every relevant tool instead of the\r\n'
    '        minimal compact-docs hint \u2014 eliminating wrong arg names and missing required fields.\r\n'
    '        """\r\n'
    '        cats = self._select_categories(step_desc, user_message, planner_categories)\r\n'
    '        # Store for execution-time category gate in _safe_execute\r\n'
    '        self._current_step_cats = {c.strip().lower() for c in cats}\r\n'
    '        return full_tool_schemas(categories={c for c in cats})'
)
NEW_STEP_TOOLS = (
    '    # Deterministic tool prediction: (category, keyword, tool_name)\r\n'
    '    _TOOL_PREDICTION_MAP: list = [\r\n'
    '        ("Web & Search",   "news",      "search.news"),\r\n'
    '        ("Web & Search",   "search",    "search.duckduckgo"),\r\n'
    '        ("Web Scraping",   "scrape",    "firecrawl.scrape"),\r\n'
    '        ("Web Scraping",   "crawl",     "firecrawl.crawl"),\r\n'
    '        ("Web Scraping",   "extract",   "firecrawl.extract"),\r\n'
    '        ("Filesystem",     "write",     "filesystem.write"),\r\n'
    '        ("Filesystem",     "append",    "filesystem.append"),\r\n'
    '        ("Filesystem",     "read",      "filesystem.read"),\r\n'
    '        ("Filesystem",     "list",      "filesystem.list"),\r\n'
    '        ("Filesystem",     "search",    "filesystem.search"),\r\n'
    '        ("Filesystem",     "delete",    "filesystem.delete"),\r\n'
    '        ("Email",          "send",      "email.send"),\r\n'
    '        ("Email",          "draft",     "email.create_draft"),\r\n'
    '        ("Email",          "read",      "email.list"),\r\n'
    '        ("Email",          "search",    "email.search"),\r\n'
    '        ("Calendar",       "add",       "calendar.add_event"),\r\n'
    '        ("Calendar",       "schedule",  "calendar.add_event"),\r\n'
    '        ("Calendar",       "recurring", "calendar.add_recurring_event"),\r\n'
    '        ("Calendar",       "delete",    "calendar.delete_event"),\r\n'
    '        ("Calendar",       "list",      "calendar.list"),\r\n'
    '        ("Calendar",       "check",     "calendar.list"),\r\n'
    '        ("Todo List",      "add",       "todo.add"),\r\n'
    '        ("Todo List",      "complete",  "todo.complete"),\r\n'
    '        ("Todo List",      "delete",    "todo.delete"),\r\n'
    '        ("Todo List",      "update",    "todo.update"),\r\n'
    '        ("Todo List",      "list",      "todo.list"),\r\n'
    '        ("WhatsApp",       "send",      "whatsapp.send"),\r\n'
    '        ("WhatsApp",       "media",     "whatsapp.send_media"),\r\n'
    '        ("WhatsApp",       "document",  "whatsapp.send_media"),\r\n'
    '        ("WhatsApp",       "status",    "whatsapp.status"),\r\n'
    '        ("Contacts",       "search",    "contacts.search"),\r\n'
    '        ("Contacts",       "find",      "contacts.search"),\r\n'
    '        ("Contacts",       "add",       "contacts.add"),\r\n'
    '        ("Contacts",       "update",    "contacts.update"),\r\n'
    '        ("Knowledge Base", "search",    "knowledge.search"),\r\n'
    '        ("Knowledge Base", "remember",  "knowledge.ingest"),\r\n'
    '        ("Knowledge Base", "ingest",    "knowledge.ingest"),\r\n'
    '        ("Buffer",         "search",    "buffer.search"),\r\n'
    '        ("Buffer",         "read",      "buffer.slice"),\r\n'
    '        ("Buffer",         "extract",   "buffer.extract"),\r\n'
    '        ("Buffer",         "pipe",      "buffer.pipe"),\r\n'
    '        ("System",         "time",      "system.time"),\r\n'
    '        ("System",         "date",      "system.time"),\r\n'
    '        ("Workflow",       "save",      "workflow.save"),\r\n'
    '        ("Workflow",       "list",      "workflow.list"),\r\n'
    '        ("Workflow",       "run",       "workflow.run"),\r\n'
    '    ]\r\n'
    '\r\n'
    '    def _predict_tool(self, step_desc: str, categories: list[str]) -> list[str]:\r\n'
    '        """Predict 1-2 most likely tools for this step (deterministic, no LLM).\r\n'
    '        Reduces per-ACT-call tool schema tokens from ~800-2000 down to ~200-400.\r\n'
    '        Critical for maintaining 4B model attention over a multi-step task.\r\n'
    '        """\r\n'
    '        desc = step_desc.lower()\r\n'
    '        cats_lower = {c.strip().lower() for c in (categories or [])}\r\n'
    '        predictions: list[str] = []\r\n'
    '        for cat, kw, tool in self._TOOL_PREDICTION_MAP:\r\n'
    '            if cat.lower() not in cats_lower:\r\n'
    '                continue\r\n'
    '            if " " in kw:\r\n'
    '                if kw in desc and tool not in predictions:\r\n'
    '                    predictions.append(tool)\r\n'
    '            else:\r\n'
    "                if re.search(rf'\\b{re.escape(kw)}\\b', desc) and tool not in predictions:\r\n"
    '                    predictions.append(tool)\r\n'
    '        return predictions[:2]\r\n'
    '\r\n'
    '    def _get_step_tools(self, step_desc: str, user_message: str = "", planner_categories: list[str] | None = None) -> str:\r\n'
    '        """Return focused tool schemas for this step.\r\n'
    '        Predicts 1-2 most likely tools and returns only their full schema cards,\r\n'
    '        plus a compact reference list of other tools in the same categories.\r\n'
    '        Falls back to full category schemas when no prediction matches.\r\n'
    '        """\r\n'
    '        cats = self._select_categories(step_desc, user_message, planner_categories)\r\n'
    '        self._current_step_cats = {c.strip().lower() for c in cats}\r\n'
    '\r\n'
    '        predicted = self._predict_tool(step_desc, list(cats))\r\n'
    '        if predicted:\r\n'
    '            from mcp_server.tools import TOOL_METADATA, get_tools_by_category\r\n'
    '            lines = ["TOOLS FOR THIS STEP (use exact names and args):"]\r\n'
    '            cats_by_group = get_tools_by_category()\r\n'
    '            cats_lower_map = {c.strip().lower(): c for c in cats_by_group}\r\n'
    '            rendered_cats: set = set()\r\n'
    '            for tool_name in predicted:\r\n'
    '                meta = TOOL_METADATA.get(tool_name)\r\n'
    '                if not meta:\r\n'
    '                    continue\r\n'
    '                cat = meta.get("category", "")\r\n'
    '                if cat not in rendered_cats:\r\n'
    '                    lines.append(f"\\n[{cat}]")\r\n'
    '                    rendered_cats.add(cat)\r\n'
    '                desc_s = (meta.get("description") or "").split("\\n")[0].strip()[:100]\r\n'
    '                params = meta.get("parameters", {})\r\n'
    '                example = meta.get("example") or ""\r\n'
    '                notes_txt = meta.get("notes") or ""\r\n'
    '                arg_parts = []\r\n'
    '                for pname, pinfo in params.items():\r\n'
    '                    ptype = pinfo.get("type", "any")\r\n'
    '                    required = pinfo.get("required", False)\r\n'
    '                    default = pinfo.get("default")\r\n'
    '                    if required:\r\n'
    '                        arg_parts.append(f"{pname}: {ptype} [required]")\r\n'
    '                    else:\r\n'
    '                        dflt_str = f", default={default!r}" if default is not None else ""\r\n'
    '                        arg_parts.append(f"{pname}: {ptype} [optional{dflt_str}]")\r\n'
    '                lines.append(f"  {tool_name}: {desc_s}")\r\n'
    '                if arg_parts:\r\n'
    '                    lines.append("    Args: " + " | ".join(arg_parts))\r\n'
    '                if example:\r\n'
    '                    lines.append(f"    Example: {example}")\r\n'
    '                if notes_txt:\r\n'
    '                    lines.append(f"    Note: {notes_txt}")\r\n'
    '            other_tools = []\r\n'
    '            for cat in cats:\r\n'
    '                actual_cat = cats_lower_map.get(cat.strip().lower())\r\n'
    '                if actual_cat:\r\n'
    '                    for t in cats_by_group.get(actual_cat, []):\r\n'
    '                        if t["name"] not in predicted and t["name"] not in other_tools:\r\n'
    '                            other_tools.append(t["name"])\r\n'
    '            if other_tools:\r\n'
    "            lines.append(f'\\nOther available: {chr(44).join(other_tools[:8])}')\r\n"
    '            return "\\n".join(lines)\r\n'
    '\r\n'
    '        return full_tool_schemas(categories={c for c in cats})'
)
patch("P3:_predict_tool+_get_step_tools", OLD_STEP_TOOLS, NEW_STEP_TOOLS)

# ─── P4: add _memory_relevant_cached + _consecutive_failures ──────────────────
patch("P4:relevant_memory+consecutive_failures",
    '        _profile_context_full_cached = self._get_profile_context(include_user=True, include_soul=True)\r\n'
    '        _profile_context_exec_cached = self._get_profile_context(include_user=False, include_soul=False)\r\n'
    '        _memory_context_cached = self._get_memory_context()\r\n'
    '        _tool_categories_summary_cached = self._get_tool_categories_summary()',

    '        _profile_context_full_cached = self._get_profile_context(include_user=True, include_soul=True)\r\n'
    '        _profile_context_exec_cached = self._get_profile_context(include_user=False, include_soul=False)\r\n'
    '        _memory_context_cached = self._get_memory_context()        # full memory for PLAN\r\n'
    '        _memory_relevant_cached = self._get_relevant_memory(text)  # keyword-filtered for ACT\r\n'
    '        _tool_categories_summary_cached = self._get_tool_categories_summary()\r\n'
    '        _consecutive_failures = 0  # counts consecutive failed steps; gates REPLAN'
)

# ─── P5: use _memory_relevant_cached in ACT ───────────────────────────────────
patch("P5:relevant_memory_in_ACT",
    '                    profile_context=_profile_context_exec_cached,\r\n'
    '                    memory_context=_memory_context_cached,\r\n'
    '                    buffer_context=buffer_context,',

    '                    profile_context=_profile_context_exec_cached,\r\n'
    '                    memory_context=_memory_relevant_cached,  # keyword-filtered; tight for 4B models\r\n'
    '                    buffer_context=buffer_context,'
)

# ─── P6: REPLAN gate on 2+ consecutive failures ───────────────────────────────
patch("P6:REPLAN_gate",
    '            # -- Replan-as-step-splitting: revise remaining plan from failed step onward --\r\n'
    '            if step_status == "failed" and not _step_rewound and _plan_revisions < MAX_PLAN_REVISIONS:',

    '            # Track consecutive failures for REPLAN gate\r\n'
    '            if step_status == "failed" and not _step_rewound:\r\n'
    '                _consecutive_failures += 1\r\n'
    '            elif step_status == "done":\r\n'
    '                _consecutive_failures = 0  # reset on any success\r\n'
    '\r\n'
    '            # -- Replan-as-step-splitting: revise remaining plan from failed step onward --\r\n'
    '            # Only trigger after 2+ consecutive failures - single failures are handled\r\n'
    '            # by ToolPolicy fallbacks and retries without a costly REPLAN LLM call.\r\n'
    '            if step_status == "failed" and not _step_rewound and _consecutive_failures >= 2 and _plan_revisions < MAX_PLAN_REVISIONS:'
)

# ─── P7: workflow auto-trigger in handle_streaming ────────────────────────────
patch("P7:workflow_auto_trigger",
    '        self._context_compiler.reset()  # fresh tool records + short-term memory per run\r\n'
    '        self._context_compiler.metrics = ContextMetricsTracker(',

    '        self._context_compiler.reset()  # fresh tool records + short-term memory per run\r\n'
    '\r\n'
    '        # -- Workflow auto-trigger --\r\n'
    '        # Check for a saved workflow matching the user message by trigger keywords.\r\n'
    '        # If matched, skip LLM planning entirely and load workflow steps directly.\r\n'
    '        try:\r\n'
    '            from mcp_server.tools.workflow_tools import auto_match_workflow\r\n'
    '            _matched_wf = auto_match_workflow(text)\r\n'
    '        except Exception:\r\n'
    '            _matched_wf = None\r\n'
    '        _workflow_steps_preloaded: list | None = None\r\n'
    '        if _matched_wf:\r\n'
    '            _wf_steps = _matched_wf.get("steps", [])\r\n'
    '            if _wf_steps:\r\n'
    '                _workflow_steps_preloaded = [\r\n'
    '                    {"goal": s, "categories": []} for s in _wf_steps\r\n'
    '                ]\r\n'
    '                wf_name = _matched_wf.get("display_name") or _matched_wf.get("name", "?")\r\n'
    '                print(f"[AGENT] Workflow \'{wf_name}\' auto-triggered ({len(_wf_steps)} steps)")\r\n'
    '                yield {"type": "thought", "content": f"Using saved workflow: {wf_name}"}\r\n'
    '\r\n'
    '        self._context_compiler.metrics = ContextMetricsTracker('
)

# ─── P8: workflow suggestion after multi-step success ─────────────────────────
patch("P8:workflow_suggestion",
    '        yield {"type": "final", "content": final_response}\r\n'
    '        self.history.append((text, final_response))',

    '        # After 3+ successful steps, suggest saving as a reusable workflow\r\n'
    '        try:\r\n'
    '            if (\r\n'
    '                not _workflow_steps_preloaded\r\n'
    '                and len(completed) >= 3\r\n'
    '                and all(s["status"] == "done" for s in completed)\r\n'
    '            ):\r\n'
    '                _suggest = (\r\n'
    '                    "\\n\\n---\\n"\r\n'
    '                    "Want to save this as a reusable workflow? "\r\n'
    '                    "Next time you ask something similar I will run these "\r\n'
    '                    f"{len(completed)} steps automatically.\\n"\r\n'
    '                    "Reply **save workflow as <name>** to save, or ignore to skip."\r\n'
    '                )\r\n'
    '                final_response = final_response + _suggest\r\n'
    '        except Exception:\r\n'
    '            pass\r\n'
    '\r\n'
    '        yield {"type": "final", "content": final_response}\r\n'
    '        self.history.append((text, final_response))'
)

# ─── P9: add DONE/NEED keyword normalizer in ACT reply parsing ────────────────
NORMALIZE_ANCHOR = '                # -- Post-reply context quality verification'
if NORMALIZE_ANCHOR in text:
    text = text.replace(
        NORMALIZE_ANCHOR,
        '                # Normalize 3-option keywords to legacy format for downstream parsers\r\n'
        '                _rline = reply.strip()\r\n'
        '                if _rline.upper().startswith("DONE:"):\r\n'
        '                    reply = "STEP_DONE:" + reply[reply.upper().index("DONE:") + 5:]\r\n'
        '                elif _rline.upper().startswith("NEED:"):\r\n'
        '                    reply = "NEED_INPUT:" + reply[reply.upper().index("NEED:") + 5:]\r\n'
        '\r\n'
        + NORMALIZE_ANCHOR,
        1
    )
    ok.append("P9:DONE/NEED_normalizer")
else:
    miss.append("P9:DONE/NEED_normalizer")

# ── Print results ─────────────────────────────────────────────────────────────
print(f"\nResults: {len(ok)} OK, {len(miss)} MISSED")
for p in ok:
    print(f"  OK  {p}")
for p in miss:
    print(f"  MISS {p}")

AGENT.write_text(text, encoding="utf-8")
print(f"\nFiles written. agent.py: {orig:,} -> {len(text):,} bytes (+{len(text)-orig:,})")
