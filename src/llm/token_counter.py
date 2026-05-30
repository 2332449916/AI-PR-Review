"""
LLM 上下文管理的 token 计数工具。

提供消息和文本内容的粗略 token 估算。这些是近似值 —— 精确的 token 化需要加载每个模型的 tokenizer，这对 CI 工具来说不切实际。

设计原理：
- 使用基于字符的启发式方法（英语+代码约 4 字符/token），因为这种方法快速、零依赖，且对预算估算足够好
- 对于 Anthropic，比率接近 3.5 字符/token（Claude 的 tokenizer 对代码更高效）
- 对于 OpenAI，比率约为 4 字符/token（与 tiktoken 兼容）
- 估算值故意偏保守（略微高估），以防止上下文溢出
"""

from __future__ import annotations

from typing import Literal


# ---------------------------------------------------------------------------
# 默认比率（每个 token 对应的字符数）
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
    """估算一段文本字符串的 token 数量。

    参数:
        text: 要估算的文本。
        provider: 用于选择比率的提供商名称。

    返回:
        估算的 token 数量（始终 >= 1）。
    """
    ratio = RATIOS.get(provider, 4.0)
    return max(1, int(len(text) / ratio))


def estimate_messages_tokens(
    messages: list[dict[str, str]],
    provider: str = "anthropic",
) -> int:
    """估算一组聊天消息列表的 token 数量。

    考虑了消息元数据的开销（每条消息约 4 个 token 用于角色信息，外加分隔符）。

    参数:
        messages: ``{"role": ..., "content": ...}`` 字典列表。
        provider: 用于选择比率的提供商名称。

    返回:
        估算的总 token 数量。
    """
    total = 0
    for msg in messages:
        # 角色开销
        total += 4
        # 内容
        total += estimate_text_tokens(msg.get("content", ""), provider)
    return total


def estimate_finding_tokens(
    findings: list[dict],
    provider: str = "anthropic",
) -> int:
    """估算序列化一组发现结果所需的 token 数量。

    发现结果以 JSON 格式序列化，包含键名、引号、括号等开销。

    参数:
        findings: 发现结果字典列表。
        provider: 用于选择比率的提供商名称。

    返回:
        估算的 token 数量。
    """
    import json
    serialised = json.dumps(findings, indent=2, ensure_ascii=False)
    return estimate_text_tokens(serialised, provider)


def is_within_limit(
    messages: list[dict[str, str]],
    limit: int,
    provider: str = "anthropic",
) -> bool:
    """检查消息是否在 token 限制范围内。

    参数:
        messages: 要检查的消息列表。
        limit: 允许的最大 token 数。
        provider: 用于选择比率的提供商名称。

    返回:
        如果估算的 token 数量在限制范围内，返回 ``True``。
    """
    return estimate_messages_tokens(messages, provider) <= limit


def truncate_to_limit(
    text: str,
    limit: int,
    provider: str = "anthropic",
) -> str:
    """截断文本使其适应 token 限制。

    在字符边界（而非 token 边界）处截断，这对我们的用例来说是可以接受的，因为我们使用的是粗略估算。

    参数:
        text: 要截断的文本。
        limit: 允许的最大 token 数。
        provider: 用于选择比率的提供商名称。

    返回:
        截断后的文本（如果限制非常小，可能为空字符串）。
    """
    ratio = RATIOS.get(provider, 4.0)
    max_chars = int(limit * ratio)
    if len(text) <= max_chars:
        return text
    return text[:max_chars]
