"""
上下文构建器 — 为 LLM 分析组装优化后的上下文单元。

这是上下文收集的核心编排模块。它的职责是：
1. 从 ``DiffParser`` 接收解析后的文件差异
2. 通过 ``GitHubFetcher`` 从 git 引用中获取原始/变更后的文件内容
3. 对变更文件执行 AST 分析（通过 ``ASTWalker``）
4. 组装 ``AnalysisUnit`` 对象，每个单元都控制在 token 预算之内
5. 应用忽略规则以跳过不需要的文件

设计理由：
- Token 预算在单元级别执行：每个 ``AnalysisUnit`` 都是一个自包含的提示，
  能够放入模型的上下文窗口
- 文件拆分：如果单个文件的差异 + 上下文超过预算，则在 hunk 边界处进行拆分
- 关联文件尽可能放在同一个单元中（跨文件上下文比随意分块更有价值）
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
# 常量
# ---------------------------------------------------------------------------

# 粗略估算：英文文本 + 代码中 1 token ≈ 4 个字符
CHARS_PER_TOKEN = 4

# 从未变更代码中引入的周围上下文的最大行数
DEFAULT_CONTEXT_LINES = 5

# head 引用 / base 引用的前缀
BASE_REF = "refs/heads/"  # 将被追加到 base 分支
HEAD_REF = "refs/heads/"  # 将被追加到 head 分支


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------


@dataclass
class AnalysisUnit:
    """一个自包含的分析单元，控制在 token 预算之内。

    每个单元包含一个或多个文件的差异，以及相关的 AST 上下文，
    格式化为可直接供 LLM 使用的提示片段。
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
# 主构建器
# ---------------------------------------------------------------------------


class ContextBuilder:
    """从文件差异构建受 token 预算限制的分析单元。

    Args:
        repo_full_name: 完整的 GitHub 仓库名（``owner/repo``）。
        fetcher: 用于获取文件内容的 ``GitHubFetcher`` 实例。
        ast_walker: ``ASTWalker`` 实例（如未提供则自动创建）。
        ignore_filter: ``IgnoreFilter`` 实例（如未提供则自动创建）。
        max_tokens_per_unit: 每个分析单元的 token 预算。
        context_lines: 每个 hunk 要包含的周围上下文行数。
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
        """将文件差异拆分为受 token 预算限制的分析单元。

        Args:
            file_diffs: 来自 ``DiffParser`` 的解析后的文件差异。
            head_ref: PR head 的 Git 引用（分支或 SHA）。
            base_ref: base 分支的 Git 引用（可选）。
            ignore_rules: 预加载的忽略规则（如未提供则自动加载）。

        Returns:
            可直接用于 LLM 分析的 ``AnalysisUnit`` 对象列表。
        """
        if ignore_rules is None:
            ignore_rules = self._ignore_filter.load_rules()

        # 通过忽略规则过滤文件
        filtered_diffs = [
            fd for fd in file_diffs
            if self._ignore_filter.should_include_file(fd.file_path, ignore_rules)
        ]

        if not filtered_diffs:
            logger.info("All files filtered out by ignore rules")
            return []

        # 排序：较小的文件优先（打包效果更好）
        filtered_diffs.sort(key=lambda fd: fd.total_changes)

        # 构建单元
        units: list[AnalysisUnit] = []
        current_unit_files: list[FileDiff] = []
        current_unit_tokens = 0

        for file_diff in filtered_diffs:
            file_tokens = self._estimate_file_tokens(file_diff)

            # 如果单个文件超过预算，则进行拆分
            if file_tokens > self._max_tokens:
                # 先清空当前单元
                if current_unit_files:
                    unit = await self._assemble_unit(current_unit_files, head_ref, base_ref)
                    units.append(unit)
                    current_unit_files = []
                    current_unit_tokens = 0

                # 拆分超大文件
                split_units = await self._split_oversized_file(file_diff, head_ref, base_ref)
                units.extend(split_units)
                continue

            # 检查此文件是否适合放入当前单元
            if current_unit_tokens + file_tokens > self._max_tokens:
                # 清空当前单元并开始新单元
                unit = await self._assemble_unit(current_unit_files, head_ref, base_ref)
                units.append(unit)
                current_unit_files = []
                current_unit_tokens = 0

            current_unit_files.append(file_diff)
            current_unit_tokens += file_tokens

        # 最后一个单元
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
        """获取内容并为一组文件差异构建上下文。"""
        files_before: dict[str, str] = {}
        files_after: dict[str, str] = {}
        code_contexts: dict[str, CodeContext] = {}

        for fd in file_diffs:
            file_path = fd.file_path

            # 获取变更后的文件（head 引用）
            if fd.status != "deleted":
                content_after = self._fetcher.fetch_file_content(
                    self._repo, file_path, head_ref
                )
                if content_after is not None:
                    files_after[file_path] = content_after

            # 获取变更前的文件（base 引用）
            if fd.status != "added" and base_ref:
                content_before = self._fetcher.fetch_file_content(
                    self._repo, file_path, base_ref
                )
                if content_before is not None:
                    files_before[file_path] = content_before

            # 对新文件内容进行 AST 分析
            content_to_analyze = files_after.get(file_path) or files_before.get(file_path)
            if content_to_analyze:
                try:
                    context = self._ast_walker.extract_definitions(file_path, content_to_analyze)
                    code_contexts[file_path] = CodeContext(symbols=context)
                except Exception as exc:
                    logger.warning("AST analysis failed for %s: %s", file_path, exc)
                    code_contexts[file_path] = CodeContext()

        # 估算 token 数量
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
        """在 hunk 边界处将单个超大文件拆分为多个分析单元。"""
        # 获取内容
        file_path = file_diff.file_path
        content_after = None
        content_before = None

        if file_diff.status != "deleted":
            content_after = self._fetcher.fetch_file_content(self._repo, file_path, head_ref)
        if file_diff.status != "added" and base_ref:
            content_before = self._fetcher.fetch_file_content(self._repo, file_path, base_ref)

        # 将 hunk 分组到预算大小的块中
        units: list[AnalysisUnit] = []
        current_hunks: list = []
        current_tokens = 0

        # 估算单元的开销 token（元数据、指令等）
        unit_overhead = 500

        for hunk in file_diff.hunks:
            hunk_tokens = self._estimate_hunk_tokens(hunk)

            if current_tokens + hunk_tokens > (self._max_tokens - unit_overhead) and current_hunks:
                # 从当前 hunk 创建一个单元
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

        # 最后一个部分单元
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
    # Token 估算
    # ------------------------------------------------------------------

    def _estimate_file_tokens(self, file_diff: FileDiff) -> int:
        """估算分析单个文件差异所需的 token 开销。"""
        tokens = 0
        # 文件头：约 50 token
        tokens += 50
        for hunk in file_diff.hunks:
            tokens += self._estimate_hunk_tokens(hunk)
        # 上下文开销：约为差异 token 的 30%
        tokens = int(tokens * 1.3)
        return tokens

    @staticmethod
    def _estimate_hunk_tokens(hunk) -> int:
        """估算单个 hunk 的 token 开销。"""
        tokens = 0
        for line in hunk.lines:
            # 每行大约每 4 个字符消耗 1 个 token
            tokens += max(1, len(line.content) // CHARS_PER_TOKEN)
            # +/- 的特殊标记增加了少量开销
            tokens += 1
        return tokens

    def _estimate_unit_tokens(
        self,
        file_diffs: list[FileDiff],
        files_before: dict[str, str],
        files_after: dict[str, str],
        code_contexts: dict[str, CodeContext],
    ) -> int:
        """估算完整分析单元的总 token 数量。"""
        total = 0
        # 系统提示开销：约 400 token
        total += 400
        # 差异 token
        for fd in file_diffs:
            total += self._estimate_file_tokens(fd)
        # 文件内容 token
        for content in files_before.values():
            total += len(content) // CHARS_PER_TOKEN
        for content in files_after.values():
            total += len(content) // CHARS_PER_TOKEN
        # 每个文件的上下文开销
        for ctx in code_contexts.values():
            total += len(ctx.symbols) * 15  # 每个符号约 15 token
        return total

    @staticmethod
    def _make_partial_diff(file_diff: FileDiff, hunks) -> FileDiff:
        """创建一个仅包含指定 hunk 的新 FileDiff。"""
        import copy
        new_diff = copy.copy(file_diff)
        new_diff.hunks = hunks
        new_diff.additions = sum(h.changed_line_count for h in hunks)
        new_diff.deletions = sum(len(h.removed_lines) for h in hunks)
        return new_diff
