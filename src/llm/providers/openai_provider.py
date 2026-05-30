"""
OpenAI (GPT) LLM 提供者实现。

使用官方 OpenAI Python SDK，支持流式传输。

设计理念：
- GPT-4o 拥有 128K 上下文窗口，虽小于 Claude 但仍相当充裕
- OpenAI SDK 的流式 API 使用 Server-Sent Events
- 我们通过模型配置同时支持 ``gpt-4o`` 和 ``gpt-4o-mini``
"""

from __future__ import annotations

import json
import logging
from typing import AsyncGenerator

from openai import AsyncOpenAI

from src.llm.providers.base import CompletionResult, LLMProvider, Message, ProviderConfig

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    """OpenAI GPT 提供者。"""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._client = AsyncOpenAI(api_key=config.api_key)

    async def complete(
        self,
        messages: list[Message],
        **kwargs,
    ) -> AsyncGenerator[str, None]:
        """从 GPT 流式获取补全结果。

        Yields:
            文本内容的增量片段，随到达逐步产出。
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
            logger.error("OpenAI API 调用失败: %s", exc)
            error_msg = json.dumps({
                "error": True,
                "message": f"OpenAI API 错误: {exc}",
                "findings": [],
            })
            yield error_msg

    async def complete_with_result(
        self,
        messages: list[Message],
        **kwargs,
    ) -> CompletionResult:
        """收集流式响应，整合为 ``CompletionResult``。"""
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
        """对 GPT 消息进行粗略的 token 估算。

        GPT 约每 4 个字符对应 1 个 token。
        """
        total_chars = sum(len(msg.content) for msg in messages)
        return max(1, total_chars // 4)
