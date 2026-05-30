"""
基于 AST 的代码上下文提取。

遍历 Python 源文件以提取：
- 函数和类定义，包括其签名和文档字符串
- import 语句
- 顶层变量赋值
- 变更代码中的符号引用

设计理由：
- 使用 Python 内置的 ``ast`` 模块（快速、无外部依赖）处理 Python 文件
- 对于非 Python 文件，回退到基于行的启发式方法（正则表达式模式）
- AST 遍历器是只读的——它永远不会修改文件系统
- 已预留 Tree-sitter 支持作为可选增强，用于多语言支持
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

SymbolKind = Literal["function", "class", "method", "variable", "import"]


@dataclass
class SymbolDefinition:
    """代码符号定义（函数、类、变量等）。"""

    name: str
    kind: SymbolKind
    file_path: str
    start_line: int
    end_line: int
    docstring: str | None = None
    signature: str = ""
    parent_name: str | None = None  # 例如：类名（用于方法）

    @property
    def qualified_name(self) -> str:
        """返回完全限定名，例如 ``ClassName.method_name``。"""
        if self.parent_name:
            return f"{self.parent_name}.{self.name}"
        return self.name

    @property
    def source_snippet(self) -> str:
        """返回定义的前 200 个字符，用于上下文显示。"""
        return self.signature[:200]


@dataclass
class CodeContext:
    """为代码变更区域组装的上下文信息。"""

    symbols: list[SymbolDefinition] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    nearby_definitions: list[SymbolDefinition] = field(default_factory=list)
    referenced_symbols: list[SymbolDefinition] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.symbols or self.imports or self.nearby_definitions)


# ---------------------------------------------------------------------------
# Python AST 遍历器
# ---------------------------------------------------------------------------


class ASTWalker:
    """从源文件中提取符号定义和代码上下文。

    目前通过 ``ast`` 模块支持 Python 文件（``.py``）。
    对于其他文件类型，请使用 ``LineBasedContextBuilder`` 作为回退方案。
    """

    SUPPORTED_EXTENSIONS = {".py"}

    def extract_definitions(self, file_path: str, content: str) -> list[SymbolDefinition]:
        """从文件中提取所有符号定义。

        Args:
            file_path: 源文件路径（用于错误消息和返回符号中的
                       ``file_path`` 字段）。
            content: 文件的完整文本内容。

        Returns:
            ``SymbolDefinition`` 对象列表；若解析失败则返回空列表。
        """
        ext = Path(file_path).suffix
        if ext not in self.SUPPORTED_EXTENSIONS:
            logger.debug("AST 遍历器：不支持的后缀 %s，文件 %s，使用回退方案", ext, file_path)
            return self._fallback_extract(file_path, content)

        try:
            tree = ast.parse(content, filename=file_path)
        except SyntaxError as exc:
            logger.warning("解析 %s 时遇到语法错误，第 %d 行: %s", file_path, exc.lineno, exc.msg)
            return self._fallback_extract(file_path, content)

        definitions: list[SymbolDefinition] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                # 顶层函数
                sig = self._function_signature(node)
                definitions.append(SymbolDefinition(
                    name=node.name,
                    kind="function",
                    file_path=file_path,
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    docstring=ast.get_docstring(node),
                    signature=sig,
                ))
            elif isinstance(node, ast.AsyncFunctionDef):
                sig = self._function_signature(node, is_async=True)
                definitions.append(SymbolDefinition(
                    name=node.name,
                    kind="function",
                    file_path=file_path,
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    docstring=ast.get_docstring(node),
                    signature=sig,
                ))
            elif isinstance(node, ast.ClassDef):
                sig = self._class_signature(node)
                definitions.append(SymbolDefinition(
                    name=node.name,
                    kind="class",
                    file_path=file_path,
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    docstring=ast.get_docstring(node),
                    signature=sig,
                ))
                # 提取类中的方法
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        is_async = isinstance(child, ast.AsyncFunctionDef)
                        method_sig = self._function_signature(child, is_async=is_async)
                        definitions.append(SymbolDefinition(
                            name=child.name,
                            kind="method",
                            file_path=file_path,
                            start_line=child.lineno,
                            end_line=child.end_lineno or child.lineno,
                            docstring=ast.get_docstring(child),
                            signature=method_sig,
                            parent_name=node.name,
                        ))

        # 提取 import 语句
        imports = self._extract_imports(tree)
        for imp in imports:
            definitions.append(SymbolDefinition(
                name=imp,
                kind="import",
                file_path=file_path,
                start_line=0,
                end_line=0,
                signature=imp,
            ))

        logger.debug("从 %s 提取了 %d 个符号", len(definitions), file_path)
        return definitions

    def find_nearby_context(
        self,
        file_path: str,
        content: str,
        line_no: int,
        context_lines: int = 50,
    ) -> CodeContext:
        """查找给定行所在的函数/类以及附近的符号。

        Args:
            file_path: 源文件路径。
            content: 文件完整内容。
            line_no: 要查找上下文的行号（从 1 开始）。
            context_lines: 向前/后扫描多少行以查找定义。

        Returns:
            包含与给定行相关符号的 ``CodeContext``。
        """
        all_defs = self.extract_definitions(file_path, content)
        context = CodeContext(symbols=all_defs)

        # 查找包含给定行的定义
        for sym in all_defs:
            if sym.kind == "import":
                continue
            if sym.start_line <= line_no <= sym.end_line:
                context.nearby_definitions.append(sym)

        # 查找给定行附近的定义
        for sym in all_defs:
            if sym.kind == "import":
                continue
            if abs(sym.start_line - line_no) <= context_lines and sym not in context.nearby_definitions:
                context.nearby_definitions.append(sym)

        # 提取 import 语句
        context.imports = [sym.signature for sym in all_defs if sym.kind == "import"]

        return context

    def resolve_reference(
        self,
        file_path: str,
        symbol_name: str,
        repo_files: dict[str, str],
    ) -> SymbolDefinition | None:
        """跨多个文件将符号引用解析为其定义。

        在提供的仓库文件中搜索 *symbol_name* 的定义。
        这是一个简单的基于名称的搜索；不会解析限定导入
        （例如 ``from module import Class``）。

        Args:
            file_path: 引用出现的文件（用于优先级评分）。
            symbol_name: 要查找的符号名称。
            repo_files: 将 ``file_path -> file_content`` 映射的字典，用于搜索。

        Returns:
            匹配的 ``SymbolDefinition``，若未找到则返回 ``None``。
        """
        # 首先尝试当前文件
        content = repo_files.get(file_path)
        if content:
            defs = self.extract_definitions(file_path, content)
            for d in defs:
                if d.name == symbol_name and d.kind in ("function", "class", "method"):
                    return d

        # 然后搜索其他文件
        for path, content in repo_files.items():
            if path == file_path:
                continue
            defs = self.extract_definitions(path, content)
            for d in defs:
                if d.name == symbol_name and d.kind in ("function", "class", "method"):
                    return d

        return None

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef, is_async: bool = False) -> str:
        """从 AST 节点构建紧凑的函数签名字符串。"""
        prefix = "async " if is_async else ""
        args = node.args
        arg_parts: list[str] = []

        # 位置参数
        for arg in args.args:
            annotation = ""
            if arg.annotation:
                annotation = f": {ast.unparse(arg.annotation)}"
            arg_parts.append(f"{arg.arg}{annotation}")

        # *args
        if args.vararg:
            annotation = ""
            if args.vararg.annotation:
                annotation = f": {ast.unparse(args.vararg.annotation)}"
            arg_parts.append(f"*{args.vararg.arg}{annotation}")

        # 仅关键字参数
        for arg in args.kwonlyargs:
            annotation = ""
            if arg.annotation:
                annotation = f": {ast.unparse(arg.annotation)}"
            arg_parts.append(f"{arg.arg}{annotation}")

        # **kwargs
        if args.kwarg:
            annotation = ""
            if args.kwarg.annotation:
                annotation = f": {ast.unparse(args.kwarg.annotation)}"
            arg_parts.append(f"**{args.kwarg.arg}{annotation}")

        return_annotation = ""
        if node.returns:
            return_annotation = f" -> {ast.unparse(node.returns)}"

        return f"{prefix}def {node.name}({', '.join(arg_parts)}){return_annotation}"

    @staticmethod
    def _class_signature(node: ast.ClassDef) -> str:
        """从 AST 节点构建类签名字符串。"""
        bases = [ast.unparse(b) for b in node.bases]
        base_str = f"({', '.join(bases)})" if bases else ""
        return f"class {node.name}{base_str}"

    @staticmethod
    def _extract_imports(tree: ast.AST) -> list[str]:
        """将 import 语句提取为字符串。"""
        imports: list[str] = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    alias_str = f" as {alias.asname}" if alias.asname else ""
                    imports.append(f"import {alias.name}{alias_str}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = [f"{alias.name}" + (f" as {alias.asname}" if alias.asname else "") for alias in node.names]
                imports.append(f"from {module} import {', '.join(names)}")
        return imports

    @staticmethod
    def _fallback_extract(file_path: str, content: str) -> list[SymbolDefinition]:
        """对非 Python 文件使用正则表达式启发式方法进行回退提取。"""
        definitions: list[SymbolDefinition] = []
        ext = Path(file_path).suffix

        patterns: dict[str, list[tuple[str, str, str]]] = {
            ".py": [],  # .py 文件不应走到这里
            ".js": [
                (r"(?:export\s+)?(?:async\s+)?function\s+(\w+)", "function", "function {name}(...)"),
                (r"(?:export\s+)?class\s+(\w+)", "class", "class {name}"),
                (r"(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:\([^)]*\)\s*)?=>", "function", "const {name} = (...) =>"),
            ],
            ".ts": [
                (r"(?:export\s+)?(?:async\s+)?function\s+(\w+)", "function", "function {name}(...)"),
                (r"(?:export\s+)?class\s+(\w+)", "class", "class {name}"),
                (r"(?:export\s+)?(?:const|let|var)\s+(\w+)\s*:\s*", "variable", "const {name}: ..."),
            ],
            ".go": [
                (r"func\s+(\w+)\(", "function", "func {name}(...)"),
                (r"type\s+(\w+)\s+struct", "class", "type {name} struct"),
                (r"type\s+(\w+)\s+interface", "class", "type {name} interface"),
            ],
            ".java": [
                (r"(?:public|private|protected)?\s*(?:static\s+)?(?:class|interface)\s+(\w+)", "class", "class {name}"),
                (r"(?:public|private|protected)?\s*(?:static\s+)?\w+\s+(\w+)\s*\(", "function", "... {name}(...)"),
            ],
        }

        file_patterns = patterns.get(ext, [])
        for line in content.splitlines():
            for regex, kind, sig_template in file_patterns:
                match = re.search(regex, line.strip())
                if match:
                    name = match.group(1)
                    definitions.append(SymbolDefinition(
                        name=name,
                        kind=kind,  # type: ignore[arg-type]
                        file_path=file_path,
                        start_line=0,
                        end_line=0,
                        signature=sig_template.replace("{name}", name),
                    ))

        return definitions


# ---------------------------------------------------------------------------
# 模块级单例
# ---------------------------------------------------------------------------

ast_walker = ASTWalker()
