"""
Token counting utilities for LLM context management.

Provides rough token estimation for messages and text content. These are
approximations — exact tokenisation would require loading each model's
tokenizer, which is impractical for CI tools.

Design rationale:
- We use character-based heuristics (~4 chars/token for English+code) because
  they are fast, dependency-free, and good enough for budget estimation
- For Anthropic, the ratio is closer to 3.5 chars/token (Claude's tokeniser
  is more efficient with code)
- For OpenAI, the ratio is ~4 chars/token (tiktoken-compatible)
- The estimates are intentionally conservative (slightly overestimating)
  to prevent context overflow
"""

from __future__ import annotations

from typing import Literal


# ---------------------------------------------------------------------------
# Default ratios (chars per token)
# ---------------------------------------------------------------------------

RATIOS: dict[str, float] = {
    "anthropic": 3.5,
    "openai": 4.0,
    "local": 4.0,
}


def estimate_text_tokens(
    text: str,
    provider: str = "anthropic",
) -> int:
    """Estimate the token count for a text string.

    Args:
        text: The text to estimate.
        provider: Provider name for ratio selection.

    Returns:
        Estimated token count (always >= 1).
    """
    ratio = RATIOS.get(provider, 4.0)
    return max(1, int(len(text) / ratio))


def estimate_messages_tokens(
    messages: list[dict[str, str]],
    provider: str = "anthropic",
) -> int:
    """Estimate the token count for a list of chat messages.

    Accounts for message metadata overhead (~4 tokens per message for roles,
    plus separators).

    Args:
        messages: List of ``{"role": ..., "content": ...}`` dicts.
        provider: Provider name for ratio selection.

    Returns:
        Estimated total token count.
    """
    total = 0
    for msg in messages:
        # Role overhead
        total += 4
        # Content
        total += estimate_text_tokens(msg.get("content", ""), provider)
    return total


def estimate_finding_tokens(
    findings: list[dict],
    provider: str = "anthropic",
) -> int:
    """Estimate the token cost of serialising a list of findings.

    Findings are serialised as JSON, which has overhead for keys, quotes,
    brackets, etc.

    Args:
        findings: List of finding dicts.
        provider: Provider name for ratio selection.

    Returns:
        Estimated token count.
    """
    import json
    serialised = json.dumps(findings, indent=2, ensure_ascii=False)
    return estimate_text_tokens(serialised, provider)


def is_within_limit(
    messages: list[dict[str, str]],
    limit: int,
    provider: str = "anthropic",
) -> bool:
    """Check if messages fit within a token limit.

    Args:
        messages: The messages to check.
        limit: Maximum allowed tokens.
        provider: Provider name for ratio selection.

    Returns:
        ``True`` if the estimated token count is within the limit.
    """
    return estimate_messages_tokens(messages, provider) <= limit


def truncate_to_limit(
    text: str,
    limit: int,
    provider: str = "anthropic",
) -> str:
    """Truncate text to fit within a token limit.

    Truncates at character boundaries (not token boundaries), which is
    acceptable for our use case since we use rough estimates.

    Args:
        text: The text to truncate.
        limit: Maximum allowed tokens.
        provider: Provider name for ratio selection.

    Returns:
        Truncated text (may be empty if limit is very small).
    """
    ratio = RATIOS.get(provider, 4.0)
    max_chars = int(limit * ratio)
    if len(text) <= max_chars:
        return text
    return text[:max_chars]
