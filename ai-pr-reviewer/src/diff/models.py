"""
Data models for parsed diff output.

These models represent the structured output of diff parsing, independent of the
underlying parsing library (unidiff). This abstraction allows swapping the parser
without affecting upstream consumers.

Design rationale:
- Line types are a Literal union, not an enum, so serialization is trivial (JSON rounds trip)
- Hunk and FileDiff carry enough metadata for the context builder to make
  token-budget decisions without re-parsing
- Nullable line numbers handle binary files and adds/deletes correctly
  (a deleted file has no new_line_no; an added file has no old_line_no)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

LineType = Literal["added", "removed", "context"]
ChangeType = Literal["added", "deleted", "modified", "renamed", "copied"]
ChangeClass = Literal[
    "feature", "bugfix", "refactor", "test", "docs", "config", "style", "other"
]
Severity = Literal["critical", "major", "minor", "info"]
FindingCategory = Literal[
    "security", "performance", "bug", "concurrency", "error_handling",
    "code_style", "maintainability", "best_practice", "potential_issue"
]


# ---------------------------------------------------------------------------
# Diff models
# ---------------------------------------------------------------------------


@dataclass
class DiffLine:
    """A single line within a diff hunk."""

    content: str
    line_type: LineType
    old_line_no: int | None = None
    new_line_no: int | None = None
    raw_line: str = ""  # original line including leading +/-/space

    @property
    def is_added(self) -> bool:
        return self.line_type == "added"

    @property
    def is_removed(self) -> bool:
        return self.line_type == "removed"

    @property
    def is_context(self) -> bool:
        return self.line_type == "context"

    @property
    def stripped_content(self) -> str:
        """Content without leading '+', '-', or ' ' diff marker."""
        return self.content


@dataclass
class Hunk:
    """A contiguous block of changes within a file."""

    source_start: int
    source_count: int
    target_start: int
    target_count: int
    heading: str
    lines: list[DiffLine] = field(default_factory=list)

    @property
    def added_lines(self) -> list[DiffLine]:
        return [l for l in self.lines if l.is_added]

    @property
    def removed_lines(self) -> list[DiffLine]:
        return [l for l in self.lines if l.is_removed]

    @property
    def changed_line_count(self) -> int:
        """Count of lines that are added or removed (not context)."""
        return len(self.added_lines) + len(self.removed_lines)

    @property
    def line_span(self) -> tuple[int, int]:
        """Return (start_line, end_line) in the *new* file for this hunk."""
        if not self.lines:
            return (self.target_start, self.target_start + self.target_count)
        # Find the min and max new_line_no among all lines
        new_nos = [l.new_line_no for l in self.lines if l.new_line_no is not None]
        if not new_nos:
            return (self.target_start, self.target_start + self.target_count)
        return (min(new_nos), max(new_nos))


@dataclass
class FileDiff:
    """Diff information for a single file."""

    source_file: str
    target_file: str
    status: ChangeType
    hunks: list[Hunk] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    similarity: float | None = None  # for rename/copy detection
    is_binary: bool = False
    encoding: str | None = None

    @property
    def file_path(self) -> str:
        """Return the target file path (or source if deleted)."""
        if self.status == "deleted":
            return self.source_file
        return self.target_file

    @property
    def extension(self) -> str:
        """Return the file extension without leading dot."""
        path = self.file_path
        if "." in path:
            return path.rsplit(".", 1)[-1]
        return ""

    @property
    def total_changes(self) -> int:
        return self.additions + self.deletions

    def is_test_file(self) -> bool:
        """Heuristic: detect test files by path convention."""
        path = self.file_path.lower()
        return any(
            marker in path
            for marker in ("test_", "_test", "/test/", "/tests/", "spec_", "_spec")
        )

    def classify_change(self) -> ChangeClass:
        """Heuristic classification of the change type based on file metadata.

        This is a best-effort classification used to select the appropriate
        analysis prompt template. It is not perfectly accurate but good enough
        for prompt routing.
        """
        path = self.file_path.lower()
        if self.is_test_file():
            return "test"
        if any(ext in path for ext in (".md", ".rst", ".txt", "/docs/")):
            return "docs"
        if any(ext in path for ext in (".toml", ".yaml", ".yml", ".json", ".cfg", ".ini")):
            return "config"
        if any(marker in path for marker in (".css", ".scss", ".less")):
            return "style"
        return "feature"  # default — refined during LLM analysis


@dataclass
class DiffStats:
    """Aggregate statistics for a complete PR diff."""

    total_files: int = 0
    total_additions: int = 0
    total_deletions: int = 0

    @property
    def total_changed_lines(self) -> int:
        return self.total_additions + self.total_deletions
