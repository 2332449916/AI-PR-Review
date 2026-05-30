"""
Anthropic (Claude) LLM provider implementation.

Uses the official Anthropic Python SDK with streaming support.

Design rationale:
- Claude's 200K context window means we rarely need aggressive chunking
- The SDK handles retries and rate limits internally
- We use the ``messages`` API (not the older ``completions`` API) for
  system-prompt support
"""

from __future__ import annotations

import json
import logging
from typing import AsyncGenerator

from anthropic import AnthropicVertex, AsyncAnthropic, AsyncAnthropicBedrock

from src.llm.providers.base import CompletionResult, LLMProvider, Message, ProviderConfig

logger = logging.getLogger(__name__)


class AnthropicProvider(LLMProvider):
    """Anthropic Claude provider."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._client = AsyncAnthropic(api_key=config.api_key)

    async def complete(
        self,
        messages: list[Message],
        **kwargs,
    ) -> AsyncGenerator[str, None]:
        """Stream a completion from Claude.

        Yields:
            Text content deltas as they arrive.
        """
        system_prompt = None
        api_messages = []

        for msg in messages:
            if msg.role == "system":
                system_prompt = msg.content
            else:
                api_messages.append({"role": msg.role, "content": msg.content})

        # Map role names: "user" → "user", "assistant" → "assistant"
        # (Anthropic uses the same roles, so no mapping needed)

        # Map our kwargs to Anthropic parameters
        anthropic_kwargs = {
            "model": self._config.model,
            "max_tokens": kwargs.get("max_tokens", self._config.max_tokens),
            "temperature": kwargs.get("temperature", self._config.temperature),
            "messages": api_messages,
        }
        if system_prompt:
            anthropic_kwargs["system"] = system_prompt

        try:
            async with self._client.messages.stream(**anthropic_kwargs) as stream:
                async for chunk in stream:
                    if chunk.type == "content_block_delta":
                        # Handle different delta types:
                        # - text_delta: regular text (has .text)
                        # - thinking_delta: extended thinking (has .thinking, skip)
                        delta_type = getattr(chunk.delta, "type", None)
                        if delta_type == "text_delta":
                            text = getattr(chunk.delta, "text", None)
                            if text:
                                yield text
                        elif delta_type == "input_json_delta":
                            partial = getattr(chunk.delta, "partial_json", None)
                            if partial:
                                yield partial
                        # Skip thinking_delta (Claude's extended thinking) — we
                        # only want the final response text
        except Exception as exc:
            logger.error("Anthropic API call failed: %s", exc)
            error_msg = json.dumps({
                "error": True,
                "message": f"Anthropic API error: {exc}",
                "findings": [],
            })
            yield error_msg

    async def complete_with_result(
        self,
        messages: list[Message],
        **kwargs,
    ) -> CompletionResult:
        """Collect streaming response into a ``CompletionResult``."""
        chunks: list[str] = []
        async for chunk in self.complete(messages, **kwargs):
            chunks.append(chunk)
        content = "".join(chunks)

        # Estimate tokens (Anthropic doesn't return counts in streaming mode)
        input_tokens = self.estimate_tokens(messages)
        output_tokens = len(content) // 4  # rough estimate

        return CompletionResult(
            content=content,
            model=self._config.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def estimate_tokens(self, messages: list[Message]) -> int:
        """Rough token estimation for Claude messages.

        Claude uses approximately 1 token per 3.5 characters for code.
        """
        total_chars = sum(len(msg.content) for msg in messages)
        return max(1, total_chars // 3)
