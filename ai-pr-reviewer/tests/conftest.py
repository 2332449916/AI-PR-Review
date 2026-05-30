"""Shared test fixtures and configuration."""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_diff_simple() -> str:
    """Return a simple single-file diff."""
    return (FIXTURES_DIR / "sample_diff_simple.txt").read_text(encoding="utf-8")


@pytest.fixture
def sample_diff_multifile() -> str:
    """Return a multi-file diff."""
    return (FIXTURES_DIR / "sample_diff_multifile.txt").read_text(encoding="utf-8")


@pytest.fixture
def sample_diff_security() -> str:
    """Return a diff containing security issues."""
    return (FIXTURES_DIR / "sample_diff_security.txt").read_text(encoding="utf-8")


@pytest.fixture
def empty_diff() -> str:
    """Return an empty diff."""
    return ""


@pytest.fixture
def sample_python_file() -> str:
    """Return a sample Python file for AST testing."""
    return """\"\"\"Sample module for testing.\"\"\"

import os
import sys
from typing import Optional

from src.database import Database

VERSION = "1.0.0"


class UserManager:
    \"\"\"Manages user operations.\"\"\"

    def __init__(self, db: Database) -> None:
        self.db = db
        self._cache: dict[str, "User"] = {}

    async def get_user(self, user_id: int) -> Optional["User"]:
        \"\"\"Fetch a user by ID.\"\"\"
        if user_id in self._cache:
            return self._cache[user_id]
        user = await self.db.query("SELECT * FROM users WHERE id = ?", user_id)
        if user:
            self._cache[user_id] = user
        return user

    def create_user(self, name: str, email: str) -> "User":
        \"\"\"Create a new user.\"\"\"
        if not name or not email:
            raise ValueError("Name and email are required")
        return User(name=name, email=email)


def format_date(timestamp: float, fmt: str = "%Y-%m-%d") -> str:
    \"\"\"Format a timestamp as a date string.\"\"\"
    from datetime import datetime
    return datetime.fromtimestamp(timestamp).strftime(fmt)
"""


@pytest.fixture
def ai_review_ignore_content() -> str:
    """Return a sample .ai-review-ignore file content."""
    return """# Generated files
*.generated.py
**/migrations/*

# Noisy rules
rule:no-console-log
rule:style-preference

# Per-path threshold
[threshold:major]
**/test/**
**/docs/**
"""
