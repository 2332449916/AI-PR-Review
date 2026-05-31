"""
GitHub PR 数据获取器。

负责：
- 获取 PR 元数据（标题、描述、作者、分支）
- 获取原始 diff
- 获取指定引用处的文件内容（用于上下文构建）
- 将审查评论回帖到 PR

设计原理：
- 使用 PyGithub 进行 API 对象映射和分页
- 原始 diff 通过直接 HTTP 获取（而非 PyGithub），以避免该库对大型 PR
  进行 diff 序列化时的开销
- 速率限制处理是透明的：遇到 429/403 时等待并重试
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
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class PRInfo:
    """Pull request 元数据。"""

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
    """Pull request diff —— 原始字符串加上结构化统计信息。"""

    repo_full_name: str
    pr_number: int
    raw_diff: str
    stats: DiffStats = field(default_factory=DiffStats)


# ---------------------------------------------------------------------------
# URL 解析
# ---------------------------------------------------------------------------

_PR_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/pull/(?P<number>\d+)"
)


def parse_pr_url(url: str) -> tuple[str, int]:
    """将 GitHub PR URL 解析为 ``(repo_full_name, pr_number)``。

    Args:
        url: 例如 ``https://github.com/owner/repo/pull/42``

    Returns:
        ``("owner/repo", 42)``

    Raises:
        ValueError: 如果 URL 不是有效的 GitHub PR URL。
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
# 主获取器
# ---------------------------------------------------------------------------


class GitHubFetcher:
    """用于 PR 审查操作的高层 GitHub API 客户端。

    Args:
        auth_provider: 一个 ``AuthProvider`` 实例（默认为 ``PATAuth``）。
        base_url: GitHub API 基础 URL（对 GHES 有用）。
        timeout: HTTP 请求超时时间，以秒为单位。
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
    # 内部方法：延迟初始化客户端
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
        """关闭底层 HTTP 客户端。"""
        if self._client:
            self._client.close()
        if self._httpx_client:
            self._httpx_client.close()

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def fetch_pr_info(self, repo: str, pr_number: int) -> PRInfo:
        """从 GitHub 获取 PR 元数据。

        Args:
            repo: 完整仓库名，例如 ``"owner/repo"``。
            pr_number: PR 编号。

        Returns:
            一个包含元数据的 ``PRInfo`` 对象。

        Raises:
            ValueError: 如果 PR 不存在或无法访问。
            GithubException: 发生 GitHub API 错误时。
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
        """从 GitHub 获取原始 PR diff。

        使用 ``application/vnd.github.v3.diff`` 媒体类型来获取
        纯文本统一 diff（比 JSON 表示更小、更快）。

        Args:
            repo: 完整仓库名，例如 ``"owner/repo"``。
            pr_number: PR 编号。

        Returns:
            一个包含原始 diff 字符串的 ``PRDiff`` 对象。

        Raises:
            ValueError: 如果 PR 不存在。
            httpx.HTTPError: 发生 HTTP 级错误时。
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
        """获取指定 Git 引用处单个文件的内容。

        如果文件在该引用处不存在（例如，该文件是在此 PR 中新增的，
        在基础分支上并不存在），则返回 ``None``。

        Args:
            repo: 完整仓库名。
            path: 仓库内的文件路径。
            ref: Git 引用（分支、标签或提交 SHA）。

        Returns:
            解码后的文件内容，以 UTF-8 字符串形式，或 ``None``。
        """
        try:
            gh_repo = self._github_client.get_repo(repo)
            content = gh_repo.get_contents(path, ref=ref)
            if content and content.content:
                import base64
                try:
                    return base64.b64decode(content.content).decode("utf-8")
                except UnicodeDecodeError:
                    logger.debug("Skipping binary file %s at ref %s", path, ref)
                    return None
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
        """发布一条 PR 级别的评论。

        Args:
            repo: 完整仓库名。
            pr_number: PR 编号。
            body: Markdown 格式的评论正文。

        Returns:
            如果评论发布成功则返回 ``True``。
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
        """检查当前 GitHub API 速率限制状态。"""
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
    # 内部辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_diff_stats(raw_diff: str) -> DiffStats:
        """从原始文本中粗略估计 diff 统计信息。"""
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
