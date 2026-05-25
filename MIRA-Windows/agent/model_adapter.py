import subprocess
import os
import json
import time
import threading
import requests
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ============================================================
# llama.cpp SERVER CONFIG
# The local model runs via llama.cpp server (OpenAI-compatible API).
# Start it with start_llama_server.bat before running MIRA.
# Override via .env: LLAMA_SERVER_URL, LLAMA_MODEL_NAME
# ============================================================
LLAMA_BASE_URL  = os.getenv("LLAMA_SERVER_URL",  "http://127.0.0.1:8080")
LLAMA_MODEL_NAME = os.getenv("LLAMA_MODEL_NAME", "gemma-4-E2B-it")  # label only


# ============================================================
# SMART RATE LIMIT MANAGEMENT
# Tracks cooldowns per provider so we skip rate-limited ones
# instead of wasting requests.
# ============================================================

_provider_cooldowns = {}  # {"groq": timestamp_when_available, ...}
COOLDOWN_SECONDS = 65     # How long to skip a provider after rate limit

def _is_available(provider: str) -> bool:
    """Check if a provider is past its cooldown period."""
    if provider not in _provider_cooldowns:
        return True
    return time.time() >= _provider_cooldowns[provider]

def _mark_rate_limited(provider: str):
    """Mark a provider as rate-limited for COOLDOWN_SECONDS."""
    _provider_cooldowns[provider] = time.time() + COOLDOWN_SECONDS
    print(f"[RATELIMIT] {provider} marked as rate-limited for {COOLDOWN_SECONDS}s")


# ============================================================
# LLM USAGE + COST TRACKING
# Tracks per-call token usage and run totals.
# ============================================================

_LLM_USAGE_LOG_ENABLED = os.getenv("MIRA_LOG_LLM_USAGE", "1").strip().lower() not in {
    "0", "false", "no", "off"
}
_LLM_INPUT_COST_PER_MTOK = float(os.getenv("MIRA_LLM_INPUT_COST_PER_MTOK", "0") or 0)
_LLM_OUTPUT_COST_PER_MTOK = float(os.getenv("MIRA_LLM_OUTPUT_COST_PER_MTOK", "0") or 0)

_llm_usage_lock = threading.Lock()
_llm_usage_totals = {
    "calls": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "estimated_calls": 0,
    "cost_usd": 0.0,
}
_llm_usage_by_provider = {}


def reset_llm_usage_totals() -> None:
    """Reset per-run LLM usage totals."""
    with _llm_usage_lock:
        _llm_usage_totals["calls"] = 0
        _llm_usage_totals["prompt_tokens"] = 0
        _llm_usage_totals["completion_tokens"] = 0
        _llm_usage_totals["total_tokens"] = 0
        _llm_usage_totals["estimated_calls"] = 0
        _llm_usage_totals["cost_usd"] = 0.0
        _llm_usage_by_provider.clear()


def get_llm_usage_totals() -> dict:
    """Return a snapshot of current run-level LLM usage totals."""
    with _llm_usage_lock:
        return {
            "calls": int(_llm_usage_totals["calls"]),
            "prompt_tokens": int(_llm_usage_totals["prompt_tokens"]),
            "completion_tokens": int(_llm_usage_totals["completion_tokens"]),
            "total_tokens": int(_llm_usage_totals["total_tokens"]),
            "estimated_calls": int(_llm_usage_totals["estimated_calls"]),
            "cost_usd": float(_llm_usage_totals["cost_usd"]),
            "by_provider": {k: dict(v) for k, v in _llm_usage_by_provider.items()},
        }


def render_llm_usage_summary() -> str:
    """Render a compact terminal-friendly summary of LLM token usage."""
    snap = get_llm_usage_totals()
    if snap["calls"] <= 0:
        return "[LLM USAGE] No successful LLM calls recorded in this run."

    lines = [
        (
            "[LLM USAGE SUMMARY] "
            f"calls={snap['calls']} "
            f"prompt_tokens={snap['prompt_tokens']} "
            f"completion_tokens={snap['completion_tokens']} "
            f"total_tokens={snap['total_tokens']}"
        )
    ]

    if snap["estimated_calls"] > 0:
        lines.append(
            f"[LLM USAGE SUMMARY] estimated_calls={snap['estimated_calls']} "
            "(provider usage metadata missing for those calls)"
        )

    for provider, stats in sorted(snap["by_provider"].items()):
        lines.append(
            (
                f"[LLM USAGE SUMMARY] provider={provider} "
                f"calls={int(stats.get('calls', 0))} "
                f"total_tokens={int(stats.get('total_tokens', 0))}"
            )
        )

    if _LLM_INPUT_COST_PER_MTOK > 0 or _LLM_OUTPUT_COST_PER_MTOK > 0:
        lines.append(f"[LLM USAGE SUMMARY] estimated_cost_usd=${snap['cost_usd']:.6f}")

    return "\n".join(lines)


def _safe_int(value) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _estimate_tokens_from_text(text: str) -> int:
    if not text:
        return 0
    # Conservative fallback estimate when provider does not return usage metadata.
    return max(1, len(str(text)) // 4)


def _estimate_tokens_from_messages(messages: list[dict] | None) -> int:
    if not messages:
        return 0
    parts = []
    for msg in messages:
        if not isinstance(msg, dict):
            parts.append(str(msg))
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            chunk = []
            for item in content:
                if isinstance(item, dict):
                    chunk.append(str(item.get("text", "")))
                else:
                    chunk.append(str(item))
            parts.append(" ".join(chunk))
        else:
            parts.append(str(content))
    return _estimate_tokens_from_text("\n".join(parts))


def _estimate_tokens_from_gemini_content(content_dict: dict | None) -> int:
    if not isinstance(content_dict, dict):
        return 0
    parts = []
    si = content_dict.get("systemInstruction", {})
    if isinstance(si, dict):
        for p in si.get("parts", []) or []:
            if isinstance(p, dict):
                parts.append(str(p.get("text", "")))
    for item in content_dict.get("contents", []) or []:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        for p in item.get("parts", []) or []:
            if isinstance(p, dict):
                parts.append(str(p.get("text", "")))
            else:
                parts.append(str(p))
    return _estimate_tokens_from_text("\n".join(parts))


def _extract_usage_openai_payload(data: dict | None) -> dict | None:
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None

    prompt_tokens = _safe_int(
        usage.get("prompt_tokens", usage.get("input_tokens", usage.get("promptTokenCount", 0)))
    )
    completion_tokens = _safe_int(
        usage.get("completion_tokens", usage.get("output_tokens", usage.get("candidatesTokenCount", 0)))
    )
    total_tokens = _safe_int(usage.get("total_tokens", usage.get("totalTokenCount", 0)))

    if total_tokens <= 0:
        total_tokens = prompt_tokens + completion_tokens
    if prompt_tokens <= 0 and completion_tokens <= 0 and total_tokens <= 0:
        return None

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _extract_usage_gemini_payload(data: dict | None) -> dict | None:
    if not isinstance(data, dict):
        return None
    usage = data.get("usageMetadata")
    if not isinstance(usage, dict):
        return None

    prompt_tokens = _safe_int(usage.get("promptTokenCount", 0))
    completion_tokens = _safe_int(
        usage.get("candidatesTokenCount", usage.get("outputTokenCount", 0))
    )
    total_tokens = _safe_int(usage.get("totalTokenCount", 0))

    if total_tokens <= 0:
        total_tokens = prompt_tokens + completion_tokens
    if prompt_tokens <= 0 and completion_tokens <= 0 and total_tokens <= 0:
        return None

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _record_llm_usage(
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    estimated: bool = False,
) -> None:
    prompt_tokens = max(0, _safe_int(prompt_tokens))
    completion_tokens = max(0, _safe_int(completion_tokens))
    total_tokens = max(0, _safe_int(total_tokens))

    if total_tokens <= 0:
        total_tokens = prompt_tokens + completion_tokens
    if total_tokens <= 0 and prompt_tokens <= 0 and completion_tokens <= 0:
        return

    if completion_tokens <= 0 and total_tokens > prompt_tokens:
        completion_tokens = max(0, total_tokens - prompt_tokens)

    cost_usd = (
        (prompt_tokens / 1_000_000.0) * _LLM_INPUT_COST_PER_MTOK
        + (completion_tokens / 1_000_000.0) * _LLM_OUTPUT_COST_PER_MTOK
    )

    with _llm_usage_lock:
        _llm_usage_totals["calls"] += 1
        _llm_usage_totals["prompt_tokens"] += prompt_tokens
        _llm_usage_totals["completion_tokens"] += completion_tokens
        _llm_usage_totals["total_tokens"] += total_tokens
        _llm_usage_totals["cost_usd"] += cost_usd
        if estimated:
            _llm_usage_totals["estimated_calls"] += 1

        pstats = _llm_usage_by_provider.setdefault(
            provider,
            {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
            },
        )
        pstats["calls"] += 1
        pstats["prompt_tokens"] += prompt_tokens
        pstats["completion_tokens"] += completion_tokens
        pstats["total_tokens"] += total_tokens
        pstats["cost_usd"] += cost_usd

        call_index = int(_llm_usage_totals["calls"])

    if _LLM_USAGE_LOG_ENABLED:
        est_note = " (estimated)" if estimated else ""
        cost_note = (
            f" cost_usd=${cost_usd:.6f}"
            if (_LLM_INPUT_COST_PER_MTOK > 0 or _LLM_OUTPUT_COST_PER_MTOK > 0)
            else ""
        )
        print(
            "[LLM USAGE]"
            f" call={call_index}"
            f" provider={provider}"
            f" model={model}"
            f" prompt_tokens={prompt_tokens}"
            f" completion_tokens={completion_tokens}"
            f" total_tokens={total_tokens}"
            f"{est_note}{cost_note}"
        )


def _record_openai_response_usage(
    provider: str,
    model: str,
    payload: dict,
    prompt_text: str = "",
    messages: list[dict] | None = None,
    completion_text: str = "",
) -> None:
    usage = _extract_usage_openai_payload(payload)
    if usage:
        _record_llm_usage(
            provider=provider,
            model=model,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            estimated=False,
        )
        return

    prompt_tokens_est = _estimate_tokens_from_messages(messages) if messages else _estimate_tokens_from_text(prompt_text)
    completion_tokens_est = _estimate_tokens_from_text(completion_text)
    _record_llm_usage(
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens_est,
        completion_tokens=completion_tokens_est,
        total_tokens=prompt_tokens_est + completion_tokens_est,
        estimated=True,
    )


def _record_gemini_response_usage(
    provider: str,
    model: str,
    payload: dict,
    prompt_text: str = "",
    content_dict: dict | None = None,
    completion_text: str = "",
) -> None:
    usage = _extract_usage_gemini_payload(payload)
    if usage:
        _record_llm_usage(
            provider=provider,
            model=model,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            estimated=False,
        )
        return

    prompt_tokens_est = _estimate_tokens_from_text(prompt_text)
    if prompt_tokens_est <= 0 and content_dict is not None:
        prompt_tokens_est = _estimate_tokens_from_gemini_content(content_dict)
    completion_tokens_est = _estimate_tokens_from_text(completion_text)
    _record_llm_usage(
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens_est,
        completion_tokens=completion_tokens_est,
        total_tokens=prompt_tokens_est + completion_tokens_est,
        estimated=True,
    )


# ============================================================
# PROVIDER FUNCTIONS
# Each returns: string on success, dict with error/rate_limited on failure
# ============================================================

def generate_groq(prompt: str, model: str = "llama-3.3-70b-versatile", max_tokens: int = 8000, temperature: float = 0.2):
    """Groq API — ultra-fast inference (free: 30 req/min, 6000 TPM)."""
    try:
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            return {"error": "GROQ_API_KEY not set", "rate_limited": False}
        
        base_url = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
        max_tokens = min(max_tokens, 4096)
        
        print(f"[PROVIDER] Groq → {model}")
        
        response = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": temperature
            },
            timeout=30
        )
        
        if response.status_code == 429:
            _mark_rate_limited("groq")
            return {"error": "Rate limited", "rate_limited": True}
        elif response.status_code != 200:
            return {"error": f"API Error {response.status_code}", "rate_limited": False}

        data = response.json()
        content = data["choices"][0]["message"]["content"]
        _record_openai_response_usage(
            provider="groq",
            model=model,
            payload=data,
            prompt_text=prompt,
            completion_text=content,
        )
        return content
        
    except Exception as e:
        return {"error": str(e), "rate_limited": False}


def generate_gemini(prompt: str, model: str = "gemini-2.5-flash", max_tokens: int = 8000, temperature: float = 0.2):
    """Google Gemini API — multimodal, 2.5 series (free: 15 RPM)."""
    try:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return {"error": "GEMINI_API_KEY not set", "rate_limited": False}
        
        print(f"[PROVIDER] Gemini → {model}")
        
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        
        response = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}
            },
            timeout=60
        )
        
        if response.status_code == 429:
            _mark_rate_limited("gemini")
            return {"error": "Rate limited", "rate_limited": True}
        elif response.status_code != 200:
            return {"error": f"API Error {response.status_code}", "rate_limited": False}
        
        data = response.json()
        if "candidates" in data and data["candidates"]:
            parts = data["candidates"][0].get("content", {}).get("parts", [])
            if parts and "text" in parts[0]:
                content = parts[0]["text"]
                _record_gemini_response_usage(
                    provider="gemini",
                    model=model,
                    payload=data,
                    prompt_text=prompt,
                    completion_text=content,
                )
                return content

        _record_gemini_response_usage(
            provider="gemini",
            model=model,
            payload=data,
            prompt_text=prompt,
            completion_text="",
        )
        
        return {"error": "Unexpected response format", "rate_limited": False}
        
    except Exception as e:
        return {"error": str(e), "rate_limited": False}


def generate_cerebras(prompt: str, model: str = "llama-3.3-70b", max_tokens: int = 8000, temperature: float = 0.2):
    """Cerebras API — fastest inference on the planet (free: 30 RPM, 60K TPM)."""
    try:
        api_key = os.getenv("CEREBRAS_API_KEY")
        if not api_key:
            return {"error": "CEREBRAS_API_KEY not set", "rate_limited": False}
        
        print(f"[PROVIDER] Cerebras → {model}")
        
        response = requests.post(
            "https://api.cerebras.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": min(max_tokens, 8192),
                "temperature": temperature
            },
            timeout=30
        )
        
        if response.status_code == 429:
            _mark_rate_limited("cerebras")
            return {"error": "Rate limited", "rate_limited": True}
        elif response.status_code != 200:
            return {"error": f"API Error {response.status_code}", "rate_limited": False}

        data = response.json()
        content = data["choices"][0]["message"]["content"]
        _record_openai_response_usage(
            provider="cerebras",
            model=model,
            payload=data,
            prompt_text=prompt,
            completion_text=content,
        )
        return content
        
    except Exception as e:
        return {"error": str(e), "rate_limited": False}


def generate_sambanova(prompt: str, model: str = "Meta-Llama-3.3-70B-Instruct", max_tokens: int = 8000, temperature: float = 0.2):
    """SambaNova API — free Llama 3.1 405B access (free tier available)."""
    try:
        api_key = os.getenv("SAMBANOVA_API_KEY")
        if not api_key:
            return {"error": "SAMBANOVA_API_KEY not set", "rate_limited": False}
        
        print(f"[PROVIDER] SambaNova → {model}")
        
        response = requests.post(
            "https://api.sambanova.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": min(max_tokens, 4096),
                "temperature": temperature
            },
            timeout=60
        )
        
        if response.status_code == 429:
            _mark_rate_limited("sambanova")
            return {"error": "Rate limited", "rate_limited": True}
        elif response.status_code != 200:
            return {"error": f"API Error {response.status_code}", "rate_limited": False}

        data = response.json()
        content = data["choices"][0]["message"]["content"]
        _record_openai_response_usage(
            provider="sambanova",
            model=model,
            payload=data,
            prompt_text=prompt,
            completion_text=content,
        )
        return content
        
    except Exception as e:
        return {"error": str(e), "rate_limited": False}


def generate_openrouter(prompt: str, model: str = "meta-llama/llama-3.3-70b-instruct:free", max_tokens: int = 8000, temperature: float = 0.2):
    """OpenRouter API — aggregator with free models (free tier: 20 RPM)."""
    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            return {"error": "OPENROUTER_API_KEY not set", "rate_limited": False}
        
        print(f"[PROVIDER] OpenRouter → {model}")
        
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://mira-agent.local",
                "X-Title": "MIRA Agent"
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": min(max_tokens, 4096),
                "temperature": temperature
            },
            timeout=60
        )
        
        if response.status_code == 429:
            _mark_rate_limited("openrouter")
            return {"error": "Rate limited", "rate_limited": True}
        elif response.status_code != 200:
            return {"error": f"API Error {response.status_code}", "rate_limited": False}

        data = response.json()
        content = data["choices"][0]["message"]["content"]
        _record_openai_response_usage(
            provider="openrouter",
            model=model,
            payload=data,
            prompt_text=prompt,
            completion_text=content,
        )
        return content
        
    except Exception as e:
        return {"error": str(e), "rate_limited": False}




# ============================================================
# FALLBACK CHAIN — The core hack
# Instead of 2 providers, we rotate through 5+.
# Each has independent rate limits.
# 5 providers × 30 RPM each = ~150 RPM effective capacity.
# ============================================================

FALLBACK_CHAIN = [
    # (provider_name, generator_function, default_model)
    ("groq",       generate_groq,       "llama-3.3-70b-versatile"),
    ("gemini",     generate_gemini,      "gemini-2.5-flash-lite"),
    ("cerebras",   generate_cerebras,    "llama-3.3-70b"),
    ("sambanova",  generate_sambanova,   "Meta-Llama-3.3-70B-Instruct"),
    ("openrouter", generate_openrouter,  "meta-llama/llama-3.3-70b-instruct:free"),
]


def _try_fallback_chain(prompt: str, max_tokens: int, skip_provider: str = None, temperature: float = 0.2):
    """
    Try each provider in the fallback chain, skipping rate-limited ones.
    Returns the first successful result.
    """
    for name, gen_fn, default_model in FALLBACK_CHAIN:
        if name == skip_provider:
            continue
        if not _is_available(name):
            print(f"[ROTATION] Skipping {name} (still in cooldown)")
            continue
        
        # Check if API key exists before trying
        key_map = {
            "groq": "GROQ_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "cerebras": "CEREBRAS_API_KEY",
            "sambanova": "SAMBANOVA_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }
        if not os.getenv(key_map.get(name, "")):
            continue
        
        print(f"[ROTATION] Trying {name} ({default_model})...")
        result = gen_fn(prompt, model=default_model, max_tokens=max_tokens, temperature=temperature)
        
        if isinstance(result, dict) and result.get("rate_limited"):
            continue  # Already marked by the gen_fn
        
        if isinstance(result, dict) and "error" in result:
            print(f"[ROTATION] {name} error: {result['error']}, trying next...")
            continue
        
        return result
    
    # All providers exhausted — wait and retry the one with shortest cooldown
    print("[ROTATION] All providers exhausted! Waiting 10s for cooldown...")
    time.sleep(10)
    
    # Clear all cooldowns and try once more
    _provider_cooldowns.clear()
    for name, gen_fn, default_model in FALLBACK_CHAIN:
        if not os.getenv({"groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY",
                          "cerebras": "CEREBRAS_API_KEY", "sambanova": "SAMBANOVA_API_KEY",
                          "openrouter": "OPENROUTER_API_KEY"}.get(name, "")):
            continue
        result = gen_fn(prompt, model=default_model, max_tokens=max_tokens, temperature=temperature)
        if isinstance(result, str):
            return result
    
    return "[All cloud providers are rate-limited. Please use a local model or wait 60 seconds.]"


# ============================================================
# llama.cpp LOCAL BACKEND HELPER
# Shared by generate(), generate_with_messages(), generate_with_native_tools().
# Context size, KV cache quant, RoPE, and GPU layers are set at SERVER start
# (start_llama_server.bat) — not repeated per request.
# ============================================================

def _generate_llamacpp_chat(
    messages: list[dict],
    max_tokens: int = 2048,
    timeout: int = 240,
    temperature: float = 0.1,
    tools: list[dict] | None = None,
) -> str | dict:
    """
    POST to llama.cpp server /v1/chat/completions (OpenAI-compatible).

    Returns str on plain generation, dict when tools are supplied
    (mirrors generate_with_native_tools return shape).
    """
    import re as _re
    try:
        print(f"[PROVIDER] llama.cpp → {LLAMA_MODEL_NAME}")
        body: dict = {
            "model": LLAMA_MODEL_NAME,
            "messages": messages,
            "max_tokens": min(max_tokens, 2048),
            "temperature": temperature,
            "stream": False,
        }
        if tools:
            body["tools"] = tools

        resp = requests.post(
            f"{LLAMA_BASE_URL}/v1/chat/completions",
            json=body,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]

        if tools:
            content = (msg.get("content") or "").strip()
            tool_calls = msg.get("tool_calls") or []
            completion_for_estimate = content
            if not completion_for_estimate and tool_calls:
                completion_for_estimate = json.dumps(tool_calls)[:2000]
            _record_openai_response_usage(
                provider="llamacpp",
                model=LLAMA_MODEL_NAME,
                payload=data,
                messages=messages,
                completion_text=completion_for_estimate,
            )
            return {"content": content, "tool_calls": tool_calls}

        result = (msg.get("content") or "").strip()
        _record_openai_response_usage(
            provider="llamacpp",
            model=LLAMA_MODEL_NAME,
            payload=data,
            messages=messages,
            completion_text=result,
        )
        # Strip residual <think>...</think> blocks only when a visible answer remains.
        # Some local models put the entire useful response inside a think block; if we
        # remove it unconditionally, downstream tools see an empty success payload.
        _without_think = _re.sub(r"<think>.*?</think>", "", result, flags=_re.DOTALL).strip()
        if _without_think:
            result = _without_think
        return result

    except requests.exceptions.Timeout:
        err = "[Error: llama.cpp server call timed out]"
        return {"error": err} if tools else err
    except requests.exceptions.ConnectionError:
        err = (
            "[Error: Could not connect to llama.cpp server. "
            f"Run start_llama_server.bat first (or set LLAMA_SERVER_URL in .env). "
            f"Expected at: {LLAMA_BASE_URL}]"
        )
        return {"error": err} if tools else err
    except Exception as e:
        err = f"[Error running llama.cpp: {e}]"
        return {"error": err} if tools else err


# ============================================================
# MAIN ENTRY POINT
# ============================================================

def generate(prompt: str, model: str = "local", max_tokens: int = 92024, timeout: int = 240, temperature: float = 0.1):
    """
    Universal generation function with automatic multi-provider rotation.
    
    Routes:
    - 'groq:*'      → Groq API (fast, 70B models)
    - 'gemini:*'     → Gemini API (multimodal, 2.5 series)
    - 'cerebras:*'   → Cerebras API (fastest, 70B)
    - 'sambanova:*'  → SambaNova API (405B)
    - 'openrouter:*' → OpenRouter (aggregator, free models)
    - Local models   → llama.cpp server
    
    Smart rotation: If the chosen provider is rate-limited, tries the
    entire fallback chain before giving up. Cooldown tracking ensures
    we don't waste requests on providers we know are rate-limited.
    """
    
    # Map of cloud provider prefixes to their functions
    provider_map = {
        "groq:":       ("groq",       generate_groq),
        "gemini:":     ("gemini",     generate_gemini),
        "cerebras:":   ("cerebras",   generate_cerebras),
        "sambanova:":  ("sambanova",  generate_sambanova),
        "openrouter:": ("openrouter", generate_openrouter),
    }
    
    # Check if it's a cloud model
    for prefix, (provider_name, gen_fn) in provider_map.items():
        if model.startswith(prefix):
            actual_model = model[len(prefix):]
            
            # Try primary provider first (if not in cooldown)
            if _is_available(provider_name):
                result = gen_fn(prompt, model=actual_model, max_tokens=max_tokens, temperature=temperature)
                
                if isinstance(result, str):
                    return result  # Success!
                
                if isinstance(result, dict) and result.get("rate_limited"):
                    print(f"[ROTATION] {provider_name} rate limited, trying fallback chain...")
                    return _try_fallback_chain(prompt, max_tokens, skip_provider=provider_name, temperature=temperature)
                
                if isinstance(result, dict) and "error" in result:
                    return f"[{provider_name.title()} API Error] {result['error']}"
            else:
                # Primary is in cooldown, go straight to fallback chain
                print(f"[ROTATION] {provider_name} in cooldown, trying fallback chain...")
                return _try_fallback_chain(prompt, max_tokens, skip_provider=provider_name, temperature=temperature)
            
            return result
    
    # ── Local llama.cpp server (OpenAI-compatible API) ────────────────────
    # Context size, KV cache quant, RoPE scaling, and GPU layers are all
    # configured at server startup via start_llama_server.bat — not here.
    result = _generate_llamacpp_chat(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        timeout=timeout,
        temperature=temperature,
    )
    # Auto-fallback: if local server is down, route through cloud chain
    if isinstance(result, str) and "Could not connect to llama.cpp" in result:
        print("[ROTATION] llama.cpp unreachable — auto-falling back to cloud chain")
        cloud = _try_fallback_chain(prompt, max_tokens, temperature=temperature)
        return cloud if cloud else result
    return result


def generate_streaming(prompt: str, model: str = "local", max_tokens: int = 512):
    """
    Stream tokens from the llama.cpp server via SSE for real-time voice output.
    Yields text chunks as they arrive over the Server-Sent Events stream.

    Requires the llama.cpp server to be running (start_llama_server.bat).
    """
    try:
        print(f"[PROVIDER] llama.cpp SSE stream → {LLAMA_MODEL_NAME}")
        resp = requests.post(
            f"{LLAMA_BASE_URL}/v1/chat/completions",
            json={
                "model": LLAMA_MODEL_NAME,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.7,
                "stream": True,
            },
            stream=True,
            timeout=120,
        )
        resp.raise_for_status()
        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
                delta = chunk["choices"][0]["delta"].get("content", "")
                if delta:
                    yield delta
            except Exception:
                continue
    except requests.exceptions.ConnectionError:
        # llama.cpp server not running — fall back to one cloud call
        print("[ROTATION] llama.cpp stream unreachable — falling back to cloud")
        flat_resp = _try_fallback_chain(prompt, max_tokens)
        if flat_resp:
            yield flat_resp
        else:
            yield "[Error: llama.cpp server is not running. Start it with start_llama_server.bat]"
    except Exception as e:
        yield f"[Stream error: {e}]"


# ============================================================
# STRUCTURED MESSAGE GENERATION
# Uses system/user role separation for providers that support it.
# This enables provider-native caching of system-role content.
# ============================================================

def generate_with_messages(
    messages: list[dict],
    model: str = "local",
    max_tokens: int = 92024,
    timeout: int = 240,
    temperature: float = 0.1,
    thinking: bool = False,
) -> str:
    """
    Generate with structured messages (system/user role separation).

    For cloud providers that accept OpenAI-compatible messages format,
    this sends proper role-separated messages instead of flattening
    everything into a single user message. This enables:
    - Server-side system message caching (Groq, Cerebras)
    - Better instruction following through role clarity
    - Reduced per-request token costs for stable prefixes

    For local llama.cpp models, uses /v1/chat/completions so the
    model's proper chat template is applied to the message list.
    """
    # Map of cloud provider prefixes to their functions and capabilities
    provider_map = {
        "groq:":       ("groq",       generate_groq,       True),
        "gemini:":     ("gemini",     generate_gemini,      False),  # Gemini uses systemInstruction format
        "cerebras:":   ("cerebras",   generate_cerebras,    True),
        "sambanova:":  ("sambanova",  generate_sambanova,   True),
        "openrouter:": ("openrouter", generate_openrouter,  True),
    }

    for prefix, (provider_name, gen_fn, supports_messages) in provider_map.items():
        if model.startswith(prefix):
            actual_model = model[len(prefix):]

            if supports_messages and messages:
                # Send structured messages directly via OpenAI-compatible endpoint
                return _generate_with_openai_messages(
                    provider_name, actual_model, messages,
                    max_tokens=max_tokens, temperature=temperature,
                )
            elif provider_name == "gemini" and isinstance(messages, dict):
                # Gemini systemInstruction dict from ContextCompiler
                return _generate_with_gemini_content(
                    actual_model, messages,
                    max_tokens=max_tokens, temperature=temperature,
                )
            else:
                # Fall back to flat prompt
                flat = _flatten_messages(messages) if isinstance(messages, list) and messages else ""
                return gen_fn(flat, model=actual_model, max_tokens=max_tokens, temperature=temperature)

    # ── Local llama.cpp server (OpenAI-compatible /v1/chat/completions) ────
    # The llama.cpp server applies the model's proper chat template server-side.
    # Context size / KV cache quant / RoPE / GPU layers are set at server start.
    try:
        result = _generate_llamacpp_chat(
            messages=messages if isinstance(messages, list) else [],
            max_tokens=max_tokens,
            timeout=timeout,
            temperature=temperature,
        )
        if isinstance(result, dict) and "error" in result:
            return result["error"]
        return result
    except Exception as e:
        # Last-resort: flatten to single-user prompt and retry
        print(f"[PROVIDER] llama.cpp /chat failed ({e}), retrying as flat prompt")
        flat = _flatten_messages(messages) if isinstance(messages, list) and messages else ""
        result = _generate_llamacpp_chat(
            [{"role": "user", "content": flat}],
            max_tokens=max_tokens,
            timeout=timeout,
            temperature=temperature,
        )
        # Auto-fallback to cloud if local server unavailable
        if isinstance(result, str) and "Could not connect to llama.cpp" in result:
            print("[ROTATION] llama.cpp unreachable in generate_with_messages — falling back to cloud")
            cloud = _try_fallback_chain(flat, max_tokens, temperature=temperature)
            return cloud if cloud else result
        return result


def supports_local_native_tools(model: str) -> bool:
    """
    Return True when the local llama.cpp model supports native tool calling.

    llama.cpp server exposes the OpenAI tools API for virtually all modern
    instruction-tuned models, including Gemma 4, Llama 3, Mistral, and Qwen.
    Cloud-prefixed models (groq:, gemini:, etc.) are excluded — they use
    their own cloud adapters.
    """
    m = (model or "").lower().strip()
    if not m:
        return False
    # Cloud-prefixed models skip local inference entirely
    if any(m.startswith(p) for p in ("groq:", "gemini:", "cerebras:", "sambanova:", "openrouter:")):
        return False
    # llama.cpp tool-call support is near-universal for instruct models
    return True


# Backwards-compatibility alias (previously named after Ollama)
supports_ollama_native_tools = supports_local_native_tools


def generate_with_native_tools(
    messages: list[dict],
    tools: list[dict],
    model: str = "local",
    timeout: int = 240,
    temperature: float = 0.1,
) -> dict:
    """
    Local llama.cpp /v1/chat/completions call with native tool-calling enabled.
    Uses the OpenAI-compatible tools API exposed by llama.cpp server.

    Returns:
      {
        "content": "...assistant text...",
        "tool_calls": [...],   # optional list of tool call objects
      }
    """
    if not isinstance(messages, list) or not messages:
        return {"error": "messages must be a non-empty list"}
    if not isinstance(tools, list) or not tools:
        return {"error": "tools must be a non-empty list"}

    result = _generate_llamacpp_chat(
        messages=messages,
        max_tokens=1024,
        timeout=timeout,
        temperature=temperature,
        tools=tools,
    )
    if isinstance(result, dict):
        return result
    # Shouldn't happen when tools are supplied, but handle gracefully
    return {"content": str(result), "tool_calls": []}


def _flatten_messages(messages: list[dict]) -> str:
    """Flatten structured messages back into a single prompt string."""
    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            parts.append(content)
        elif role == "user":
            parts.append(content)
        elif role == "assistant":
            parts.append(f"Assistant: {content}")
    return "\n\n".join(parts)


def _generate_with_gemini_content(
    model: str,
    content_dict: dict,
    max_tokens: int = 8000,
    temperature: float = 0.2,
) -> str:
    """
    Send Gemini-native structured content with systemInstruction separation.

    The content_dict comes from ProviderStateManager.build_gemini_content()
    and has the shape:
        {
            "systemInstruction": {"parts": [{"text": "..."}]},
            "contents": [{"parts": [{"text": "..."}]}]
        }
    This enables Gemini server-side caching of the system instruction.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return "[GEMINI_API_KEY not set]"

    print(f"[PROVIDER] Gemini (systemInstruction) → {model}")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    body = {
        **content_dict,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }

    try:
        response = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json=body,
            timeout=60,
        )

        if response.status_code == 429:
            _mark_rate_limited("gemini")
            # Flatten and fall back
            sys_text = ""
            user_text = ""
            si = content_dict.get("systemInstruction", {})
            if si and si.get("parts"):
                sys_text = si["parts"][0].get("text", "")
            contents = content_dict.get("contents", [])
            if contents and contents[0].get("parts"):
                user_text = contents[0]["parts"][0].get("text", "")
            flat = f"{sys_text}\n\n{user_text}".strip()
            return _try_fallback_chain(flat, max_tokens, skip_provider="gemini", temperature=temperature)
        elif response.status_code != 200:
            return f"[Gemini API Error {response.status_code}]"

        data = response.json()
        if "candidates" in data and data["candidates"]:
            parts = data["candidates"][0].get("content", {}).get("parts", [])
            if parts and "text" in parts[0]:
                content = parts[0]["text"]
                _record_gemini_response_usage(
                    provider="gemini",
                    model=model,
                    payload=data,
                    content_dict=content_dict,
                    completion_text=content,
                )
                return content

        _record_gemini_response_usage(
            provider="gemini",
            model=model,
            payload=data,
            content_dict=content_dict,
            completion_text="",
        )

        return "[Gemini: unexpected response format]"

    except Exception as e:
        # Flatten and retry via normal generate()
        sys_text = ""
        user_text = ""
        si = content_dict.get("systemInstruction", {})
        if si and si.get("parts"):
            sys_text = si["parts"][0].get("text", "")
        contents = content_dict.get("contents", [])
        if contents and contents[0].get("parts"):
            user_text = contents[0]["parts"][0].get("text", "")
        flat = f"{sys_text}\n\n{user_text}".strip()
        return generate(flat, model=f"gemini:{model}", max_tokens=max_tokens, temperature=temperature)


def _generate_with_openai_messages(
    provider_name: str,
    model: str,
    messages: list[dict],
    max_tokens: int = 8000,
    temperature: float = 0.2,
) -> str:
    """
    Send structured messages to an OpenAI-compatible provider.
    Uses system/user role separation for better caching and instruction following.
    """
    key_map = {
        "groq": ("GROQ_API_KEY", "GROQ_BASE_URL", "https://api.groq.com/openai/v1", 4096),
        "cerebras": ("CEREBRAS_API_KEY", None, "https://api.cerebras.ai/v1", 8192),
        "sambanova": ("SAMBANOVA_API_KEY", None, "https://api.sambanova.ai/v1", 4096),
        "openrouter": ("OPENROUTER_API_KEY", None, "https://openrouter.ai/api/v1", 4096),
    }

    key_env, url_env, default_url, max_cap = key_map.get(
        provider_name, ("", None, "", 4096)
    )
    api_key = os.getenv(key_env)
    if not api_key:
        return f"[{provider_name} API key not set]"

    base_url = os.getenv(url_env) if url_env else default_url
    max_tokens = min(max_tokens, max_cap)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if provider_name == "openrouter":
        headers["HTTP-Referer"] = "https://mira-agent.local"
        headers["X-Title"] = "MIRA Agent"

    try:
        print(f"[PROVIDER] {provider_name} (structured messages) → {model}")
        response = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=60,
        )

        if response.status_code == 429:
            _mark_rate_limited(provider_name)
            # Fall back to single-prompt via fallback chain
            flat = _flatten_messages(messages)
            return _try_fallback_chain(flat, max_tokens, skip_provider=provider_name, temperature=temperature)
        elif response.status_code != 200:
            return f"[{provider_name} API Error {response.status_code}]"

        data = response.json()
        content = data["choices"][0]["message"]["content"]
        _record_openai_response_usage(
            provider=provider_name,
            model=model,
            payload=data,
            messages=messages,
            completion_text=content,
        )
        return content

    except Exception as e:
        # Fall back to flat prompt on any error
        flat = _flatten_messages(messages)
        return generate(flat, model=f"{provider_name}:{model}", max_tokens=max_tokens, temperature=temperature)
