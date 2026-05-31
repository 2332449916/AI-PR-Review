"""
报告生成器 — 从分析结果生成 Markdown 和 JSON 报告。

设计原理：
- Markdown 输出针对 GitHub PR 评论进行了优化（有限格式，
  不支持 HTML，支持 emoji）
- JSON 输出专为 CI/CD 集成设计（结构化、可解析、
  机器可读）
- "both" 格式同时输出两种文件
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.llm.analyzer import AnalysisReport, Finding
from src.report.templates import (
    CATEGORY_EMOJI,
    CATEGORY_LABELS,
    MARKDOWN_FOOTER,
    SEVERITY_EMOJI,
    SEVERITY_LABELS,
)

logger = logging.getLogger(__name__)

# 严重程度排序
_SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "info": 3}

# 严重程度颜色标签
_SEVERITY_BADGES = {
    "critical": "🔴 严重",
    "major": "🟠 主要",
    "minor": "🟡 次要",
    "info": "🔵 建议",
}


class ReportGenerator:
    """从分析结果生成结构化报告。"""

    def _build_severity_bar(self, by_severity: dict[str, int], total: int) -> str:
        """生成直观的严重程度分布条形图。"""
        if total == 0:
            return ""
        bars = []
        for sev in ["critical", "major", "minor", "info"]:
            count = by_severity.get(sev, 0)
            if count > 0:
                pct = count / total * 100
                bar_len = max(1, round(pct / 5))  # 每个单位 5%
                bar = "█" * bar_len
                label = SEVERITY_LABELS.get(sev, sev)
                bars.append(f"  {SEVERITY_EMOJI.get(sev, '')} **{label}**: {bar} {count} ({pct:.0f}%)")
        return "\n".join(bars)

    def _build_finding_card(self, finding: Finding, index: int) -> list[str]:
        """生成单个问题卡片。"""
        parts = []

        # 问题编号 + 标题
        severity_badge = _SEVERITY_BADGES.get(finding.severity, finding.severity)
        category_icon = CATEGORY_EMOJI.get(finding.category, "")
        category_label = CATEGORY_LABELS.get(finding.category, finding.category)
        confidence_pct = int(finding.confidence * 100)

        parts.append(f"### {index}. {finding.title}")
        parts.append("")

        # 元信息行：严重程度 / 分类 / 置信度 / 位置
        meta_parts = [f"**{severity_badge}**"]
        if category_icon:
            meta_parts.append(f"{category_icon} {category_label}")
        meta_parts.append(f"🎯 {confidence_pct}% 置信度")
        if finding.file_path:
            loc = f"`{finding.file_path}"
            if finding.line_start:
                loc += f":{finding.line_start}"
                if finding.line_end and finding.line_end != finding.line_start:
                    loc += f"-{finding.line_end}"
            loc += "`"
            meta_parts.append(f"📄 {loc}")
        parts.append("> " + " · ".join(meta_parts))
        parts.append("")

        # 描述
        if finding.description:
            parts.append(finding.description)
            parts.append("")

        # 建议
        if finding.suggestion:
            parts.append("---")
            parts.append(f"**💡 修复建议**")
            parts.append("")
            parts.append(finding.suggestion)
            parts.append("")

        # 代码示例
        if finding.code_example:
            parts.append("")
            parts.append("**📝 代码示例**")
            parts.append("")
            parts.append(f"```\n{finding.code_example}\n```")
            parts.append("")

        return parts

    def generate_markdown(self, report: AnalysisReport, pr_title: str = "") -> str:
        """生成格式化的 Markdown 报告。

        参数:
            report: 分析报告。
            pr_title: 可选的 PR 标题，用于报告头部。

        返回:
            适用于 GitHub PR 评论的 Markdown 字符串。
        """
        parts: list[str] = []

        # ===== 头部 =====
        if pr_title:
            parts.append(f"# 🔍 AI PR 审查报告: {pr_title}")
        else:
            parts.append("# 🔍 AI PR 审查报告")
        parts.append("")

        # ===== 摘要 =====
        if report.summary:
            parts.append("## 📋 审查总结")
            parts.append("")
            parts.append(report.summary)
            parts.append("")

        # ===== 问题统计 =====
        if report.stats and report.findings:
            total = report.stats.get("total_findings", len(report.findings))
            by_severity = report.stats.get("by_severity", {})
            by_category = report.stats.get("by_category", {})

            parts.append("## 📊 问题统计")
            parts.append("")

            # 严重程度分布条形图
            parts.append("### 严重程度分布")
            parts.append("")
            bar_chart = self._build_severity_bar(by_severity, total)
            if bar_chart:
                parts.append(bar_chart)
                parts.append("")

            # 分类分布
            if by_category:
                parts.append("### 问题分类")
                parts.append("")
                for cat, count in sorted(by_category.items(), key=lambda x: -x[1]):
                    cat_icon = CATEGORY_EMOJI.get(cat, "")
                    cat_label = CATEGORY_LABELS.get(cat, cat)
                    pct = count / total * 100
                    bar_len = max(1, round(pct / 5))
                    bar = "█" * bar_len
                    parts.append(f"  {cat_icon} **{cat_label}**: {bar} {count}")
                parts.append("")

            # 概要行
            sev_parts = []
            for s in ["critical", "major", "minor", "info"]:
                c = by_severity.get(s, 0)
                if c > 0:
                    sev_parts.append(f"{SEVERITY_EMOJI.get(s, '')} {SEVERITY_LABELS.get(s, s)} {c}")
            parts.append(f"> **共发现 {total} 个问题** —— {' · '.join(sev_parts)}")
            parts.append("")

        elif report.findings:
            total = len(report.findings)
            parts.append(f"> **共发现 {total} 个问题**")
            parts.append("")
        else:
            parts.append("> ✨ **未发现问题，代码质量良好！**")
            parts.append("")

        # ===== 问题详情 =====
        if report.findings:
            parts.append("---")
            parts.append("")
            parts.append("## 🔎 问题详情")
            parts.append("")

            for i, finding in enumerate(report.findings, 1):
                card = self._build_finding_card(finding, i)
                parts.extend(card)

                # 分隔符
                if i < len(report.findings):
                    parts.append("---")
                    parts.append("")

        # ===== 分析信息 =====
        if report.metadata:
            parts.append("---")
            parts.append("")
            parts.append("### ⚙️ 分析信息")
            parts.append("")
            meta = report.metadata
            parts.append(f"- **模型**: {meta.model}")
            parts.append(f"- **提供商**: {meta.provider}")
            parts.append(f"- **耗时**: {meta.analysis_duration_seconds:.1f}s")
            parts.append(f"- **分析单元**: {meta.units_analysed} 个已分析, {meta.units_failed} 个失败")
            parts.append(f"- **Token 用量**: {meta.total_input_tokens:,} in / {meta.total_output_tokens:,} out")
            parts.append("")

        # ===== 页脚 =====
        parts.append(MARKDOWN_FOOTER)

        return "\n".join(parts)

    def generate_json(self, report: AnalysisReport) -> dict[str, Any]:
        """生成用于 CI/CD 集成的 JSON 可序列化报告。

        返回:
            可序列化为 JSON 的字典。
        """
        return {
            "version": "0.1.0",
            "summary": report.summary,
            "stats": report.stats or {},
            "findings": [
                {
                    "file_path": f.file_path,
                    "line_start": f.line_start,
                    "line_end": f.line_end,
                    "severity": f.severity,
                    "category": f.category,
                    "title": f.title,
                    "description": f.description,
                    "suggestion": f.suggestion,
                    "code_example": f.code_example,
                    "confidence": round(f.confidence, 2),
                    "rule_id": f.rule_id,
                }
                for f in report.findings
            ],
            "metadata": {
                "model": report.metadata.model,
                "provider": report.metadata.provider,
                "duration_seconds": round(report.metadata.analysis_duration_seconds, 1),
                "input_tokens": report.metadata.total_input_tokens,
                "output_tokens": report.metadata.total_output_tokens,
                "timestamp": report.metadata.timestamp,
                "units_analysed": report.metadata.units_analysed,
                "units_failed": report.metadata.units_failed,
            },
        }

    def generate_github_comment(self, report: AnalysisReport, pr_title: str = "") -> str:
        """生成针对 GitHub PR 优化的 Markdown 评论。

        GitHub 评论有一些限制：
        - 不支持带有复杂格式的 HTML 表格
        - Emoji 得到良好支持
        - 长评论可折叠

        此方法生成更紧凑版本的 Markdown 报告，
        适合直接作为 PR 评论发布。
        """
        return self.generate_markdown(report, pr_title)

    def save_report(
        self,
        report: AnalysisReport,
        output_path: str,
        pr_title: str = "",
    ) -> str:
        """将报告保存到文件（根据扩展名自动检测格式）。

        参数:
            report: 分析报告。
            output_path: 文件路径（.md 或 .json）。
            pr_title: 可选的 PR 标题。

        返回:
            已保存文件的路径。
        """
        if output_path.endswith(".json"):
            data = self.generate_json(report)
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        else:
            content = self.generate_markdown(report, pr_title)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)

        logger.info("Report saved to %s", output_path)
        return output_path
