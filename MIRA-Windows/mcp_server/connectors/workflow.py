import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..tools import register_tool

_WORKSPACE = Path(__file__).parent.parent.parent.resolve()
_WORKFLOW_DIR = (_WORKSPACE / "SANDBOX" / "workflows").resolve()
_WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)


def _slugify(name: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9_\-\s]", "", (name or "")).strip().lower()
    base = re.sub(r"\s+", "_", base)
    return base[:80]


def _workflow_path(name: str) -> Path:
    slug = _slugify(name)
    if not slug:
        raise ValueError("workflow name is required")
    p = (_WORKFLOW_DIR / f"{slug}.json").resolve()
    try:
        p.relative_to(_WORKFLOW_DIR)
    except ValueError:
        raise ValueError("invalid workflow path")
    return p


def _ensure_steps(steps):
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except Exception as e:
            raise ValueError(f"steps must be a list or valid JSON list: {e}")
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps must be a non-empty list")
    out = []
    for i, st in enumerate(steps):
        if not isinstance(st, dict):
            raise ValueError(f"steps[{i}] must be an object")
        goal = str(st.get("goal", "")).strip()
        if not goal:
            raise ValueError(f"steps[{i}].goal is required")
        cats = st.get("categories", [])
        if isinstance(cats, str):
            cats = [c.strip() for c in re.split(r"[,/|]+", cats) if c.strip()]
        if not isinstance(cats, list):
            cats = []
        out.append({"goal": goal, "categories": [str(c).strip() for c in cats if str(c).strip()]})
    return out


@register_tool(
    "workflow.save",
    category="Workflows",
    description="Save a reusable workflow JSON under SANDBOX/workflows/",
    example='{"tool":"workflow.save","args":{"name":"research_company","trigger_keywords":["research"],"steps":[{"goal":"Search for {target}","categories":["Web & Search"]}],"variables":["target"]}}',
)
def workflow_save(
    name: str,
    steps,
    trigger_keywords: list | None = None,
    variables: list | None = None,
    description: str = "",
) -> dict:
    p = _workflow_path(name)
    step_list = _ensure_steps(steps)
    triggers = [str(x).strip().lower() for x in (trigger_keywords or []) if str(x).strip()]
    vars_list = [str(x).strip() for x in (variables or []) if str(x).strip()]
    payload = {
        "name": _slugify(name),
        "display_name": str(name).strip(),
        "description": str(description or "").strip(),
        "trigger_keywords": triggers,
        "variables": vars_list,
        "steps": step_list,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if p.exists():
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
            payload["created_at"] = existing.get("created_at") or payload["updated_at"]
        except Exception:
            payload["created_at"] = payload["updated_at"]
    else:
        payload["created_at"] = payload["updated_at"]
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"path": str(p), "name": payload["name"], "steps": len(step_list)}


@register_tool(
    "workflow.list",
    category="Workflows",
    description="List available saved workflows in SANDBOX/workflows/",
    example='{"tool":"workflow.list","args":{}}',
)
def workflow_list() -> dict:
    _WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for f in sorted(_WORKFLOW_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            items.append(
                {
                    "name": data.get("name") or f.stem,
                    "display_name": data.get("display_name") or data.get("name") or f.stem,
                    "trigger_keywords": data.get("trigger_keywords", []),
                    "variables": data.get("variables", []),
                    "steps": len(data.get("steps", [])),
                    "path": str(f),
                }
            )
        except Exception:
            items.append(
                {
                    "name": f.stem,
                    "display_name": f.stem,
                    "trigger_keywords": [],
                    "variables": [],
                    "steps": 0,
                    "path": str(f),
                    "error": "invalid_json",
                }
            )
    return {"count": len(items), "workflows": items}


@register_tool(
    "workflow.run",
    category="Workflows",
    description="Load a workflow and substitute variables into its steps.",
    example='{"tool":"workflow.run","args":{"name":"research_company","variables":{"target":"Notion"}}}',
)
def workflow_run(name: str, variables: dict | None = None) -> dict:
    p = _workflow_path(name)
    if not p.exists():
        raise FileNotFoundError(f"workflow '{name}' not found")
    data = json.loads(p.read_text(encoding="utf-8"))
    vars_map = {str(k): str(v) for k, v in (variables or {}).items()}
    out_steps = []
    for st in data.get("steps", []):
        goal = str(st.get("goal", ""))
        for k, v in vars_map.items():
            goal = goal.replace("{" + k + "}", v)
        out_steps.append({"goal": goal, "categories": st.get("categories", [])})
    return {
        "name": data.get("name") or p.stem,
        "display_name": data.get("display_name") or data.get("name") or p.stem,
        "steps": out_steps,
        "variables": vars_map,
    }

