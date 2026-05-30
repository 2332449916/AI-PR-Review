"""Tests for the diff parser module ."""

from __future__ import annotations

from src.diff.models import DiffLine, FileDiff, Hunk
from src.diff.parser import DiffParser


class TestDiffParser:
    """Test suite for DiffParser."""

    def setup_method(self) -> None:
        self.parser = DiffParser()

    def test_parse_simple_diff(self, sample_diff_simple: str) -> None:
        """Test parsing a simple single-file diff."""
        files = self.parser.parse(sample_diff_simple)
        assert len(files) == 1

        fd = files[0]
        assert fd.file_path == "utils.py"
        assert fd.status == "modified"
        assert fd.additions >= 1
        assert fd.deletions >= 1

    def test_parse_multifile_diff(self, sample_diff_multifile: str) -> None:
        """Test parsing a multi-file diff."""
        files = self.parser.parse(sample_diff_multifile)
        assert len(files) == 2

        # Files should be sorted by appearance order
        file_paths = [f.file_path for f in files]
        assert "src/main.py" in file_paths
        assert "src/utils.py" in file_paths

    def test_parse_security_diff(self, sample_diff_security: str) -> None:
        """Test parsing a diff with security issues."""
        files = self.parser.parse(sample_diff_security)
        assert len(files) == 1
        assert files[0].file_path == "api.py"

    def test_parse_empty_diff(self, empty_diff: str) -> None:
        """Test parsing an empty diff returns empty list."""
        files = self.parser.parse(empty_diff)
        assert files == []

    def test_hunk_structure(self, sample_diff_simple: str) -> None:
        """Test that hunks have the correct structure."""
        files = self.parser.parse(sample_diff_simple)
        assert len(files) == 1
        assert len(files[0].hunks) == 1

        hunk = files[0].hunks[0]
        assert isinstance(hunk.source_start, int)
        assert isinstance(hunk.target_start, int)
        assert hunk.changed_line_count >= 1
        assert hunk.line_span[0] <= hunk.line_span[1]
        assert len(hunk.lines) > 0

    def test_line_types(self, sample_diff_simple: str) -> None:
        """Test that diff lines have correct line types."""
        files = self.parser.parse(sample_diff_simple)
        hunk = files[0].hunks[0]

        has_added = any(l.line_type == "added" for l in hunk.lines)
        has_removed = any(l.line_type == "removed" for l in hunk.lines)
        has_context = any(l.line_type == "context" for l in hunk.lines)

        assert has_added, "Expected at least one added line"
        assert has_removed, "Expected at least one removed line"
        assert has_context, "Expected at least one context line"

    def test_line_numbers(self, sample_diff_simple: str) -> None:
        """Test that line numbers are correctly assigned."""
        files = self.parser.parse(sample_diff_simple)
        hunk = files[0].hunks[0]

        for line in hunk.lines:
            if line.line_type == "added":
                assert line.new_line_no is not None, "Added lines should have new_line_no"
            elif line.line_type == "removed":
                assert line.old_line_no is not None, "Removed lines should have old_line_no"

    def test_parse_single_file(self, sample_diff_multifile: str) -> None:
        """Test extracting a single file from a multi-file diff."""
        result = self.parser.parse_single_file(sample_diff_multifile, "src/main.py")
        assert result is not None
        assert result.file_path == "src/main.py"

        result_missing = self.parser.parse_single_file(sample_diff_multifile, "nonexistent.py")
        assert result_missing is None

    def test_get_changed_lines(self, sample_diff_simple: str) -> None:
        """Test that changed lines are correctly identified."""
        files = self.parser.parse(sample_diff_simple)
        changed = self.parser.get_changed_lines(files[0])
        assert len(changed) > 0
        for file_path, line_no in changed:
            assert file_path == "utils.py"
            assert isinstance(line_no, int)

    def test_file_diff_properties(self) -> None:
        """Test FileDiff computed properties."""
        from src.diff.models import FileDiff

        # Test test file detection
        test_file = FileDiff(
            source_file="a/tests/test_main.py",
            target_file="b/tests/test_main.py",
            status="modified",
        )
        assert test_file.is_test_file()

        non_test = FileDiff(
            source_file="a/src/main.py",
            target_file="b/src/main.py",
            status="modified",
        )
        assert not non_test.is_test_file()

        # Test deleted file path
        deleted = FileDiff(
            source_file="src/old.py",
            target_file="b/dev/null",
            status="deleted",
        )
        assert deleted.file_path == "src/old.py"

        # Test classification
        assert test_file.classify_change() == "test"
        assert non_test.classify_change() == "feature"

        docs_file = FileDiff(
            source_file="a/docs/readme.md",
            target_file="b/docs/readme.md",
            status="modified",
        )
        assert docs_file.classify_change() == "docs"

    def test_hunk_properties(self) -> None:
        """Test Hunk computed properties."""
        hunk = Hunk(
            source_start=10,
            source_count=5,
            target_start=10,
            target_count=7,
            heading="def calculate_total",
            lines=[
                DiffLine(content="def calculate_total(items):", line_type="context", old_line_no=10, new_line_no=10),
                DiffLine(content='    """Calculate total."""', line_type="added", old_line_no=None, new_line_no=11),
                DiffLine(content="    return sum(items)", line_type="removed", old_line_no=11, new_line_no=None),
                DiffLine(content="    return sum(items) * 0.9", line_type="added", old_line_no=None, new_line_no=12),
            ],
        )

        assert hunk.changed_line_count == 3  # 2 added + 1 removed
        assert len(hunk.added_lines) == 2
        assert len(hunk.removed_lines) == 1

        start, end = hunk.line_span
        assert start == 10
        assert end == 12
