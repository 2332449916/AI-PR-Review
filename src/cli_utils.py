"""
CLI 工具函数 — 进度条、重试处理器和控制台辅助函数。

为所有 CLI 操作提供一致的用户体验：
- 多步骤分析的动画进度条
- 按严重程度着色输出
- API 调用的指数退避重试
- 与 Rich 集成的结构化日志
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
# 编码检测 — 在 GBK/Windows 终端上回退到 ASCII 表情符号
# ---------------------------------------------------------------------------

_USE_ASCII = False
try:
    enc = sys.stdout.encoding.lower() if sys.stdout.encoding else ""
    if "gbk" in enc or "latin" in enc:
        _USE_ASCII = True
except Exception:
    pass
# 允许通过环境变量覆盖
if os.environ.get("AI_REVIEWER_ASCII"):
    _USE_ASCII = True


def _emoji(utf8: str, ascii_fallback: str) -> str:
    """根据终端编码返回表情符号或 ASCII 回退字符。"""
    return ascii_fallback if _USE_ASCII else utf8

from src.diff.models import Severity

# ---------------------------------------------------------------------------
# Rich 控制台
# ---------------------------------------------------------------------------

console = Console(highlight=False)
error_console = Console(stderr=True, style="bold red")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SEVERITY_STYLES: dict[str, str] = {
    "critical": "bold red",
    "major": "bold yellow",
    "minor": "yellow",
    "info": "blue",
}

SEVERITY_LABELS: dict[str, str] = {
    "critical": _emoji("\U0001f534", "[X]") + " 严重",
    "major": _emoji("\U0001f7e0", "[!]") + " 主要",
    "minor": _emoji("\U0001f7e1", "[-]") + " 次要",
    "info": _emoji("\U0001f535", "[i]") + " 建议",
}


# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------


def setup_rich_logging(verbose: bool = False) -> None:
    """配置日志，使用 Rich 处理器以获得更美观的输出。"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )
    # 降低第三方库的日志噪音
    for noisy in ("httpx", "github", "urllib3", "anthropic", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# 重试辅助函数
# ---------------------------------------------------------------------------

T = TypeVar("T")


def api_retry(
    attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 30.0,
) -> Callable:
    """API 调用的指数退避重试装饰器。

    参数:
        attempts: 最大重试次数。
        min_wait: 重试之间的最小等待时间（秒）。
        max_wait: 重试之间的最大等待时间（秒）。
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
    """执行带重试和控制台反馈的函数。

    参数:
        func: 要执行的函数。
        label: 可读的操作标签。
        *args: 函数的位置参数。
        **kwargs: 函数的关键字参数。

    返回:
        函数的返回值。

    异常:
        如果所有重试都耗尽，则抛出最后一个异常。
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
# 进度管理器
# ---------------------------------------------------------------------------


class ProgressManager:
    """管理多步骤操作的 Rich 进度显示。

    用法::

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
        """向进度显示添加新任务。"""
        task_id = self._progress.add_task(description, total=total)
        self._tasks[description] = task_id
        return task_id

    def advance(self, description: str, advance: float = 100.0) -> None:
        """通过推进到 100% 来标记任务完成。"""
        task_id = self._tasks.get(description)
        if task_id is not None:
            self._progress.update(task_id, advance=advance)

    def update(self, description: str, completed: float) -> None:
        """更新任务的进度。"""
        task_id = self._tasks.get(description)
        if task_id is not None:
            self._progress.update(task_id, completed=completed)

    def remove_task(self, description: str) -> None:
        """从显示中移除任务。"""
        task_id = self._tasks.pop(description, None)
        if task_id is not None:
            self._progress.remove_task(task_id)


# ---------------------------------------------------------------------------
# 显示辅助函数
# ---------------------------------------------------------------------------


def print_header(text: str) -> None:
    """打印节标题。"""
    console.print()
    console.print(Panel(text, style="bold cyan", padding=(0, 1)))
    console.print()


def print_finding_count(count: int, duration: float) -> None:
    """打印分析结果的摘要。"""
    style = "bold green" if count == 0 else "bold yellow"
    chart = _emoji("\U0001f4ca", "[ ]")
    console.print(
        f"\n{chart} Analysis complete: [bold]{count}[/] findings in {duration:.1f}s",
        style=style,
    )


def print_error(message: str, hint: str | None = None) -> None:
    """打印错误消息，可附带修复提示。"""
    x = _emoji("❌", "[x]")
    error_console.print(f"{x} {message}")
    if hint:
        bulb = _emoji("\U0001f4a1", "[?]")
        console.print(f"{bulb} [bold]Hint:[/] {hint}", style="blue")


def print_warning(message: str) -> None:
    """打印警告消息。"""
    warn = _emoji("⚠️", "[!]")
    console.print(f"{warn} {message}", style="yellow")


def print_success(message: str) -> None:
    """打印成功消息。"""
    console.print(f"✅ {message}", style="bold green")


def print_step(message: str) -> None:
    """打印步骤指示器。"""
    console.print(f"  • {message}")


def print_findings_summary(
    by_severity: dict[str, int],
    by_category: dict[str, int],
    total: int,
) -> None:
    """打印格式化的问题发现表格。

    参数:
        by_severity: 每个严重级别的发现数量。
        by_category: 每个类别的发现数量。
        total: 发现的总数。
    """
    if total == 0:
        sparkle = _emoji("✨", "*")
        console.print(f"\n[bold green]{sparkle} 未发现问题 — 代码质量良好![/]")
        return

    # 严重级别分解
    table = Table(title="问题统计", title_style="bold", box=None)
    table.add_column("严重程度", style="bold")
    table.add_column("数量", justify="right")

    severity_order = ["critical", "major", "minor", "info"]
    for sev in severity_order:
        count = by_severity.get(sev, 0)
        if count > 0:
            style = SEVERITY_STYLES.get(sev, "")
            label = SEVERITY_LABELS.get(sev, sev)
            table.add_row(label, str(count), style=style)

    console.print(table)

    # 类别分解
    if by_category:
        cat_table = Table(title="问题分类", title_style="bold", box=None)
        cat_table.add_column("分类", style="bold")
        cat_table.add_column("数量", justify="right")

        for cat, count in sorted(by_category.items(), key=lambda x: -x[1]):
            from src.report.templates import CATEGORY_LABELS
            cat_label = CATEGORY_LABELS.get(cat, cat.replace("_", " ").title())
            cat_table.add_row(cat_label, str(count))

        console.print(cat_table)


def print_severity_distribution(findings_count: int, duration: float) -> None:
    """打印紧凑的单行分析结果摘要。"""
    sparkle = _emoji("✨", "*")
    chart = _emoji("\U0001f4ca", "[#]")
    if findings_count == 0:
        console.print(f"\n{sparkle} [bold green]未发现问题[/] 耗时 {duration:.1f}s")
    else:
        console.print(
            f"\n{chart} [bold]{findings_count}[/] 个问题 耗时 {duration:.1f}s"
        )
