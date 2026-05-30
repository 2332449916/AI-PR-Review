"""
AST-based code context extraction.

Walks Python source files to extract:
- Function and class definitions with their signatures and docstrings
- Import statements
- Top-level variable assignments
- Symbol references within changed code

Design rationale:
- We use Python's built-in ``ast`` module (fast, no dependencies) for Python files
- For non-Python files we fall back to line-based heuristics (regex patterns)
- The AST walker is read-only — it never modifies the file system
- Tree-sitter support is prepared as an optional enhancement for multi-language support
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
# Data models
# ---------------------------------------------------------------------------

SymbolKind = Literal["function", "class", "method", "variable", "import"]


@dataclass
class SymbolDefinition:
    """A code symbol definition (function, class, variable, etc.)."""

    name: str
    kind: SymbolKind
    file_path: str
    start_line: int
    end_line: int
    docstring: str | None = None
    signature: str = ""
    parent_name: str | None = None  # e.g., class name for methods

    @property
    def qualified_name(self) -> str:
        """Return the fully qualified name, e.g. ``ClassName.method_name``."""
        if self.parent_name:
            return f"{self.parent_name}.{self.name}"
        return self.name

    @property
    def source_snippet(self) -> str:
        """Return the first 200 chars of the definition for context display."""
        return self.signature[:200]


@dataclass
class CodeContext:
    """Assembled context for a changed region of code."""

    symbols: list[SymbolDefinition] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    nearby_definitions: list[SymbolDefinition] = field(default_factory=list)
    referenced_symbols: list[SymbolDefinition] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.symbols or self.imports or self.nearby_definitions)


# ---------------------------------------------------------------------------
# Python AST Walker
# ---------------------------------------------------------------------------


class ASTWalker:
    """Extract symbol definitions and code context from source files.

    Currently supports Python files (``.py``) via the ``ast`` module.
    For other file types, use the ``LineBasedContextBuilder`` fallback.
    """

    SUPPORTED_EXTENSIONS = {".py"}

    def extract_definitions(self, file_path: str, content: str) -> list[SymbolDefinition]:
        """Extract all symbol definitions from a file.

        Args:
            file_path: Path to the source file (used for error messages and
                       as the ``file_path`` field in returned symbols).
            content: The full text content of the file.

        Returns:
            A list of ``SymbolDefinition`` objects, or empty list if parsing fails.
        """
        ext = Path(file_path).suffix
        if ext not in self.SUPPORTED_EXTENSIONS:
            logger.debug("AST walker: unsupported extension %s for %s, using fallback", ext, file_path)
            return self._fallback_extract(file_path, content)

        try:
            tree = ast.parse(content, filename=file_path)
        except SyntaxError as exc:
            logger.warning("Syntax error parsing %s at line %d: %s", file_path, exc.lineno, exc.msg)
            return self._fallback_extract(file_path, content)

        definitions: list[SymbolDefinition] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                # Top-level function
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
                # Extract methods within the class
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

        # Extract imports
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

        logger.debug("Extracted %d symbols from %s", len(definitions), file_path)
        return definitions

    def find_nearby_context(
        self,
        file_path: str,
        content: str,
        line_no: int,
        context_lines: int = 50,
    ) -> CodeContext:
        """Find the enclosing function/class and nearby symbols for a line.

        Args:
            file_path: Path to the source file.
            content: Full file content.
            line_no: The line number to find context for (1-based).
            context_lines: How many lines before/after to scan for definitions.

        Returns:
            A ``CodeContext`` with symbols relevant to the given line.
        """
        all_defs = self.extract_definitions(file_path, content)
        context = CodeContext(symbols=all_defs)

        # Find the enclosing definition(s)
        for sym in all_defs:
            if sym.kind == "import":
                continue
            if sym.start_line <= line_no <= sym.end_line:
                context.nearby_definitions.append(sym)

        # Find definitions near the line
        for sym in all_defs:
            if sym.kind == "import":
                continue
            if abs(sym.start_line - line_no) <= context_lines and sym not in context.nearby_definitions:
                context.nearby_definitions.append(sym)

        # Extract imports
        context.imports = [sym.signature for sym in all_defs if sym.kind == "import"]

        return context

    def resolve_reference(
        self,
        file_path: str,
        symbol_name: str,
        repo_files: dict[str, str],
    ) -> SymbolDefinition | None:
        """Resolve a symbol reference to its definition across multiple files.

        Searches through the provided repo files for the definition of
        *symbol_name*. This is a simple name-based search; it does not resolve
        qualified imports (e.g. ``from module import Class``).

        Args:
            file_path: The file where the reference occurs (for priority scoring).
            symbol_name: The name of the symbol to find.
            repo_files: Dict mapping ``file_path -> file_content`` for files to search.

        Returns:
            The matching ``SymbolDefinition``, or ``None`` if not found.
        """
        # First, try the current file
        content = repo_files.get(file_path)
        if content:
            defs = self.extract_definitions(file_path, content)
            for d in defs:
                if d.name == symbol_name and d.kind in ("function", "class", "method"):
                    return d

        # Then search other files
        for path, content in repo_files.items():
            if path == file_path:
                continue
            defs = self.extract_definitions(path, content)
            for d in defs:
                if d.name == symbol_name and d.kind in ("function", "class", "method"):
                    return d

        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef, is_async: bool = False) -> str:
        """Build a compact function signature string from an AST node."""
        prefix = "async " if is_async else ""
        args = node.args
        arg_parts: list[str] = []

        # Positional arguments
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

        # Keyword-only args
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
        """Build a class signature string from an AST node."""
        bases = [ast.unparse(b) for b in node.bases]
        base_str = f"({', '.join(bases)})" if bases else ""
        return f"class {node.name}{base_str}"

    @staticmethod
    def _extract_imports(tree: ast.AST) -> list[str]:
        """Extract import statements as strings."""
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
        """Fallback extraction for non-Python files using regex heuristics."""
        definitions: list[SymbolDefinition] = []
        ext = Path(file_path).suffix

        patterns: dict[str, list[tuple[str, str, str]]] = {
            ".py": [],  # should not reach here for .py files
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
# Module-level singleton
# ---------------------------------------------------------------------------

ast_walker = ASTWalker()
