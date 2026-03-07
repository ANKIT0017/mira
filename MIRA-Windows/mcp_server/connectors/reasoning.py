"""
Reasoning connector for MIRA
Provides a 'think' tool: call the LLM with a plain prompt to reason over
already-fetched data. Also supports reading a SANDBOX file as context via 'file'.
"""
import sys
from pathlib import Path
from ..tools import register_tool

# Ensure agent package is importable regardless of working directory
_root = str(Path(__file__).parent.parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

_SANDBOX = (Path(__file__).parent.parent.parent / "SANDBOX").resolve()

try:
    from agent.model_adapter import generate as _generate
    _LLM_AVAILABLE = True
except Exception:
    _LLM_AVAILABLE = False


def _read_sandbox_file(filename: str) -> str:
    """Read a file from SANDBOX and return its text content."""
    clean = filename.lstrip('/\\')
    for prefix in ('SANDBOX/', 'SANDBOX\\', 'sandbox/', 'sandbox\\'):
        if clean.startswith(prefix):
            clean = clean[len(prefix):]
            break
    p = (_SANDBOX / clean).resolve()
    try:
        p.relative_to(_SANDBOX)
    except ValueError:
        raise ValueError(f"file '{filename}' is outside SANDBOX")
    if not p.exists():
        raise FileNotFoundError(f"File not found in SANDBOX: {clean}")
    return p.read_text(encoding='utf-8', errors='replace')


@register_tool(
    "think",
    category="Reasoning",
    description=(
        "Reason over data or synthesize a response using the LLM directly. "
        "Accepts buffer id (buffer=1), inline text (context='...'), or a SANDBOX file (file='data.txt') as context — "
        "all three can be combined with a prompt. "
        "Use to analyse, summarise, format, or compose output without a full agent loop."
    ),
    example='{"tool": "think", "args": {"prompt": "Format as a markdown report:", "buffer": 1}}',
    notes=(
        "prompt: instruction for the LLM. "
        "buffer: optional int — agent buffer id whose content is appended to the prompt. "
        "context: optional inline text string appended to the prompt. "
        "file: optional SANDBOX filename whose content is appended to the prompt. "
        "model: optional model override. "
        "max_tokens: optional int (default 1024)."
    )
)
def think(prompt: str, file: str = None, model: str = None, max_tokens: int = 1024) -> dict:
    """
    Run a single LLM call and return the text response.

    Args:
        prompt:     Instruction / question for the LLM.
        file:       Optional SANDBOX filename to read and append to the prompt as context.
        model:      Optional model override (e.g. 'qwen3:4b-instruct').
        max_tokens: Max tokens for the response (default 1024).

    Returns:
        {"text": "...", "model": "..."}
    """
    if not _LLM_AVAILABLE:
        return {
            "status": "error",
            "error": "LLM not available — agent.model_adapter could not be imported."
        }

    if not prompt:
        return {"status": "error", "error": "prompt is required"}

    full_prompt = prompt

    # Append SANDBOX file content if requested
    if file:
        try:
            content = _read_sandbox_file(file)
            full_prompt = f"{prompt}\n\n--- FILE: {file} ---\n{content}\n--- END FILE ---"
        except (ValueError, FileNotFoundError) as e:
            return {"status": "error", "error": str(e)}

    try:
        # Normalize model arg: empty string / 'default' / 'none' all mean "use agent default"
        _INVALID_MODELS = {"", "default", "none", "null"}
        if not model or model.strip().lower() in _INVALID_MODELS:
            model = None
        kwargs = {"max_tokens": max_tokens}
        if model:
            kwargs["model"] = model

        result = _generate(full_prompt, **kwargs)

        if isinstance(result, dict):
            return result

        return {"text": str(result), "model": model or "default"}

    except Exception as e:
        return {"status": "error", "error": f"LLM call failed: {e}"}
