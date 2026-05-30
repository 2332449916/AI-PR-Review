"""
Prompt templates for AI-powered code review.

This module defines three distinct prompt templates, each optimised for a
specific task in the review pipeline:

1. **Risk Analysis Prompt**: The core prompt — analyses a diff and returns
   structured findings (bugs, security issues, etc.) as JSON.
2. **Summary Prompt**: Synthesises all findings into a natural-language
   PR summary.
3. **Suggestion Prompt**: Generates concrete code suggestions for a specific
   finding.

Design rationale:
- **Why three separate prompts instead of one?** Each task has different
  optimal instruction sets. Combining them dilutes focus and increases token
  waste. The risk analysis needs structured output; the summary needs
  narrative generation; the suggestion needs code synthesis.
- **Why JSON-in-prompt instead of function calling?** JSON-in-prompt works
  across all providers and models, including local ones that lack function
  calling support. The trade-off is slightly less reliable parsing, which we
  mitigate with a fallback parser.
- **Why include "confidence" and "uncertainty_reason"?** These fields are
  our primary false-positive mitigation. By explicitly asking the model to
  rate its own confidence and explain uncertainty, we get a calibrated signal
  we can threshold against.
- **Few-shot examples**: Each template includes representative examples that
  demonstrate the expected output format and reasoning depth. These are not
  real findings — they are designed to calibrate the model's output.
"""

from __future__ import annotations

from typing import Any

from src.diff.models import FileDiff
from src.context.builder import AnalysisUnit


# ---------------------------------------------------------------------------
# System prompts
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
5. Return findings as valid JSON"""

SYSTEM_PROMPT_SUMMARISER = """You are a technical writing assistant specialised in code review
summarisation. Your task is to synthesise multiple analysis results into a
clear, actionable PR summary. Write in a professional tone, be concise, and
focus on what the reader needs to know."""


# ---------------------------------------------------------------------------
# Analysis Prompt Builder
# ---------------------------------------------------------------------------


def build_analysis_prompt(unit: AnalysisUnit) -> list[dict[str, str]]:
    """Build the analysis prompt messages for a single analysis unit.

    Args:
        unit: The analysis unit containing diffs and context.

    Returns:
        A list of message dicts with ``role`` and ``content`` keys,
        suitable for passing to any LLM provider.
    """
    # --- System message ---
    system_msg = {
        "role": "system",
        "content": SYSTEM_PROMPT_REVIEWER,
    }

    # --- User message with diff and context ---
    user_parts: list[str] = []

    # PR context header
    user_parts.append(f"## PR Context\n")
    user_parts.append(f"Files in this unit: {len(unit.file_diffs)}")
    user_parts.append("")

    # File-by-file diff + context
    for fd in unit.file_diffs:
        file_path = fd.file_path
        user_parts.append(f"### File: {file_path}")
        user_parts.append(f"Status: {fd.status}  (+{fd.additions}/-{fd.deletions})")
        user_parts.append("")

        # AST context for this file
        code_ctx = unit.code_contexts.get(file_path)
        if code_ctx and code_ctx.symbols:
            user_parts.append("#### Symbols in this file:")
            for sym in code_ctx.symbols[:15]:  # limit to top 15
                user_parts.append(f"- `{sym.qualified_name}` ({sym.kind})")
            user_parts.append("")

        # Diff content
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

    # --- Instructions ---
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
# Summary Prompt Builder
# ---------------------------------------------------------------------------


def build_summary_prompt(
    pr_title: str,
    pr_description: str,
    file_count: int,
    additions: int,
    deletions: int,
    findings_json: str,
) -> list[dict[str, str]]:
    """Build the summary generation prompt.

    Args:
        pr_title: The PR title.
        pr_description: The PR body/description.
        file_count: Number of changed files.
        additions: Lines added.
        deletions: Lines deleted.
        findings_json: JSON string of all findings.

    Returns:
        Messages list for the LLM.
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

Format as Markdown with appropriate headers. Keep it professional and actionable."""

    return [
        system_msg,
        {"role": "user", "content": user_content},
    ]


# ---------------------------------------------------------------------------
# Suggestion Prompt Builder
# ---------------------------------------------------------------------------


def build_suggestion_prompt(
    finding_title: str,
    finding_description: str,
    file_path: str,
    line_start: int | None,
    current_code: str,
    language: str = "python",
) -> list[dict[str, str]]:
    """Build a detailed suggestion prompt for a specific finding.

    Args:
        finding_title: Short title of the finding.
        finding_description: Detailed description.
        file_path: File path where the issue is located.
        line_start: Starting line number.
        current_code: The current code snippet.
        language: Programming language for syntax highlighting.

    Returns:
        Messages list for the LLM.
    """
    system_msg: dict[str, str] = {
        "role": "system",
        "content": """You are a senior engineer providing code review suggestions.
Your suggestions must be:
1. Specific — show exact code changes, not general advice
2. Safe — consider edge cases and potential regressions
3. Minimal — make the smallest change that fixes the issue
4. Well-explained — say WHY the fix works""",
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
