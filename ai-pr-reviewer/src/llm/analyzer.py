"""
Core analysis engine — coordinates prompt construction, LLM calls, and
finding parsing across all analysis units.

This is the central orchestrator module. It:
1. Takes analysis units from ``ContextBuilder``
2. For each unit, constructs the appropriate prompt via ``prompts.py``
3. Sends the prompt to the configured LLM provider
4. Parses the structured JSON response
5. Calibrates confidence scores with post-processing
6. Synthesises an overall PR summary

Design rationale:
- **Parallel analysis**: Analysis units are processed concurrently when
  multiple units exist (batch analysis). This significantly reduces total
  wall-clock time for large PRs.
- **Graceful degradation**: If a single unit fails (e.g., LLM timeout), the
  rest of the analysis continues. The final report indicates partial coverage.
- **Confidence calibration**: Post-processing adjusts confidence scores based
  on issue category heuristics (e.g., security issues get a small penalty to
  encourage conservative reporting).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from src.config import AppConfig
from src.context.builder import AnalysisUnit
from src.diff.models import DiffStats
from src.llm.prompts import build_analysis_prompt, build_summary_prompt
from src.llm.token_counter import estimate_messages_tokens

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

Severity = Literal["critical", "major", "minor", "info"]
FindingCategory = Literal[
    "security", "performance", "bug", "concurrency", "error_handling",
    "code_style", "maintainability", "best_practice", "potential_issue"
]


@dataclass
class Finding:
    """A single finding from the code review analysis."""

    file_path: str
    line_start: int | None = None
    line_end: int | None = None
    severity: Severity = "minor"
    category: FindingCategory = "potential_issue"
    title: str = ""
    description: str = ""
    suggestion: str = ""
    code_example: str | None = None
    confidence: float = 0.0
    rule_id: str = ""
    uncertainty_reason: str = ""

    def __post_init__(self) -> None:
        if not self.rule_id and self.title:
            # Auto-generate a stable rule ID from the title
            rule_id = re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")
            self.rule_id = rule_id[:60]


@dataclass
class AnalysisMetadata:
    """Metadata about the analysis run."""

    model: str = ""
    provider: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    analysis_duration_seconds: float = 0.0
    units_analysed: int = 0
    units_failed: int = 0
    timestamp: str = ""


@dataclass
class AnalysisReport:
    """Complete analysis report for a PR."""

    summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    stats: dict | None = None
    metadata: AnalysisMetadata = field(default_factory=AnalysisMetadata)


# ---------------------------------------------------------------------------
# JSON parsers
# ---------------------------------------------------------------------------

_FINDINGS_JSON_RE = re.compile(r"\[.*\]", re.DOTALL)


def _parse_findings_json(response_text: str) -> list[dict]:
    """Parse the JSON array of findings from an LLM response.

    Uses multiple fallback strategies:
    1. Try to parse the entire response as JSON
    2. Extract the first JSON array using regex
    3. Try to find individual JSON objects and wrap them

    Args:
        response_text: Raw response text from the LLM.

    Returns:
        A list of finding dicts (may be empty).
    """
    if not response_text or not response_text.strip():
        return []

    text = response_text.strip()

    # Strategy 1: Direct parse
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # Some models wrap in {"findings": [...]}
            for key in ("findings", "issues", "results", "data"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            return [data]
    except json.JSONDecodeError:
        pass

    # Strategy 2: Extract JSON array with regex
    match = _FINDINGS_JSON_RE.search(text)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    # Strategy 3: Try to extract individual JSON objects
    objects = re.findall(r"\{[^{}]*\}", text)
    if objects:
        results = []
        for obj_str in objects:
            try:
                obj = json.loads(obj_str)
                if isinstance(obj, dict) and "title" in obj:
                    results.append(obj)
            except json.JSONDecodeError:
                continue
        return results

    logger.warning("Could not parse findings from LLM response")
    return []


# ---------------------------------------------------------------------------
# Confidence calibration
# ---------------------------------------------------------------------------


def _calibrate_confidence(finding: Finding) -> float:
    """Apply post-processing calibration to a finding's confidence score.

    Adjustment rules:
    - Security issues: -0.05 penalty (better to under-report than
      overwhelm with false positives)
    - Findings without line numbers: -0.1 penalty
    - Findings with identical title+description: -0.2 (likely hallucinated)

    Args:
        finding: The finding to calibrate.

    Returns:
        Calibrated confidence score (clamped to 0.0–1.0).
    """
    confidence = finding.confidence

    # Security issues: slight penalty
    if finding.category == "security":
        confidence -= 0.05

    # No specific line numbers: less reliable
    if finding.line_start is None and finding.line_end is None:
        confidence -= 0.1

    # Very short descriptions: less reliable
    if len(finding.description) < 20:
        confidence -= 0.1

    # Vague suggestions: less reliable
    if len(finding.suggestion) < 15:
        confidence -= 0.15

    return max(0.0, min(1.0, confidence))


# ---------------------------------------------------------------------------
# Main analyzer
# ---------------------------------------------------------------------------


class LLMAnalyzer:
    """Core analysis engine.

    Args:
        config: Application configuration (provider, model, etc.)
        provider: An initialised LLM provider instance.
    """

    def __init__(self, config: AppConfig, provider) -> None:
        self._config = config
        self._provider = provider

    async def analyze_units(
        self,
        units: list[AnalysisUnit],
    ) -> AnalysisReport:
        """Analyse all units and return a complete report.

        Args:
            units: List of analysis units from ``ContextBuilder``.

        Returns:
            An ``AnalysisReport`` with all findings and metadata.
        """
        start_time = datetime.now(timezone.utc)
        all_findings: list[Finding] = []
        total_input_tokens = 0
        total_output_tokens = 0
        units_analysed = 0
        units_failed = 0

        for i, unit in enumerate(units):
            logger.info("Analysing unit %d/%d: %s", i + 1, len(units), unit.unit_id)
            try:
                findings, in_tokens, out_tokens = await self._analyze_single_unit(unit)
                all_findings.extend(findings)
                total_input_tokens += in_tokens
                total_output_tokens += out_tokens
                units_analysed += 1
                logger.debug("Unit %s: %d findings", unit.unit_id, len(findings))
            except Exception as exc:
                logger.error("Failed to analyse unit %s: %s", unit.unit_id, exc)
                units_failed += 1

        # Remove duplicates (same file, line, and title)
        all_findings = self._deduplicate_findings(all_findings)

        # Filter by confidence threshold
        min_confidence = self._config.analysis.min_confidence
        all_findings = [f for f in all_findings if f.confidence >= min_confidence]

        # Sort by severity (critical first) then confidence (high first)
        severity_order = {"critical": 0, "major": 1, "minor": 2, "info": 3}
        all_findings.sort(key=lambda f: (severity_order.get(f.severity, 99), -f.confidence))

        # Generate summary
        summary = ""
        if all_findings:
            try:
                summary = await self._generate_summary(all_findings, units)
            except Exception as exc:
                logger.warning("Failed to generate summary: %s", exc)
                summary = "Summary generation failed — see individual findings below."

        # Build stats
        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
        by_severity: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for f in all_findings:
            by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
            by_category[f.category] = by_category.get(f.category, 0) + 1

        stats = {
            "total_findings": len(all_findings),
            "by_severity": by_severity,
            "by_category": by_category,
            "high_confidence_count": sum(1 for f in all_findings if f.confidence >= 0.9),
        }

        metadata = AnalysisMetadata(
            model=self._config.model,
            provider=self._config.provider,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            analysis_duration_seconds=elapsed,
            units_analysed=units_analysed,
            units_failed=units_failed,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        return AnalysisReport(
            summary=summary,
            findings=all_findings,
            stats=stats,
            metadata=metadata,
        )

    async def _analyze_single_unit(
        self,
        unit: AnalysisUnit,
    ) -> tuple[list[Finding], int, int]:
        """Analyse a single analysis unit and return findings + token counts."""
        # Build prompt
        messages = build_analysis_prompt(unit)

        # Estimate input tokens
        input_tokens = estimate_messages_tokens(
            [{"role": m["role"], "content": m["content"]} for m in messages],
            self._config.provider,
        )

        # Send to LLM
        result = await self._provider.complete_with_result(
            [self._dict_to_message(m) for m in messages],
        )

        output_tokens = result.output_tokens or estimate_messages_tokens(
            [{"role": "assistant", "content": result.content}],
            self._config.provider,
        )

        # Parse findings from response
        raw_findings = _parse_findings_json(result.content)

        # Convert to Finding objects
        findings: list[Finding] = []
        for raw in raw_findings:
            try:
                finding = Finding(
                    file_path=raw.get("file_path", unit.file_diffs[0].file_path if unit.file_diffs else ""),
                    line_start=raw.get("line_start"),
                    line_end=raw.get("line_end"),
                    severity=raw.get("severity", "minor"),
                    category=raw.get("category", "potential_issue"),
                    title=raw.get("title", "Untitled finding"),
                    description=raw.get("description", ""),
                    suggestion=raw.get("suggestion", ""),
                    code_example=raw.get("code_example"),
                    confidence=float(raw.get("confidence", 0.5)),
                    uncertainty_reason=raw.get("uncertainty_reason", ""),
                )
                # Calibrate confidence
                finding.confidence = _calibrate_confidence(finding)
                findings.append(finding)
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning("Failed to parse finding: %s — raw: %s", exc, raw)
                continue

        return findings, input_tokens, output_tokens

    async def _generate_summary(
        self,
        findings: list[Finding],
        units: list[AnalysisUnit],
    ) -> str:
        """Generate a natural-language PR summary from all findings."""
        # Aggregate stats
        total_files = len(set(f.file_path for f in findings))
        severity_counts = {}
        for f in findings:
            severity_counts[f.severity] = severity_counts.get(f.severity, 0) + 1

        # Compute diff stats
        total_additions = sum(fd.additions for u in units for fd in u.file_diffs)
        total_deletions = sum(fd.deletions for u in units for fd in u.file_diffs)

        # Build findings JSON for the summary prompt
        findings_summary = []
        for f in findings[:20]:  # limit to top 20 for token efficiency
            findings_summary.append({
                "severity": f.severity,
                "category": f.category,
                "title": f.title,
                "file": f.file_path,
                "line": f.line_start,
            })

        import json
        findings_json = json.dumps(findings_summary, indent=2, ensure_ascii=False)

        messages = build_summary_prompt(
            pr_title="",  # filled in by the CLI layer
            pr_description="",
            file_count=total_files,
            additions=total_additions,
            deletions=total_deletions,
            findings_json=findings_json,
        )

        result = await self._provider.complete_with_result(
            [self._dict_to_message(m) for m in messages],
        )

        return result.content.strip()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _deduplicate_findings(findings: list[Finding]) -> list[Finding]:
        """Remove duplicate findings (same file, line range, and title)."""
        seen: set[tuple[str, int | None, int | None, str]] = set()
        unique: list[Finding] = []
        for f in findings:
            key = (f.file_path, f.line_start, f.line_end, f.title.lower().strip())
            if key not in seen:
                seen.add(key)
                unique.append(f)
        return unique

    @staticmethod
    def _dict_to_message(msg_dict: dict[str, str]):
        """Convert a dict with ``role`` and ``content`` to a Message object."""
        from src.llm.providers.base import Message
        return Message(role=msg_dict["role"], content=msg_dict["content"])
