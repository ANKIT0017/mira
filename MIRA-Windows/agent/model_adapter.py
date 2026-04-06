import subprocess
import os
import json
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

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
        
        return response.json()["choices"][0]["message"]["content"]
        
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
                return parts[0]["text"]
        
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
        
        return response.json()["choices"][0]["message"]["content"]
        
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
        
        return response.json()["choices"][0]["message"]["content"]
        
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
        
        return response.json()["choices"][0]["message"]["content"]
        
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
# MAIN ENTRY POINT
# ============================================================

def generate(prompt: str, model: str = "qwen3:4b-instruct", max_tokens: int = 92024, timeout: int = 240, temperature: float = 0.1):
    """
    Universal generation function with automatic multi-provider rotation.
    
    Routes:
    - 'groq:*'      → Groq API (fast, 70B models)
    - 'gemini:*'     → Gemini API (multimodal, 2.5 series)
    - 'cerebras:*'   → Cerebras API (fastest, 70B)
    - 'sambanova:*'  → SambaNova API (405B)
    - 'openrouter:*' → OpenRouter (aggregator, free models)
    - Local models   → Ollama
    
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
    
    # Otherwise use local Ollama
    # --- Local Ollama via HTTP API (keeps model in VRAM, no cold-start) ---
    try:
        print(f"[PROVIDER] Ollama HTTP → {model}")

        # Detect Qwen3 family: qwen3:4b-instruct, qwen3.5:4b, etc.
        is_qwen3 = "qwen3" in model.lower()
        # qwen3.5 supports 32k context; qwen3 (non-3.5) use 16k to save VRAM
        _num_ctx = 32768 if "qwen3.5" in model.lower() else 16384

        import re
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_ctx": _num_ctx,
                "num_predict": 2048,
                "temperature": temperature,
            }
        }
        if is_qwen3:
            payload["think"] = False  # Qwen3-specific: skip the <think> reasoning block

        resp = requests.post(
            "http://localhost:11434/api/generate",
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        result = data.get("response", "").strip()

        # Strip any residual <think>...</think> blocks just in case
        result = re.sub(r"<think>.*?</think>", "", result, flags=re.DOTALL).strip()

        return result
    except requests.exceptions.Timeout:
        return "[Error: Ollama call timed out]"
    except requests.exceptions.ConnectionError:
        return "[Error: Could not connect to Ollama. Is it running on localhost:11434?]"
    except Exception as e:
        return f"[Error running Ollama: {e}]"



def generate_streaming(prompt: str, model: str = "deepseek-r1:1.5b", max_tokens: int = 512):
    """
    Stream tokens from Ollama for real-time voice output.
    Yields tokens as they are generated.
    """
    try:
        cmd = ["ollama", "run", model, "--nowordwrap"]
        
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        
        proc.stdin.write(prompt + "\n")
        proc.stdin.flush()
        proc.stdin.close()
        
        buffer = ""
        for line in proc.stdout:
            buffer += line
            while ' ' in buffer or '\n' in buffer:
                if ' ' in buffer:
                    idx = buffer.index(' ')
                    token = buffer[:idx + 1]
                    buffer = buffer[idx + 1:]
                elif '\n' in buffer:
                    idx = buffer.index('\n')
                    token = buffer[:idx + 1]
                    buffer = buffer[idx + 1:]
                
                if token.strip():
                    yield token
        
        if buffer.strip():
            yield buffer
            
        proc.wait()
        
    except Exception as e:
        yield f"[Error: {e}]"


# ============================================================
# STRUCTURED MESSAGE GENERATION
# Uses system/user role separation for providers that support it.
# This enables provider-native caching of system-role content.
# ============================================================

def generate_with_messages(
    messages: list[dict],
    model: str = "qwen3:4b-instruct",
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

    For local Ollama models, uses /api/chat (not /api/generate) so the
    model's proper chat template is applied to the message list.

    thinking=True enables Qwen3's extended reasoning on PLAN/REPLAN calls
    only. ACT/SYNTH calls should pass thinking=False (default) for speed.
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

    # ── Local Ollama via /api/chat (preserves model's chat template) ──────────────
    # Using /api/chat instead of /api/generate ensures the system/user roles
    # are fed through the model's proper Jinja chat template rather than being
    # concatenated as raw text, which significantly improves tool-call reliability.
    is_qwen3 = "qwen3" in model.lower()
    use_think = thinking and is_qwen3  # only Qwen3 supports the think flag
    # qwen3.5 supports 32k context; qwen3 (non-3.5) use 16k to save VRAM
    _num_ctx = 32768 if "qwen3.5" in model.lower() else 16384

    import re as _re
    try:
        print(f"[PROVIDER] Ollama /api/chat → {model} (think={use_think})")
        payload = {
            "model": model,
            "messages": messages if isinstance(messages, list) else [],
            "stream": False,
            "options": {
                "num_ctx": _num_ctx,
                # Give thinking calls more room to reason; ACT calls stay tight
                "num_predict": min(max_tokens, 4096) if use_think else min(max_tokens, 2048),
                "temperature": temperature,
            },
        }
        if is_qwen3:
            payload["think"] = use_think  # Qwen3 flag: emit or suppress <think> block

        resp = requests.post(
            "http://localhost:11434/api/chat",
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        # /api/chat returns {"message": {"role": "assistant", "content": "...", "thinking": "..."}}
        # The "content" field already excludes the thinking block when think=True
        result = data.get("message", {}).get("content", "").strip()
        # Safety-net: strip any residual <think> blocks (e.g. models that ignore the flag)
        if not use_think:
            result = _re.sub(r"<think>.*?</think>", "", result, flags=_re.DOTALL).strip()
        return result

    except requests.exceptions.Timeout:
        return "[Error: Ollama call timed out]"
    except requests.exceptions.ConnectionError:
        return "[Error: Could not connect to Ollama. Is it running on localhost:11434?]"
    except Exception as e:
        # Last-resort fallback: flat prompt via /api/generate
        print(f"[PROVIDER] Ollama /api/chat failed ({e}), falling back to /api/generate")
        flat = _flatten_messages(messages) if isinstance(messages, list) and messages else ""
        return generate(flat, model=model, max_tokens=max_tokens, timeout=timeout, temperature=temperature)


def supports_ollama_native_tools(model: str) -> bool:
    """
    Return True when the local Ollama model likely supports native tool calling.
    """
    m = (model or "").lower().strip()
    if not m:
        return False
    # Provider-prefixed models are routed through cloud adapters, not local Ollama.
    if any(m.startswith(p) for p in ("groq:", "gemini:", "cerebras:", "sambanova:", "openrouter:")):
        return False
    return any(tag in m for tag in ("qwen2.5", "llama3.2", "mistral"))


def generate_with_native_tools(
    messages: list[dict],
    tools: list[dict],
    model: str = "qwen2.5:7b-instruct",
    timeout: int = 240,
    temperature: float = 0.1,
) -> dict:
    """
    Local Ollama /api/chat call with native tool-calling enabled.

    Returns:
      {
        "content": "...assistant text...",
        "tool_calls": [...],   # optional
      }
    """
    if not isinstance(messages, list) or not messages:
        return {"error": "messages must be a non-empty list"}
    if not isinstance(tools, list) or not tools:
        return {"error": "tools must be a non-empty list"}

    # qwen3.5 supports 32k context; others default to 16k for local stability.
    _num_ctx = 32768 if "qwen3.5" in model.lower() else 16384
    try:
        print(f"[PROVIDER] Ollama /api/chat(native-tools) -> {model} tools={len(tools)}")
        payload = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "options": {
                "num_ctx": _num_ctx,
                "num_predict": 1024,
                "temperature": temperature,
            },
        }
        resp = requests.post(
            "http://localhost:11434/api/chat",
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        msg = data.get("message", {}) if isinstance(data, dict) else {}
        tool_calls = msg.get("tool_calls", []) if isinstance(msg, dict) else []
        content = (msg.get("content", "") if isinstance(msg, dict) else "") or ""
        return {"content": content.strip(), "tool_calls": tool_calls}
    except requests.exceptions.Timeout:
        return {"error": "Ollama native tool call timed out"}
    except requests.exceptions.ConnectionError:
        return {"error": "Could not connect to Ollama on localhost:11434"}
    except Exception as e:
        return {"error": f"Ollama native tool call failed: {e}"}


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
                return parts[0]["text"]

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

        return response.json()["choices"][0]["message"]["content"]

    except Exception as e:
        # Fall back to flat prompt on any error
        flat = _flatten_messages(messages)
        return generate(flat, model=f"{provider_name}:{model}", max_tokens=max_tokens, temperature=temperature)
