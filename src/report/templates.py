"""
报告模板辅助函数，用于保持一致的格式。
"""

from __future__ import annotations

# 严重程度表情符号映射
SEVERITY_EMOJI = {
    "critical": "🔴",
    "major": "🟠",
    "minor": "🟡",
    "info": "🔵",
}

# 严重程度中文标签
SEVERITY_LABELS = {
    "critical": "严重",
    "major": "主要",
    "minor": "次要",
    "info": "建议",
}

# 分类表情符号映射
CATEGORY_EMOJI = {
    "security": "🔒",
    "performance": "⚡",
    "bug": "🐛",
    "concurrency": "🧵",
    "error_handling": "⚠️",
    "code_style": "🎨",
    "maintainability": "📦",
    "best_practice": "✅",
    "potential_issue": "❓",
}

# 分类中文标签
CATEGORY_LABELS = {
    "security": "安全",
    "performance": "性能",
    "bug": "缺陷",
    "concurrency": "并发",
    "error_handling": "错误处理",
    "code_style": "代码风格",
    "maintainability": "可维护性",
    "best_practice": "最佳实践",
    "potential_issue": "潜在问题",
}

MARKDOWN_FOOTER = """
---
<sub>🤖 由 [ai-pr-reviewer](https://github.com/pengxueqi616-commits/ai-pr-reviewer) 生成</sub>
"""
