"""
本地 LLM 提供者（Ollama、llama.cpp 等）。

连接到任何兼容 OpenAI 接口的本地端点。已测试的平台：
- Ollama (http://localhost:11434/v1)
- llama.cpp server
- LocalAI

设计原理：
- 底层使用 OpenAI SDK，因为大多数本地模型服务器
  都暴露了兼容 OpenAI 的 API
- 可配置的 base_url 以支持不同的本地服务器
- 当本地服务器不可用时能够优雅地降级处理
"""

from __future__ import annotations

import json
import logging
from typing import AsyncGenerator

from openai import AsyncOpenAI

from src.llm.providers.base import CompletionResult, LLMProvider, Message, ProviderConfig

logger = logging.getLogger(__name__)


class LocalProvider(LLMProvider):
    """通过兼容 OpenAI 的 API 提供本地模型服务。

    参数：
        config: 提供者配置。
        base_url: 本地 API 服务器的基地址
                  （例如 Ollama 使用 ``http://localhost:11434/v1``）。
    """

    def __init__(self, config: ProviderConfig, base_url: str = "http://localhost:11434/v1") -> None:
        super().__init__(config)
        self._client = AsyncOpenAI(
            api_key=config.api_key or "ollama",  # Ollama 不需要 API 密钥
            base_url=base_url,
        )

    async def complete(
        self,
        messages: list[Message],
        **kwargs,
    ) -> AsyncGenerator[str, None]:
        """从本地模型流式获取补全结果。"""
        api_messages = []
        for msg in messages:
            role = msg.role
            if role == "system":
                api_messages.append({"role": "system", "content": msg.content})
            elif role in ("user", "assistant"):
                api_messages.append({"role": role, "content": msg.content})
            else:
                api_messages.append({"role": "user", "content": msg.content})

        local_kwargs = {
            "model": self._config.model,
            "max_tokens": kwargs.get("max_tokens", self._config.max_tokens),
            "temperature": kwargs.get("temperature", self._config.temperature),
            "messages": api_messages,
            "stream": True,
        }

        try:
            stream = await self._client.chat.completions.create(**local_kwargs)
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as exc:
            logger.error("本地 LLM API 调用失败: %s", exc)
            error_msg = json.dumps({
                "error": True,
                "message": f"本地 LLM API 错误: {exc}",
                "findings": [],
            })
            yield error_msg

    async def complete_with_result(
        self,
        messages: list[Message],
        **kwargs,
    ) -> CompletionResult:
        """收集流式响应并封装为 ``CompletionResult``。"""
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
        """对本地模型的粗略 token 估算。"""
        total_chars = sum(len(msg.content) for msg in messages)
        return max(1, total_chars // 4)
