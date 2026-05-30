"""
LLM 提供者的抽象接口。

定义了所有 LLM 提供者（Anthropic、OpenAI、本地模型）必须实现的契约。
这一抽象层使得分析引擎能够独立于底层模型 API 运作。

设计理念：
- 流式输出从设计之初就被纳入接口——非流式提供者只需生成单个数据块即可。
- ``complete()`` 方法返回一个异步生成器，调用方可以逐步处理生成的
  词元（用于进度条、实时预览等）。
- 提供者的配置（模型名称、temperature、最大 token 数）通过
  ``ProviderConfig`` 数据类传入，而非直接塞进接口，
  以此保持接口的简洁性和可扩展性。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncGenerator


@dataclass
class Message:
    """聊天对话中的单条消息。"""

    role: str  # "system"、"user"、"assistant"
    content: str


@dataclass
class ProviderConfig:
    """LLM 提供者的配置。"""

    model: str
    api_key: str
    max_tokens: int = 4096
    temperature: float = 0.3  # 使用较低的 temperature 以获得更确定性的评审结果
    timeout_seconds: int = 120


@dataclass
class CompletionResult:
    """一次完成的 LLM 调用的结果。"""

    content: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMProvider(ABC):
    """LLM 提供者的抽象基类。"""

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config

    @property
    def config(self) -> ProviderConfig:
        return self._config

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        **kwargs,
    ) -> AsyncGenerator[str, None]:
        """将消息发送到 LLM 并以流式方式接收响应。

        Args:
            messages: 构成对话上下文的消息列表。

        Yields:
            模型生成的文本片段。
        """
        ...

    async def complete_with_result(
        self,
        messages: list[Message],
        **kwargs,
    ) -> CompletionResult:
        """基于 ``complete()`` 的非流式便捷封装。

        收集所有文本片段并返回一个包含 token 计数的 ``CompletionResult``。
        """
        chunks: list[str] = []
        async for chunk in self.complete(messages, **kwargs):
            chunks.append(chunk)
        full_content = "".join(chunks)

        return CompletionResult(
            content=full_content,
            model=self._config.model,
        )

    @abstractmethod
    def estimate_tokens(self, messages: list[Message]) -> int:
        """估算消息列表的总 token 数。

        上下文构建器使用此方法来决定何时拆分分析单元。
        """
        ...
