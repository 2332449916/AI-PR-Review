"""
Anthropic (Claude) LLM 提供程序实现。

使用官方的 Anthropic Python SDK 并支持流式输出。

设计原理：
- Claude 拥有 200K 上下文窗口，因此我们很少需要激进的文本分块
- SDK 内部处理重试和速率限制
- 我们使用 ``messages`` API（而非旧的 ``completions`` API）以支持系统提示
"""

from __future__ import annotations

import json
import logging
from typing import AsyncGenerator

from anthropic import AnthropicVertex, AsyncAnthropic, AsyncAnthropicBedrock

from src.llm.providers.base import CompletionResult, LLMProvider, Message, ProviderConfig

logger = logging.getLogger(__name__)


class AnthropicProvider(LLMProvider):
    """Anthropic Claude 提供程序。"""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._client = AsyncAnthropic(api_key=config.api_key)

    async def complete(
        self,
        messages: list[Message],
        **kwargs,
    ) -> AsyncGenerator[str, None]:
        """从 Claude 流式获取补全结果。

        产出：
            当文本内容增量到达时逐个产出。
        """
        system_prompt = None
        api_messages = []

        for msg in messages:
            if msg.role == "system":
                system_prompt = msg.content
            else:
                api_messages.append({"role": msg.role, "content": msg.content})

        # 映射角色名称："user" → "user"，"assistant" → "assistant"
        # （Anthropic 使用相同的角色名称，因此无需映射）

        # 将我们的 kwargs 映射为 Anthropic 参数
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
                        # 处理不同类型的增量：
                        # - text_delta：普通文本（包含 .text 属性）
                        # - thinking_delta：扩展思考内容（包含 .thinking 属性，跳过）
                        delta_type = getattr(chunk.delta, "type", None)
                        if delta_type == "text_delta":
                            text = getattr(chunk.delta, "text", None)
                            if text:
                                yield text
                        elif delta_type == "input_json_delta":
                            partial = getattr(chunk.delta, "partial_json", None)
                            if partial:
                                yield partial
                        # 跳过 thinking_delta（Claude 的扩展思考过程）—— 我们只需要最终的响应文本
        except Exception as exc:
            logger.error("Anthropic API 调用失败：%s", exc)
            error_msg = json.dumps({
                "error": True,
                "message": f"Anthropic API 错误：{exc}",
                "findings": [],
            })
            yield error_msg

    async def complete_with_result(
        self,
        messages: list[Message],
        **kwargs,
    ) -> CompletionResult:
        """将流式响应收集到一个 ``CompletionResult`` 中。"""
        chunks: list[str] = []
        async for chunk in self.complete(messages, **kwargs):
            chunks.append(chunk)
        content = "".join(chunks)

        # 估算 token 数量（Anthropic 在流式模式下不返回 token 计数）
        input_tokens = self.estimate_tokens(messages)
        output_tokens = len(content) // 4  # 粗略估算

        return CompletionResult(
            content=content,
            model=self._config.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def estimate_tokens(self, messages: list[Message]) -> int:
        """对 Claude 消息进行粗略的 token 估算。

        对于代码，Claude 大约每 3.5 个字符消耗 1 个 token。
        """
        total_chars = sum(len(msg.content) for msg in messages)
        return max(1, total_chars // 3)
