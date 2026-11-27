#!/usr/bin/env python3
"""Import a week of local AI sessions and show the first project query."""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from contextlib import closing
from datetime import date, timedelta
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import sys
import tempfile
from zoneinfo import ZoneInfoNotFoundError

import collect
from multi_machine import atomic_write_json
import query_memory
from project_identity import project_id
from summarizer import fallback_rule_summarize, first_import_project_labels


CLIENTS = {
    "antigravity": "Antigravity",
    "codex": "Codex",
    "claude": "Claude Code",
    "workbuddy": "WorkBuddy",
}
DEFAULT_OUTPUT = Path(collect.BASE_DIR) / "data" / "first-import"


def has_readable_log(paths: Iterable[str]) -> bool:
    for name in paths:
        try:
            with open(name, "rb") as handle:
                if handle.read(1):
                    return True
        except OSError:
            continue
    return False


def choose_clients(available: dict[str, bool], start: date, end: date) -> set[str]:
    choices = []
    for client, label in CLIENTS.items():
        if available[client]:
            choices.append(client)
            print(f"  {len(choices)}. {label}（可读）")
        else:
            print(f"     {label}（未发现可读历史）")
    if not choices:
        return set()

    numbers = {str(i): client for i, client in enumerate(choices, 1)}
    print(f"导入最近 7 天（{start} 至 {end}，含当前工作日）。")
    while True:
        answer = input("回车导入全部可读客户端，输入编号选择（可用逗号分隔），输入 0 退出 > ").strip()
        if not answer:
            return set(choices)
        if answer == "0":
            return set()
        parts = [part.strip() for part in answer.replace("，", ",").split(",")]
        if all(part in numbers for part in parts):
            return {numbers[part] for part in parts}
        print("请输入列表中的编号，例如 1 或 1,2。")


def save_import(output: Path, end: date, raw_days: list[dict], days: list[dict]) -> Path:
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    snapshot = Path(tempfile.mkdtemp(prefix=f"{end}-", dir=output))
    try:
        for raw in raw_days:
            workday = raw["workday"]
            atomic_write_json(snapshot / "raw" / workday[:7] / f"raw_{workday}.json", raw)
        atomic_write_json(snapshot / "summary_all_history.json", {"days": days})
    except BaseException:
        shutil.rmtree(snapshot, ignore_errors=True)
        raise
    return snapshot


def import_week(output: Path) -> int:
    cfg = query_memory.load_config()
    end = date.fromisoformat(collect.current_workday(cfg["timezone"], int(cfg["day_start_hour"])))
    start = end - timedelta(days=6)
    print("首次导入 · 正在检查本机客户端", flush=True)

    codex_paths = collect.list_codex_rollout_paths()
    if not collect.supports_zstd():
        uncompressed = [path for path in codex_paths if not path.endswith(".zst")]
        if len(uncompressed) < len(codex_paths):
            print("Codex 的 .zst 归档需要 Python 3.14+ 或 zstandard，本次仅检查普通日志。")
        codex_paths = uncompressed
    workbuddy_meta, workbuddy_paths = collect.load_workbuddy_metadata()
    available = {
        "antigravity": has_readable_log(collect.list_antigravity_transcript_paths()),
        "codex": has_readable_log(codex_paths),
        "claude": has_readable_log(collect.list_claude_session_paths()),
        "workbuddy": has_readable_log(workbuddy_paths[sid] for sid in workbuddy_meta.keys() & workbuddy_paths.keys()),
    }
    selected = choose_clients(available, start, end)
    if not any(available.values()):
        print("未发现可导入的本机会话。使用任一支持的客户端开始会话后，可重新运行向导。")
        return 1
    if not selected:
        print("已取消导入。")
        return 0

    raw_days = []
    for offset in range(7):
        workday = (start + timedelta(days=offset)).isoformat()
        print(f"正在导入 {workday}…", flush=True)
        raw = collect.build_day_payload(workday, cfg, session_clients=selected, codex_paths=codex_paths)
        raw_days.append(raw)
    labels = first_import_project_labels(raw_days)
    days = []
    active_raw_days = []
    for raw in raw_days:
        day = fallback_rule_summarize(raw, first_import=True, import_labels=labels)
        if day["work_items"]:
            active_raw_days.append(raw)
            days.append(day)
    if not days:
        print("所选客户端近 7 天没有可导入的会话。可重新运行向导，选择其他可读客户端。")
        return 1

    snapshot = save_import(output.resolve(), end, active_raw_days, days)
    imported = query_memory.load_all_days(snapshot)
    ordered_days = sorted(imported, key=lambda day: (day["workday"] < end.isoformat(), day["workday"]), reverse=True)
    items = [item for day in ordered_days for item in day["work_items"]]
    example = next((item for item in items if any(ref.get("workspace") for ref in item["sources"])), items[0])
    pid = project_id(example["project"])
    result = query_memory.query_by_project(imported, pid, data_dir=snapshot, limit=1)
    print(f"\n已导入 {len(items)} 条会话记录，覆盖 {len(imported)} 天。\n")
    print(query_memory.format_project_query_md(result))
    path = os.path.relpath(snapshot) if snapshot.is_relative_to(Path.cwd()) else str(snapshot)
    if os.name == "nt":
        python = "python"
        shell_path = "'" + path.replace("'", "''") + "'"
    else:
        python = "python3"
        shell_path = shlex.quote(path)
    print(f"\n再次查询\n  {python} query_memory.py --data-dir {shell_path} --project {pid}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="导入最近 7 天的本机会话，找到一个真实项目。")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT, help="保存导入结果的上级目录。")
    args = parser.parse_args()
    try:
        return import_week(args.output_dir)
    except (EOFError, KeyboardInterrupt):
        print("\n已取消导入。")
        return 0
    except ZoneInfoNotFoundError as exc:
        print(f"无法读取配置的时区 {exc}。请检查时区名称或安装 tzdata。", file=sys.stderr)
        return 1
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
