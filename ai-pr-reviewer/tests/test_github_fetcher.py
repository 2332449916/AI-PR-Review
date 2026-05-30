"""Tests for the GitHub fetcher and URL parser."""

from __future__ import annotations

import pytest

from src.github_client.fetcher import parse_pr_url
from src.github_client.auth import PATAuth


class TestParsePRUrl:
    """Test suite for PR URL parsing."""

    def test_standard_url(self) -> None:
        """Test parsing a standard GitHub PR URL."""
        repo, number = parse_pr_url("https://github.com/owner/repo/pull/42")
        assert repo == "owner/repo"
        assert number == 42

    def test_url_with_hyphens(self) -> None:
        """Test parsing URL with hyphens in repo/owner names."""
        repo, number = parse_pr_url("https://github.com/my-org/my-repo/pull/123")
        assert repo == "my-org/my-repo"
        assert number == 123

    def test_url_with_dots(self) -> None:
        """Test parsing URL with dots in names."""
        repo, number = parse_pr_url("https://github.com/org.example/repo.name/pull/7")
        assert repo == "org.example/repo.name"
        assert number == 7

    def test_invalid_url_no_pr(self) -> None:
        """Test that URL without PR number raises ValueError."""
        with pytest.raises(ValueError, match="Invalid GitHub PR URL"):
            parse_pr_url("https://github.com/owner/repo")

    def test_invalid_url_not_github(self) -> None:
        """Test that non-GitHub URL raises ValueError."""
        with pytest.raises(ValueError, match="Invalid GitHub PR URL"):
            parse_pr_url("https://gitlab.com/owner/repo/merge_requests/1")

    def test_invalid_url_empty(self) -> None:
        """Test that empty string raises ValueError."""
        with pytest.raises(ValueError, match="Invalid GitHub PR URL"):
            parse_pr_url("")


class TestPATAuth:
    """Test suite for PAT authentication."""

    def test_get_token(self) -> None:
        auth = PATAuth(token="gh_test_token_123")
        assert auth.get_token() == "gh_test_token_123"

    def test_auth_type(self) -> None:
        auth = PATAuth(token="test")
        assert auth.auth_type == "pat"
