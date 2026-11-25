#!/usr/bin/env python3
"""Idempotently sync the DAILY-SUMMARY global memory pointer block and skill symlinks across AI clients."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

START_MARKER = "<!-- DAILY-SUMMARY START -->"
END_MARKER = "<!-- DAILY-SUMMARY END -->"
MARKER_RE = re.compile(
    re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
    re.DOTALL,
)

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_NAME = os.path.basename(SKILL_DIR)

CANONICAL_BLOCK = f"""{START_MARKER}
# Agent Context Bus · 跨端 AI 记忆中枢

When the user asks to locate past work, projects, files, or previous Antigravity/Codex/WorkBuddy/T3 Code sessions (e.g., "找一下我前几天弄的…", "接着之前的…继续", "刷新今天/补跑某天汇总", or "导出对外日报/周报"), use the local memory index first (avoid parsing `.xlsx` unless editing/verifying Excel output):

- **AI memory entry & current-state snapshot (read first)**: `~/Documents/DailyReport/MEMORY_INDEX.md` (auto-refreshed by `write_excel.py`, includes registered project paths) and `~/Documents/DailyReport/README.md`
- **Fast on-demand query, provenance & project-identity CLI (zero-dep `python3`)**: `python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py` (`--project`, `--keyword`, `--days`, `--scope work|personal|life`, `--date`, `--raw`, `--project-review`, `--merge-projects`, `--split-project`, `--add-path`, `--remove-path`, `--set-paths`, `--rebuild-index`)
- **Raw local session snippet search**: `python3 ~/.gemini/antigravity/scratch/daily-summary/search_sessions.py` (`--keyword`, `--session-id`, `--client antigravity|codex|workbuddy|t3`, `--date`, `--from`, `--to`, `--json`)
- Official work & personal Excel views (human-facing): `~/Documents/DailyReport/每日工作汇总.xlsx`, `~/Documents/DailyReport/个人技术与折腾汇总.xlsx`, monthly archives `~/Documents/DailyReport/<YYYY-MM>/`, and sanitized external reports `~/Documents/DailyReport/对外报告/` (never use external reports as full source of truth)
- Underlying JSON indexes & pipeline scripts: `~/.gemini/antigravity/scratch/daily-summary/` (`data/summary_all_history.json`, `data/months/summary_<YYYY-MM>.json`, `data/raw/<YYYY-MM>/raw_*.json`, `data/project_registry.json`, `onboarding.py`, and `.venv/bin/python` with `collect.py`, `write_excel.py`, `export_report.py`)
{END_MARKER}"""

DEFAULT_RULE_TARGETS: dict[str, str] = {
    "antigravity": os.path.expanduser("~/.gemini/GEMINI.md"),
    "codex": os.path.expanduser("~/.codex/AGENTS.md"),
    "claude": os.path.expanduser("~/.claude/CLAUDE.md"),
    "workbuddy": os.path.expanduser("~/.workbuddy/USER.md"),
}

DEFAULT_SKILL_LINK_DIRS: dict[str, str] = {
    "antigravity": os.path.expanduser("~/.gemini/config/skills"),
    "codex": os.path.expanduser("~/.codex/skills"),
    "claude": os.path.expanduser("~/.claude/skills"),
    "workbuddy": os.path.expanduser("~/.workbuddy/skills"),
}


def compute_synced_content(existing: str, block: str = CANONICAL_BLOCK) -> tuple[str, str]:
    """Return (new_content, status) where status is 'unchanged', 'updated', or 'appended'."""
    norm_existing = existing.replace("\r\n", "\n")
    m = MARKER_RE.search(norm_existing)
    if m:
        if m.group(0) == block:
            return existing, "unchanged"
        replaced = norm_existing[: m.start()] + block + norm_existing[m.end() :]
        if not replaced.endswith("\n"):
            replaced += "\n"
        return replaced, "updated"

    stripped = norm_existing.rstrip()
    if not stripped:
        return block + "\n", "appended"
    return stripped + "\n\n" + block + "\n", "appended"


def check_rule_file(path: str, block: str = CANONICAL_BLOCK) -> str:
    if not os.path.exists(path):
        return "missing_file"
    with open(path, "r", encoding="utf-8") as f:
        existing = f.read()
    _, status = compute_synced_content(existing, block=block)
    if status == "unchanged":
        return "synced"
    return "outdated" if START_MARKER in existing else "missing_block"


def sync_rule_file(
    path: str,
    block: str = CANONICAL_BLOCK,
    dry_run: bool = False,
    create_if_parent_exists: bool = True,
) -> dict[str, Any]:
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.exists(path):
        if not os.path.isdir(parent) or not create_if_parent_exists:
            return {"path": path, "action": "skipped_missing_dir"}
        existing = ""
    else:
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read()

    new_content, status = compute_synced_content(existing, block=block)
    if status == "unchanged":
        return {"path": path, "action": "unchanged"}
    if not dry_run:
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        os.replace(tmp_path, path)
    return {"path": path, "action": f"dry_run_{status}" if dry_run else status}


def sync_skill_symlink(
    skills_dir: str,
    source_dir: str = SKILL_DIR,
    skill_name: str = SKILL_NAME,
    dry_run: bool = False,
) -> dict[str, Any]:
    link_path = os.path.join(skills_dir, skill_name)
    if not os.path.isdir(skills_dir):
        return {"path": link_path, "action": "skipped_missing_dir"}
    if os.path.islink(link_path):
        current = os.readlink(link_path)
        if os.path.realpath(link_path) == os.path.realpath(source_dir):
            return {"path": link_path, "action": "unchanged", "target": current}
        if not dry_run:
            os.unlink(link_path)
            os.symlink(source_dir, link_path)
        return {"path": link_path, "action": "dry_run_relinked" if dry_run else "relinked", "target": source_dir}
    if os.path.exists(link_path):
        return {"path": link_path, "action": "skipped_existing_non_symlink"}
    if not dry_run:
        os.symlink(source_dir, link_path)
    return {"path": link_path, "action": "dry_run_linked" if dry_run else "linked", "target": source_dir}


def parse_target_names(raw: str | None) -> list[str]:
    if not raw:
        return list(DEFAULT_RULE_TARGETS.keys())
    names = [x.strip().lower() for x in raw.split(",") if x.strip()]
    unknown = [n for n in names if n not in DEFAULT_RULE_TARGETS]
    if unknown:
        raise ValueError(f"Unknown target(s): {', '.join(unknown)}. Valid: {', '.join(DEFAULT_RULE_TARGETS)}")
    return names


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Idempotently sync DAILY-SUMMARY global memory rules and skill symlinks across AI clients."
    )
    parser.add_argument("--check", action="store_true", help="Read-only check of rule & skill sync status.")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without modifying any files.")
    parser.add_argument("--sync-skills", action="store_true", help="Also create/verify daily-memory skill symlinks.")
    parser.add_argument(
        "--targets",
        help=f"Comma-separated subset of targets ({','.join(DEFAULT_RULE_TARGETS.keys())}).",
    )
    parser.add_argument("--json", dest="as_json", action="store_true", help="Output JSON.")
    args = parser.parse_args()

    try:
        targets = parse_target_names(args.targets)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)

    report: dict[str, Any] = {"rules": {}, "skills": {}}

    if args.check:
        for name in targets:
            r_path = DEFAULT_RULE_TARGETS[name]
            report["rules"][name] = {"path": r_path, "status": check_rule_file(r_path)}
            s_dir = DEFAULT_SKILL_LINK_DIRS[name]
            link_path = os.path.join(s_dir, SKILL_NAME)
            if os.path.islink(link_path) and os.path.realpath(link_path) == os.path.realpath(SKILL_DIR):
                s_status = "linked"
            elif os.path.exists(link_path):
                s_status = "conflict"
            elif not os.path.isdir(s_dir):
                s_status = "missing_dir"
            else:
                s_status = "missing_link"
            report["skills"][name] = {"path": link_path, "status": s_status}
    else:
        for name in targets:
            report["rules"][name] = sync_rule_file(DEFAULT_RULE_TARGETS[name], dry_run=args.dry_run)
            if args.sync_skills:
                report["skills"][name] = sync_skill_symlink(
                    DEFAULT_SKILL_LINK_DIRS[name], dry_run=args.dry_run
                )

    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    print("Rules:")
    for name, info in report["rules"].items():
        state = info.get("status") or info.get("action")
        print(f"  - {name:12s} [{state:15s}] {info['path']}")
    if report["skills"]:
        print("Skills:")
        for name, info in report["skills"].items():
            state = info.get("status") or info.get("action")
            print(f"  - {name:12s} [{state:15s}] {info['path']}")


if __name__ == "__main__":
    main()
