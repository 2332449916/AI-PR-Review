"""
GitHub PR data fetcher.

Responsible for:
- Fetching PR metadata (title, description, author, branches)
- Fetching raw diffs
- Fetching file contents at specific refs (for context building)
- Posting review comments back to PRs

Design rationale:
- Uses PyGithub for API object mapping and pagination
- Raw diffs are fetched via direct HTTP (not PyGithub) to avoid the library's
  diff serialisation overhead for large PRs
- Rate-limit handling is transparent: we wait and retry on 429/403
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from urllib.parse import urlparse

import httpx
import github as gh
from github import Github, GithubException, RateLimitExceededException

from src.github_client.auth import AuthProvider, PATAuth
from src.diff.models import DiffStats

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class PRInfo:
    """Pull request metadata."""

    repo_full_name: str
    pr_number: int
    title: str
    description: str
    base_branch: str
    head_branch: str
    author: str
    changed_files: int
    created_at: datetime
    html_url: str
    commit_sha: str | None = None


@dataclass
class PRDiff:
    """Pull request diff — raw string plus structured stats."""

    repo_full_name: str
    pr_number: int
    raw_diff: str
    stats: DiffStats = field(default_factory=DiffStats)


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

_PR_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/pull/(?P<number>\d+)"
)


def parse_pr_url(url: str) -> tuple[str, int]:
    """Parse a GitHub PR URL into ``(repo_full_name, pr_number)``.

    Args:
        url: e.g. ``https://github.com/owner/repo/pull/42``

    Returns:
        ``("owner/repo", 42)``

    Raises:
        ValueError: If the URL is not a valid GitHub PR URL.
    """
    match = _PR_URL_RE.match(url)
    if not match:
        raise ValueError(
            f"Invalid GitHub PR URL: {url!r}. "
            "Expected format: https://github.com/owner/repo/pull/<number>"
        )
    repo = f"{match.group('owner')}/{match.group('repo')}"
    pr_number = int(match.group("number"))
    return repo, pr_number


# ---------------------------------------------------------------------------
# Main fetcher
# ---------------------------------------------------------------------------


class GitHubFetcher:
    """High-level GitHub API client for PR review operations.

    Args:
        auth_provider: An ``AuthProvider`` instance (defaults to ``PATAuth``).
        base_url: GitHub API base URL (useful for GHES).
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        auth_provider: AuthProvider | None = None,
        base_url: str = "https://api.github.com",
        timeout: int = 30,
    ) -> None:
        self._auth_provider = auth_provider
        self._base_url = base_url
        self._timeout = timeout
        self._client: Github | None = None
        self._httpx_client: httpx.Client | None = None

    # ------------------------------------------------------------------
    # Internal: lazy-init clients
    # ------------------------------------------------------------------

    @property
    def _github_client(self) -> Github:
        if self._client is None:
            token = self._auth_provider.get_token() if self._auth_provider else None
            if token:
                auth = gh.Auth.Token(token)
                self._client = Github(
                    auth=auth,
                    base_url=self._base_url,
                    timeout=self._timeout,
                    retry=3,
                    per_page=100,
                )
            else:
                self._client = Github(
                    base_url=self._base_url,
                    timeout=self._timeout,
                    retry=3,
                    per_page=100,
                )
        return self._client

    @property
    def _http(self) -> httpx.Client:
        if self._httpx_client is None:
            token = self._auth_provider.get_token() if self._auth_provider else None
            headers = {
                "Accept": "application/vnd.github.v3.diff",
                "User-Agent": "ai-pr-reviewer/0.1.0",
            }
            if token:
                headers["Authorization"] = f"Bearer {token}"
            self._httpx_client = httpx.Client(
                base_url=self._base_url.replace("/api/v3", "").rstrip("/"),
                headers=headers,
                timeout=self._timeout,
                follow_redirects=True,
            )
        return self._httpx_client

    def close(self) -> None:
        """Close underlying HTTP clients."""
        if self._client:
            self._client.close()
        if self._httpx_client:
            self._httpx_client.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_pr_info(self, repo: str, pr_number: int) -> PRInfo:
        """Fetch PR metadata from GitHub.

        Args:
            repo: Full repo name, e.g. ``"owner/repo"``.
            pr_number: PR number.

        Returns:
            A ``PRInfo`` object with metadata.

        Raises:
            ValueError: If the PR does not exist or is not accessible.
            GithubException: On GitHub API errors.
        """
        try:
            gh_repo = self._github_client.get_repo(repo)
            gh_pr = gh_repo.get_pull(pr_number)
        except RateLimitExceededException:
            logger.error("GitHub API rate limit exceeded fetching PR info")
            raise
        except GithubException as exc:
            if exc.status == 404:
                raise ValueError(
                    f"PR #{pr_number} not found in {repo}. "
                    "Check the repository name and PR number."
            ) from exc
            logger.error("GitHub API error fetching PR %s #%d: %s", repo, pr_number, exc)
            raise

        return PRInfo(
            repo_full_name=repo,
            pr_number=pr_number,
            title=gh_pr.title,
            description=gh_pr.body or "",
            base_branch=gh_pr.base.ref,
            head_branch=gh_pr.head.ref,
            author=gh_pr.user.login if gh_pr.user else "unknown",
            changed_files=gh_pr.changed_files,
            created_at=gh_pr.created_at,
            html_url=gh_pr.html_url,
            commit_sha=gh_pr.head.sha,
        )

    def fetch_diff(self, repo: str, pr_number: int) -> PRDiff:
        """Fetch the raw PR diff from GitHub.

        Uses the ``application/vnd.github.v3.diff`` media type to get a
        plain-text unified diff (smaller and faster than the JSON representation).

        Args:
            repo: Full repo name, e.g. ``"owner/repo"``.
            pr_number: PR number.

        Returns:
            A ``PRDiff`` object containing the raw diff string.

        Raises:
            ValueError: If the PR does not exist.
            httpx.HTTPError: On HTTP-level errors.
        """
        url = f"/repos/{repo}/pulls/{pr_number}"
        headers = {
            "Accept": "application/vnd.github.v3.diff",
        }
        token = self._auth_provider.get_token() if self._auth_provider else None
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            resp = self._http.get(url, headers=headers)
            resp.raise_for_status()
            raw_diff = resp.text

            stats = self._estimate_diff_stats(raw_diff)

            return PRDiff(
                repo_full_name=repo,
                pr_number=pr_number,
                raw_diff=raw_diff,
                stats=stats,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ValueError(
                    f"PR #{pr_number} not found in {repo}. "
                    "Check the repository name and PR number."
                ) from exc
            if exc.response.status_code == 410:
                raise ValueError(
                    f"Diff for PR #{pr_number} is empty (maybe all changes were already merged)."
                ) from exc
            logger.error(
                "HTTP error fetching diff for %s #%d: %d %s",
                repo, pr_number, exc.response.status_code, exc.response.text[:200],
            )
            raise

    def fetch_file_content(self, repo: str, path: str, ref: str) -> str | None:
        """Fetch a single file's content at a given Git ref.

        Returns ``None`` if the file does not exist at that ref (e.g., it was
        added in this PR and doesn't exist on the base branch).

        Args:
            repo: Full repo name.
            path: File path within the repository.
            ref: Git ref (branch, tag, or commit SHA).

        Returns:
            Decoded file content as a UTF-8 string, or ``None``.
        """
        try:
            gh_repo = self._github_client.get_repo(repo)
            content = gh_repo.get_contents(path, ref=ref)
            if content and content.content:
                import base64
                return base64.b64decode(content.content).decode("utf-8")
            return None
        except GithubException as exc:
            if exc.status == 404:
                logger.debug("File %s not found at ref %s in %s", path, ref, repo)
                return None
            if exc.status == 409:
                logger.debug("File %s is a submodule or binary at %s", path, ref)
                return None
            logger.warning("Error fetching %s at %s: %s", path, ref, exc)
            return None

    def post_comment(self, repo: str, pr_number: int, body: str) -> bool:
        """Post a PR-level comment.

        Args:
            repo: Full repo name.
            pr_number: PR number.
            body: Markdown comment body.

        Returns:
            ``True`` if the comment was posted successfully.
        """
        try:
            gh_repo = self._github_client.get_repo(repo)
            gh_pr = gh_repo.get_pull(pr_number)
            gh_pr.create_comment(body)
            logger.info("Posted review comment on %s #%d", repo, pr_number)
            return True
        except GithubException as exc:
            logger.error("Failed to post comment on %s #%d: %s", repo, pr_number, exc)
            return False

    def get_rate_limit_status(self) -> dict:
        """Check current GitHub API rate limit status."""
        try:
            rate = self._github_client.get_rate_limit()
            core = rate.core
            return {
                "core_limit": core.limit,
                "core_remaining": core.remaining,
                "core_reset": core.reset.isoformat() if core.reset else "unknown",
            }
        except Exception as exc:
            logger.warning("Failed to get rate limit status: %s", exc)
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_diff_stats(raw_diff: str) -> DiffStats:
        """Roughly estimate diff statistics from the raw text."""
        additions = 0
        deletions = 0
        file_count = 0

        for line in raw_diff.splitlines():
            if line.startswith("diff --git"):
                file_count += 1
            elif line.startswith("+"):
                additions += 1
            elif line.startswith("-"):
                deletions += 1

        return DiffStats(
            total_files=file_count,
            total_additions=additions,
            total_deletions=deletions,
        )
