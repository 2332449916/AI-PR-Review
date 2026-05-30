"""
Configuration management for ai-pr-reviewer.

Supports loading from:
1. Environment variables (highest priority)
2. YAML config file (.ai-review-config.yaml)
3. CLI arguments (passed through from cli.py)

Design rationale:
- Provider-agnostic: all provider configs share the same base structure
- Token budgets are runtime-computed, not hardcoded, to adapt to different LLM context windows
- Sensitive values (API keys, tokens) are never logged or persisted
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

ProviderName = Literal["anthropic", "openai", "local"]
AuthType = Literal["pat", "app"]
OutputFormat = Literal["markdown", "json", "both"]
SeverityLevel = Literal["critical", "major", "minor", "info"]


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------


@dataclass
class GitHubConfig:
    """GitHub integration settings."""

    auth_type: AuthType = "pat"
    token_env: str = "GITHUB_TOKEN"
    base_url: str = "https://api.github.com"

    def resolve_token(self) -> str | None:
        """Resolve the GitHub token from the configured environment variable."""
        token = os.environ.get(self.token_env)
        if not token and self.auth_type == "pat":
            token = os.environ.get("GH_TOKEN")
        return token


@dataclass
class AnalysisConfig:
    """Analysis behaviour settings."""

    min_confidence: float = 0.7
    max_context_tokens: int = 6000
    severity_threshold: SeverityLevel = "minor"
    max_files: int = 50
    enable_ast_context: bool = True
    enable_cross_file_analysis: bool = True

    def __post_init__(self) -> None:
        """Validate bounds on numeric fields."""
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError(f"min_confidence must be between 0 and 1, got {self.min_confidence}")
        if self.max_context_tokens < 512:
            raise ValueError(f"max_context_tokens must be >= 512, got {self.max_context_tokens}")


@dataclass
class OutputConfig:
    """Output formatting settings."""

    format: OutputFormat = "markdown"
    auto_comment: bool = False
    color: bool = True


@dataclass
class AppConfig:
    """Top-level application configuration."""

    provider: ProviderName = "anthropic"
    model: str = "claude-sonnet-4-20250514"
    api_key_env: str = "ANTHROPIC_API_KEY"

    github: GitHubConfig = field(default_factory=GitHubConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    output: OutputConfig = field(default_factory=OutputConfig)

    # --- computed at runtime ---
    _config_path: Path | None = None

    @classmethod
    def from_yaml(cls, path: str | Path) -> AppConfig:
        """Load configuration from a YAML file.

        Args:
            path: Path to the YAML config file.

        Returns:
            AppConfig instance with values from the file merged with defaults.

        Raises:
            FileNotFoundError: If the config file does not exist.
            yaml.YAMLError: If the YAML is malformed.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

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
        """Walk up from *start_dir* to find and load ``.ai-review-config.yaml``.

        If no config file is found, returns default configuration.
        """
        search_dir = Path(start_dir).resolve() if start_dir else Path.cwd()
        for parent in [search_dir] + list(search_dir.parents):
            candidate = parent / ".ai-review-config.yaml"
            if candidate.exists():
                return cls.from_yaml(candidate)
        return cls()

    def resolve_api_key(self) -> str | None:
        """Resolve the LLM API key from the configured environment variable."""
        return os.environ.get(self.api_key_env)

    @property
    def effective_max_context_tokens(self) -> int:
        """Return the max context tokens adjusted for the selected provider.

        Different providers have different context windows; we cap at 80% of
        the known maximum to leave room for the response.
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
        """Return a list of configuration errors (empty means valid)."""
        errors: list[str] = []

        api_key = self.resolve_api_key()
        if not api_key and self.provider != "local":
            errors.append(
                f"{self.api_key_env} is not set. "
                f"Set it via environment variable or provide a valid API key."
            )

        gh_token = self.github.resolve_token()
        if not gh_token:
            errors.append(
                f"{self.github.token_env} is not set. "
                "GitHub operations (fetching PRs, posting comments) will fail."
            )

        valid_providers = {"anthropic", "openai", "local"}
        if self.provider not in valid_providers:
            errors.append(
                f"Invalid provider '{self.provider}'. "
                f"Must be one of: {', '.join(sorted(valid_providers))}"
            )

        valid_formats = {"markdown", "json", "both"}
        if self.output.format not in valid_formats:
            errors.append(
                f"Invalid output format '{self.output.format}'. "
                f"Must be one of: {', '.join(sorted(valid_formats))}"
            )

        errors.extend(self.analysis.__post_init__() or [])
        return errors
