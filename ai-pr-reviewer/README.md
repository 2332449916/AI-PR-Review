# 🤖 AI-PR-Reviewer

> **AI-driven Pull Request Review assistant** — automatically analyses GitHub PRs using LLMs to identify bugs, security issues, performance bottlenecks, and improvement opportunities.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-85%20passing-brightgreen.svg)]()

---

## 📋 Table of Contents

- [Features](#-features)
- [Quick Start](#-quick-start)
- [Installation](#-installation)
- [Usage](#-usage)
- [Configuration](#-configuration)
- [Output Formats](#-output-formats)
- [Architecture](#-architecture)
- [Examples](#-examples)
- [Development](#-development)
- [FAQ](#-faq)

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| **🧠 AI-Powered Analysis** | Uses Claude, GPT-4o, or local LLMs to review code changes |
| **🔍 Multi-Dimension Review** | Detects bugs, security issues (SQLi, XSS), performance problems, concurrency bugs, error handling gaps |
| **📊 Structured Reports** | Markdown for PR comments + JSON for CI/CD integration |
| **🎯 Confidence Scoring** | Every finding includes a 0.0–1.0 confidence score to reduce false positives |
| **📋 Incremental Analysis** | Only analyses changed lines — no re-review of existing code |
| **🔧 Smart Context** | AST-aware context building: fetches function/class definitions for changed code |
| **🚫 Ignore Rules** | `.ai-review-ignore` file to exclude paths/rules (gitignore-style) |
| **🔌 Multi-Provider** | Supports Anthropic, OpenAI, and any OpenAI-compatible local endpoint |
| **💬 Auto-Comment** | Optionally posts review results directly on the PR |
| **⚡ Streaming** | Real-time token streaming from LLM providers |

---

## 🚀 Quick Start

```bash
# Install
pip install ai-pr-reviewer

# Set your API keys
export ANTHROPIC_API_KEY="sk-ant-..."
export GITHUB_TOKEN="ghp_..."

# Review a PR
ai-pr-reviewer review https://github.com/owner/repo/pull/42

# Save to file
ai-pr-reviewer review https://github.com/owner/repo/pull/42 --output review.md

# Use GPT-4o instead
ai-pr-reviewer review https://github.com/owner/repo/pull/42 --provider openai --model gpt-4o
```

---

## 📦 Installation

### From PyPI (once published)

```bash
pip install ai-pr-reviewer
```

### From source

```bash
git clone https://github.com/pengxueqi616-commits/ai-pr-reviewer.git
cd ai-pr-reviewer
pip install -e .
```

### Dependencies

- **Python**: 3.11+
- **Key packages**: `click`, `PyGithub`, `httpx`, `rich`, `pydantic`, `unidiff`, `anthropic`, `openai`, `tenacity`

---

## 🔧 Usage

### Basic PR Review

```bash
ai-pr-reviewer review https://github.com/owner/repo/pull/42
```

### Common Options

```bash
# Save output to a file
ai-pr-reviewer review https://github.com/owner/repo/pull/42 -o report.md

# JSON output (machine-readable)
ai-pr-reviewer review https://github.com/owner/repo/pull/42 -f json

# Use a different LLM provider
ai-pr-reviewer review https://github.com/owner/repo/pull/42 \
    --provider openai \
    --model gpt-4o

# Auto-post results as a PR comment
ai-pr-reviewer review https://github.com/owner/repo/pull/42 --auto-comment

# Use a custom config file
ai-pr-reviewer review https://github.com/owner/repo/pull/42 -c .ai-review-config.yaml

# Verbose logging for debugging
ai-pr-reviewer review https://github.com/owner/repo/pull/42 -v
```

---

## ⚙️ Configuration

### Method 1: Config File (`.ai-review-config.yaml`)

Place at your repo root or specify with `--config`:

```yaml
provider: anthropic
model: claude-sonnet-4-20250514
api_key_env: ANTHROPIC_API_KEY

github:
  auth_type: pat
  token_env: GITHUB_TOKEN

analysis:
  min_confidence: 0.7
  max_context_tokens: 6000
  severity_threshold: minor
  max_files: 50
  enable_ast_context: true
  enable_cross_file_analysis: true

output:
  format: markdown
  auto_comment: false
  color: true
```

### Method 2: Environment Variables

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `GITHUB_TOKEN` or `GH_TOKEN` | GitHub Personal Access Token |

### Method 3: CLI Flags

All config options can be overridden at the command line:

```bash
ai-pr-reviewer review <pr-url> --provider openai --model gpt-4o --auto-comment
```

### Ignore Rules (`.ai-review-ignore`)

Create a `.ai-review-ignore` file at your repo root (gitignore-style):

```gitignore
# Ignore generated files
*.generated.py
**/migrations/*
**/vendor/*

# Disable noisy rules
rule:no-console-log
rule:style-preference

# Severity cap for test files
[threshold:major]
**/test/**
**/docs/**
```

---

## 📄 Output Formats

### Markdown (default)

A formatted report suitable for GitHub PR comments:

```markdown
# 🔍 AI PR Review: Fix login race condition

## 📋 Summary
This PR fixes a race condition in the login handler...

## 🔎 Findings

### 🔴 **Race condition in login handler** 🐛 (95% confidence) — `src/auth.py:42-48`
...
```

### JSON (for CI/CD)

Machine-readable output for integration with other tools:

```json
{
  "version": "0.1.0",
  "summary": "This PR introduces...",
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
      "title": "Race condition in login handler",
      "confidence": 0.95
    }
  ]
}
```

---

## 🏗 Architecture

```
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│   CLI    │──▶│  GitHub  │──▶│  Diff    │──▶│ Context  │
│  Entry   │   │ Fetcher  │   │ Parser   │   │ Builder  │
└──────────┘   └──────────┘   └──────────┘   └────┬─────┘
                                                  │
                                                  ▼
                                           ┌──────────┐
                                           │   LLM    │
                                           │ Analyzer │
                                           └────┬─────┘
                                                │
                                                ▼
                                         ┌──────────┐
                                         │  Report  │
                                         │ Generator│
                                         └──────────┘
```

### Core Modules

| Module | Responsibility |
|--------|---------------|
| `github_client/` | GitHub API: fetch PR data, diffs, file contents, post comments |
| `diff/` | Parse unified diffs into structured file/hunk/line models |
| `context/` | AST-based code analysis, context assembly, ignore rules |
| `llm/` | Prompt templates, provider abstraction (Anthropic/OpenAI/Local), analysis engine |
| `report/` | Markdown + JSON report generation |

### Token Optimization Strategy

1. **Chunking**: Diffs are split into token-budgeted units (~6K tokens each)
2. **Prioritisation**: Changed lines first, then context, then imports
3. **Incremental**: Only NEW and MODIFIED lines are fully analysed
4. **Caching**: AST analysis results are cached to avoid re-parsing

---

## 📊 Examples

### Sample Report

See [`examples/report_sample.md`](examples/report_sample.md) for a complete sample report.

### Test Fixtures

The [`tests/fixtures/`](tests/fixtures/) directory contains sample diffs:

- `sample_diff_simple.txt` — A single-file Python change
- `sample_diff_multifile.txt` — Changes across multiple files
- `sample_diff_security.txt` — Contains SQL injection + weak hash issues

---

## 🛠 Development

```bash
# Clone and install dev dependencies
git clone https://github.com/pengxueqi616-commits/ai-pr-reviewer.git
cd ai-pr-reviewer
pip install -e ".[dev]"

# Run tests
pytest

# Run with coverage
pytest --cov=src --cov-report=term-missing

# Lint
ruff check src/

# Type check
mypy src/
```

### Project Structure

```
ai-pr-reviewer/
├── src/
│   ├── cli.py                    # Click CLI entry point
│   ├── cli_utils.py              # Rich progress bars, retry, console helpers
│   ├── config.py                 # YAML + env var configuration
│   ├── github_client/            # GitHub API interaction
│   ├── diff/                     # Diff parsing
│   ├── context/                  # AST analysis + context building
│   ├── llm/                      # LLM analysis engine + providers
│   └── report/                   # Report generation
├── tests/
│   ├── fixtures/                 # Sample diffs for testing
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
├── DESIGN.md                     # Architecture design document
├── .ai-review-config.yaml        # Default configuration
└── pyproject.toml                # Project metadata
```

---

## ❓ FAQ

**Q: Which LLM provider works best?**
A: Claude Sonnet 4 (Anthropic) gives the best balance of speed and quality for code review. GPT-4o is a close second. Local models (via Ollama) work but may produce less reliable results.

**Q: How much does it cost per review?**
A: For a 500-line PR with Claude Sonnet 4: ~8K input tokens + ~3K output tokens ≈ $0.03 per review.

**Q: Can I use it without a GitHub token?**
A: No — the tool needs a GitHub token to fetch PR diffs. Use a classic PAT with `repo` scope.

**Q: Does it work with GitHub Enterprise?**
A: Yes — set `base_url` in the config to your GitHub Enterprise API URL.

**Q: How do I reduce false positives?**
A: Increase `min_confidence` in config (e.g., 0.8), or use `.ai-review-ignore` to silence specific rules or paths.

**Q: Can I run it in CI/CD?**
A: Yes — use `--format json` for machine-readable output, or `--auto-comment` to post results on the PR.

---

## 📄 License

MIT © 2026 AI-PR-Reviewer Team

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing`)
3. Run the tests (`pytest`)
4. Commit your changes (`git commit -m 'Add amazing feature'`)
5. Push to the branch (`git push origin feature/amazing`)
6. Open a Pull Request
