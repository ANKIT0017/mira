"""
Provider-native state primitives.

Wraps provider-specific features that reduce redundant context transmission:
- OpenAI: previous_response_id (conversation continuation)
- Anthropic: prompt caching for static prefixes (system + tools)
- Gemini: context caching for stable preamble blocks
- Groq/Cerebras/SambaNova: OpenAI-compatible, use system message splitting

Falls back to standard single-prompt mode when primitives are unavailable.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class CacheEntry:
    """Cached prefix/preamble for a provider."""
    cache_key: str
    content: str
    content_hash: str
    created_at: float = field(default_factory=time.time)
    ttl_seconds: float = 300.0  # 5 minutes default
    hit_count: int = 0

    @property
    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds


class ProviderStateManager:
    """
    Manages provider-specific state primitives for efficient context reuse.

    For providers that support it, this splits the monolithic prompt into:
    - Stable prefix (system prompt + tool docs + profile) → cacheable
    - Dynamic suffix (memory + history + step context) → per-call

    This reduces token costs and latency by avoiding retransmission of
    unchanging content.
    """

    def __init__(self):
        self._prefix_cache: dict[str, CacheEntry] = {}
        self._openai_response_ids: dict[str, str] = {}  # session -> last response_id
        self._gemini_cache_names: dict[str, str] = {}    # content_hash -> cache_name

    def _content_hash(self, content: str) -> str:
        return hashlib.md5(content.encode()).hexdigest()[:16]

    # ── Prefix caching (all providers) ──────────────────────────────────

    def cache_stable_prefix(self, provider: str, prefix_content: str) -> CacheEntry:
        """Cache the stable portion of the prompt (system + tools + profile)."""
        content_hash = self._content_hash(prefix_content)
        existing = self._prefix_cache.get(provider)

        if existing and existing.content_hash == content_hash and not existing.is_expired:
            existing.hit_count += 1
            return existing

        entry = CacheEntry(
            cache_key=f"{provider}_prefix",
            content=prefix_content,
            content_hash=content_hash,
        )
        self._prefix_cache[provider] = entry
        return entry

    def get_cached_prefix(self, provider: str) -> Optional[str]:
        """Retrieve cached prefix if still valid."""
        entry = self._prefix_cache.get(provider)
        if entry and not entry.is_expired:
            entry.hit_count += 1
            return entry.content
        return None

    # ── OpenAI-compatible structured messages ─────────────────────────────

    def build_openai_messages(
        self,
        system_prompt: str,
        tool_docs: str,
        profile_context: str,
        memory_context: str,
        dynamic_context: str,
        user_message: str,
    ) -> list[dict]:
        """
        Build OpenAI-compatible message array with system/user role separation.

        This is a significant upgrade from the current flat single-user-message
        approach. Benefits:
        - System message is cached by providers (Groq, Cerebras support this)
        - Clear role separation improves model instruction following
        - Enables future use of assistant prefill for structured outputs
        """
        messages = [
            {
                "role": "system",
                "content": f"{system_prompt}\n\n{tool_docs}\n\n{profile_context}",
            },
        ]

        # Memory + dynamic context as user context
        context_parts = []
        if memory_context:
            context_parts.append(memory_context)
        if dynamic_context:
            context_parts.append(dynamic_context)

        if context_parts:
            messages.append({
                "role": "user",
                "content": "\n".join(context_parts),
            })

        # The actual user request
        messages.append({
            "role": "user",
            "content": user_message,
        })

        return messages

    # ── Gemini structured content ─────────────────────────────────────────

    def build_gemini_content(
        self,
        system_prompt: str,
        tool_docs: str,
        profile_context: str,
        dynamic_context: str,
        user_message: str,
    ) -> dict:
        """
        Build Gemini API content with systemInstruction separation.

        Gemini supports systemInstruction as a separate field, which is
        cached server-side and not counted against per-request tokens
        on repeated calls.
        """
        return {
            "systemInstruction": {
                "parts": [{"text": f"{system_prompt}\n\n{tool_docs}\n\n{profile_context}"}]
            },
            "contents": [
                {
                    "parts": [{"text": f"{dynamic_context}\n\n{user_message}"}]
                }
            ],
        }

    # ── Provider detection ────────────────────────────────────────────────

    @staticmethod
    def detect_provider(model: str) -> str:
        """Detect provider from model string prefix."""
        prefixes = ["groq:", "gemini:", "cerebras:", "sambanova:", "openrouter:"]
        for prefix in prefixes:
            if model.startswith(prefix):
                return prefix.rstrip(":")
        return "llamacpp"

    @staticmethod
    def supports_system_role(provider: str) -> bool:
        """Check if provider supports system role in messages."""
        return provider in ("groq", "cerebras", "sambanova", "openrouter")

    @staticmethod
    def supports_system_instruction(provider: str) -> bool:
        """Check if provider supports Gemini-style systemInstruction."""
        return provider == "gemini"

    # ── Diagnostic info ───────────────────────────────────────────────────

    def cache_stats(self) -> dict:
        """Return cache hit/miss statistics."""
        stats = {}
        for provider, entry in self._prefix_cache.items():
            stats[provider] = {
                "cache_key": entry.cache_key,
                "content_hash": entry.content_hash,
                "hit_count": entry.hit_count,
                "expired": entry.is_expired,
                "age_seconds": round(time.time() - entry.created_at, 1),
            }
        return stats
