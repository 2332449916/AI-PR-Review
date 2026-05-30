"""Tests for the configuration module."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from src.config import AppConfig, AnalysisConfig, GitHubConfig, OutputConfig


class TestAnalysisConfig:
    """Test analysis configuration validation."""

    def test_valid_confidence(self) -> None:
        cfg = AnalysisConfig(min_confidence=0.5)
        assert cfg.min_confidence == 0.5

    def test_invalid_confidence(self) -> None:
        with pytest.raises(ValueError, match="min_confidence"):
            AnalysisConfig(min_confidence=1.5)

    def test_invalid_confidence_negative(self) -> None:
        with pytest.raises(ValueError, match="min_confidence"):
            AnalysisConfig(min_confidence=-0.1)

    def test_invalid_tokens(self) -> None:
        with pytest.raises(ValueError, match="max_context_tokens"):
            AnalysisConfig(max_context_tokens=100)


class TestGitHubConfig:
    """Test GitHub configuration."""

    def test_defaults(self) -> None:
        cfg = GitHubConfig()
        assert cfg.auth_type == "pat"
        assert cfg.token_env == "GITHUB_TOKEN"

    def test_resolve_token(self, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "gh_test_token")
        cfg = GitHubConfig()
        assert cfg.resolve_token() == "gh_test_token"

    def test_resolve_gh_token_fallback(self, monkeypatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GH_TOKEN", "gh_test_fallback")
        cfg = GitHubConfig()
        assert cfg.resolve_token() == "gh_test_fallback"


class TestOutputConfig:
    """Test output configuration."""

    def test_defaults(self) -> None:
        cfg = OutputConfig()
        assert cfg.format == "markdown"
        assert not cfg.auto_comment
        assert cfg.color


class TestAppConfig:
    """Test application configuration."""

    def test_defaults(self) -> None:
        cfg = AppConfig()
        assert cfg.provider == "anthropic"
        assert cfg.model == "claude-sonnet-4-20250514"

    def test_from_yaml(self, tmp_path: Path) -> None:
        config_file = tmp_path / ".ai-review-config.yaml"
        config_data = {
            "provider": "openai",
            "model": "gpt-4o",
            "github": {
                "auth_type": "pat",
            },
            "analysis": {
                "min_confidence": 0.8,
                "max_context_tokens": 8000,
            },
        }
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)

        cfg = AppConfig.from_yaml(config_file)
        assert cfg.provider == "openai"
        assert cfg.model == "gpt-4o"
        assert cfg.analysis.min_confidence == 0.8
        assert cfg.analysis.max_context_tokens == 8000

    def test_from_yaml_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            AppConfig.from_yaml("/nonexistent/config.yaml")

    def test_resolve_api_key(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        cfg = AppConfig()
        assert cfg.resolve_api_key() == "sk-test-key"

    def test_effective_max_context_tokens(self) -> None:
        # Anthropic: large limit
        cfg = AppConfig(provider="anthropic", analysis=AnalysisConfig(max_context_tokens=10000))
        assert cfg.effective_max_context_tokens == 10000

        # Local: capped by provider limit
        cfg_local = AppConfig(provider="local", analysis=AnalysisConfig(max_context_tokens=50000))
        assert cfg_local.effective_max_context_tokens <= 25600  # 80% of 32K

    def test_validate_no_api_key(self, monkeypatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        cfg = AppConfig()
        errors = cfg.validate()
        assert len(errors) >= 2  # missing API key + missing GitHub token

    def test_validate_invalid_provider(self) -> None:
        cfg = AppConfig(provider="invalid-provider")
        errors = cfg.validate()
        provider_errors = [e for e in errors if "provider" in e.lower()]
        assert len(provider_errors) >= 1

    def test_discover_and_load(self, tmp_path: Path, monkeypatch) -> None:
        """Test discovering config file by walking up directories."""
        config_file = tmp_path / ".ai-review-config.yaml"
        config_data = {"provider": "openai", "model": "gpt-4o"}
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)

        # Should find the config in tmp_path
        cfg = AppConfig.discover_and_load(str(tmp_path))
        assert cfg.provider == "openai"
        assert cfg.model == "gpt-4o"
