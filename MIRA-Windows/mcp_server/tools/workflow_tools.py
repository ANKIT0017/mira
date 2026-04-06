"""
Workflow persistence tools for MIRA.
Allows saving, listing, and running reusable task workflows.
Stored as JSON files in SANDBOX/workflows/.
"""
import json
import os
import re
from pathlib import Path
from datetime import datetime
from . import register_tool

WORKFLOWS_DIR = Path("SANDBOX/workflows")


def _ensure_workflows_dir():
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)


def _workflow_path(name: str) -> Path:
    safe = re.sub(r"[^\w\-]", "_", name.strip().lower())
    return WORKFLOWS_DIR / f"{safe}.json"


def _load_all_workflows() -> list[dict]:
    _ensure_workflows_dir()
    workflows = []
    for f in sorted(WORKFLOWS_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            workflows.append(data)
        except Exception:
            pass
    return workflows


def _match_workflow(user_message: str, workflows: list[dict]) -> dict | None:
    """Return first workflow whose trigger_keywords match user_message."""
    msg_lower = user_message.lower()
    for wf in workflows:
        for kw in wf.get("trigger_keywords", []):
            if kw.lower() in msg_lower:
                return wf
    return None


@register_tool(
    name="workflow.save",
    description="Save the current task plan as a reusable workflow for future use. Stores steps to SANDBOX/workflows/.",
    category="Workflow",
    example='{"name": "research_topic", "trigger_keywords": ["research", "look up"], "steps": ["Search for {target}", "Scrape top result", "Summarize findings"], "variables": ["target"]}',
    notes="Variables in steps are written as {variable_name} and substituted at runtime.",
)
def workflow_save(name: str, steps: list, trigger_keywords: list = None, variables: list = None, description: str = ""):
    """
    Save a workflow definition.

    Args:
        name: Short name for the workflow (e.g. 'research_topic')
        steps: List of step descriptions (strings). Use {varname} for variables.
        trigger_keywords: Words/phrases that auto-trigger this workflow.
        variables: Variable names used in steps (e.g. ['target', 'recipient']).
        description: Human-readable description of what the workflow does.
    """
    _ensure_workflows_dir()
    if not name or not name.strip():
        return {"status": "error", "error": "name is required"}
    if not steps or not isinstance(steps, list):
        return {"status": "error", "error": "steps must be a non-empty list"}

    safe_name = re.sub(r"[^\w\-]", "_", name.strip().lower())
    wf = {
        "name": safe_name,
        "display_name": name.strip(),
        "description": description or name.strip(),
        "trigger_keywords": trigger_keywords or [],
        "variables": variables or [],
        "steps": [str(s) for s in steps],
        "created_at": datetime.now().isoformat(),
        "run_count": 0,
    }
    path = _workflow_path(safe_name)
    path.write_text(json.dumps(wf, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "status": "ok",
        "result": {
            "saved": str(path),
            "name": safe_name,
            "steps": len(steps),
            "trigger_keywords": trigger_keywords or [],
        }
    }


@register_tool(
    name="workflow.list",
    description="List all saved workflows in SANDBOX/workflows/.",
    category="Workflow",
    example="{}",
)
def workflow_list():
    """Return all saved workflows with their names, descriptions, and trigger keywords."""
    workflows = _load_all_workflows()
    if not workflows:
        return {
            "status": "ok",
            "result": {
                "count": 0,
                "workflows": [],
                "message": "No workflows saved yet. Use workflow.save to create one.",
            }
        }
    summary = [
        {
            "name": wf.get("name"),
            "description": wf.get("description", ""),
            "trigger_keywords": wf.get("trigger_keywords", []),
            "steps": len(wf.get("steps", [])),
            "run_count": wf.get("run_count", 0),
        }
        for wf in workflows
    ]
    return {"status": "ok", "result": {"count": len(workflows), "workflows": summary}}


@register_tool(
    name="workflow.run",
    description="Load and return steps from a saved workflow, substituting any variables.",
    category="Workflow",
    example='{"name": "research_topic", "variables": {"target": "Notion"}}',
    notes="Returns the expanded steps list. The agent should then execute each step in order.",
)
def workflow_run(name: str, variables: dict = None):
    """
    Load a workflow and substitute variables.

    Args:
        name: Workflow name (as saved by workflow.save)
        variables: Dict of {variable_name: value} substitutions for step descriptions.
    """
    _ensure_workflows_dir()
    path = _workflow_path(name)
    if not path.exists():
        # Try fuzzy match
        all_wf = _load_all_workflows()
        match = next((w for w in all_wf if name.lower() in w.get("name", "")), None)
        if not match:
            names = [w.get("name") for w in all_wf]
            return {"status": "error", "error": f"Workflow '{name}' not found. Available: {names}"}
        path = _workflow_path(match["name"])

    wf = json.loads(path.read_text(encoding="utf-8"))
    steps = wf.get("steps", [])
    subs = variables or {}

    # Substitute variables in step descriptions
    expanded = []
    for step in steps:
        for var, val in subs.items():
            step = step.replace(f"{{{var}}}", str(val))
        expanded.append(step)

    # Update run count
    wf["run_count"] = wf.get("run_count", 0) + 1
    wf["last_run"] = datetime.now().isoformat()
    path.write_text(json.dumps(wf, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "status": "ok",
        "result": {
            "name": wf.get("name"),
            "description": wf.get("description", ""),
            "steps": expanded,
            "step_count": len(expanded),
            "variables_applied": subs,
        }
    }


@register_tool(
    name="workflow.delete",
    description="Delete a saved workflow by name.",
    category="Workflow",
    example='{"name": "research_topic"}',
)
def workflow_delete(name: str):
    """Delete a workflow file."""
    path = _workflow_path(name)
    if not path.exists():
        return {"status": "error", "error": f"Workflow '{name}' not found"}
    path.unlink()
    return {"status": "ok", "result": {"deleted": name}}


def auto_match_workflow(user_message: str) -> dict | None:
    """
    Check if any saved workflow matches the user message by trigger keywords.
    Returns the workflow dict if matched, else None.
    Used at the start of handle_streaming() to skip LLM planning.
    """
    try:
        workflows = _load_all_workflows()
        return _match_workflow(user_message, workflows)
    except Exception:
        return None
