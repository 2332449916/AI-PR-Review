"""Tests for the ignore rules  engine."""

from __future__ import annotations

from pathlib import Path

from src.context.ignore import IgnoreFilter, IgnoreRules


class TestIgnoreFilter:
    """Test suite for IgnoreFilter."""

    def setup_method(self) -> None:
        self.filter = IgnoreFilter()

    def test_should_include_file_no_rules(self) -> None:
        """Test that all files are included when no ignore rules exist."""
        rules = IgnoreRules()
        assert self.filter.should_include_file("src/main.py", rules)
        assert self.filter.should_include_file("tests/test_main.py", rules)

    def test_should_exclude_generated_file(self) -> None:
        """Test excluding generated files."""
        rules = IgnoreRules(path_patterns=["*.generated.py"])
        assert not self.filter.should_include_file("src/utils.generated.py", rules)
        assert self.filter.should_include_file("src/utils.py", rules)

    def test_should_exclude_migrations(self) -> None:
        """Test excluding migration files."""
        rules = IgnoreRules(path_patterns=["**/migrations/*"])
        assert not self.filter.should_include_file("src/migrations/001_create_users.py", rules)
        assert self.filter.should_include_file("src/main.py", rules)

    def test_ignore_all(self) -> None:
        """Test that ignore_all=True excludes everything."""
        rules = IgnoreRules(ignore_all=True)
        assert not self.filter.should_include_file("any/file.py", rules)

    def test_filter_findings_by_rule_id(self) -> None:
        """Test filtering findings by rule ID."""
        from src.llm.analyzer import Finding

        rules = IgnoreRules(rule_ids=["no-console-log", "style-preference"])

        findings = [
            Finding(
                file_path="src/main.py",
                title="Console log in production",
                rule_id="no-console-log",
                confidence=0.9,
            ),
            Finding(
                file_path="src/main.py",
                title="Unused variable",
                rule_id="unused-variable",
                confidence=0.9,
            ),
        ]

        filtered = self.filter.filter_findings(findings, rules)
        assert len(filtered) == 1
        assert filtered[0].rule_id == "unused-variable"

    def test_glob_matching(self) -> None:
        """Test glob pattern matching."""
        # Single-level wildcard
        assert IgnoreFilter._matches_glob("src/main.py", "*.py")
        assert IgnoreFilter._matches_glob("src/main.py", "src/*.py")
        assert not IgnoreFilter._matches_glob("src/main.py", "tests/*.py")

        # Recursive wildcard
        assert IgnoreFilter._matches_glob("src/sub/deep/file.py", "**/deep/*")

    def test_ignore_all_findings(self) -> None:
        """Test that ignore_all=True removes all findings."""
        from src.llm.analyzer import Finding

        rules = IgnoreRules(ignore_all=True)
        findings = [
            Finding(file_path="src/main.py", title="Bug", confidence=0.9),
            Finding(file_path="src/utils.py", title="Security issue", confidence=0.9),
        ]

        filtered = self.filter.filter_findings(findings, rules)
        assert len(filtered) == 0

    def test_load_rules_from_content(self, tmp_path: Path, ai_review_ignore_content: str) -> None:
        """Test loading rules from a .ai-review-ignore file."""
        ignore_file = tmp_path / ".ai-review-ignore"
        ignore_file.write_text(ai_review_ignore_content)

        filter_with_path = IgnoreFilter(repo_path=str(tmp_path))
        rules = filter_with_path.load_rules()

        assert "*.generated.py" in rules.path_patterns
        assert "no-console-log" in rules.rule_ids
        assert "style-preference" in rules.rule_ids
        assert "**/migrations/*" in rules.path_patterns

        # Test path thresholds
        assert "**/test/**" in rules.path_thresholds
        assert rules.path_thresholds["**/test/**"] == "major"
