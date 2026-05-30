"""."""

from __future__ import annotations

import json
from pathlib import Path

from src.llm.analyzer import AnalysisMetadata, AnalysisReport, Finding
from src.report.generator import ReportGenerator


class TestReportGenerator:
    """Test suite for ReportGenerator."""

    def setup_method(self) -> None:
        self.generator = ReportGenerator()

        # Sample data
        self.findings = [
            Finding(
                file_path="src/api.py",
                line_start=15,
                line_end=17,
                severity="critical",
                category="security",
                title="SQL Injection vulnerability",
                description="User input is directly concatenated into SQL query.",
                suggestion="Use parameterised queries instead of string formatting.",
                code_example="# Before:\nquery = f\"SELECT * FROM data WHERE value = '{user_input}'\"\n\n# After:\nquery = \"SELECT * FROM data WHERE value = ?\"\ncursor.execute(query, (user_input,))",
                confidence=0.95,
            ),
            Finding(
                file_path="src/utils.py",
                line_start=5,
                severity="major",
                category="error_handling",
                title="Missing input validation",
                description="Function does not validate input parameters.",
                suggestion="Add type checking and None guards.",
                confidence=0.85,
            ),
            Finding(
                file_path="src/main.py",
                severity="minor",
                category="code_style",
                title="Unused import",
                description="The 'os' module is imported but not used.",
                suggestion="Remove unused import.",
                confidence=0.7,
            ),
        ]

        self.report = AnalysisReport(
            summary="This PR introduces a new API endpoint but contains a critical SQL injection vulnerability.",
            findings=self.findings,
            stats={
                "total_findings": 3,
                "by_severity": {"critical": 1, "major": 1, "minor": 1},
                "by_category": {"security": 1, "error_handling": 1, "code_style": 1},
                "high_confidence_count": 1,
            },
            metadata=AnalysisMetadata(
                model="claude-sonnet-4-20250514",
                provider="anthropic",
                total_input_tokens=4500,
                total_output_tokens=1200,
                analysis_duration_seconds=15.3,
                units_analysed=2,
                units_failed=0,
                timestamp="2026-05-30T10:00:00",
            ),
        )

    def test_generate_markdown_contains_findings(self) -> None:
        """Test that markdown output contains all findings."""
        md = self.generator.generate_markdown(self.report, pr_title="Fix API security issues")
        assert "SQL Injection vulnerability" in md
        assert "Missing input validation" in md
        assert "Unused import" in md
        assert "3 findings" in md

    def test_generate_markdown_contains_severity_indicators(self) -> None:
        """Test that severity emoji is present in markdown."""
        md = self.generator.generate_markdown(self.report)
        assert "🔴" in md  # critical
        assert "🟠" in md  # major
        assert "🟡" in md  # minor

    def test_generate_markdown_contains_metadata(self) -> None:
        """Test that analysis metadata appears in the report."""
        md = self.generator.generate_markdown(self.report)
        assert "claude-sonnet-4-20250514" in md
        assert "anthropic" in md
        assert "15.3s" in md or "15" in md

    def test_generate_json_structure(self) -> None:
        """Test that JSON output has the correct structure."""
        data = self.generator.generate_json(self.report)
        assert data["version"] == "0.1.0"
        assert len(data["findings"]) == 3
        assert data["stats"]["total_findings"] == 3

        # Check finding structure
        first = data["findings"][0]
        assert first["file_path"] == "src/api.py"
        assert first["severity"] == "critical"
        assert first["category"] == "security"
        assert first["confidence"] == 0.95

        # Check metadata
        assert data["metadata"]["model"] == "claude-sonnet-4-20250514"
        assert data["metadata"]["duration_seconds"] == 15.3

    def test_generate_github_comment(self) -> None:
        """Test that GitHub comment generation works."""
        comment = self.generator.generate_github_comment(self.report, "Test PR")
        assert "Test PR" in comment
        assert "SQL Injection vulnerability" in comment

    def test_save_report_markdown(self, tmp_path: Path) -> None:
        """Test saving a markdown report to file."""
        output_path = str(tmp_path / "report.md")
        saved = self.generator.save_report(self.report, output_path, "Test PR")
        assert saved == output_path
        assert Path(output_path).exists()
        content = Path(output_path).read_text(encoding="utf-8")
        assert "SQL Injection vulnerability" in content

    def test_save_report_json(self, tmp_path: Path) -> None:
        """Test saving a JSON report to file."""
        output_path = str(tmp_path / "report.json")
        saved = self.generator.save_report(self.report, output_path)
        assert saved == output_path
        assert Path(output_path).exists()
        data = json.loads(Path(output_path).read_text())
        assert len(data["findings"]) == 3

    def test_empty_findings(self) -> None:
        """Test report generation with no findings."""
        empty_report = AnalysisReport(
            summary="No issues found.",
            findings=[],
            stats={"total_findings": 0, "by_severity": {}, "by_category": {}, "high_confidence_count": 0},
        )
        md = self.generator.generate_markdown(empty_report)
        assert "0 findings" in md
