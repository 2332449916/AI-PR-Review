"""
统一的 diff 解析器。

封装了 ``unidiff`` 库，将其输出转换为我们自己的领域模型
（``FileDiff``、``Hunk``、``DiffLine``）。这一层间接封装允许我们：
- 标准化不同 diff 格式版本的输出结构
- 附加额外的元数据（变更分类、测试文件启发式标记）
- 在需要时轻松替换解析器后端

设计理由：
- 优先选择 ``unidiff`` 而非 ``diff-parser``，因为它开箱即用地支持 GitHub 风格的
  统一 diff 格式（重命名/复制文件标记、二进制文件等）
- 解析器是无状态的——所有实例可以互换——因此可以安全地
  作为模块级单例使用
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

import unidiff

from src.diff.models import ChangeType, DiffLine, DiffStats, FileDiff, Hunk

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量：unidiff 0.7.5 使用 '+' / '-' / ' ' 作为 line_type 的值
# ---------------------------------------------------------------------------

_LINE_TYPE_MAP: dict[str, str] = {
    unidiff.LINE_TYPE_ADDED: "added",
    unidiff.LINE_TYPE_REMOVED: "removed",
    unidiff.LINE_TYPE_CONTEXT: "context",
}


class DiffParser:
    """解析原始的统一 diff 字符串，生成结构化的 ``FileDiff`` 对象。"""

    def parse(self, raw_diff: str) -> list[FileDiff]:
        """解析完整的原始 Git diff 为按文件划分的 diff 列表。

        Args:
            raw_diff: GitHub diff 接口返回的原始 diff 字符串。

        Returns:
            ``FileDiff`` 对象列表，每个变更文件对应一个。

        Raises:
            ValueError: 如果 diff 为空或无法解析。
        """
        if not raw_diff or not raw_diff.strip():
            logger.warning("向解析器提供了一个空 diff")
            return []

        try:
            patch_set = unidiff.PatchSet(raw_diff)
        except Exception as exc:
            logger.error("解析 diff 失败：%s", exc)
            raise ValueError(f"无法解析 diff：{exc}") from exc

        files: list[FileDiff] = []
        for patched_file in patch_set:
            try:
                file_diff = self._convert_patched_file(patched_file)
                files.append(file_diff)
            except Exception as exc:
                logger.warning("由于解析错误，跳过 diff 中的文件：%s", exc)
                continue

        logger.debug("从 diff 中解析出 %d 个文件", len(files))
        return files

    def parse_single_file(self, raw_diff: str, file_path: str) -> FileDiff | None:
        """从多文件 diff 中提取特定文件的 diff。

        当你已经知道要关注哪个文件，想要避免处理整个 diff 时很有用。

        Args:
            raw_diff: 原始的多文件 diff 字符串。
            file_path: 要提取的文件路径（同时匹配源文件路径和
                       目标文件路径）。

        Returns:
            匹配文件的 ``FileDiff``，如果未找到则返回 ``None``。
        """
        files = self.parse(raw_diff)
        for f in files:
            if file_path in (f.source_file, f.target_file):
                return f
        return None

    def get_changed_lines(self, file_diff: FileDiff) -> set[tuple[str, int]]:
        """获取新增/修改行的 ``(文件路径, 新行号)`` 集合。

        这是"哪些行被实际更改"的主要接口，
        被增量分析过滤器所使用。
        """
        changed: set[tuple[str, int]] = set()
        for hunk in file_diff.hunks:
            for line in hunk.lines:
                if line.line_type in ("added", "removed") and line.new_line_no is not None:
                    changed.add((file_diff.file_path, line.new_line_no))
        return changed

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    def _convert_patched_file(self, patched_file: unidiff.PatchedFile) -> FileDiff:
        """将 unidiff 的 ``PatchedFile`` 转换为我们的 ``FileDiff`` 模型。"""
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
            # unidiff 0.7.5 可能没有 similarity/is_binary/encoding 属性
        )

    def _convert_hunk(self, uhunk: unidiff.Hunk) -> Hunk:
        """将 unidiff 的 ``Hunk`` 转换为我们的 ``Hunk`` 模型。"""
        lines: list[DiffLine] = []
        for uline in uhunk:
            line_type = _LINE_TYPE_MAP.get(uline.line_type, "context")
            # 去除行尾的换行符以便更清晰地显示
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
            heading="",  # unidiff 0.7.5 的 Hunk 没有 section_heading 字段
            lines=lines,
        )

    @staticmethod
    def _normalise_path(path: str) -> str:
        """去除 Git 在 diff 路径中添加的 ``a/`` 或 ``b/`` 前缀。"""
        if path.startswith("a/") or path.startswith("b/"):
            path = path[2:]
        # 规范化为 POSIX 路径分隔符（Git 始终使用 /）
        return str(PurePosixPath(path))


# 模块级单例，方便调用
parser = DiffParser()
