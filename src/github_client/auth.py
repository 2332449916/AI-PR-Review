"""
GitHub 认证辅助模块。

支持两种认证模式：
1. **个人访问令牌 (PAT)**：面向个人使用的简单令牌认证。
2. **GitHub App**：适用于多仓库或组织场景的基于安装的认证。

设计理念：
- 将认证逻辑与数据获取器解耦，使同一个获取器可以和不同的认证策略配合使用
- GitHub App 认证需要 JWT 生成和令牌交换 —— 这些细节在此模块中处理，
  使得代码库的其他部分无需关心这些底层细节
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 协议
# ---------------------------------------------------------------------------


class AuthProvider(Protocol):
    """GitHub 认证策略的协议定义。"""

    def get_token(self) -> str:
        """返回有效的 GitHub API 令牌。"""
        ...

    @property
    def auth_type(self) -> str:
        """人类可读的认证类型名称（用于日志记录和调试）。"""
        ...


# ---------------------------------------------------------------------------
# PAT 认证
# ---------------------------------------------------------------------------


@dataclass
class PATAuth:
    """个人访问令牌认证。"""

    token: str
    auth_type: str = field(default="pat", init=False)

    def get_token(self) -> str:
        return self.token


# ---------------------------------------------------------------------------
# GitHub App 认证
# ---------------------------------------------------------------------------

try:
    import jwt as _jwt
except ImportError:
    _jwt = None  # type: ignore[assignment]


@dataclass
class GitHubAppAuth:
    """GitHub App 安装认证。

    使用私钥 (PEM) 签名 JWT，然后用该 JWT 换取安装访问令牌。

    参数：
        app_id: GitHub App ID（数字类型）。
        private_key_pem: 完整的 PEM 编码私钥字符串。
        installation_id: 安装 ID（注意：不是 App ID）。
    """

    app_id: str
    private_key_pem: str
    installation_id: str

    auth_type: str = field(default="github_app", init=False)
    _cached_token: str | None = field(default=None, init=False, repr=False)
    _token_expires_at: float = field(default=0.0, init=False, repr=False)

    def get_token(self) -> str:
        """返回有效的安装令牌；若已过期则自动刷新。"""
        if self._cached_token and time.time() < self._token_expires_at - 60:
            return self._cached_token

        if _jwt is None:
            raise ImportError(
                "PyJWT is required for GitHub App authentication. "
                "Install it with: pip install pyjwt"
            )

        # 使用 App 私钥签名创建一个 JWT
        now = int(time.time())
        payload = {
            "iat": now - 60,  # 签发时间设为 60 秒前（容忍时钟偏差）
            "exp": now + 600,  # 10 分钟后过期
            "iss": self.app_id,
        }
        app_token = _jwt.encode(payload, self.private_key_pem, algorithm="RS256")

        # 用 JWT 换取安装访问令牌
        import httpx

        headers = {
            "Authorization": f"Bearer {app_token}",
            "Accept": "application/vnd.github+json",
        }
        url = (
            f"https://api.github.com/app/installations/"
            f"{self.installation_id}/access_tokens"
        )

        try:
            resp = httpx.post(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            self._cached_token = data["token"]
            self._token_expires_at = time.time() + (data.get("expires_in", 3600) - 60)
            logger.info("已获取 GitHub App 安装令牌（%d 秒后过期）", data.get("expires_in", 3600))
            return self._cached_token
        except httpx.HTTPStatusError as exc:
            logger.error(
                "获取安装令牌失败: %s %s",
                exc.response.status_code,
                exc.response.text[:200],
            )
            raise
