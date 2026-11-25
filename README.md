<div align="center">

# Agent Context Bus

**Stop re-explaining your project every time you switch AI coding agents.**

Local-first, **0-token** real-time memory & project context bus across **Claude Code**, **Codex CLI**, **Antigravity**, and **WorkBuddy**.

[![License: MIT](https://img.shields.io/badge/License-MIT-34d399.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-38bdf8.svg)](https://www.python.org/)
[![Zero Extra Tokens](https://img.shields.io/badge/Live_WAL-0_Extra_Tokens-a78bfa.svg)](#how-it-works)
[![Stop Hooks](https://img.shields.io/badge/Stop_Hooks-%3C50ms_Non--blocking-fbbf24.svg)](#real-time-0-token-wal--stop-hooks)

**English** | [简体中文](README_CN.md)

</div>

---

<p align="center">
  <img src="docs/assets/hero-demo.png" alt="Agent Context Bus — cross-agent timeline, live WAL, and verbatim session search" width="100%" />
</p>

## Why Agent Context Bus?

If you bounce between **Claude Code**, **Codex**, **Antigravity**, and **WorkBuddy**, every tool keeps its own siloed history. Switching agents mid-task means re-explaining where your repo lives, which config file you just edited, and what broke ten minutes ago.

**Agent Context Bus** stitches your local AI sessions and Git commits into one lightweight, **local-first**, **zero-dependency** memory layer—without running a vector database, without uploading private code, and without injecting bloated prompts into every new chat.

- **0 Extra LLM Tokens in Real Time**: Official `Stop` hooks (`Claude Code`, `Codex`, `Antigravity`) fire asynchronously in **<50ms** and write a same-day WAL (`data/live/live_YYYY-MM-DD.json`) by reusing native summaries and local Git logs.
- **No `SessionStart` Prompt Bloat**: Never injects walls of stale memory into casual chats. Agents only drill down when you actually ask *"where did we leave off?"*
- **Stable Project Identities & Physical Paths**: Map messy session names and aliases to one canonical `project_id`, and pin physical directories & key config files (`--add-path`) so any agent finds them immediately.
- **Verified Provenance & Verbatim Recall**: Every timeline entry cites exact session IDs and commit hashes; drop down to raw local transcripts (`search_sessions.py`) for exact error messages or commands.
- **Pure Local Python Standard Library**: Onboarding, real-time WAL, hooks, CLI queries, and verbatim search run on stock **Python 3.10+** with **zero pip packages** and **no API keys**.

---

## 30-Second Quick Start

```bash
git clone https://github.com/SiLeDeAnSha/agent-context-bus.git
cd agent-context-bus

# 1. Detect local Claude Code / Codex / Antigravity / WorkBuddy logs & import last 7 days (no API key, no DB)
python3 onboarding.py

# 2. Query any project timeline + live WAL + registered paths
python3 query_memory.py --project "<project name or alias>"

# 3. Search verbatim transcripts across all local AI clients
python3 search_sessions.py --keyword "ECONNRESET" --limit 5
```

> **Windows PowerShell**: use `python` instead of `python3`. Reading compressed Codex `.jsonl.zst` archives requires Python 3.14+ or `pip install zstandard`.

---

## Supported Clients

| Client | Native Session Collection | Real-Time 0-Token WAL | Official `Stop` Hook | Verbatim Search (`search_sessions.py`) | Global Rule Sync |
|---|:---:|:---:|:---:|:---:|:---:|
| **Claude Code** (`~/.claude/`) | ✅ | ✅ | ✅ `settings.json` | ✅ | ✅ `CLAUDE.md` |
| **Codex CLI** (`~/.codex/`) | ✅ | ✅ | ✅ `hooks.json` | ✅ | ✅ `AGENTS.md` |
| **Antigravity** (`~/.gemini/antigravity/`) | ✅ | ✅ | ✅ `hooks.json` | ✅ | ✅ `GEMINI.md` |
| **WorkBuddy** (`~/.workbuddy/`) | ✅ | ✅ | 🟡 File scan | ✅ | ✅ `USER.md` |
| **Local Git Repos** | ✅ | ✅ | 🟡 Auto-scanned | — | — |

---

## How It Works

<p align="center">
  <img src="docs/assets/architecture.png" alt="Agent Context Bus 4-layer architecture" width="100%" />
</p>

### 4-Layer Progressive Recall

| Layer | File / Command | Size / Cost | When to Use |
|---|---|---|---|
| **L0** | Global pointer (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, `USER.md`) | ~150 tokens | Tells any agent where the bus lives |
| **L1** | `MEMORY_INDEX.md` | ~18 KB flat Markdown | Instant overview: today's live WAL, active projects, physical paths |
| **L2** | `query_memory.py` (`--project`, `--keyword`, `--date`, `--raw`) | 0 tokens, <50ms | Full project timeline fused with today's live WAL & verified citations |
| **L3** | `search_sessions.py` (`--keyword`, `--client`, `--session-id`) | 0 tokens | Exact grep over raw local transcripts with `file:line` provenance |

---

## Real-Time 0-Token WAL & Stop Hooks

Enable automatic background capture across **Claude Code**, **Codex**, and **Antigravity** with one idempotent command:

```bash
# Preview status without modifying anything
python3 skills/daily-memory/scripts/sync_global_rules.py --check

# Sync global memory pointer rules, skill symlinks, and non-blocking Stop hooks
python3 skills/daily-memory/scripts/sync_global_rules.py --sync-skills --sync-hooks

# Disable/remove hooks cleanly at any time
python3 skills/daily-memory/scripts/sync_global_rules.py --disable-hooks
```

**Safety guarantees of `hook_bridge.py`**:
- Spawns `live_memory.py` detached (`start_new_session=True`) and exits `0` with `{}` in **<50ms**—never stalls or breaks your agent loop.
- Uses file locking + 12-second debounce so rapid tool turns don't cause disk churn.
- Once a workday is compacted into `summary_all_history.json`, its live WAL section automatically exits the top of `MEMORY_INDEX.md` to keep the index compact.

### Day-to-Day CLI Cheatsheet

```bash
# Record an immediate decision or handoff note (auto-refreshes MEMORY_INDEX.md)
python3 query_memory.py --note "payments-api" "Switched webhook verifier to HMAC-SHA256" \
  --note-path ~/dev/payments-api ~/dev/payments-api/config/stripe.yaml

# Force a 0-token live scan of today's 4 clients + Git commits
python3 query_memory.py --live-sync

# Pin, remove, or replace physical paths & key config files for a project
python3 query_memory.py --add-path "payments-api" "~/dev/payments-api" "~/dev/payments-api/.env.example"
python3 query_memory.py --remove-path "payments-api" "~/dev/old-payments"

# Review AI merge suggestions, merge aliases, or split a misgrouped alias
python3 query_memory.py --project-review
python3 query_memory.py --merge-projects "payments-api" "stripe-billing" "webhook-worker"
python3 query_memory.py --split-project "webhook-worker"

# Verbatim drill-down across raw local transcripts
python3 search_sessions.py --keyword "idempotency" --client claude --limit 5
```

---

## Optional Extensions

Everything above works out of the box on a single laptop with zero dependencies. When you need more, enable optional modules on demand:

<details>
<summary><strong>1. Multi-Machine NAS Hub &amp; Remote Failover</strong> (Mac + Windows + Linux)</summary>

Run the lightweight Docker hub (`server/`) on a home NAS or VPS to merge traces from multiple machines without conflicts:

- Clients automatically route in priority order: **LAN → Tailscale → IPv6 HTTPS → IPv4 HTTPS → Offline Local Cache**.
- Push daily traces from each machine via `python3 collect.py --yesterday --push --machine win-desktop`.
- Includes optional Bearer-authenticated HTTP API & Remote MCP endpoint.
- See [`deploy/NAS_DEPLOYMENT.md`](deploy/NAS_DEPLOYMENT.md) for Docker Compose, `frpc`, and scheduled collector setup scripts (`setup_mac_collector.sh` / `setup_windows_collector.ps1`).

</details>

<details>
<summary><strong>2. Daily Compaction, Dual Excel Workbooks &amp; Sanitized Weekly Reports</strong></summary>

Install `pip install -r requirements.txt` (`openpyxl`) and copy `config.example.json` → `config.json` if you also want human-readable spreadsheets:

- **Strict Work / Personal Separation**: generates separate Work and Personal `.xlsx` workbooks plus monthly archive folders (`YYYY-MM/`).
- **Verified Provenance**: `summarizer.py` / `write_excel.py` validates every `session` / `commit` / `file` citation against the day's raw archive.
- **Sanitized External Reports**: `python3 export_report.py --days 7` exports a redacted Markdown weekly report with local paths and private items stripped.

</details>

---

## Privacy & Security

- **100% Local-First**: Your session histories, project registry, and live WAL stay on your machine (`data/` is gitignored).
- **No Telemetry**: Zero outbound network calls unless you explicitly configure a self-hosted remote NAS endpoint or optional LLM summarizer API.
- **Open & Auditable**: Standard-library Python scripts with full unit test coverage:

```bash
python3 -m unittest -v test_daily_summary.py
```

---

## License

[MIT](LICENSE) © 2026 SiLeDeAnSha
