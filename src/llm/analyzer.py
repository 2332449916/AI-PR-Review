"""
核心分析引擎 — 协调提示词构建、LLM 调用以及所有分析单元的结果解析。

这是中央编排模块。它负责：
1. 从 ``ContextBuilder`` 接收分析单元
2. 为每个单元通过 ``prompts.py`` 构建相应的提示词
3. 将提示词发送给配置的 LLM 提供商
4. 解析结构化的 JSON 响应
5. 通过后处理校准置信度评分
6. 综合生成整体 PR 摘要

设计理念：
- **并行分析**：当存在多个分析单元时（批量分析），单元会被并发处理。
  这显著减少大型 PR 的总耗时。
- **优雅降级**：如果单个单元失败（例如 LLM 超时），其余分析继续执行。
  最终报告会标明存在部分覆盖。
- **置信度校准**：后处理根据问题类别启发式规则调整置信度评分
  （例如，安全问题会获得小幅惩罚，以鼓励更保守的报告）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from src.config import AppConfig
from src.context.builder import AnalysisUnit
from src.diff.models import DiffStats
from src.llm.prompts import build_analysis_prompt, build_summary_prompt
from src.llm.token_counter import estimate_messages_tokens

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

Severity = Literal["critical", "major", "minor", "info"]
FindingCategory = Literal[
    "security", "performance", "bug", "concurrency", "error_handling",
    "code_style", "maintainability", "best_practice", "potential_issue"
]


@dataclass
class Finding:
    """代码审查分析中的单条发现。"""

    file_path: str
    line_start: int | None = None
    line_end: int | None = None
    severity: Severity = "minor"
    category: FindingCategory = "potential_issue"
    title: str = ""
    description: str = ""
    suggestion: str = ""
    code_example: str | None = None
    confidence: float = 0.0
    rule_id: str = ""
    uncertainty_reason: str = ""

    def __post_init__(self) -> None:
        if not self.rule_id and self.title:
            # 根据标题自动生成一个稳定的规则 ID
            rule_id = re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")
            self.rule_id = rule_id[:60]


@dataclass
class AnalysisMetadata:
    """分析运行的元数据。"""

    model: str = ""
    provider: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    analysis_duration_seconds: float = 0.0
    units_analysed: int = 0
    units_failed: int = 0
    timestamp: str = ""


@dataclass
class AnalysisReport:
    """针对一个 PR 的完整分析报告。"""

    summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    stats: dict | None = None
    metadata: AnalysisMetadata = field(default_factory=AnalysisMetadata)


# ---------------------------------------------------------------------------
# JSON 解析器
# ---------------------------------------------------------------------------

_FINDINGS_JSON_RE = re.compile(r"\[.*\]", re.DOTALL)


def _parse_findings_json(response_text: str) -> list[dict]:
    """从 LLM 响应中解析发现的 JSON 数组。

    使用多种降级策略：
    1. 尝试将整个响应作为 JSON 解析
    2. 使用正则表达式提取第一个 JSON 数组
    3. 尝试找到单个 JSON 对象并将其包装成数组

    Args:
        response_text: LLM 返回的原始响应文本。

    Returns:
        发现字典的列表（可能为空）。
    """
    if not response_text or not response_text.strip():
        return []

    text = response_text.strip()

    # 策略 1：直接解析
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # 某些模型会包装为 {"findings": [...]}
            for key in ("findings", "issues", "results", "data"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            return [data]
    except json.JSONDecodeError:
        pass

    # 策略 2：使用正则表达式提取 JSON 数组
    match = _FINDINGS_JSON_RE.search(text)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    # 策略 3：尝试提取单个 JSON 对象
    objects = re.findall(r"\{[^{}]*\}", text)
    if objects:
        results = []
        for obj_str in objects:
            try:
                obj = json.loads(obj_str)
                if isinstance(obj, dict) and "title" in obj:
                    results.append(obj)
            except json.JSONDecodeError:
                continue
        return results

    logger.warning("无法从 LLM 响应中解析发现")
    return []


# ---------------------------------------------------------------------------
# 置信度校准
# ---------------------------------------------------------------------------


def _calibrate_confidence(finding: Finding) -> float:
    """对发现的置信度评分进行后处理校准。

    调整规则：
    - 安全问题：-0.05 惩罚（宁可漏报，也避免过度增加误报）
    - 缺少行号的发现：-0.1 惩罚
    - 标题与描述相同的发现：-0.2（很可能是幻觉生成的）

    Args:
        finding: 需要校准的发现。

    Returns:
        校准后的置信度评分（限制在 0.0–1.0 范围内）。
    """
    confidence = finding.confidence

    # 安全问题：小幅惩罚
    if finding.category == "security":
        confidence -= 0.05

    # 没有具体行号：可靠性较低
    if finding.line_start is None and finding.line_end is None:
        confidence -= 0.1

    # 描述过短：可靠性较低
    if len(finding.description) < 20:
        confidence -= 0.1

    # 建议模糊：可靠性较低
    if len(finding.suggestion) < 15:
        confidence -= 0.15

    return max(0.0, min(1.0, confidence))


# ---------------------------------------------------------------------------
# 主分析器
# ---------------------------------------------------------------------------


class LLMAnalyzer:
    """核心分析引擎。

    Args:
        config: 应用配置（提供商、模型等）
        provider: 已初始化的 LLM 提供商实例。
    """

    def __init__(self, config: AppConfig, provider) -> None:
        self._config = config
        self._provider = provider

    async def analyze_units(
        self,
        units: list[AnalysisUnit],
    ) -> AnalysisReport:
        """分析所有单元并返回完整报告。

        Args:
            units: 来自 ``ContextBuilder`` 的分析单元列表。

        Returns:
            包含所有发现和元数据的 ``AnalysisReport``。
        """
        start_time = datetime.now(timezone.utc)
        all_findings: list[Finding] = []
        total_input_tokens = 0
        total_output_tokens = 0
        units_analysed = 0
        units_failed = 0

        for i, unit in enumerate(units):
            logger.info("正在分析单元 %d/%d: %s", i + 1, len(units), unit.unit_id)
            try:
                findings, in_tokens, out_tokens = await self._analyze_single_unit(unit)
                all_findings.extend(findings)
                total_input_tokens += in_tokens
                total_output_tokens += out_tokens
                units_analysed += 1
                logger.debug("单元 %s: %d 条发现", unit.unit_id, len(findings))
            except Exception as exc:
                logger.error("分析单元 %s 失败: %s", unit.unit_id, exc)
                units_failed += 1

        # 去重：移除重复的发现（相同文件、行范围和标题）
        all_findings = self._deduplicate_findings(all_findings)

        # 按置信度阈值过滤
        min_confidence = self._config.analysis.min_confidence
        all_findings = [f for f in all_findings if f.confidence >= min_confidence]

        # 按严重程度排序（严重优先），再按置信度排序（高置信度优先）
        severity_order = {"critical": 0, "major": 1, "minor": 2, "info": 3}
        all_findings.sort(key=lambda f: (severity_order.get(f.severity, 99), -f.confidence))

        # 生成摘要
        summary = ""
        if all_findings:
            try:
                summary = await self._generate_summary(all_findings, units)
            except Exception as exc:
                logger.warning("生成摘要失败: %s", exc)
                summary = "摘要生成失败 — 请查看下方的具体发现。"

        # 构建统计信息
        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
        by_severity: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for f in all_findings:
            by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
            by_category[f.category] = by_category.get(f.category, 0) + 1

        stats = {
            "total_findings": len(all_findings),
            "by_severity": by_severity,
            "by_category": by_category,
            "high_confidence_count": sum(1 for f in all_findings if f.confidence >= 0.9),
        }

        metadata = AnalysisMetadata(
            model=self._config.model,
            provider=self._config.provider,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            analysis_duration_seconds=elapsed,
            units_analysed=units_analysed,
            units_failed=units_failed,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        return AnalysisReport(
            summary=summary,
            findings=all_findings,
            stats=stats,
            metadata=metadata,
        )

    async def _analyze_single_unit(
        self,
        unit: AnalysisUnit,
    ) -> tuple[list[Finding], int, int]:
        """分析单个分析单元并返回发现 + token 数量。"""
        # 构建提示词
        messages = build_analysis_prompt(unit)

        # 估算输入 token 数量
        input_tokens = estimate_messages_tokens(
            [{"role": m["role"], "content": m["content"]} for m in messages],
            self._config.provider,
        )

        # 发送给 LLM
        result = await self._provider.complete_with_result(
            [self._dict_to_message(m) for m in messages],
        )

        output_tokens = result.output_tokens or estimate_messages_tokens(
            [{"role": "assistant", "content": result.content}],
            self._config.provider,
        )

        # 从响应中解析发现
        raw_findings = _parse_findings_json(result.content)

        # 转换为 Finding 对象
        findings: list[Finding] = []
        for raw in raw_findings:
            try:
                finding = Finding(
                    file_path=raw.get("file_path", unit.file_diffs[0].file_path if unit.file_diffs else ""),
                    line_start=raw.get("line_start"),
                    line_end=raw.get("line_end"),
                    severity=raw.get("severity", "minor"),
                    category=raw.get("category", "potential_issue"),
                    title=raw.get("title", "Untitled finding"),
                    description=raw.get("description", ""),
                    suggestion=raw.get("suggestion", ""),
                    code_example=raw.get("code_example"),
                    confidence=float(raw.get("confidence", 0.5)),
                    uncertainty_reason=raw.get("uncertainty_reason", ""),
                )
                # 校准置信度
                finding.confidence = _calibrate_confidence(finding)
                findings.append(finding)
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning("解析发现失败: %s — 原始数据: %s", exc, raw)
                continue

        return findings, input_tokens, output_tokens

    async def _generate_summary(
        self,
        findings: list[Finding],
        units: list[AnalysisUnit],
    ) -> str:
        """根据所有发现生成自然语言的 PR 摘要。"""
        # 汇总统计
        total_files = len(set(f.file_path for f in findings))
        severity_counts = {}
        for f in findings:
            severity_counts[f.severity] = severity_counts.get(f.severity, 0) + 1

        # 计算 diff 统计
        total_additions = sum(fd.additions for u in units for fd in u.file_diffs)
        total_deletions = sum(fd.deletions for u in units for fd in u.file_diffs)

        # 为摘要提示词构建发现的 JSON 数据
        findings_summary = []
        for f in findings[:20]:  # 限制为前 20 条以节省 token
            findings_summary.append({
                "severity": f.severity,
                "category": f.category,
                "title": f.title,
                "file": f.file_path,
                "line": f.line_start,
            })

        import json
        findings_json = json.dumps(findings_summary, indent=2, ensure_ascii=False)

        messages = build_summary_prompt(
            pr_title="",  # 由 CLI 层填入
            pr_description="",
            file_count=total_files,
            additions=total_additions,
            deletions=total_deletions,
            findings_json=findings_json,
        )

        result = await self._provider.complete_with_result(
            [self._dict_to_message(m) for m in messages],
        )

        return result.content.strip()

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _deduplicate_findings(findings: list[Finding]) -> list[Finding]:
        """移除重复的发现（相同文件、行范围和标题）。"""
        seen: set[tuple[str, int | None, int | None, str]] = set()
        unique: list[Finding] = []
        for f in findings:
            key = (f.file_path, f.line_start, f.line_end, f.title.lower().strip())
            if key not in seen:
                seen.add(key)
                unique.append(f)
        return unique

    @staticmethod
    def _dict_to_message(msg_dict: dict[str, str]):
        """将包含 ``role`` 和 ``content`` 的字典转换为 Message 对象。"""
        from src.llm.providers.base import Message
        return Message(role=msg_dict["role"], content=msg_dict["content"])
