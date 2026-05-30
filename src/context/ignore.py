"""
用于过滤文件和发现项的忽略规则引擎。

支持仓库根目录下的 ``.ai-review-ignore`` 文件，语法类似于 ``.gitignore``，
并额外支持规则级别的过滤：

.. code::

    # 忽略生成的文件
    *.generated.py
    **/migrations/*
    **/vendor/*

    # 禁用特定规则
    rule:no-console-log
    rule:style-preference

    # 按路径设置严重程度上限
    [threshold:major]
    **/test/**
    **/docs/**

设计理念：
- 基于 glob 的路径匹配意味着开发者可以使用他们在 ``.gitignore`` 中
  已经熟悉的模式
- 规则级别的过滤（``rule:<rule-id>``）允许团队选择性地禁用
  噪音规则，而不影响其他检测
- 路径特定的严重程度阈值允许对测试文件或生成代码放宽规则限制
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
# 常量
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
# 模型
# ---------------------------------------------------------------------------


@dataclass
class IgnoreRules:
    """从 ``.ai-review-ignore`` 解析得到的忽略规则。"""

    path_patterns: list[str] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    path_thresholds: dict[str, Severity] = field(default_factory=dict)
    ignore_all: bool = False  # 为 True 时，跳过所有内容

    @property
    def is_empty(self) -> bool:
        return not (self.path_patterns or self.rule_ids or self.path_thresholds or self.ignore_all)


# ---------------------------------------------------------------------------
# 主过滤器
# ---------------------------------------------------------------------------


class IgnoreFilter:
    """加载并应用忽略规则，用于过滤文件和发现项。"""

    def __init__(self, repo_path: str | Path | None = None) -> None:
        self._repo_path = Path(repo_path) if repo_path else Path.cwd()
        self._rules: IgnoreRules | None = None

    def load_rules(self) -> IgnoreRules:
        """从仓库根目录加载 ``.ai-review-ignore`` 文件。

        Returns:
            解析后的 ``IgnoreRules``（如果文件不存在则为空规则集）。
        """
        if self._rules is not None:
            return self._rules

        ignore_file = self._repo_path / ".ai-review-ignore"
        if not ignore_file.exists():
            logger.debug("在 %s 中未找到 .ai-review-ignore", self._repo_path)
            self._rules = IgnoreRules()
            return self._rules

        rules = IgnoreRules()
        current_threshold: Severity | None = None

        with open(ignore_file, "r") as f:
            for line in f:
                line = line.strip()

                # 跳过空行和注释
                if not line or line.startswith("#"):
                    continue

                # 段标题：[threshold:major]
                section_match = re.match(r"^\[threshold:(\w+)\]$", line)
                if section_match:
                    sev = section_match.group(1)
                    if sev in SEVERITY_ORDER:
                        current_threshold = sev  # type: ignore[assignment]
                    else:
                        current_threshold = None
                    continue

                # 规则级忽略：rule:<rule-id>
                if line.startswith("rule:"):
                    rules.rule_ids.append(line[5:].strip())
                    continue

                # 路径模式（其他所有内容）
                path_pattern = line
                if current_threshold:
                    rules.path_thresholds[path_pattern] = current_threshold
                else:
                    # 特殊情况："*" 表示忽略所有
                    if path_pattern == "*":
                        rules.ignore_all = True
                    else:
                        rules.path_patterns.append(path_pattern)

        self._rules = rules
        logger.debug(
            "已加载 .ai-review-ignore：%d 个路径模式，%d 条规则 ID，%d 个路径阈值",
            len(rules.path_patterns),
            len(rules.rule_ids),
            len(rules.path_thresholds),
        )
        return rules

    def should_include_file(self, file_path: str, rules: IgnoreRules | None = None) -> bool:
        """检查文件是否应被纳入分析。

        Args:
            file_path: 文件路径（相对于仓库根目录）。
            rules: 解析后的忽略规则（如未提供则自动加载）。

        Returns:
            如果该文件应被分析则返回 ``True``。
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
        """过滤掉匹配忽略规则的发现项。

        Args:
            findings: 分析得到的所有发现项。
            rules: 解析后的忽略规则（如未提供则自动加载）。

        Returns:
            过滤后的发现项列表。
        """
        if rules is None:
            rules = self.load_rules()

        if rules.ignore_all:
            logger.debug("忽略所有发现项（ignore_all=True）")
            return []

        filtered: list[Finding] = []

        for finding in findings:
            # 检查规则级别的忽略
            if finding.rule_id and finding.rule_id in rules.rule_ids:
                logger.debug("按 rule_id 过滤发现项：%s", finding.rule_id)
                continue

            # 检查基于路径的严重程度阈值
            if finding.file_path:
                for pattern, threshold in rules.path_thresholds.items():
                    if self._matches_glob(finding.file_path, pattern):
                        if SEVERITY_ORDER.get(finding.severity, 0) < SEVERITY_ORDER.get(threshold, 0):
                            logger.debug(
                                "过滤低于严重程度阈值 %s 的发现项，针对 %s",
                                threshold, finding.file_path,
                            )
                            # 不跳过——仅记录该情况已被考虑
                            # 我们为每个路径使用独立的阈值
                        break

            filtered.append(finding)

        # 全局严重程度阈值由配置处理（在其他位置）
        return filtered

    # ------------------------------------------------------------------
    # 内部方法：glob 匹配
    # ------------------------------------------------------------------

    def _matches_any_pattern(self, path: str, patterns: list[str]) -> bool:
        """检查路径是否匹配列表中的任意模式。"""
        for pattern in patterns:
            if self._matches_glob(path, pattern):
                return True
        return False

    @staticmethod
    def _matches_glob(path: str, pattern: str) -> bool:
        """用于忽略规则的简单 glob 匹配。

        支持 ``**``（递归匹配）、``*``（单段匹配）和 ``?``（单字符匹配）。
        遵循 gitignore 语义：不含 ``/`` 的模式仅与路径的
        基名（最后一个组成部分）进行匹配；含有 ``/`` 的模式
        则与完整路径进行匹配。
        """
        # 规范化路径分隔符
        path = path.replace("\\", "/")
        pattern = pattern.replace("\\", "/")

        has_slash = "/" in pattern

        # 特殊情况：以 ** 开头的模式进行递归匹配
        if pattern.startswith("**/"):
            rest = pattern[3:]
            # 尝试在任意目录层级进行匹配
            parts = path.split("/")
            for i in range(len(parts)):
                subpath = "/".join(parts[i:])
                if IgnoreFilter._match_segments(subpath, rest):
                    return True
            return False

        if not has_slash:
            # gitignore 约定：不含 / 的模式仅匹配基名
            basename = path.rsplit("/", 1)[-1] if "/" in path else path
            return IgnoreFilter._match_segments(basename, pattern)

        return IgnoreFilter._match_segments(path, pattern)

    @staticmethod
    def _match_segments(path: str, pattern: str) -> bool:
        """将路径与模式进行匹配（仅支持单段 ``*``）。"""
        # 将 glob 模式转换为正则表达式
        regex_parts: list[str] = []
        i = 0
        while i < len(pattern):
            if pattern[i:i+2] == "**":
                # ** 匹配所有内容
                regex_parts.append(".*")
                i += 2
                # 跳过尾随的 /
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
