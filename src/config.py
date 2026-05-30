"""
ai-pr-reviewer 的配置管理。

支持从以下来源加载配置：
1. 环境变量（优先级最高）
2. YAML 配置文件（.ai-review-config.yaml）
3. 命令行参数（从 cli.py 传入）

设计理念：
- 供应商无关：所有供应商的配置共享同一基础结构
- Token 预算在运行时计算，而非硬编码，以适应不同 LLM 的上下文窗口
- 敏感值（API 密钥、Token）不会被记录或持久化存储
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

# ---------------------------------------------------------------------------
# 类型别名
# ---------------------------------------------------------------------------

ProviderName = Literal["anthropic", "openai", "local"]
AuthType = Literal["pat", "app"]
OutputFormat = Literal["markdown", "json", "both"]
SeverityLevel = Literal["critical", "major", "minor", "info"]


# ---------------------------------------------------------------------------
# 配置数据类
# ---------------------------------------------------------------------------


@dataclass
class GitHubConfig:
    """GitHub 集成设置。"""

    auth_type: AuthType = "pat"
    token_env: str = "GITHUB_TOKEN"
    base_url: str = "https://api.github.com"

    def resolve_token(self) -> str | None:
        """从配置的环境变量中解析 GitHub Token。"""
        token = os.environ.get(self.token_env)
        if not token and self.auth_type == "pat":
            token = os.environ.get("GH_TOKEN")
        return token


@dataclass
class AnalysisConfig:
    """分析行为设置。"""

    min_confidence: float = 0.7
    max_context_tokens: int = 6000
    severity_threshold: SeverityLevel = "minor"
    max_files: int = 50
    enable_ast_context: bool = True
    enable_cross_file_analysis: bool = True

    def __post_init__(self) -> None:
        """验证数值字段的范围约束。"""
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError(f"min_confidence 必须在 0 到 1 之间，当前值为 {self.min_confidence}")
        if self.max_context_tokens < 512:
            raise ValueError(f"max_context_tokens 必须 >= 512，当前值为 {self.max_context_tokens}")


@dataclass
class OutputConfig:
    """输出格式化设置。"""

    format: OutputFormat = "markdown"
    auto_comment: bool = False
    color: bool = True


@dataclass
class AppConfig:
    """顶层应用程序配置。"""

    provider: ProviderName = "anthropic"
    model: str = "claude-sonnet-4-20250514"
    api_key_env: str = "ANTHROPIC_API_KEY"

    github: GitHubConfig = field(default_factory=GitHubConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    output: OutputConfig = field(default_factory=OutputConfig)

    # --- 运行时计算 ---
    _config_path: Path | None = None

    @classmethod
    def from_yaml(cls, path: str | Path) -> AppConfig:
        """从 YAML 文件加载配置。

        Args:
            path: YAML 配置文件的路径。

        Returns:
            AppConfig 实例，其中包含从文件合并的值以及默认值。

        Raises:
            FileNotFoundError: 配置文件不存在时抛出。
            yaml.YAMLError: YAML 格式错误时抛出。
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"配置文件未找到: {path}")

        with open(path, "r") as f:
            raw = yaml.safe_load(f)

        if raw is None:
            return cls()

        provider = raw.get("provider", "anthropic")
        model = raw.get("model", "claude-sonnet-4-20250514")
        api_key_env = raw.get("api_key_env", "ANTHROPIC_API_KEY")

        github_raw = raw.get("github", {})
        analysis_raw = raw.get("analysis", {})
        output_raw = raw.get("output", {})

        cfg = cls(
            provider=provider,
            model=model,
            api_key_env=api_key_env,
            github=GitHubConfig(**github_raw),
            analysis=AnalysisConfig(**analysis_raw),
            output=OutputConfig(**output_raw),
            _config_path=path,
        )
        return cfg

    @classmethod
    def discover_and_load(cls, start_dir: str | Path | None = None) -> AppConfig:
        """从 *start_dir* 向上遍历目录，查找并加载 ``.ai-review-config.yaml``。

        如果未找到配置文件，则返回默认配置。
        """
        search_dir = Path(start_dir).resolve() if start_dir else Path.cwd()
        for parent in [search_dir] + list(search_dir.parents):
            candidate = parent / ".ai-review-config.yaml"
            if candidate.exists():
                return cls.from_yaml(candidate)
        return cls()

    def resolve_api_key(self) -> str | None:
        """从配置的环境变量中解析 LLM API 密钥。"""
        return os.environ.get(self.api_key_env)

    @property
    def effective_max_context_tokens(self) -> int:
        """返回根据所选供应商调整后的最大上下文 Token 数。

        不同供应商有不同的上下文窗口；我们将其限制在已知最大值的 80%，
        以为响应留出空间。
        """
        provider_limits = {
            "anthropic": 200_000,
            "openai": 128_000,
            "local": 32_000,
        }
        ceiling = provider_limits.get(self.provider, 32_000)
        budget = self.analysis.max_context_tokens
        return min(budget, int(ceiling * 0.8))

    def validate(self) -> list[str]:
        """返回配置错误列表（空列表表示配置有效）。"""
        errors: list[str] = []

        api_key = self.resolve_api_key()
        if not api_key and self.provider != "local":
            errors.append(
                f"{self.api_key_env} 未设置。"
                f"请通过环境变量设置该变量，或提供有效的 API 密钥。"
            )

        gh_token = self.github.resolve_token()
        if not gh_token:
            errors.append(
                f"{self.github.token_env} 未设置。"
                "GitHub 操作（获取 PR、发布评论）将失败。"
            )

        valid_providers = {"anthropic", "openai", "local"}
        if self.provider not in valid_providers:
            errors.append(
                f"无效的供应商 '{self.provider}'。"
                f"必须是以下之一: {', '.join(sorted(valid_providers))}"
            )

        valid_formats = {"markdown", "json", "both"}
        if self.output.format not in valid_formats:
            errors.append(
                f"无效的输出格式 '{self.output.format}'。"
                f"必须是以下之一: {', '.join(sorted(valid_formats))}"
            )

        errors.extend(self.analysis.__post_init__() or [])
        return errors
