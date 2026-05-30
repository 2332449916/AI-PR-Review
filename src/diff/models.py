"""
解析后的 diff 输出数据模型。

这些模型表示 diff 解析的结构化输出，独立于底层的解析库（unidiff）。
这种抽象允许在不影响上游消费者的情况下更换解析器。

设计理念：
- 行类型使用 Literal 联合类型而非枚举，以便序列化更简单（JSON 可往返序列化）
- Hunk 和 FileDiff 携带足够的元数据，使上下文构建器能够在不重新解析的情况下做出 token 预算决策
- 可空的行号能够正确处理二进制文件以及新增/删除操作
  （已删除的文件没有 new_line_no；新增的文件没有 old_line_no）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


# ---------------------------------------------------------------------------
# 类型定义
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
# Diff 模型
# ---------------------------------------------------------------------------


@dataclass
class DiffLine:
    """diff hunk 中的单行。"""

    content: str
    line_type: LineType
    old_line_no: int | None = None
    new_line_no: int | None = None
    raw_line: str = ""  # 原始行，包含前导的 +/-/空格

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
        """去除前导的 '+'、'-' 或 ' ' diff 标记后的内容。"""
        return self.content


@dataclass
class Hunk:
    """文件中一个连续的变更块。"""

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
        """新增或删除（非上下文）的行数。"""
        return len(self.added_lines) + len(self.removed_lines)

    @property
    def line_span(self) -> tuple[int, int]:
        """返回此 hunk 在*新*文件中的 (start_line, end_line)。"""
        if not self.lines:
            return (self.target_start, self.target_start + self.target_count)
        # 在所有行中找出最小和最大的 new_line_no
        new_nos = [l.new_line_no for l in self.lines if l.new_line_no is not None]
        if not new_nos:
            return (self.target_start, self.target_start + self.target_count)
        return (min(new_nos), max(new_nos))


@dataclass
class FileDiff:
    """单个文件的 diff 信息。"""

    source_file: str
    target_file: str
    status: ChangeType
    hunks: list[Hunk] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    similarity: float | None = None  # 用于重命名/复制检测
    is_binary: bool = False
    encoding: str | None = None

    @property
    def file_path(self) -> str:
        """返回目标文件路径（若已删除则返回源文件路径）。"""
        if self.status == "deleted":
            return self.source_file
        return self.target_file

    @property
    def extension(self) -> str:
        """返回文件扩展名（不含前导点号）。"""
        path = self.file_path
        if "." in path:
            return path.rsplit(".", 1)[-1]
        return ""

    @property
    def total_changes(self) -> int:
        return self.additions + self.deletions

    def is_test_file(self) -> bool:
        """启发式判断：根据路径约定检测测试文件。"""
        path = self.file_path.lower()
        return any(
            marker in path
            for marker in ("test_", "_test", "/test/", "/tests/", "spec_", "_spec")
        )

    def classify_change(self) -> ChangeClass:
        """基于文件元数据对变更类型进行启发式分类。

        这是一个尽力而为的分类，用于选择适当的分析 prompt 模板。
        虽然并非完全准确，但对于 prompt 路由来说已经足够。
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
        return "feature"  # 默认 — 在 LLM 分析阶段进一步细化


@dataclass
class DiffStats:
    """完整 PR diff 的聚合统计信息。"""

    total_files: int = 0
    total_additions: int = 0
    total_deletions: int = 0

    @property
    def total_changed_lines(self) -> int:
        return self.total_additions + self.total_deletions
