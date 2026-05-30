"""
Context builder — assembles optimised context units for LLM analysis.

This is the central orchestration module for context gathering. It:
1. Takes parsed file diffs from the ``DiffParser``
2. Fetches original/changed file content from the git ref (via ``GitHubFetcher``)
3. Runs AST analysis on changed files (via ``ASTWalker``)
4. Assembles ``AnalysisUnit`` objects, each fitting within a token budget
5. Applies ignore rules to skip unwanted files

Design rationale:
- Token budget is enforced at the unit level: each ``AnalysisUnit`` is a
  self-contained prompt that fits within the model's context window
- File splitting: if a single file's diff + context exceeds the budget, it is
  split at hunk boundaries
- Related files are kept together in the same unit when possible (cross-file
  context is more valuable than arbitrary chunking)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from src.context.ast_walker import ASTWalker, CodeContext, SymbolDefinition
from src.context.ignore import IgnoreFilter, IgnoreRules
from src.diff.models import FileDiff

if TYPE_CHECKING:
    from src.github_client.fetcher import GitHubFetcher

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Rough estimate: 1 token ≈ 4 characters for English text + code
CHARS_PER_TOKEN = 4

# Max lines of surrounding context to include from unchanged code
DEFAULT_CONTEXT_LINES = 5

# Head ref / base ref prefixes
BASE_REF = "refs/heads/"  # will be appended to base branch
HEAD_REF = "refs/heads/"  # will be appended to head branch


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class AnalysisUnit:
    """A self-contained analysis unit that fits within the token budget.

    Each unit contains the diffs for one or more files, plus relevant AST
    context, formatted as a prompt segment ready for the LLM.
    """

    file_diffs: list[FileDiff]
    files_content_before: dict[str, str] = field(default_factory=dict)
    files_content_after: dict[str, str] = field(default_factory=dict)
    code_contexts: dict[str, CodeContext] = field(default_factory=dict)
    estimated_tokens: int = 0
    unit_id: str = ""

    @property
    def changed_files(self) -> list[str]:
        return [f.file_path for f in self.file_diffs]


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------


class ContextBuilder:
    """Build token-budgeted analysis units from file diffs.

    Args:
        repo_full_name: Full GitHub repository name (``owner/repo``).
        fetcher: ``GitHubFetcher`` instance for fetching file contents.
        ast_walker: ``ASTWalker`` instance (created automatically if not provided).
        ignore_filter: ``IgnoreFilter`` instance (created automatically if not provided).
        max_tokens_per_unit: Token budget per analysis unit.
        context_lines: Lines of surrounding context to include per hunk.
    """

    def __init__(
        self,
        repo_full_name: str,
        fetcher: GitHubFetcher,
        ast_walker: ASTWalker | None = None,
        ignore_filter: IgnoreFilter | None = None,
        max_tokens_per_unit: int = 6000,
        context_lines: int = DEFAULT_CONTEXT_LINES,
    ) -> None:
        self._repo = repo_full_name
        self._fetcher = fetcher
        self._ast_walker = ast_walker or ASTWalker()
        self._ignore_filter = ignore_filter or IgnoreFilter()
        self._max_tokens = max_tokens_per_unit
        self._context_lines = context_lines

    async def build_analysis_units(
        self,
        file_diffs: list[FileDiff],
        head_ref: str,
        base_ref: str | None = None,
        ignore_rules: IgnoreRules | None = None,
    ) -> list[AnalysisUnit]:
        """Split file diffs into token-budgeted analysis units.

        Args:
            file_diffs: Parsed file diffs from ``DiffParser``.
            head_ref: The Git ref (branch or SHA) for the PR head.
            base_ref: The Git ref for the base branch (optional).
            ignore_rules: Pre-loaded ignore rules (loaded automatically if not provided).

        Returns:
            A list of ``AnalysisUnit`` objects ready for LLM analysis.
        """
        if ignore_rules is None:
            ignore_rules = self._ignore_filter.load_rules()

        # Filter files through ignore rules
        filtered_diffs = [
            fd for fd in file_diffs
            if self._ignore_filter.should_include_file(fd.file_path, ignore_rules)
        ]

        if not filtered_diffs:
            logger.info("All files filtered out by ignore rules")
            return []

        # Sort: smaller files first (better packing)
        filtered_diffs.sort(key=lambda fd: fd.total_changes)

        # Build units
        units: list[AnalysisUnit] = []
        current_unit_files: list[FileDiff] = []
        current_unit_tokens = 0

        for file_diff in filtered_diffs:
            file_tokens = self._estimate_file_tokens(file_diff)

            # If a single file exceeds the budget, split it
            if file_tokens > self._max_tokens:
                # Flush current unit first
                if current_unit_files:
                    unit = await self._assemble_unit(current_unit_files, head_ref, base_ref)
                    units.append(unit)
                    current_unit_files = []
                    current_unit_tokens = 0

                # Split the oversized file
                split_units = await self._split_oversized_file(file_diff, head_ref, base_ref)
                units.extend(split_units)
                continue

            # Check if this file fits in the current unit
            if current_unit_tokens + file_tokens > self._max_tokens:
                # Flush and start a new unit
                unit = await self._assemble_unit(current_unit_files, head_ref, base_ref)
                units.append(unit)
                current_unit_files = []
                current_unit_tokens = 0

            current_unit_files.append(file_diff)
            current_unit_tokens += file_tokens

        # Final unit
        if current_unit_files:
            unit = await self._assemble_unit(current_unit_files, head_ref, base_ref)
            units.append(unit)

        logger.info("Built %d analysis units from %d files", len(units), len(filtered_diffs))
        return units

    async def _assemble_unit(
        self,
        file_diffs: list[FileDiff],
        head_ref: str,
        base_ref: str | None,
    ) -> AnalysisUnit:
        """Fetch content and build context for a group of file diffs."""
        files_before: dict[str, str] = {}
        files_after: dict[str, str] = {}
        code_contexts: dict[str, CodeContext] = {}

        for fd in file_diffs:
            file_path = fd.file_path

            # Fetch file after changes (head ref)
            if fd.status != "deleted":
                content_after = self._fetcher.fetch_file_content(
                    self._repo, file_path, head_ref
                )
                if content_after is not None:
                    files_after[file_path] = content_after

            # Fetch file before changes (base ref)
            if fd.status != "added" and base_ref:
                content_before = self._fetcher.fetch_file_content(
                    self._repo, file_path, base_ref
                )
                if content_before is not None:
                    files_before[file_path] = content_before

            # AST analysis on the new file content
            content_to_analyze = files_after.get(file_path) or files_before.get(file_path)
            if content_to_analyze:
                try:
                    context = self._ast_walker.extract_definitions(file_path, content_to_analyze)
                    code_contexts[file_path] = CodeContext(symbols=context)
                except Exception as exc:
                    logger.warning("AST analysis failed for %s: %s", file_path, exc)
                    code_contexts[file_path] = CodeContext()

        # Estimate tokens
        estimated = self._estimate_unit_tokens(file_diffs, files_before, files_after, code_contexts)
        unit_id = f"{file_diffs[0].file_path}~+{len(file_diffs)}" if len(file_diffs) == 1 else f"{len(file_diffs)}files"

        return AnalysisUnit(
            file_diffs=file_diffs,
            files_content_before=files_before,
            files_content_after=files_after,
            code_contexts=code_contexts,
            estimated_tokens=estimated,
            unit_id=unit_id,
        )

    async def _split_oversized_file(
        self,
        file_diff: FileDiff,
        head_ref: str,
        base_ref: str | None,
    ) -> list[AnalysisUnit]:
        """Split a single oversized file into multiple analysis units at hunk boundaries."""
        # Fetch content
        file_path = file_diff.file_path
        content_after = None
        content_before = None

        if file_diff.status != "deleted":
            content_after = self._fetcher.fetch_file_content(self._repo, file_path, head_ref)
        if file_diff.status != "added" and base_ref:
            content_before = self._fetcher.fetch_file_content(self._repo, file_path, base_ref)

        # Group hunks into budget-sized chunks
        units: list[AnalysisUnit] = []
        current_hunks: list = []
        current_tokens = 0

        # Estimate the overhead tokens for a unit (metadata, instructions)
        unit_overhead = 500

        for hunk in file_diff.hunks:
            hunk_tokens = self._estimate_hunk_tokens(hunk)

            if current_tokens + hunk_tokens > (self._max_tokens - unit_overhead) and current_hunks:
                # Create a unit from current hunks
                partial_diff = self._make_partial_diff(file_diff, current_hunks)
                unit = AnalysisUnit(
                    file_diffs=[partial_diff],
                    files_content_before={file_path: content_before} if content_before else {},
                    files_content_after={file_path: content_after} if content_after else {},
                    estimated_tokens=current_tokens + unit_overhead,
                    unit_id=f"{file_path}~hunk-{current_hunks[0].source_start}",
                )
                units.append(unit)
                current_hunks = []
                current_tokens = 0

            current_hunks.append(hunk)
            current_tokens += hunk_tokens

        # Final partial unit
        if current_hunks:
            partial_diff = self._make_partial_diff(file_diff, current_hunks)
            unit = AnalysisUnit(
                file_diffs=[partial_diff],
                files_content_before={file_path: content_before} if content_before else {},
                files_content_after={file_path: content_after} if content_after else {},
                estimated_tokens=current_tokens + unit_overhead,
                unit_id=f"{file_path}~hunk-{current_hunks[0].source_start}",
            )
            units.append(unit)

        return units

    # ------------------------------------------------------------------
    # Token estimation
    # ------------------------------------------------------------------

    def _estimate_file_tokens(self, file_diff: FileDiff) -> int:
        """Estimate the token cost for analysing a single file diff."""
        tokens = 0
        # File header: ~50 tokens
        tokens += 50
        for hunk in file_diff.hunks:
            tokens += self._estimate_hunk_tokens(hunk)
        # Context overhead: ~30% of diff tokens
        tokens = int(tokens * 1.3)
        return tokens

    @staticmethod
    def _estimate_hunk_tokens(hunk) -> int:
        """Estimate token cost for a single hunk."""
        tokens = 0
        for line in hunk.lines:
            # Each line costs roughly 1 token per 4 characters
            tokens += max(1, len(line.content) // CHARS_PER_TOKEN)
            # Special marker for +/- adds a small overhead
            tokens += 1
        return tokens

    def _estimate_unit_tokens(
        self,
        file_diffs: list[FileDiff],
        files_before: dict[str, str],
        files_after: dict[str, str],
        code_contexts: dict[str, CodeContext],
    ) -> int:
        """Estimate total tokens for a complete analysis unit."""
        total = 0
        # System prompt overhead: ~400 tokens
        total += 400
        # Diff tokens
        for fd in file_diffs:
            total += self._estimate_file_tokens(fd)
        # File content tokens
        for content in files_before.values():
            total += len(content) // CHARS_PER_TOKEN
        for content in files_after.values():
            total += len(content) // CHARS_PER_TOKEN
        # Context overhead per file
        for ctx in code_contexts.values():
            total += len(ctx.symbols) * 15  # ~15 tokens per symbol
        return total

    @staticmethod
    def _make_partial_diff(file_diff: FileDiff, hunks) -> FileDiff:
        """Create a new FileDiff containing only the specified hunks."""
        import copy
        new_diff = copy.copy(file_diff)
        new_diff.hunks = hunks
        new_diff.additions = sum(h.changed_line_count for h in hunks)
        new_diff.deletions = sum(len(h.removed_lines) for h in hunks)
        return new_diff
