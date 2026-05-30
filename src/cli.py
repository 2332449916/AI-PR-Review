"""
ai-pr-reviewer 的 CLI 入口点。

提供 ``review`` 命令，该命令接受 PR URL 并编排完整的
分析流水线：获取 → 解析 → 上下文构建 → 分析 → 报告。

用法::

    ai-pr-reviewer review https://github.com/owner/repo/pull/42
    ai-pr-reviewer review https://github.com/owner/repo/pull/42 --output report.md
    ai-pr-reviewer review https://github.com/owner/repo/pull/42 --provider openai --model gpt-4o
    ai-pr-reviewer review https://github.com/owner/repo/pull/42 --auto-comment

设计原理：
- 使用 Click 而非 argparse，以支持更清晰的 CLI 定义、嵌套命令和自动生成帮助
- 每个 CLI 选项直接映射到 ``AppConfig`` 中的配置字段
- 基于 Rich 的控制台输出提供进度条和颜色编码的结果
- 基于 Tenacity 的重试透明地处理瞬态 API 故障
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

import click

from src import __version__
from src.cli_utils import (
    ProgressManager,
    console,
    error_console,
    print_error,
    print_findings_summary,
    print_header,
    print_severity_distribution,
    print_step,
    print_success,
    print_warning,
    retry_with_console,
    setup_rich_logging,
    _emoji,
)
from src.config import AppConfig
from src.github_client.fetcher import GitHubFetcher, parse_pr_url
from src.github_client.auth import PATAuth
from src.diff.parser import DiffParser
from src.context.builder import ContextBuilder
from src.llm.analyzer import LLMAnalyzer
from src.llm.providers.base import ProviderConfig
from src.llm.providers.anthropic_provider import AnthropicProvider
from src.llm.providers.openai_provider import OpenAIProvider
from src.llm.providers.local_provider import LocalProvider
from src.report.generator import ReportGenerator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider 工厂
# ---------------------------------------------------------------------------


def _create_provider(config: AppConfig):
    """根据应用配置创建 LLM provider。"""
    api_key = config.resolve_api_key() or ""
    provider_config = ProviderConfig(
        model=config.model,
        api_key=api_key,
        max_tokens=config.effective_max_context_tokens,
    )

    if config.provider == "anthropic":
        return AnthropicProvider(provider_config)
    elif config.provider == "openai":
        return OpenAIProvider(provider_config)
    elif config.provider == "local":
        return LocalProvider(provider_config)
    else:
        raise ValueError(
            f"Unsupported provider: {config.provider}. "
            f"Supported: anthropic, openai, local"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group(invoke_without_command=False)
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to YAML config file.",
)
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging.")
@click.version_option(version=__version__, prog_name="ai-pr-reviewer")
@click.pass_context
def cli(ctx: click.Context, config: str | None, verbose: bool) -> None:
    """AI 驱动的 PR 审查助手。

    使用 LLM 自动分析 GitHub PR，以识别 Bug、安全问题和改进机会。
    """
    setup_rich_logging(verbose)

    if config:
        cfg = AppConfig.from_yaml(config)
    else:
        cfg = AppConfig.discover_and_load()

    errors = cfg.validate()
    if errors:
        for err in errors:
            console.print(f"  {_emoji('⚠️', '[!]')} {err}", style="yellow")
        console.print("  Run with --verbose for details.", style="dim")

    ctx.ensure_object(dict)
    ctx.obj["config"] = cfg


@cli.command()
@click.argument("pr_url", type=str)
@click.option(
    "--output", "-o",
    type=click.Path(dir_okay=False),
    help="Output file path (.md or .json). Default: stdout.",
)
@click.option(
    "--format", "-f",
    type=click.Choice(["markdown", "json", "both"]),
    help="Output format (overrides config).",
)
@click.option(
    "--provider",
    type=click.Choice(["anthropic", "openai", "local"]),
    help="LLM provider (overrides config).",
)
@click.option(
    "--model",
    type=str,
    help="Model name (overrides config).",
)
@click.option(
    "--auto-comment",
    is_flag=True,
    help="Auto-post the review as a PR comment.",
)
@click.option(
    "--no-color",
    is_flag=True,
    help="Disable coloured output.",
)
@click.pass_context
def review(
    ctx: click.Context,
    pr_url: str,
    output: str | None,
    format: str | None,
    provider: str | None,
    model: str | None,
    auto_comment: bool,
    no_color: bool,
) -> None:
    """分析 GitHub Pull Request 并生成审查报告。

    PR_URL 应为 PR 的完整 URL，例如：

    \b
        https://github.com/owner/repo/pull/42
    """
    config: AppConfig = ctx.obj["config"]

    # CLI 覆盖配置
    if provider:
        config.provider = provider  # type: ignore[assignment]
    if model:
        config.model = model
    if format:
        config.output.format = format  # type: ignore[assignment]
    if auto_comment:
        config.output.auto_comment = True
    if no_color:
        config.output.color = False

    # 验证配置
    errors = config.validate()
    if errors:
        for err in errors:
            console.print(f"  {_emoji('⚠️', '[!]')} {err}", style="yellow")
        if not any("not set" in e for e in errors):
            sys.exit(1)

    # 运行分析
    try:
        report, pr_info, fetcher = asyncio.run(
            _run_analysis(pr_url, config)
        )
    except KeyboardInterrupt:
        console.print(f"\n{_emoji('⚠️', '[!]')} Analysis cancelled by user", style="yellow")
        sys.exit(130)
    except ValueError as exc:
        print_error(str(exc))
        sys.exit(1)
    except Exception as exc:
        print_error(f"Analysis failed: {exc}")
        logger.exception("Analysis failed")
        sys.exit(1)

    # 输出报告
    _output_report(report, pr_info, output, config, fetcher)


# ---------------------------------------------------------------------------
# 输出处理
# ---------------------------------------------------------------------------


def _output_report(report, pr_info, output: str | None, config: AppConfig, fetcher) -> None:
    """将分析报告渲染到 stdout、文件和/或 PR 评论。"""
    generator = ReportGenerator()

    # --- 写入文件（如果指定） ---
    if output:
        path = generator.save_report(report, output, pr_title=pr_info.title)
        print_success(f"Report saved to {path}")

    # --- 打印到 stdout ---
    if config.output.format == "json":
        import json as _json
        data = generator.generate_json(report)
        console.print(_json.dumps(data, indent=2, ensure_ascii=False))
    elif config.output.format != "json":
        markdown = generator.generate_markdown(report, pr_title=pr_info.title)
        console.print(markdown)

    # --- 发布 PR 评论 ---
    if config.output.auto_comment:
        comment = generator.generate_github_comment(report, pr_title=pr_info.title)
        success = retry_with_console(
            fetcher.post_comment,
            pr_info.repo_full_name,
            pr_info.pr_number,
            comment,
            label="Posting PR comment",
        )
        if success:
            print_success(
                f"Review comment posted on {pr_info.repo_full_name}#{pr_info.pr_number}"
            )
        else:
            print_warning("Failed to post review comment")

    # --- 摘要 ---
    if report.stats:
        duration = report.metadata.analysis_duration_seconds
        total = report.stats.get("total_findings", 0)
        by_severity = report.stats.get("by_severity", {})
        by_category = report.stats.get("by_category", {})

        print_findings_summary(by_severity, by_category, total)
        print_severity_distribution(total, duration)


# ---------------------------------------------------------------------------
# 流水线编排
# ---------------------------------------------------------------------------


async def _run_analysis(pr_url: str, config: AppConfig) -> tuple:
    """编排完整的分析流水线。

    返回:
        (AnalysisReport, PRInfo, GitHubFetcher) 的元组。
    """
    repo, pr_number = parse_pr_url(pr_url)
    console.print(f"\n🔍 Analysing [bold]{repo}#{pr_number}[/]...\n", style="cyan")

    # --- 认证配置 ---
    gh_token = config.github.resolve_token()
    if not gh_token:
        console.print(f"  {_emoji('⚠️', '[!]')} No GitHub token found. PR fetch will likely fail.", style="yellow")

    auth = PATAuth(token=gh_token) if gh_token else None
    fetcher = GitHubFetcher(auth_provider=auth, base_url=config.github.base_url)

    try:
        with ProgressManager() as pm:
            # --- 步骤 1: 获取 PR 信息 ---
            pm.add_task("📥 Fetching PR data", total=1)
            pr_info = retry_with_console(
                fetcher.fetch_pr_info, repo, pr_number,
                label="Fetching PR info",
            )
            pm.advance("📥 Fetching PR data")
            print_step(f"[bold]{pr_info.title}[/]")
            print_step(
                f"{pr_info.changed_files} files  |  Author: {pr_info.author}  "
                f"|  {pr_info.base_branch} ← {pr_info.head_branch}"
            )

            # --- 步骤 2: 获取 diff ---
            pm.add_task("📥 Fetching diff", total=1)
            pr_diff = retry_with_console(
                fetcher.fetch_diff, repo, pr_number,
                label="Fetching PR diff",
            )
            pm.advance("📥 Fetching diff")
            print_step(
                f"+{pr_diff.stats.total_additions}/-{pr_diff.stats.total_deletions} "
                f"in {pr_diff.stats.total_files} files"
            )

            # --- 步骤 3: 解析 diff ---
            pm.add_task("🔧 Parsing diff", total=1)
            parser = DiffParser()
            file_diffs = parser.parse(pr_diff.raw_diff)
            pm.advance("🔧 Parsing diff")
            print_step(f"Parsed {len(file_diffs)} file diffs")

            if not file_diffs:
                console.print("  ⚠️  Empty diff — nothing to analyse.", style="yellow")
                from src.llm.analyzer import AnalysisMetadata, AnalysisReport
                empty = AnalysisReport(
                    summary="No changes to analyse — the diff was empty.",
                    metadata=AnalysisMetadata(
                        model=config.model,
                        provider=config.provider,
                        timestamp=__import__("datetime").datetime.now().isoformat(),
                    ),
                )
                return empty, pr_info, fetcher

            # --- 步骤 4: 构建上下文 ---
            pm.add_task("📚 Building code context", total=1)
            context_builder = ContextBuilder(
                repo_full_name=repo,
                fetcher=fetcher,
                max_tokens_per_unit=config.analysis.max_context_tokens,
            )
            units = await context_builder.build_analysis_units(
                file_diffs,
                head_ref=pr_info.commit_sha or pr_info.head_branch,
                base_ref=pr_info.base_branch if config.analysis.enable_ast_context else None,
            )
            pm.advance("📚 Building code context")
            print_step(f"Created {len(units)} analysis unit{'s' if len(units) != 1 else ''}")

            if not units:
                console.print(
                    "  ⚠️  No files to analyse (all filtered by ignore rules).",
                    style="yellow",
                )
                from src.llm.analyzer import AnalysisMetadata, AnalysisReport
                empty = AnalysisReport(
                    summary="All changes were filtered by ignore rules.",
                    metadata=AnalysisMetadata(
                        model=config.model,
                        provider=config.provider,
                    ),
                )
                return empty, pr_info, fetcher

            # --- 步骤 5: LLM 分析 ---
            pm.add_task("🤖 Running LLM analysis", total=1)
            provider = _create_provider(config)
            analyzer = LLMAnalyzer(config=config, provider=provider)

            start = time.time()
            report = await analyzer.analyze_units(units)
            elapsed = time.time() - start

            pm.advance("🤖 Running LLM analysis")
            print_step(
                f"[bold]{len(report.findings)}[/] issue{'s' if len(report.findings) != 1 else ''} "
                f"found in {elapsed:.1f}s "
                f"(+{report.metadata.total_input_tokens:,} in / "
                f"{report.metadata.total_output_tokens:,} out)"
            )

        return report, pr_info, fetcher

    finally:
        fetcher.close()


# ---------------------------------------------------------------------------
# 入口点
# ---------------------------------------------------------------------------


def main() -> None:
    """"ai-pr-reviewer"" 控制台脚本的入口点。"""
    cli()


if __name__ == "__main__":
    main()
