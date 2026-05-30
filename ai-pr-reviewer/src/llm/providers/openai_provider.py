"""
OpenAI (GPT) LLM provider implementation.

Uses the official OpenAI Python SDK with streaming support.

Design rationale:
- GPT-4o has a 128K context window, smaller than Claude but still generous
- The OpenAI SDK's streaming API uses Server-Sent Events
- We support both ``gpt-4o`` and ``gpt-4o-mini`` via the model config
"""

from __future__ import annotations

import json
import logging
from typing import AsyncGenerator

from openai import AsyncOpenAI

from src.llm.providers.base import CompletionResult, LLMProvider, Message, ProviderConfig

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    """OpenAI GPT provider."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._client = AsyncOpenAI(api_key=config.api_key)

    async def complete(
        self,
        messages: list[Message],
        **kwargs,
    ) -> AsyncGenerator[str, None]:
        """Stream a completion from GPT.

        Yields:
            Text content deltas as they arrive.
        """
        api_messages = []
        for msg in messages:
            role = msg.role
            if role == "system":
                api_messages.append({"role": "system", "content": msg.content})
            elif role in ("user", "assistant"):
                api_messages.append({"role": role, "content": msg.content})
            else:
                api_messages.append({"role": "user", "content": msg.content})

        openai_kwargs = {
            "model": self._config.model,
            "max_tokens": kwargs.get("max_tokens", self._config.max_tokens),
            "temperature": kwargs.get("temperature", self._config.temperature),
            "messages": api_messages,
            "stream": True,
        }

        try:
            stream = await self._client.chat.completions.create(**openai_kwargs)
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as exc:
            logger.error("OpenAI API call failed: %s", exc)
            error_msg = json.dumps({
                "error": True,
                "message": f"OpenAI API error: {exc}",
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

        return CompletionResult(
            content=content,
            model=self._config.model,
            input_tokens=self.estimate_tokens(messages),
            output_tokens=len(content) // 4,
        )

    def estimate_tokens(self, messages: list[Message]) -> int:
        """Rough token estimation for GPT messages.

        GPT uses approximately 1 token per 4 characters.
        """
        total_chars = sum(len(msg.content) for msg in messages)
        return max(1, total_chars // 4)
