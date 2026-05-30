"""Tests for the prompt templates module."""

from __future__ import annotations

from src.diff.models import DiffLine, FileDiff, Hunk
from src.context.builder import AnalysisUnit
from src.llm.prompts import build_analysis_prompt, build_summary_prompt, build_suggestion_prompt


class TestPromptBuilder:
    """Test suite for prompt building."""

    def test_build_analysis_prompt_structure(self) -> None:
        """Test that the analysis prompt has the correct structure."""
        file_diff = FileDiff(
            source_file="a/src/main.py",
            target_file="b/src/main.py",
            status="modified",
            hunks=[
                Hunk(
                    source_start=10,
                    source_count=5,
                    target_start=10,
                    target_count=7,
                    heading="def main",
                    lines=[
                        DiffLine(content="def main():", line_type="context", old_line_no=10, new_line_no=10),
                        DiffLine(content="    print('hello')", line_type="removed", old_line_no=11, new_line_no=None),
                        DiffLine(content="    print('hello world')", line_type="added", old_line_no=None, new_line_no=11),
                    ],
                )
            ],
            additions=1,
            deletions=1,
        )

        unit = AnalysisUnit(
            file_diffs=[file_diff],
            estimated_tokens=500,
            unit_id="src/main.py",
        )

        messages = build_analysis_prompt(unit)
        assert len(messages) == 2

        # System message
        assert messages[0]["role"] == "system"
        assert "expert code reviewer" in messages[0]["content"].lower()

        # User message
        assert messages[1]["role"] == "user"
        content = messages[1]["content"]
        assert "src/main.py" in content
        assert "print('hello')" in content
        assert "print('hello world')" in content

    def test_build_summary_prompt(self) -> None:
        """Test summary prompt generation."""
        messages = build_summary_prompt(
            pr_title="Fix login bug",
            pr_description="Fixes a race condition in the login handler",
            file_count=3,
            additions=45,
            deletions=12,
            findings_json='[{"severity": "critical", "title": "Race condition"}]',
        )

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

        content = messages[1]["content"]
        assert "Fix login bug" in content
        assert "3 files" in content
        assert "+45/-12" in content or "+45" in content

    def test_build_suggestion_prompt(self) -> None:
        """Test suggestion prompt generation."""
        messages = build_suggestion_prompt(
            finding_title="SQL Injection",
            finding_description="User input is concatenated into SQL query",
            file_path="src/api.py",
            line_start=42,
            current_code="query = f\"SELECT * FROM users WHERE id = '{user_id}'\"",
            language="python",
        )

        assert len(messages) == 2
        content = messages[1]["content"]
        assert "SQL Injection" in content
        assert "src/api.py" in content
        assert "SELECT" in content

    def test_analysis_prompt_includes_format_instructions(self) -> None:
        """Test that the analysis prompt includes output format instructions."""
        file_diff = FileDiff(
            source_file="a/src/main.py",
            target_file="b/src/main.py",
            status="modified",
        )
        unit = AnalysisUnit(file_diffs=[file_diff], estimated_tokens=100, unit_id="test")

        messages = build_analysis_prompt(unit)
        user_content = messages[1]["content"]
        assert "confidence" in user_content
        assert "severity" in user_content
        assert "file_path" in user_content
        assert "critical" in user_content or "critical" in user_content.lower()
