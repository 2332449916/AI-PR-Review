"""Tests for the token counter module."""

from __future__ import annotations

from src.llm.token_counter import (
    estimate_text_tokens,
    estimate_messages_tokens,
    is_within_limit,
    truncate_to_limit,
)


class TestTokenCounter:
    """Test suite for token counting utilities."""

    def test_empty_text(self) -> None:
        assert estimate_text_tokens("") >= 1

    def test_short_text(self) -> None:
        tokens = estimate_text_tokens("hello world")
        assert tokens >= 1

    def test_longer_text(self) -> None:
        text = "Hello world, this is a test sentence with some words in it."
        tokens = estimate_text_tokens(text)
        assert tokens > 1
        # ~3.5-4 chars per token means ~55 chars should be ~14-16 tokens
        assert tokens >= 8

    def test_provider_ratios(self) -> None:
        text = "def hello(name: str) -> str:\n    return f'Hello {name}'"
        anthropic_tokens = estimate_text_tokens(text, "anthropic")
        openai_tokens = estimate_text_tokens(text, "openai")
        # Anthropic has lower chars/token ratio, so count should be higher
        assert anthropic_tokens >= openai_tokens

    def test_estimate_messages(self) -> None:
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Review this code."},
        ]
        total = estimate_messages_tokens(messages)
        assert total > 4  # At least role overhead

    def test_is_within_limit(self) -> None:
        messages = [
            {"role": "user", "content": "short"},
        ]
        assert is_within_limit(messages, 1000)
        assert not is_within_limit(messages, 2)

    def test_truncate_to_limit_short_text(self) -> None:
        text = "short text"
        result = truncate_to_limit(text, 1000)
        assert result == text

    def test_truncate_to_limit_long_text(self) -> None:
        text = "a" * 1000
        result = truncate_to_limit(text, 10, "openai")
        assert len(result) < len(text)
