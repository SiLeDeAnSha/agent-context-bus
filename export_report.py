#!/usr/bin/env python3
"""Export a sanitized, external-safe Markdown daily or weekly work report from Excel."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta
import json
import os
import re
from typing import Any
from zoneinfo import ZoneInfo

try:
    from openpyxl import load_workbook
except ImportError:
    load_workbook = None  # type: ignore[assignment]

import redact

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
EXAMPLE_CONFIG_PATH = os.path.join(BASE_DIR, "config.example.json")


def load_config() -> dict[str, Any]:
    path = CONFIG_PATH if os.path.exists(CONFIG_PATH) else EXAMPLE_CONFIG_PATH
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k in ("excel_path", "personal_excel_path", "export_dir"):
        if isinstance(cfg.get(k), str):
            cfg[k] = os.path.expanduser(cfg[k])
    return cfg


_PATH_STOP = r"\r\n,，。；;）)\"'\`<>|：:"
_WIN_PATH_RE = re.compile(
    rf"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^{_PATH_STOP}]+?(?=(?:[{_PATH_STOP}]|$|\s+(?=[\u4e00-\u9fff]|[-–—]|(?:and|or|to|with|for|in|on|at|from|via|by)\b)))",
    re.IGNORECASE,
)
_POSIX_USER_PATH_RE = re.compile(
    rf"(?:(?:/(?:Users|home)/)|(?:~/))[^{_PATH_STOP}]+?(?=(?:[{_PATH_STOP}]|$|\s+(?=[\u4e00-\u9fff]|[-–—]|(?:and|or|to|with|for|in|on|at|from|via|by)\b)))"
)


def sanitize_text(text: str) -> str:
    if not text:
        return ""
    s = redact.redact_text(str(text))
    # Strip Windows drive paths (including spaces inside segments) and macOS/Linux user paths.
    s = _WIN_PATH_RE.sub("", s)
    s = _POSIX_USER_PATH_RE.sub("", s)
    s = re.sub(r"\[(?:📁|目录:)[^\]]+\]", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" ，、；;")
    return s


def is_excluded(project: str, category: str, summary: str, cfg: dict[str, Any]) -> bool:
    ext = cfg.get("external_report", {})
    if category in set(ext.get("exclude_categories", [])):
        return True
    blob = f"{project} {category} {summary}".lower()
    for kw in ext.get("exclude_keywords", []):
        if str(kw).lower() in blob:
            return True
    return False


def generate_report(
    xlsx_path: str,
    start_date: str,
    end_date: str,
    cfg: dict[str, Any],
) -> str:
    if not os.path.exists(xlsx_path):
        raise FileNotFoundError(f"Excel file not found: {xlsx_path}")

    wb = load_workbook(xlsx_path, read_only=True, data_only=True)
    if "项目流程明细" not in wb.sheetnames:
        wb.close()
        raise ValueError("Missing sheet '项目流程明细'")

    ws = wb["项目流程明细"]
    rows_iter = ws.iter_rows(values_only=True)
    for row in rows_iter:
        if row and row[0] == "项目名称":
            break

    by_proj: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"category": "", "items": [], "outputs": [], "status": "", "next_step": ""}
    )

    for r in rows_iter:
        if not r or not r[0] or not r[1]:
            continue
        proj = str(r[0]).strip()
        d_str = str(r[1]).strip()
        if not (start_date <= d_str <= end_date):
            continue
        cat = str(r[3] or "").strip()
        what = str(r[6] or "").strip()
        out_txt = str(r[7] or "").strip()
        status = str(r[8] or "").strip()
        next_step = str(r[9] or "").strip()

        if is_excluded(proj, cat, what, cfg):
            continue

        entry = by_proj[proj]
        entry["category"] = cat
        clean_what = sanitize_text(what)
        if clean_what:
            entry["items"].append(f"[{d_str[5:]}] {clean_what}")
        clean_out = sanitize_text(out_txt)
        if clean_out and clean_out != "-":
            entry["outputs"].append(clean_out)
        if status:
            entry["status"] = status
        if next_step and next_step != "-":
            entry["next_step"] = sanitize_text(next_step)

    wb.close()

    title = f"工作日报（{start_date}）" if start_date == end_date else f"工作周报汇总（{start_date} ~ {end_date}）"
    lines = [f"# {title}", ""]

    if not by_proj:
        lines.append("本周期无符合对外汇报口径的公开工作项。")
        return "\n".join(lines)

    for idx, (proj, info) in enumerate(by_proj.items(), start=1):
        clean_proj = sanitize_text(proj) or proj
        status_tag = f"（{info['status']}）" if info["status"] else ""
        lines.append(f"## {idx}. {clean_proj}{status_tag}")
        for it in info["items"]:
            lines.append(f"- {it}")
        if info["outputs"]:
            dedup_outs = list(dict.fromkeys(info["outputs"]))
            lines.append(f"- **主要产出**：{'；'.join(dedup_outs)}")
        if info["next_step"] and info["status"] not in ("已完成", "阶段完成"):
            lines.append(f"- **后续计划**：{info['next_step']}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def main() -> None:
    cfg = load_config()
    parser = argparse.ArgumentParser(description="Export sanitized Markdown report for external sharing.")
    parser.add_argument("--date", help="Single workday YYYY-MM-DD.")
    parser.add_argument("--start", help="Start date YYYY-MM-DD.")
    parser.add_argument("--end", help="End date YYYY-MM-DD.")
    parser.add_argument("--days", type=int, help="Last N days ending at yesterday.")
    parser.add_argument("--output", help="Optional Markdown file output path.")
    args = parser.parse_args()

    tz = ZoneInfo(cfg["timezone"])
    logical_today = (datetime.now(tz) - timedelta(hours=int(cfg["day_start_hour"]))).date()
    default_end = (logical_today - timedelta(days=1)).strftime("%Y-%m-%d")

    if args.date:
        start_d = end_d = args.date
    elif args.start or args.end:
        end_d = args.end or default_end
        start_d = args.start or end_d
    else:
        n = args.days or 7
        end_dt = datetime.strptime(default_end, "%Y-%m-%d").date()
        start_d = (end_dt - timedelta(days=n - 1)).strftime("%Y-%m-%d")
        end_d = default_end

    xlsx_path = os.path.expanduser(cfg["excel_path"])
    report = generate_report(xlsx_path, start_d, end_d, cfg)

    out_dir = os.path.expanduser(cfg.get("export_dir") or os.path.dirname(xlsx_path))
    default_name = f"对外日报_{start_d}.md" if start_d == end_d else f"对外周报_{start_d}_至_{end_d}.md"
    out_path = os.path.expanduser(args.output or os.path.join(out_dir, default_name))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"Saved sanitized report to {out_path}\n")
    print(report)


if __name__ == "__main__":
    main()
