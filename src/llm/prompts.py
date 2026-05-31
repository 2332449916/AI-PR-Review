"""
AI 代码审查的提示词模板。

本模块定义了三个不同的提示词模板，每个模板针对审查流程中的特定任务进行了优化：

1. **风险分析提示词**：核心提示词——分析 diff 并以结构化 JSON 返回
   发现项（缺陷、安全问题等）。
2. **总结提示词**：将所有发现项综合为自然语言的 PR 总结。
3. **建议提示词**：针对特定发现项生成具体的代码改进建议。

设计理由：
- **为什么用三个独立的提示词而不是一个？** 每个任务有不同的最佳指令集。
  将它们合并会稀释关注点并增加 token 浪费。风险分析需要结构化输出；
  总结需要叙事生成；建议需要代码合成。
- **为什么用 JSON-in-prompt 而不是 function calling？** JSON-in-prompt 可以
  跨所有提供商和模型工作，包括那些不支持 function calling 的本地模型。
  代价是解析的可靠性稍低，我们通过备选解析器来缓解这个问题。
- **为什么包含 "confidence" 和 "uncertainty_reason" 字段？** 这两个字段是
  我们主要的误报缓解手段。通过明确要求模型评估自身置信度并解释不确定性，
  我们得到一个可以设置阈值进行过滤的校准信号。
- **少样本示例**：每个模板都包含代表性示例，展示期望的输出格式和推理深度。
  这些不是真实发现项——它们旨在校准模型的输出。
"""

from __future__ import annotations

from typing import Any

from src.diff.models import FileDiff
from src.context.builder import AnalysisUnit


# ---------------------------------------------------------------------------
# 系统提示词
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_REVIEWER = """You are an expert code reviewer with deep knowledge of:
- Security vulnerabilities (OWASP Top 10, CWE)
- Performance optimisation (algorithms, I/O, caching, concurrency)
- Concurrent programming (race conditions, deadlocks, thread safety)
- Language-specific pitfalls and best practices
- Error handling and defensive programming
- Maintainability and code clarity

Your task is to analyse code diffs and identify potential issues. You MUST:
1. Only report real issues with high confidence
2. Provide concrete, actionable suggestions (not generic advice)
3. Include specific code snippets for before/after comparisons
4. Rate your confidence in each finding (0.0 to 1.0)
5. Return findings as valid JSON

IMPORTANT: Respond in Chinese. All titles, descriptions, suggestions, and summaries must be written in Chinese."""

SYSTEM_PROMPT_SUMMARISER = """You are a technical writing assistant specialised in code review
summarisation. Your task is to synthesise multiple analysis results into a
clear, actionable PR summary. Write in a professional tone, be concise, and
focus on what the reader needs to know.

IMPORTANT: Respond in Chinese. All summaries and assessments must be written in Chinese."""


# ---------------------------------------------------------------------------
# 分析提示词构建器
# ---------------------------------------------------------------------------


def build_analysis_prompt(unit: AnalysisUnit) -> list[dict[str, str]]:
    """为单个分析单元构建分析提示词消息。

    参数:
        unit: 包含 diff 和上下文信息的分析单元。

    返回:
        包含 ``role`` 和 ``content`` 键的消息字典列表，
        适用于传递给任何 LLM 提供商。
    """
    # --- 系统消息 ---
    system_msg = {
        "role": "system",
        "content": SYSTEM_PROMPT_REVIEWER,
    }

    # --- 包含 diff 和上下文的用户消息 ---
    user_parts: list[str] = []

    # PR 上下文头部
    user_parts.append(f"## PR Context\n")
    user_parts.append(f"Files in this unit: {len(unit.file_diffs)}")
    user_parts.append("")

    # 逐文件的 diff + 上下文
    for fd in unit.file_diffs:
        file_path = fd.file_path
        user_parts.append(f"### File: {file_path}")
        user_parts.append(f"Status: {fd.status}  (+{fd.additions}/-{fd.deletions})")
        user_parts.append("")

        # 此文件的 AST 上下文
        code_ctx = unit.code_contexts.get(file_path)
        if code_ctx and code_ctx.symbols:
            user_parts.append("#### Symbols in this file:")
            for sym in code_ctx.symbols[:15]:  # 限制最多 15 个符号
                user_parts.append(f"- `{sym.qualified_name}` ({sym.kind})")
            user_parts.append("")

        # Diff 内容
        user_parts.append("#### Diff:")
        for hunk in fd.hunks:
            hunk_header = f"@@ -{hunk.source_start},{hunk.source_count} +{hunk.target_start},{hunk.target_count} @@"
            if hunk.heading:
                hunk_header += f" {hunk.heading}"
            user_parts.append(hunk_header)

            for line in hunk.lines:
                marker = {"added": "+", "removed": "-", "context": " "}[line.line_type]
                user_parts.append(f"{marker}{line.content}")

            user_parts.append("")

    # --- 指令 ---
    user_parts.append("---")
    user_parts.append("""## Instructions

Analyse the above diff and identify potential issues. For each issue, respond with exactly one JSON object following this schema:

```json
{
  "file_path": "<file path>",
  "line_start": <int or null>,
  "line_end": <int or null>,
  "severity": "critical" | "major" | "minor" | "info",
  "category": "security" | "performance" | "bug" | "concurrency" | "error_handling" | "code_style" | "maintainability" | "best_practice" | "potential_issue",
  "title": "<short, specific title>",
  "description": "<detailed explanation including why it matters and impact>",
  "suggestion": "<concrete, specific fix or improvement>",
  "code_example": "<before/after code snippet, if applicable; use \\n for newlines>",
  "confidence": <0.0 to 1.0>,
  "uncertainty_reason": "<if confidence < 0.8, explain what additional information would help resolve uncertainty>"
}
```

### Language Requirement:
All text fields (title, description, suggestion, code_example, uncertainty_reason) MUST be written in Chinese.

### Quality Rules:
1. ONLY report issues with confidence >= 0.7 for "critical" or "major" severity
2. For confidence < 0.7, set severity to "info" or "minor"
3. If no issues found, respond with an empty JSON array: []
4. DO NOT report style preferences as bugs
5. Consider the diff in context — a standalone change may look wrong but be correct in context
6. Flag missing error handling, especially around I/O, network calls, and user input
7. Each issue MUST have a concrete, actionable suggestion

### Output Format:
Wrap ALL findings in a single JSON array:
[
  { ... finding 1 ... },
  { ... finding 2 ... }
]

If there are no findings, return:
[]
""")

    user_msg = {"role": "user", "content": "\n".join(user_parts)}

    return [system_msg, user_msg]


# ---------------------------------------------------------------------------
# 总结提示词构建器
# ---------------------------------------------------------------------------


def build_summary_prompt(
    pr_title: str,
    pr_description: str,
    file_count: int,
    additions: int,
    deletions: int,
    findings_json: str,
) -> list[dict[str, str]]:
    """构建总结生成提示词。

    参数:
        pr_title: PR 标题。
        pr_description: PR 正文/描述。
        file_count: 变更文件数量。
        additions: 新增行数。
        deletions: 删除行数。
        findings_json: 所有发现项的 JSON 字符串。

    返回:
        用于 LLM 的消息列表。
    """
    system_msg = {
        "role": "system",
        "content": SYSTEM_PROMPT_SUMMARISER,
    }

    user_content = f"""## Pull Request Summary Request

PR Title: {pr_title}
PR Description: {pr_description}

Files Changed: {file_count} files, +{additions}/-{deletions}

## Analysis Findings
{findings_json}

## Instructions
Write a concise summary (2-4 paragraphs) covering:

1. **Business Intent**: What is this PR trying to achieve?
2. **Technical Approach**: How does it solve the problem? What patterns are used?
3. **Key Issues**: The 1-3 most critical findings that need attention before merge
4. **Overall Assessment**: One of:
   - ✅ **Approved** — No critical issues, ready to merge
   - ⚠️ **Changes Needed** — Minor issues to address
   - ❌ **Review Required** — Critical issues that must be fixed

Format as Markdown with appropriate headers. Keep it professional and actionable.

**IMPORTANT: Write the entire response in Chinese, including all headers, descriptions, and suggestions.**"""

    return [
        system_msg,
        {"role": "user", "content": user_content},
    ]


# ---------------------------------------------------------------------------
# 建议提示词构建器
# ---------------------------------------------------------------------------


def build_suggestion_prompt(
    finding_title: str,
    finding_description: str,
    file_path: str,
    line_start: int | None,
    current_code: str,
    language: str = "python",
) -> list[dict[str, str]]:
    """为特定发现项构建详细的建议提示词。

    参数:
        finding_title: 发现项的简短标题。
        finding_description: 详细描述。
        file_path: 问题所在的文件路径。
        line_start: 起始行号。
        current_code: 当前的代码片段。
        language: 用于语法高亮的编程语言。

    返回:
        用于 LLM 的消息列表。
    """
    system_msg: dict[str, str] = {
        "role": "system",
        "content": """You are a senior engineer providing code review suggestions.
Your suggestions must be:
1. Specific — show exact code changes, not general advice
2. Safe — consider edge cases and potential regressions
3. Minimal — make the smallest change that fixes the issue
4. Well-explained — say WHY the fix works

IMPORTANT: Respond in Chinese. All suggestions and explanations must be written in Chinese.""",
    }

    user_content = f"""## Code Fix Suggestion

### Finding
- **File**: {file_path}:{line_start or "?"}
- **Issue**: {finding_title}
- **Description**: {finding_description}

### Current Code
```{language}
{current_code}
```

### Instructions
Provide a concrete, actionable fix. Return a JSON object:

```json
{{
  "suggestion": "<detailed explanation of the fix>",
  "before": "<current code, verbatim>",
  "after": "<fixed code>",
  "risk_of_fix": "low" | "medium" | "high",
  "alternative_approaches": ["<alternative 1>", "<alternative 2>"],
  "edge_cases": ["<edge case 1>", "<edge case 2>"]
}}
```"""

    return [
        system_msg,
        {"role": "user", "content": user_content},
    ]
