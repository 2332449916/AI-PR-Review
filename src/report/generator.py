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
from dataclasses import asdict
from typing import Any

from src.llm.analyzer import AnalysisReport, Finding
from src.report.templates import CATEGORY_EMOJI, MARKDOWN_FOOTER, SEVERITY_EMOJI

logger = logging.getLogger(__name__)

# 严重程度排序
_SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "info": 3}


class ReportGenerator:
    """从分析结果生成结构化报告。"""

    def generate_markdown(self, report: AnalysisReport, pr_title: str = "") -> str:
        """生成格式化的 Markdown 报告。

        参数:
            report: 分析报告。
            pr_title: 可选的 PR 标题，用于报告头部。

        返回:
            适用于 GitHub PR 评论的 Markdown 字符串。
        """
        parts: list[str] = []

        # 头部
        if pr_title:
            parts.append(f"# 🔍 AI PR Review: {pr_title}")
        else:
            parts.append("# 🔍 AI PR Review")
        parts.append("")

        # 摘要
        if report.summary:
            parts.append("## 📋 Summary")
            parts.append("")
            parts.append(report.summary)
            parts.append("")

        # 统计栏
        if report.stats:
            total = report.stats.get("total_findings", len(report.findings))
            by_severity = report.stats.get("by_severity", {})
            severity_str = " · ".join(
                f"{SEVERITY_EMOJI.get(s, '')} {s}: {c}"
                for s, c in sorted(by_severity.items(), key=lambda x: _SEVERITY_ORDER.get(x[0], 99))
            )
            parts.append(f"**{total} findings** — {severity_str}")
            parts.append("")

        # 发现
        if report.findings:
            parts.append("## 🔎 Findings")
            parts.append("")

            for i, finding in enumerate(report.findings, 1):
                severity_icon = SEVERITY_EMOJI.get(finding.severity, "")
                category_icon = CATEGORY_EMOJI.get(finding.category, "")
                confidence_pct = int(finding.confidence * 100)

                # 发现头部
                location = ""
                if finding.file_path:
                    loc = finding.file_path
                    if finding.line_start:
                        loc += f":{finding.line_start}"
                        if finding.line_end and finding.line_end != finding.line_start:
                            loc += f"-{finding.line_end}"
                    location = f" — `{loc}`"

                parts.append(
                    f"### {severity_icon} **{finding.title}** "
                    f"{category_icon} ({confidence_pct}% confidence){location}"
                )
                parts.append("")

                # 描述
                if finding.description:
                    parts.append(finding.description)
                    parts.append("")

                # 建议
                if finding.suggestion:
                    parts.append("**💡 Suggestion:**")
                    parts.append("")
                    parts.append(finding.suggestion)
                    parts.append("")

                # 代码示例
                if finding.code_example:
                    parts.append("**📝 Example:**")
                    parts.append("")
                    parts.append(f"```\n{finding.code_example}\n```")
                    parts.append("")

                # 分隔符
                if i < len(report.findings):
                    parts.append("---")
                    parts.append("")

        # 元数据
        if report.metadata:
            parts.append("---")
            parts.append("")
            parts.append("### ⚙️ Analysis Details")
            parts.append("")
            meta = report.metadata
            parts.append(f"- **Model**: {meta.model}")
            parts.append(f"- **Provider**: {meta.provider}")
            parts.append(f"- **Duration**: {meta.analysis_duration_seconds:.1f}s")
            parts.append(f"- **Units**: {meta.units_analysed} analysed, {meta.units_failed} failed")
            parts.append(f"- **Tokens**: {meta.total_input_tokens:,} in / {meta.total_output_tokens:,} out")
            parts.append("")

        # 页脚
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
