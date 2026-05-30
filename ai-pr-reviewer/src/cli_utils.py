"""
CLI utilities — progress bars, retry handlers, and console helpers.

Provides a consistent user experience across all CLI operations:
- Animated progress bars for multi-step analysis
- Colored output for severity levels
- Retry-with-backoff for API calls
- Structured logging that integrates with Rich
"""

from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import contextmanager
from typing import Any, Callable, Generator, TypeVar

from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# ---------------------------------------------------------------------------
# Encoding detection — fall back to ASCII emoji on GBK/Windows terminals
# ---------------------------------------------------------------------------

_USE_ASCII = False
try:
    enc = sys.stdout.encoding.lower() if sys.stdout.encoding else ""
    if "gbk" in enc or "latin" in enc:
        _USE_ASCII = True
except Exception:
    pass
# Allow override via environment variable
if os.environ.get("AI_REVIEWER_ASCII"):
    _USE_ASCII = True


def _emoji(utf8: str, ascii_fallback: str) -> str:
    """Return emoji or ASCII fallback depending on terminal encoding."""
    return ascii_fallback if _USE_ASCII else utf8

from src.diff.models import Severity

# ---------------------------------------------------------------------------
# Rich console
# ---------------------------------------------------------------------------

console = Console(highlight=False)
error_console = Console(stderr=True, style="bold red")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEVERITY_STYLES: dict[str, str] = {
    "critical": "bold red",
    "major": "bold yellow",
    "minor": "yellow",
    "info": "blue",
}

SEVERITY_LABELS: dict[str, str] = {
    "critical": _emoji("🔴", "[X]") + " CRITICAL",
    "major": _emoji("🟠", "[!]") + " MAJOR",
    "minor": _emoji("🟡", "[-]") + " MINOR",
    "info": _emoji("🔵", "[i]") + " INFO",
}


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def setup_rich_logging(verbose: bool = False) -> None:
    """Configure logging with Rich handler for prettier output."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )
    # Quiet down noisy libraries
    for noisy in ("httpx", "github", "urllib3", "anthropic", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------

T = TypeVar("T")


def api_retry(
    attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 30.0,
) -> Callable:
    """Decorator for API calls with exponential backoff retry.

    Args:
        attempts: Maximum number of retry attempts.
        min_wait: Minimum wait between retries (seconds).
        max_wait: Maximum wait between retries (seconds).
    """
    return retry(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        retry=retry_if_exception_type(
            (ConnectionError, TimeoutError, IOError)
        ),
        reraise=True,
    )


def retry_with_console(
    func: Callable[..., T],
    *args: Any,
    label: str = "Operation",
    **kwargs: Any,
) -> T:
    """Execute a function with retry and console feedback.

    Args:
        func: The function to execute.
        label: Human-readable label for the operation.
        *args: Positional arguments for the function.
        **kwargs: Keyword arguments for the function.

    Returns:
        The function's return value.

    Raises:
        The last exception if all retries are exhausted.
    """
    last_exc = None
    for attempt in range(3):
        try:
            return func(*args, **kwargs)
        except (ConnectionError, TimeoutError, IOError) as exc:
            last_exc = exc
            if attempt < 2:
                wait = 2 ** attempt
                console.print(
                    f"  {_emoji('⚠️', '[!]')} {label} failed (attempt {attempt + 1}/3): {exc}",
                    style="yellow",
                )
                console.print(f"  {_emoji('⏳', '[_]')} Retrying in {wait}s...")
                time.sleep(wait)
            else:
                console.print(
                    f"  {_emoji('❌', '[x]')} {label} failed after 3 attempts: {exc}",
                    style="bold red",
                )
                raise
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Progress manager
# ---------------------------------------------------------------------------


class ProgressManager:
    """Manages a Rich progress display for multi-step operations.

    Usage::

        with ProgressManager() as pm:
            pm.add_task("Fetching PR data")
            # ... do work ...
            pm.advance("Fetching PR data")
    """

    def __init__(self) -> None:
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        )
        self._tasks: dict[str, TaskID] = {}

    def __enter__(self) -> ProgressManager:
        self._progress.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self._progress.stop()

    def add_task(self, description: str, total: float = 100.0) -> TaskID:
        """Add a new task to the progress display."""
        task_id = self._progress.add_task(description, total=total)
        self._tasks[description] = task_id
        return task_id

    def advance(self, description: str, advance: float = 100.0) -> None:
        """Mark a task as complete by advancing to 100%."""
        task_id = self._tasks.get(description)
        if task_id is not None:
            self._progress.update(task_id, advance=advance)

    def update(self, description: str, completed: float) -> None:
        """Update a task's progress."""
        task_id = self._tasks.get(description)
        if task_id is not None:
            self._progress.update(task_id, completed=completed)

    def remove_task(self, description: str) -> None:
        """Remove a task from the display."""
        task_id = self._tasks.pop(description, None)
        if task_id is not None:
            self._progress.remove_task(task_id)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def print_header(text: str) -> None:
    """Print a section header."""
    console.print()
    console.print(Panel(text, style="bold cyan", padding=(0, 1)))
    console.print()


def print_finding_count(count: int, duration: float) -> None:
    """Print a summary of analysis results."""
    style = "bold green" if count == 0 else "bold yellow"
    chart = _emoji("\U0001f4ca", "[ ]")
    console.print(
        f"\n{chart} Analysis complete: [bold]{count}[/] findings in {duration:.1f}s",
        style=style,
    )


def print_error(message: str, hint: str | None = None) -> None:
    """Print an error message with optional fix hint."""
    x = _emoji("❌", "[x]")
    error_console.print(f"{x} {message}")
    if hint:
        bulb = _emoji("\U0001f4a1", "[?]")
        console.print(f"{bulb} [bold]Hint:[/] {hint}", style="blue")


def print_warning(message: str) -> None:
    """Print a warning message."""
    warn = _emoji("⚠️", "[!]")
    console.print(f"{warn} {message}", style="yellow")


def print_success(message: str) -> None:
    """Print a success message."""
    console.print(f"✅ {message}", style="bold green")


def print_step(message: str) -> None:
    """Print a step indicator."""
    console.print(f"  • {message}")


def print_findings_summary(
    by_severity: dict[str, int],
    by_category: dict[str, int],
    total: int,
) -> None:
    """Print a formatted table of findings.

    Args:
        by_severity: Count of findings per severity level.
        by_category: Count of findings per category.
        total: Total number of findings.
    """
    if total == 0:
        sparkle = _emoji("✨", "*")
        console.print(f"\n[bold green]{sparkle} No issues found — PR looks clean![/]")
        return

    # Severity breakdown
    table = Table(title="Findings Summary", title_style="bold", box=None)
    table.add_column("Severity", style="bold")
    table.add_column("Count", justify="right")

    severity_order = ["critical", "major", "minor", "info"]
    for sev in severity_order:
        count = by_severity.get(sev, 0)
        if count > 0:
            style = SEVERITY_STYLES.get(sev, "")
            label = SEVERITY_LABELS.get(sev, sev)
            table.add_row(label, str(count), style=style)

    console.print(table)

    # Category breakdown
    if by_category:
        cat_table = Table(title="Categories", title_style="bold", box=None)
        cat_table.add_column("Category", style="bold")
        cat_table.add_column("Count", justify="right")

        for cat, count in sorted(by_category.items(), key=lambda x: -x[1]):
            cat_table.add_row(cat.replace("_", " ").title(), str(count))

        console.print(cat_table)


def print_severity_distribution(findings_count: int, duration: float) -> None:
    """Print a compact one-line summary of analysis results."""
    sparkle = _emoji("✨", "*")
    chart = _emoji("\U0001f4ca", "[#]")
    if findings_count == 0:
        console.print(f"\n{sparkle} [bold green]No issues found[/] in {duration:.1f}s")
    else:
        console.print(
            f"\n{chart} [bold]{findings_count}[/] {'issue' if findings_count == 1 else 'issues'} "
            f"found in {duration:.1f}s"
        )
