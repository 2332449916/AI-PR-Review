# 🤖 AI-PR-Reviewer

demo 视频链接：  https://www.bilibili.com/video/BV1vFVS6YEQ1/?spm_id_from=333.1387.homepage.video_card.click&vd_source=896fc097ed1adade1c763684cc5d7870

> **AI 驱动的 PR 代码审查助手** — 自动分析 GitHub PR，利用大语言模型识别 Bug、安全漏洞、性能瓶颈和改进机会。

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-85%20passing-brightgreen.svg)]()

---

## 📋 目录

- [功能特性](#-功能特性)
- [快速开始](#-快速开始)
- [安装](#-安装)
- [使用](#-使用)
- [配置](#-配置)
- [输出格式](#-输出格式)
- [架构](#-架构)
- [示例](#-示例)
- [开发](#-开发)
- [常见问题](#-常见问题)

---

## ✨ 功能特性

| 功能 | 说明 |
|------|------|
| **🧠 AI 智能分析** | 使用 Claude、GPT-4o 或本地模型审查代码变更 |
| **🔍 多维审查** | 检测 Bug、安全漏洞（SQL注入、XSS）、性能问题、并发问题、错误处理缺失 |
| **📊 结构化报告** | Markdown 格式用于 PR 评论 + JSON 格式用于 CI/CD 集成 |
| **🎯 置信度评分** | 每个问题附带 0.0–1.0 的置信度分数，降低误报率 |
| **📋 增量分析** | 只分析新增/修改的代码行，不重复审查未变更代码 |
| **🔧 智能上下文** | 基于 AST 的上下文构建：自动拉取变更函数/类的定义 |
| **🚫 忽略规则** | 通过 `.ai-review-ignore` 文件排除特定路径或规则（类 gitignore 语法） |
| **🔌 多 Provider 支持** | 支持 Anthropic、OpenAI 及任何兼容 OpenAI 接口的本地模型 |
| **💬 自动评论** | 可选择将审查结果自动发布到 PR 评论区 |
| **⚡ 流式输出** | LLM 返回结果实时流式显示，无需等待完整响应 |

---

## 🚀 快速开始

```bash
# 安装
pip install ai-pr-reviewer

# 设置 API 密钥
export ANTHROPIC_API_KEY="sk-ant-..."
export GITHUB_TOKEN="ghp_..."

# 审查一个 PR
ai-pr-reviewer review https://github.com/owner/repo/pull/42

# 保存到文件
ai-pr-reviewer review https://github.com/owner/repo/pull/42 --output review.md

# 使用 GPT-4o
ai-pr-reviewer review https://github.com/owner/repo/pull/42 --provider openai --model gpt-4o
```

---

## 📦 安装

### 从源码安装

```bash
git clone https://github.com/pengxueqi616-commits/ai-pr-reviewer.git
cd ai-pr-reviewer
pip install -e .
```

### 依赖

- **Python**: 3.11+
- **关键包**: `click`, `PyGithub`, `httpx`, `rich`, `pydantic`, `unidiff`, `anthropic`, `openai`, `tenacity`

---

## 🔧 使用

### 基本用法

```bash
ai-pr-reviewer review https://github.com/owner/repo/pull/42
```

### 常用选项

```bash
# 保存输出到文件
ai-pr-reviewer review https://github.com/owner/repo/pull/42 -o report.md

# JSON 输出（便于 CI/CD 集成）
ai-pr-reviewer review https://github.com/owner/repo/pull/42 -f json

# 使用不同的 LLM 提供商
ai-pr-reviewer review https://github.com/owner/repo/pull/42 \
    --provider openai \
    --model gpt-4o

# 自动发布审查结果为 PR 评论
ai-pr-reviewer review https://github.com/owner/repo/pull/42 --auto-comment

# 使用自定义配置文件
ai-pr-reviewer review https://github.com/owner/repo/pull/42 -c .ai-review-config.yaml

# 开启详细日志（调试用）
ai-pr-reviewer review https://github.com/owner/repo/pull/42 -v
```

---

## ⚙️ 配置

### 方式一：配置文件（`.ai-review-config.yaml`）

放在仓库根目录，或用 `--config` 指定：

```yaml
provider: anthropic
model: claude-sonnet-4-20250514
api_key_env: ANTHROPIC_API_KEY

github:
  auth_type: pat
  token_env: GITHUB_TOKEN

analysis:
  min_confidence: 0.7        # 最低置信度阈值
  max_context_tokens: 6000   # 每个分析单元的最大 Token 数
  severity_threshold: minor   # 最低报告级别
  max_files: 50              # 最大分析文件数
  enable_ast_context: true   # 启用 AST 上下文分析
  enable_cross_file_analysis: true  # 启用跨文件分析

output:
  format: markdown           # 输出格式：markdown / json / both
  auto_comment: false        # 自动发布到 PR
  color: true                # 彩色输出
```

### 方式二：环境变量

| 变量 | 用途 |
|------|------|
| `ANTHROPIC_API_KEY` | Anthropic API 密钥 |
| `OPENAI_API_KEY` | OpenAI API 密钥 |
| `GITHUB_TOKEN` 或 `GH_TOKEN` | GitHub 个人访问令牌 |

### 方式三：命令行参数

所有配置都可以通过命令行覆盖：

```bash
ai-pr-reviewer review <pr-url> --provider openai --model gpt-4o --auto-comment
```

### 忽略规则（`.ai-review-ignore`）

在仓库根目录创建 `.ai-review-ignore` 文件，gitignore 风格的语法：

```gitignore
# 忽略生成的文件
*.generated.py
**/migrations/*
**/vendor/*

# 关闭特定规则的报告
rule:no-console-log
rule:style-preference

# 针对测试文件提高严重级别阈值
[threshold:major]
**/test/**
**/docs/**
```

---

## 📄 输出格式

### Markdown（默认）

适合 GitHub PR 评论的精美格式：

```markdown
# 🔍 AI PR Review: 修复登录竞态条件

## 📋 总结
本 PR 修复了登录处理中的竞态条件...

## 🔎 问题发现

### 🔴 **登录处理中的竞态条件** 🐛 (95% 置信度) — `src/auth.py:42-48`
...
```

### JSON（适合 CI/CD）

机器可读的结构化输出：

```json
{
  "version": "0.1.0",
  "summary": "本 PR 引入了...",
  "stats": {
    "total_findings": 3,
    "by_severity": {"critical": 1, "major": 1, "minor": 1}
  },
  "findings": [
    {
      "file_path": "src/auth.py",
      "line_start": 42,
      "severity": "critical",
      "category": "concurrency",
      "title": "登录处理中的竞态条件",
      "confidence": 0.95
    }
  ]
}
```

---

## 🏗 架构

```
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│   CLI    │──▶│  GitHub  │──▶│  Diff    │──▶│ Context  │
│  入口    │   │ 获取器   │   │ 解析器   │   │ 构建器   │
└──────────┘   └──────────┘   └──────────┘   └────┬─────┘
                                                  │
                                                  ▼
                                           ┌──────────┐
                                           │   LLM    │
                                           │ 分析引擎  │
                                           └────┬─────┘
                                                │
                                                ▼
                                         ┌──────────┐
                                         │  Report  │
                                         │ 生成器    │
                                         └──────────┘
```

### 核心模块

| 模块 | 职责 |
|------|------|
| `github_client/` | GitHub API：获取 PR 数据、Diff、文件内容，发布评论 |
| `diff/` | 解析 Unified Diff 为结构化的文件/代码块/行模型 |
| `context/` | 基于 AST 的代码分析、上下文组装、忽略规则引擎 |
| `llm/` | Prompt 模板、Provider 抽象层（Anthropic/OpenAI/本地）、分析引擎 |
| `report/` | Markdown + JSON 报告生成 |

### Token 优化策略

1. **分块**：Diff 按 Token 预算分块（每块约 6K tokens）
2. **优先级**：变更行 > 上下文 > 导入语句
3. **增量分析**：只完整分析新增和修改的代码行
4. **缓存**：AST 分析结果缓存避免重复解析

---

## 📊 示例

### 示例报告

参见 [`examples/report_sample.md`](examples/report_sample.md) 查看完整的报告样本。

### 测试夹具

[`tests/fixtures/`](tests/fixtures/) 目录包含示例 diff：

- `sample_diff_simple.txt` — 单文件 Python 变更
- `sample_diff_multifile.txt` — 多文件跨文件变更
- `sample_diff_security.txt` — 包含 SQL 注入和弱哈希安全问题

---

## 🛠 开发

```bash
# 克隆并安装开发依赖
git clone https://github.com/pengxueqi616-commits/ai-pr-reviewer.git
cd ai-pr-reviewer
pip install -e ".[dev]"

# 运行测试
pytest

# 运行测试并查看覆盖率
pytest --cov=src --cov-report=term-missing

# 代码检查
ruff check src/

# 类型检查
mypy src/
```

### 项目结构

```
ai-pr-reviewer/
├── src/
│   ├── cli.py                    # Click CLI 入口
│   ├── cli_utils.py              # Rich 进度条、重试、控制台助手
│   ├── config.py                 # YAML + 环境变量配置
│   ├── github_client/            # GitHub API 交互
│   ├── diff/                     # Diff 解析
│   ├── context/                  # AST 分析 + 上下文构建
│   ├── llm/                      # LLM 分析引擎 + Provider
│   └── report/                   # 报告生成
├── tests/
│   ├── fixtures/                 # 测试用的示例 diff
│   ├── test_diff_parser.py
│   ├── test_ast_walker.py
│   ├── test_config.py
│   ├── test_ignore.py
│   ├── test_analyzer.py
│   ├── test_prompts.py
│   ├── test_report_generator.py
│   ├── test_token_counter.py
│   └── test_github_fetcher.py
├── examples/
│   ├── report_sample.md
│   └── report_sample.json
├── DESIGN.md                     # 架构设计文档
├── DESIGN_RATIONALE.md           # 设计权衡与反思
├── .ai-review-config.yaml        # 默认配置
└── pyproject.toml                # 项目元数据
```

