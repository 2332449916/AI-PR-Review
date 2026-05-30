"""Tests for the LLM analyzer module."""

from __future__ import annotations

from src.llm.analyzer import Finding, _parse_findings_json, _calibrate_confidence


class TestParseFindingsJson:
    """Test suite for JSON parsing from LLM responses."""

    def test_parse_valid_json_array(self) -> None:
        """Test parsing a valid JSON array of findings."""
        response = """[
            {"file_path": "src/main.py", "severity": "critical", "title": "Bug found", "confidence": 0.95, "description": "A bug", "suggestion": "Fix it", "category": "bug"}
        ]"""
        results = _parse_findings_json(response)
        assert len(results) == 1
        assert results[0]["title"] == "Bug found"
        assert results[0]["severity"] == "critical"

    def test_parse_empty_array(self) -> None:
        """Test parsing an empty JSON array."""
        results = _parse_findings_json("[]")
        assert results == []

    def test_parse_with_wrapped_findings_key(self) -> None:
        """Test parsing response where findings are wrapped in a key."""
        response = """{"findings": [
            {"file_path": "src/main.py", "severity": "major", "title": "Issue", "confidence": 0.8, "description": "desc", "suggestion": "sug", "category": "performance"}
        ]}"""
        results = _parse_findings_json(response)
        assert len(results) == 1
        assert results[0]["title"] == "Issue"

    def test_parse_with_extra_text(self) -> None:
        """Test parsing when LLM adds text before/after the JSON."""
        response = """Here are my findings:

[
    {"file_path": "src/utils.py", "severity": "minor", "title": "Style issue", "confidence": 0.7, "description": "desc", "suggestion": "sug", "category": "code_style"}
]

That's all I found."""
        results = _parse_findings_json(response)
        assert len(results) == 1
        assert results[0]["title"] == "Style issue"

    def test_parse_malformed_json(self) -> None:
        """Test parsing malformed JSON returns empty list."""
        response = "This is not JSON at all."
        results = _parse_findings_json(response)
        assert results == []

    def test_parse_empty_string(self) -> None:
        """Test parsing empty string."""
        assert _parse_findings_json("") == []
        assert _parse_findings_json("   ") == []


class TestConfidenceCalibration:
    """Test suite for confidence score calibration."""

    def test_high_confidence_preserved(self) -> None:
        """Test that high confidence findings retain their score."""
        finding = Finding(
            file_path="src/main.py",
            line_start=10,
            line_end=15,
            severity="critical",
            category="bug",
            title="Null pointer",
            description="A detailed description of the issue",
            suggestion="Add a null check",
            confidence=0.95,
        )
        calibrated = _calibrate_confidence(finding)
        assert calibrated == 0.95  # no penalty for well-formed finding

    def test_security_penalty(self) -> None:
        """Test that security issues get a slight penalty."""
        finding = Finding(
            file_path="src/api.py",
            line_start=5,
            severity="critical",
            category="security",
            title="SQL Injection",
            description="User input concatenated into query",
            suggestion="Use parameterised queries",
            confidence=0.95,
        )
        calibrated = _calibrate_confidence(finding)
        assert round(calibrated, 2) == 0.90

    def test_no_line_number_penalty(self) -> None:
        """Test missing line numbers reduce confidence."""
        finding = Finding(
            file_path="src/main.py",
            severity="major",
            category="bug",
            title="Vague finding",
            description="Some issue somewhere",
            suggestion="Look into it",
            confidence=0.95,
        )
        calibrated = _calibrate_confidence(finding)
        assert calibrated < 0.95

    def test_short_description_penalty(self) -> None:
        """Test that very short descriptions reduce confidence."""
        finding = Finding(
            file_path="src/main.py",
            line_start=10,
            severity="major",
            category="bug",
            title="Issue",
            description="Fix",
            suggestion="Fix",
            confidence=0.95,
        )
        calibrated = _calibrate_confidence(finding)
        assert calibrated < 0.85

    def test_confidence_clamped(self) -> None:
        """Test that confidence is clamped to [0, 1]."""
        finding = Finding(
            file_path="src/main.py",
            severity="info",
            category="code_style",
            title="Test",
            description="",
            suggestion="",
            confidence=0.1,
        )
        calibrated = _calibrate_confidence(finding)
        assert 0.0 <= calibrated <= 1.0


class TestFindingModel:
    """Test suite for the Finding data model."""

    def test_auto_generate_rule_id(self) -> None:
        """Test that rule_id is auto-generated from title."""
        finding = Finding(
            file_path="src/main.py",
            title="SQL Injection in login handler",
            severity="critical",
            category="security",
            description="desc",
            suggestion="sug",
            confidence=0.9,
        )
        assert "sql-injection" in finding.rule_id
        assert "login" in finding.rule_id

    def test_custom_rule_id_preserved(self) -> None:
        """Test that custom rule_id is not overwritten."""
        finding = Finding(
            file_path="src/main.py",
            title="Test finding",
            rule_id="my-custom-rule",
            confidence=0.5,
        )
        assert finding.rule_id == "my-custom-rule"

    def test_empty_title_rule_id(self) -> None:
        """Test that empty title generates empty rule_id."""
        finding = Finding(
            file_path="src/main.py",
            title="",
            confidence=0.5,
        )
        assert finding.rule_id == ""
