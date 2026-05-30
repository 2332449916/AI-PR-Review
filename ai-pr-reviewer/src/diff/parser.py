"""
Unified diff parser.

Wraps the ``unidiff`` library and converts its output into our own domain models
(``FileDiff``, ``Hunk``, ``DiffLine``). This indirection lets us:
- Normalise the output shape across different diff format versions
- Attach extra metadata (change classification, test-file heuristics)
- Easily swap the parser backend if needed

Design rationale:
- ``unidiff`` is preferred over ``diff-parser`` because it handles GitHub's flavour of
  unified diff out of the box (renamed/copied file markers, binary files, etc.)
- The parser is stateless — all instances are interchangeable — so it can be
  safely used as a module-level singleton
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

import unidiff

from src.diff.models import ChangeType, DiffLine, DiffStats, FileDiff, Hunk

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants: unidiff 0.7.5 uses '+' / '-' / ' ' as line_type values
# ---------------------------------------------------------------------------

_LINE_TYPE_MAP: dict[str, str] = {
    unidiff.LINE_TYPE_ADDED: "added",
    unidiff.LINE_TYPE_REMOVED: "removed",
    unidiff.LINE_TYPE_CONTEXT: "context",
}


class DiffParser:
    """Parse raw unified diff strings into structured ``FileDiff`` objects."""

    def parse(self, raw_diff: str) -> list[FileDiff]:
        """Parse a complete raw Git diff into a list of per-file diffs.

        Args:
            raw_diff: The raw diff string as returned by GitHub's diff endpoint.

        Returns:
            A list of ``FileDiff`` objects, one per changed file.

        Raises:
            ValueError: If the diff is empty or unparseable.
        """
        if not raw_diff or not raw_diff.strip():
            logger.warning("Empty diff provided to parser")
            return []

        try:
            patch_set = unidiff.PatchSet(raw_diff)
        except Exception as exc:
            logger.error("Failed to parse diff: %s", exc)
            raise ValueError(f"Could not parse diff: {exc}") from exc

        files: list[FileDiff] = []
        for patched_file in patch_set:
            try:
                file_diff = self._convert_patched_file(patched_file)
                files.append(file_diff)
            except Exception as exc:
                logger.warning("Skipping file in diff due to parse error: %s", exc)
                continue

        logger.debug("Parsed %d files from diff", len(files))
        return files

    def parse_single_file(self, raw_diff: str, file_path: str) -> FileDiff | None:
        """Extract the diff for a specific file from a multi-file diff.

        Useful when you already know which file you care about and want to
        avoid processing the entire diff.

        Args:
            raw_diff: The raw multi-file diff string.
            file_path: The path of the file to extract (matched against both
                       source and target file paths).

        Returns:
            A ``FileDiff`` for the matching file, or ``None`` if not found.
        """
        files = self.parse(raw_diff)
        for f in files:
            if file_path in (f.source_file, f.target_file):
                return f
        return None

    def get_changed_lines(self, file_diff: FileDiff) -> set[tuple[str, int]]:
        """Get the set of ``(file_path, new_line_number)`` for added/modified lines.

        This is the primary interface for "what lines were actually changed",
        used by the incremental analysis filter.
        """
        changed: set[tuple[str, int]] = set()
        for hunk in file_diff.hunks:
            for line in hunk.lines:
                if line.line_type in ("added", "removed") and line.new_line_no is not None:
                    changed.add((file_diff.file_path, line.new_line_no))
        return changed

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _convert_patched_file(self, patched_file: unidiff.PatchedFile) -> FileDiff:
        """Convert a unidiff ``PatchedFile`` into our ``FileDiff`` model."""
        source_file = self._normalise_path(patched_file.source_file)
        target_file = self._normalise_path(patched_file.target_file)

        if source_file == "/dev/null":
            status: ChangeType = "added"
        elif target_file == "/dev/null":
            status = "deleted"
        elif source_file != target_file:
            status = "renamed"
        else:
            status = "modified"

        hunks: list[Hunk] = []
        for uhunk in patched_file:
            hunk = self._convert_hunk(uhunk)
            hunks.append(hunk)

        return FileDiff(
            source_file=source_file,
            target_file=target_file,
            status=status,
            hunks=hunks,
            additions=patched_file.added,
            deletions=patched_file.removed,
            # unidiff 0.7.5 may not have similarity/is_binary/encoding attrs
        )

    def _convert_hunk(self, uhunk: unidiff.Hunk) -> Hunk:
        """Convert a unidiff ``Hunk`` into our ``Hunk`` model."""
        lines: list[DiffLine] = []
        for uline in uhunk:
            line_type = _LINE_TYPE_MAP.get(uline.line_type, "context")
            # Strip trailing newline from value for cleaner display
            content = uline.value.rstrip("\n") if uline.value else ""
            lines.append(
                DiffLine(
                    content=content,
                    line_type=line_type,
                    old_line_no=uline.source_line_no,
                    new_line_no=uline.target_line_no,
                    raw_line=uline.value if uline.value else "",
                )
            )

        return Hunk(
            source_start=uhunk.source_start,
            source_count=uhunk.source_length,
            target_start=uhunk.target_start,
            target_count=uhunk.target_length,
            heading="",  # unidiff 0.7.5 Hunk has no section_heading
            lines=lines,
        )

    @staticmethod
    def _normalise_path(path: str) -> str:
        """Strip the ``a/`` or ``b/`` prefix that Git adds to diff paths."""
        if path.startswith("a/") or path.startswith("b/"):
            path = path[2:]
        # Normalise to POSIX separators (Git always uses /)
        return str(PurePosixPath(path))


# Module-level singleton for convenience
parser = DiffParser()
