"""Tests for the AST walker module."""

from __future__ import annotations

from src.context.ast_walker import ASTWalker


class TestASTWalker:
    """Test suite for ASTWalker."""

    def setup_method(self) -> None:
        self.walker = ASTWalker()

    def test_extract_definitions(self, sample_python_file: str) -> None:
        """Test extracting definitions from a Python file."""
        defs = self.walker.extract_definitions("src/manager.py", sample_python_file)
        assert len(defs) > 0

        # Check for expected symbols
        names = [d.name for d in defs]
        assert "UserManager" in names
        assert "get_user" in names
        assert "create_user" in names
        assert "format_date" in names

        # Check classes
        classes = [d for d in defs if d.kind == "class"]
        assert len(classes) == 1
        assert classes[0].name == "UserManager"

        # Check functions (not methods)
        functions = [d for d in defs if d.kind == "function"]
        assert any(f.name == "format_date" for f in functions)

        # Check methods
        methods = [d for d in defs if d.kind == "method"]
        assert len(methods) >= 2
        method_names = [m.name for m in methods]
        assert "get_user" in method_names
        assert "create_user" in method_names

    def test_extract_imports(self, sample_python_file: str) -> None:
        """Test extracting import statements."""
        defs = self.walker.extract_definitions("src/manager.py", sample_python_file)
        imports = [d for d in defs if d.kind == "import"]
        assert len(imports) >= 2

        import_strs = [i.signature for i in imports]
        assert any("import os" in s for s in import_strs)
        assert any("import sys" in s for s in import_strs)

    def test_find_nearby_context(self, sample_python_file: str) -> None:
        """Test finding context near a specific line."""
        # Look for context near the format_date function definition
        context = self.walker.find_nearby_context(
            "src/manager.py", sample_python_file, line_no=44, context_lines=10
        )
        assert len(context.nearby_definitions) >= 1

        # Should find format_date nearby
        nearby_names = [d.name for d in context.nearby_definitions]
        assert "format_date" in nearby_names

    def test_docstring_extraction(self, sample_python_file: str) -> None:
        """Test that docstrings are correctly extracted."""
        defs = self.walker.extract_definitions("src/manager.py", sample_python_file)

        user_manager = [d for d in defs if d.name == "UserManager"][0]
        assert user_manager.docstring is not None
        assert "Manages user operations" in user_manager.docstring

        get_user = [d for d in defs if d.name == "get_user"][0]
        assert get_user.docstring is not None
        assert "Fetch a user by ID" in get_user.docstring

    def test_signature_extraction(self, sample_python_file: str) -> None:
        """Test function signature extraction."""
        defs = self.walker.extract_definitions("src/manager.py", sample_python_file)

        format_date = [d for d in defs if d.name == "format_date"][0]
        assert "def format_date(" in format_date.signature
        assert "timestamp" in format_date.signature

        get_user = [d for d in defs if d.name == "get_user"][0]
        assert "async def get_user(" in get_user.signature or "def get_user(" in get_user.signature
        assert "user_id" in get_user.signature

    def test_resolve_reference(self, sample_python_file: str) -> None:
        """Test resolving a reference to a definition."""
        repo_files = {
            "src/manager.py": sample_python_file,
            "src/database.py": "class Database:\n    pass\n",
        }

        result = self.walker.resolve_reference("src/manager.py", "Database", repo_files)
        assert result is not None
        assert result.name == "Database"
        assert result.kind == "class"

        # Non-existent reference
        missing = self.walker.resolve_reference("src/manager.py", "NonExistent", repo_files)
        assert missing is None

    def test_fallback_extraction(self) -> None:
        """Test fallback extraction for non-Python files."""
        js_content = """
function hello(name) {
    console.log(name);
}

class Greeter {
    constructor() { this.greeting = "Hello"; }
}
"""
        defs = self.walker.extract_definitions("src/greeter.js", js_content)
        js_names = [d.name for d in defs]
        assert "hello" in js_names
        assert "Greeter" in js_names

    def test_qualified_name(self, sample_python_file: str) -> None:
        """Test qualified name generation for methods."""
        defs = self.walker.extract_definitions("src/manager.py", sample_python_file)
        methods = [d for d in defs if d.kind == "method"]
        for method in methods:
            assert "." in method.qualified_name or method.parent_name is None
            if method.parent_name:
                assert method.parent_name in method.qualified_name
