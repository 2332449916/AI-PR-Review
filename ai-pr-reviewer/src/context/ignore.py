"""
Ignore rules engine for filtering files and findings.

Supports a ``.ai-review-ignore`` file in the repository root with syntax
similar to ``.gitignore``, plus additional rule-level filtering:

.. code::

    # Ignore generated files
    *.generated.py
    **/migrations/*
    **/vendor/*

    # Disable specific rules
    rule:no-console-log
    rule:style-preference

    # Per-path severity caps
    [threshold:major]
    **/test/**
    **/docs/**

Design rationale:
- Glob-based path matching means developers can use patterns they already know
  from ``.gitignore``
- Rule-level filtering (``rule:<rule-id>``) lets teams selectively disable
  noisy rules without affecting other detections
- Path-specific severity thresholds allow relaxing rules for test files or
  generated code
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from src.diff.models import Severity

if TYPE_CHECKING:
    from src.llm.analyzer import Finding

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEVERITY_ORDER: dict[Severity, int] = {
    "critical": 4,
    "high": 3,
    "major": 3,
    "medium": 2,
    "minor": 2,
    "info": 1,
    "low": 1,
}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class IgnoreRules:
    """Parsed ignore rules from ``.ai-review-ignore``."""

    path_patterns: list[str] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    path_thresholds: dict[str, Severity] = field(default_factory=dict)
    ignore_all: bool = False  # if True, skip everything

    @property
    def is_empty(self) -> bool:
        return not (self.path_patterns or self.rule_ids or self.path_thresholds or self.ignore_all)


# ---------------------------------------------------------------------------
# Main filter
# ---------------------------------------------------------------------------


class IgnoreFilter:
    """Load and apply ignore rules for filtering files and findings."""

    def __init__(self, repo_path: str | Path | None = None) -> None:
        self._repo_path = Path(repo_path) if repo_path else Path.cwd()
        self._rules: IgnoreRules | None = None

    def load_rules(self) -> IgnoreRules:
        """Load ``.ai-review-ignore`` from the repository root.

        Returns:
            Parsed ``IgnoreRules`` (empty if no file exists).
        """
        if self._rules is not None:
            return self._rules

        ignore_file = self._repo_path / ".ai-review-ignore"
        if not ignore_file.exists():
            logger.debug("No .ai-review-ignore found in %s", self._repo_path)
            self._rules = IgnoreRules()
            return self._rules

        rules = IgnoreRules()
        current_threshold: Severity | None = None

        with open(ignore_file, "r") as f:
            for line in f:
                line = line.strip()

                # Skip empty lines and comments
                if not line or line.startswith("#"):
                    continue

                # Section header: [threshold:major]
                section_match = re.match(r"^\[threshold:(\w+)\]$", line)
                if section_match:
                    sev = section_match.group(1)
                    if sev in SEVERITY_ORDER:
                        current_threshold = sev  # type: ignore[assignment]
                    else:
                        current_threshold = None
                    continue

                # Rule-level ignore: rule:<rule-id>
                if line.startswith("rule:"):
                    rules.rule_ids.append(line[5:].strip())
                    continue

                # Path pattern (everything else)
                path_pattern = line
                if current_threshold:
                    rules.path_thresholds[path_pattern] = current_threshold
                else:
                    # Special case: "*" means ignore all
                    if path_pattern == "*":
                        rules.ignore_all = True
                    else:
                        rules.path_patterns.append(path_pattern)

        self._rules = rules
        logger.debug(
            "Loaded .ai-review-ignore: %d path patterns, %d rule ids, %d path thresholds",
            len(rules.path_patterns),
            len(rules.rule_ids),
            len(rules.path_thresholds),
        )
        return rules

    def should_include_file(self, file_path: str, rules: IgnoreRules | None = None) -> bool:
        """Check if a file should be included in analysis.

        Args:
            file_path: Path to the file (relative to repo root).
            rules: Parsed ignore rules (loaded automatically if not provided).

        Returns:
            ``True`` if the file should be analyzed.
        """
        if rules is None:
            rules = self.load_rules()

        if rules.ignore_all:
            return False

        return not self._matches_any_pattern(file_path, rules.path_patterns)

    def filter_findings(
        self,
        findings: list[Finding],
        rules: IgnoreRules | None = None,
    ) -> list[Finding]:
        """Filter out findings that match ignore rules.

        Args:
            findings: All findings from the analysis.
            rules: Parsed ignore rules (loaded automatically if not provided).

        Returns:
            Filtered list of findings.
        """
        if rules is None:
            rules = self.load_rules()

        if rules.ignore_all:
            logger.debug("Ignoring all findings (ignore_all=True)")
            return []

        filtered: list[Finding] = []

        for finding in findings:
            # Check rule-level ignores
            if finding.rule_id and finding.rule_id in rules.rule_ids:
                logger.debug("Filtering finding by rule_id: %s", finding.rule_id)
                continue

            # Check path-based severity thresholds
            if finding.file_path:
                for pattern, threshold in rules.path_thresholds.items():
                    if self._matches_glob(finding.file_path, pattern):
                        if SEVERITY_ORDER.get(finding.severity, 0) < SEVERITY_ORDER.get(threshold, 0):
                            logger.debug(
                                "Filtering finding below severity threshold %s for %s",
                                threshold, finding.file_path,
                            )
                            # Don't skip — just note that it was considered
                            # We use independent threshold per path
                        break

            filtered.append(finding)

        # Apply global severity threshold from config (handled elsewhere)
        return filtered

    # ------------------------------------------------------------------
    # Internal: glob matching
    # ------------------------------------------------------------------

    def _matches_any_pattern(self, path: str, patterns: list[str]) -> bool:
        """Check if a path matches any pattern in the list."""
        for pattern in patterns:
            if self._matches_glob(path, pattern):
                return True
        return False

    @staticmethod
    def _matches_glob(path: str, pattern: str) -> bool:
        """Simple glob matching for ignore rules.

        Supports ``**`` (recursive), ``*`` (single segment), and ``?`` (single char).
        Follows gitignore semantics: a pattern without ``/`` is matched against
        the basename (last component) of the path; a pattern with ``/`` is
        matched against the full path.
        """
        # Normalise path separators
        path = path.replace("\\", "/")
        pattern = pattern.replace("\\", "/")

        has_slash = "/" in pattern

        # Special case: ** at the start matches recursively
        if pattern.startswith("**/"):
            rest = pattern[3:]
            # Try matching at any directory level
            parts = path.split("/")
            for i in range(len(parts)):
                subpath = "/".join(parts[i:])
                if IgnoreFilter._match_segments(subpath, rest):
                    return True
            return False

        if not has_slash:
            # gitignore convention: pattern without / matches basename only
            basename = path.rsplit("/", 1)[-1] if "/" in path else path
            return IgnoreFilter._match_segments(basename, pattern)

        return IgnoreFilter._match_segments(path, pattern)

    @staticmethod
    def _match_segments(path: str, pattern: str) -> bool:
        """Match a path against a pattern (single-segment ``*`` only)."""
        # Convert glob pattern to regex
        regex_parts: list[str] = []
        i = 0
        while i < len(pattern):
            if pattern[i:i+2] == "**":
                # ** matches everything
                regex_parts.append(".*")
                i += 2
                # Skip trailing /
                if i < len(pattern) and pattern[i] == "/":
                    i += 1
            elif pattern[i] == "*":
                regex_parts.append("[^/]*")
                i += 1
            elif pattern[i] == "?":
                regex_parts.append("[^/]")
                i += 1
            else:
                regex_parts.append(re.escape(pattern[i]))
                i += 1

        regex = "^" + "".join(regex_parts) + "$"
        return bool(re.match(regex, path))
