"""
Report generator — produces Markdown and JSON reports from analysis results.

Design rationale:
- Markdown output is optimised for GitHub PR comments (limited formatting,
  no HTML, emoji-supported)
- JSON output is designed for CI/CD integration (structured, parseable,
  machine-readable)
- The "both" format outputs both files
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any

from src.llm.analyzer import AnalysisReport, Finding
from src.report.templates import CATEGORY_EMOJI, MARKDOWN_FOOTER, SEVERITY_EMOJI

logger = logging.getLogger(__name__)

# Severity sort order
_SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "info": 3}


class ReportGenerator:
    """Generate structured reports from analysis results."""

    def generate_markdown(self, report: AnalysisReport, pr_title: str = "") -> str:
        """Generate a formatted Markdown report.

        Args:
            report: The analysis report.
            pr_title: Optional PR title for the header.

        Returns:
            Markdown string suitable for GitHub PR comments.
        """
        parts: list[str] = []

        # Header
        if pr_title:
            parts.append(f"# 🔍 AI PR Review: {pr_title}")
        else:
            parts.append("# 🔍 AI PR Review")
        parts.append("")

        # Summary
        if report.summary:
            parts.append("## 📋 Summary")
            parts.append("")
            parts.append(report.summary)
            parts.append("")

        # Stats bar
        if report.stats:
            total = report.stats.get("total_findings", len(report.findings))
            by_severity = report.stats.get("by_severity", {})
            severity_str = " · ".join(
                f"{SEVERITY_EMOJI.get(s, '')} {s}: {c}"
                for s, c in sorted(by_severity.items(), key=lambda x: _SEVERITY_ORDER.get(x[0], 99))
            )
            parts.append(f"**{total} findings** — {severity_str}")
            parts.append("")

        # Findings
        if report.findings:
            parts.append("## 🔎 Findings")
            parts.append("")

            for i, finding in enumerate(report.findings, 1):
                severity_icon = SEVERITY_EMOJI.get(finding.severity, "")
                category_icon = CATEGORY_EMOJI.get(finding.category, "")
                confidence_pct = int(finding.confidence * 100)

                # Finding header
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

                # Description
                if finding.description:
                    parts.append(finding.description)
                    parts.append("")

                # Suggestion
                if finding.suggestion:
                    parts.append("**💡 Suggestion:**")
                    parts.append("")
                    parts.append(finding.suggestion)
                    parts.append("")

                # Code example
                if finding.code_example:
                    parts.append("**📝 Example:**")
                    parts.append("")
                    parts.append(f"```\n{finding.code_example}\n```")
                    parts.append("")

                # Separator
                if i < len(report.findings):
                    parts.append("---")
                    parts.append("")

        # Metadata
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

        # Footer
        parts.append(MARKDOWN_FOOTER)

        return "\n".join(parts)

    def generate_json(self, report: AnalysisReport) -> dict[str, Any]:
        """Generate a JSON-serialisable report for CI/CD integration.

        Returns:
            A dict that can be serialised to JSON.
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
        """Generate an optimised Markdown comment for GitHub PRs.

        GitHub comments have some constraints:
        - No HTML tables with complex formatting
        - Emoji is well-supported
        - Long comments are collapsible

        This method produces a more compact version of the markdown report
        suitable for direct posting as a PR comment.
        """
        return self.generate_markdown(report, pr_title)

    def save_report(
        self,
        report: AnalysisReport,
        output_path: str,
        pr_title: str = "",
    ) -> str:
        """Save a report to a file (auto-detect format from extension).

        Args:
            report: The analysis report.
            output_path: File path (.md or .json).
            pr_title: Optional PR title.

        Returns:
            The path to the saved file.
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
