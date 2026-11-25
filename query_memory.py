#!/usr/bin/env python3
"""AI memory index generator and zero-dependency CLI query tool for daily-summary.

Provides two capabilities using only the Python standard library (works with both
system python3 and the project .venv):
1. Generate ~/Documents/DailyReport/MEMORY_INDEX.md (L2 current-state snapshot).
2. Query historical projects, daily summaries, life items, and raw session/Git/file records.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta
import glob
import json
import os
import sys
from typing import Any
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
EXAMPLE_CONFIG_PATH = os.path.join(BASE_DIR, "config.example.json")
DEFAULT_DATA_DIR = os.path.join(BASE_DIR, "data")
DEFAULT_INDEX_PATH = os.path.expanduser("~/Documents/DailyReport/MEMORY_INDEX.md")
ACTIVE_STATUSES = {"进行中", "待开启定时", "待验证", "阻塞", "待跟进"}


def load_config(config_path: str = CONFIG_PATH) -> dict[str, Any]:
    path = config_path if os.path.exists(config_path) else EXAMPLE_CONFIG_PATH
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k in ("excel_path", "personal_excel_path", "export_dir", "memory_index_path"):
        if isinstance(cfg.get(k), str):
            cfg[k] = os.path.expanduser(cfg[k])
    return cfg


def resolve_index_path(cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg if cfg is not None else load_config()
    explicit = cfg.get("memory_index_path")
    if explicit:
        return os.path.expanduser(str(explicit))
    excel_path = cfg.get("excel_path")
    if excel_path:
        return os.path.join(os.path.dirname(os.path.expanduser(str(excel_path))), "MEMORY_INDEX.md")
    return DEFAULT_INDEX_PATH


def normalize_days_payload(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("days"), list):
        return data["days"]
    if isinstance(data, dict) and "workday" in data:
        return [data]
    return []


def load_all_days(data_dir: str = DEFAULT_DATA_DIR) -> list[dict[str, Any]]:
    master_path = os.path.join(data_dir, "summary_all_history.json")
    by_day: dict[str, dict[str, Any]] = {}
    if os.path.exists(master_path):
        with open(master_path, "r", encoding="utf-8") as f:
            for d in normalize_days_payload(json.load(f)):
                if d.get("workday"):
                    by_day[str(d["workday"])] = d
    else:
        months_dir = os.path.join(data_dir, "months")
        for fp in sorted(glob.glob(os.path.join(months_dir, "summary_*.json"))):
            with open(fp, "r", encoding="utf-8") as f:
                for d in normalize_days_payload(json.load(f)):
                    if d.get("workday"):
                        by_day[str(d["workday"])] = d
    return [by_day[k] for k in sorted(by_day.keys())]


def load_raw_day(workday: str, data_dir: str = DEFAULT_DATA_DIR) -> dict[str, Any] | None:
    ym = workday[:7]
    candidates = [
        os.path.join(data_dir, "raw", ym, f"raw_{workday}.json"),
        os.path.join(data_dir, "raw", f"raw_{workday}.json"),
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    return None


def clean_cell(text: Any, limit: int = 120) -> str:
    s = " ".join(str(text or "").split())
    s = s.replace("|", "\\|")
    if not s:
        return "-"
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip(" ，、；;") + "…"


def parse_terms(keyword: str | None) -> list[str]:
    if not keyword:
        return []
    return [tok.lower() for tok in keyword.strip().split() if tok.strip()]


def matches_terms(text: str, terms: list[str]) -> bool:
    if not terms:
        return True
    hay = text.lower()
    return all(t in hay for t in terms)


def filter_days_by_date(
    days: list[dict[str, Any]],
    date_from: str | None = None,
    date_to: str | None = None,
    recent_days: int | None = None,
    exact_date: str | None = None,
) -> list[dict[str, Any]]:
    if exact_date:
        return [d for d in days if str(d.get("workday")) == exact_date]
    filtered = list(days)
    if recent_days and recent_days > 0 and filtered:
        latest_wd = max(str(d["workday"]) for d in filtered)
        try:
            latest_dt = datetime.strptime(latest_wd, "%Y-%m-%d")
            cutoff = (latest_dt - timedelta(days=recent_days - 1)).strftime("%Y-%m-%d")
            if not date_from or cutoff > date_from:
                date_from = cutoff
        except ValueError:
            pass
    if date_from:
        filtered = [d for d in filtered if str(d.get("workday", "")) >= date_from]
    if date_to:
        filtered = [d for d in filtered if str(d.get("workday", "")) <= date_to]
    return filtered


def _extract_latest_brief(
    proj: str,
    last_day: dict[str, Any],
    last_day_items: list[dict[str, Any]],
) -> str:
    briefs = last_day.get("project_briefs")
    if isinstance(briefs, dict) and isinstance(briefs.get(proj), dict):
        pb = briefs[proj]
        parts: list[str] = []
        for label, key in (("AG", "ag"), ("Codex", "codex"), ("WB", "wb"), ("WB", "workbuddy")):
            val = str(pb.get(key) or "").strip()
            if val and val != "-":
                entry = f"{label}: {val}"
                if entry not in parts:
                    parts.append(entry)
        if parts:
            return "；".join(parts)
    snippets: list[str] = []
    for it in last_day_items:
        txt = str(it.get("summary") or it.get("outputs") or "").strip()
        if txt and txt != "-":
            snippets.append(txt)
    return "；".join(snippets[:2]) if snippets else "-"


def aggregate_projects(
    days: list[dict[str, Any]],
    scope: str = "all",
) -> list[dict[str, Any]]:
    """Aggregate chronological day summaries into per-project current-state snapshots."""
    ordered_days = sorted(days, key=lambda d: str(d.get("workday", "")))
    by_proj: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)

    for day in ordered_days:
        for it in day.get("work_items") or []:
            item_scope = str(it.get("scope") or "work")
            if scope != "all" and item_scope != scope:
                continue
            proj = str(it.get("project") or "未命名项目").strip()
            by_proj[proj].append((day, it))

    results: list[dict[str, Any]] = []
    for proj, pairs in by_proj.items():
        dates = [str(d["workday"]) for d, _ in pairs]
        first_d = min(dates)
        last_d = max(dates)
        span_days = len(set(dates))

        cats: dict[str, int] = defaultdict(int)
        scopes: dict[str, int] = defaultdict(int)
        tools: list[str] = []
        seen_tools: set[str] = set()
        total_h = 0.0
        latest_status = "已完成"
        latest_next = "-"
        recent_sessions: list[str] = []
        seen_sess: set[str] = set()
        recent_outputs: list[str] = []
        seen_out: set[str] = set()

        for _, it in pairs:
            cats[str(it.get("category") or "其他")] += 1
            scopes[str(it.get("scope") or "work")] += 1
            tool = str(it.get("tool") or it.get("source") or "").strip()
            if tool and tool not in seen_tools:
                seen_tools.add(tool)
                tools.append(tool)
            total_h += float(it.get("estimated_hours") or 0.0)

            st = str(it.get("status") or "").strip()
            nx = str(it.get("next_step") or "").strip()
            if st:
                latest_status = st
            if nx and nx != "-":
                latest_next = nx
            elif st in ("已完成", "阶段完成", "已搁置") and nx == "-":
                latest_next = "-"

        for _, it in reversed(pairs):
            tool = str(it.get("tool") or it.get("source") or "").strip()
            sname = str(it.get("session_name") or "").strip()
            if sname:
                lbl = f"[{tool}] {sname}" if tool else sname
                if lbl not in seen_sess:
                    seen_sess.add(lbl)
                    recent_sessions.append(lbl)
            out = str(it.get("outputs") or "").strip()
            if out and out != "-" and out not in seen_out:
                seen_out.add(out)
                recent_outputs.append(out)
            if len(recent_sessions) >= 3 and len(recent_outputs) >= 2:
                break

        last_day_pairs = [(d, it) for d, it in pairs if str(d["workday"]) == last_d]
        latest_brief = _extract_latest_brief(proj, last_day_pairs[-1][0], [it for _, it in last_day_pairs])

        results.append(
            {
                "project": proj,
                "scope": max(scopes.items(), key=lambda kv: kv[1])[0],
                "category": max(cats.items(), key=lambda kv: kv[1])[0],
                "first_date": first_d,
                "last_date": last_d,
                "days": span_days,
                "total_hours": round(total_h, 2),
                "status": latest_status,
                "next_step": latest_next,
                "tools": tools,
                "latest_summary": latest_brief,
                "recent_outputs": recent_outputs[:2],
                "recent_sessions": recent_sessions[:3],
            }
        )

    results.sort(key=lambda r: (r["last_date"], r["total_hours"], r["project"]), reverse=True)
    return results


def render_memory_index(
    days: list[dict[str, Any]],
    tz_name: str = "Asia/Shanghai",
    now_str: str | None = None,
) -> str:
    ordered_days = sorted(days, key=lambda d: str(d.get("workday", "")))
    work_projs = aggregate_projects(ordered_days, scope="work")
    personal_projs = aggregate_projects(ordered_days, scope="personal")
    all_projs = sorted(
        work_projs + personal_projs,
        key=lambda r: (r["last_date"], r["total_hours"], r["project"]),
        reverse=True,
    )
    active_projs = [p for p in all_projs if p["status"] in ACTIVE_STATUSES]

    if not now_str:
        now_str = datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M")

    first_d = ordered_days[0]["workday"] if ordered_days else "-"
    last_d = ordered_days[-1]["workday"] if ordered_days else "-"
    total_days = len(ordered_days)

    lines: list[str] = [
        "# MEMORY_INDEX · 全项目现行状态快照",
        "",
        "> 由 `daily-summary` 流水线自动生成，请勿手工编辑（下次运行会覆盖）。",
        f"> 覆盖范围：`{first_d}` ~ `{last_d}`（共 **{total_days} 天**）｜ 正式工作：**{len(work_projs)} 项** ｜ 个人技术与折腾：**{len(personal_projs)} 项** ｜ 进行中/待跟进：**{len(active_projs)} 项** ｜ 更新时间：`{now_str}`",
        "",
        "## 按需下钻检索（CLI）",
        "",
        "- 查某项目完整时间线：`python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --project \"<项目关键词>\"`",
        "- 跨项目/随想搜关键词（加 `--raw` 可查会话 UUID、用户指令、Git 提交与改动文件）：`python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --keyword \"<关键词>\" --raw`",
        "",
    ]

    if active_projs:
        lines.extend(
            [
                "## 活跃与待跟进项目（置顶）",
                "",
                "| 维度 | 项目名称 | 最近更新 | 状态 | 下一步 / 卡点 | 最近进展摘要 |",
                "|---|---|---|---|---|---|",
            ]
        )
        for p in active_projs:
            scope_lbl = "正式工作" if p["scope"] == "work" else "个人技术"
            lines.append(
                f"| {scope_lbl} | {clean_cell(p['project'], 48)} | `{p['last_date']}` ({p['days']}d) "
                f"| {clean_cell(p['status'], 16)} | {clean_cell(p['next_step'], 60)} "
                f"| {clean_cell(p['latest_summary'], 110)} |"
            )
        lines.append("")

    def append_scope_table(title: str, projs: list[dict[str, Any]]) -> None:
        lines.extend([f"## {title}（{len(projs)} 项，按最近更新倒序）", ""])
        if not projs:
            lines.extend(["_暂无记录_", ""])
            return
        lines.extend(
            [
                "| 项目名称 | 分类 | 覆盖日期（天数） | 状态 | 下一步 / 卡点 | 最近进展与关键产出 |",
                "|---|---|---|---|---|---|",
            ]
        )
        for p in projs:
            span = (
                f"`{p['first_date']}`~`{p['last_date']}` ({p['days']}d)"
                if p["first_date"] != p["last_date"]
                else f"`{p['last_date']}` (1d)"
            )
            detail = p["latest_summary"]
            if p["recent_outputs"]:
                out_str = " / ".join(p["recent_outputs"][:1])
                if out_str and out_str not in detail:
                    detail = f"{detail}（产出：{out_str}）"
            lines.append(
                f"| {clean_cell(p['project'], 46)} | {clean_cell(p['category'], 20)} | {span} "
                f"| {clean_cell(p['status'], 14)} | {clean_cell(p['next_step'], 52)} "
                f"| {clean_cell(detail, 115)} |"
            )
        lines.append("")

    append_scope_table("正式工作项目总览 · Work", work_projs)
    append_scope_table("个人技术与折腾总览 · Personal", personal_projs)

    recent_7 = list(reversed(ordered_days[-7:]))
    lines.extend(["## 近 7 天每日速览", ""])
    if not recent_7:
        lines.extend(["_暂无记录_", ""])
    else:
        lines.extend(
            [
                "| 日期 | 星期 | 活跃时段 | 当日主线（Headline） | 核心产出 |",
                "|---|---|---|---|---|",
            ]
        )
        for d in recent_7:
            lines.append(
                f"| `{d.get('workday', '-')}` | {clean_cell(d.get('weekday', '-'), 8)} "
                f"| {clean_cell(d.get('active_span', '-'), 18)} "
                f"| {clean_cell(d.get('headline', '-'), 90)} "
                f"| {clean_cell(d.get('core_outputs', '-'), 110)} |"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def generate_memory_index(
    days: list[dict[str, Any]],
    output_path: str | None = None,
    tz_name: str = "Asia/Shanghai",
) -> dict[str, Any]:
    target = os.path.expanduser(output_path or resolve_index_path())
    content = render_memory_index(days, tz_name=tz_name)
    out_dir = os.path.dirname(os.path.abspath(target))
    os.makedirs(out_dir, exist_ok=True)
    tmp_path = target + ".tmp.md"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp_path, target)

    ordered = sorted(days, key=lambda d: str(d.get("workday", "")))
    work_cnt = len(aggregate_projects(ordered, scope="work"))
    personal_cnt = len(aggregate_projects(ordered, scope="personal"))
    return {
        "path": target,
        "days": len(ordered),
        "work_projects": work_cnt,
        "personal_projects": personal_cnt,
        "bytes": len(content.encode("utf-8")),
    }


def extract_raw_matches(
    workday: str,
    terms: list[str],
    data_dir: str = DEFAULT_DATA_DIR,
    limit: int = 8,
) -> dict[str, Any]:
    raw = load_raw_day(workday, data_dir=data_dir)
    if not raw:
        return {}

    sessions: list[dict[str, Any]] = []
    for bucket in ("antigravity", "codex", "workbuddy", "t3_other"):
        for s in raw.get(bucket) or []:
            if not isinstance(s, dict):
                continue
            blob = " ".join(
                [
                    str(s.get("source") or bucket),
                    str(s.get("title") or ""),
                    str(s.get("display_title") or ""),
                    str(s.get("workspace") or ""),
                    str(s.get("cwd") or ""),
                    " ".join(str(x) for x in (s.get("user_prompts") or [])),
                    " ".join(str(x) for x in (s.get("assistant_milestones") or [])),
                    str(s.get("last_assistant_reply") or ""),
                ]
            )
            if matches_terms(blob, terms):
                sessions.append(
                    {
                        "source": s.get("source") or bucket,
                        "id": s.get("id", ""),
                        "title": s.get("display_title") or s.get("title") or "",
                        "workspace": s.get("workspace") or s.get("cwd") or "",
                        "active_span": s.get("active_span", ""),
                        "user_prompts": (s.get("user_prompts") or [])[:4],
                        "assistant_milestones": (s.get("assistant_milestones") or [])[:3],
                    }
                )

    git_hits: list[dict[str, Any]] = []
    for repo in raw.get("git") or []:
        if not isinstance(repo, dict):
            continue
        matched_commits = []
        for c in repo.get("commits") or []:
            blob = " ".join(
                [
                    str(repo.get("repo") or ""),
                    str(repo.get("repo_name") or ""),
                    str(c.get("subject") or ""),
                    " ".join(str(x) for x in (c.get("files") or [])),
                ]
            )
            if matches_terms(blob, terms):
                matched_commits.append(
                    {
                        "hash": c.get("hash", ""),
                        "time": c.get("time", ""),
                        "subject": c.get("subject", ""),
                        "files": (c.get("files") or [])[:8],
                    }
                )
        if matched_commits:
            git_hits.append(
                {
                    "repo": repo.get("repo", ""),
                    "repo_name": repo.get("repo_name", ""),
                    "commits": matched_commits[:5],
                }
            )

    file_hits: list[dict[str, Any]] = []
    for fg in raw.get("files") or []:
        if not isinstance(fg, dict):
            continue
        blob = " ".join([str(fg.get("group") or ""), " ".join(str(x) for x in (fg.get("sample_files") or []))])
        if matches_terms(blob, terms):
            file_hits.append(
                {
                    "group": fg.get("group", ""),
                    "latest_time": fg.get("latest_time", ""),
                    "sample_files": (fg.get("sample_files") or [])[:8],
                }
            )

    if not (sessions or git_hits or file_hits):
        return {}
    return {
        "workday": workday,
        "sessions": sessions[:limit],
        "git": git_hits[:limit],
        "files": file_hits[:limit],
    }


def query_project_timeline(
    days: list[dict[str, Any]],
    project_query: str,
    scope: str = "all",
    limit: int = 30,
    asc: bool = False,
    include_raw: bool = False,
    data_dir: str = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    terms = parse_terms(project_query)
    entries: list[dict[str, Any]] = []
    matched_projects: set[str] = set()
    matched_dates: list[str] = []

    for d in sorted(days, key=lambda x: str(x.get("workday", "")), reverse=not asc):
        wd = str(d.get("workday", ""))
        for it in d.get("work_items") or []:
            item_scope = str(it.get("scope") or "work")
            if scope not in ("all", "life") and item_scope != scope:
                continue
            proj = str(it.get("project") or "")
            if matches_terms(proj, terms):
                matched_projects.add(proj)
                if wd not in matched_dates:
                    matched_dates.append(wd)
                entries.append(
                    {
                        "workday": wd,
                        "weekday": d.get("weekday", ""),
                        "scope": item_scope,
                        "project": proj,
                        "category": it.get("category", ""),
                        "tool": it.get("tool") or it.get("source") or "",
                        "session_name": it.get("session_name", ""),
                        "status": it.get("status", ""),
                        "summary": it.get("summary", ""),
                        "outputs": it.get("outputs", ""),
                        "next_step": it.get("next_step", ""),
                        "active_span": it.get("active_span", ""),
                        "estimated_hours": it.get("estimated_hours", 0),
                    }
                )

    sliced = entries[:limit]
    raw_details: list[dict[str, Any]] = []
    if include_raw:
        for wd in matched_dates[:5]:
            rm = extract_raw_matches(wd, terms, data_dir=data_dir)
            if not rm:
                rm = extract_raw_matches(wd, [], data_dir=data_dir, limit=4)
            if rm:
                raw_details.append(rm)

    return {
        "query": project_query,
        "matched_projects": sorted(matched_projects),
        "total_matches": len(entries),
        "entries": sliced,
        "raw": raw_details,
    }


def query_keyword(
    days: list[dict[str, Any]],
    keyword: str,
    scope: str = "all",
    limit: int = 15,
    include_raw: bool = False,
    data_dir: str = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    terms = parse_terms(keyword)
    hits: list[dict[str, Any]] = []
    raw_hits: list[dict[str, Any]] = []

    for d in sorted(days, key=lambda x: str(x.get("workday", "")), reverse=True):
        wd = str(d.get("workday", ""))
        day_matched = False

        if scope in ("all", "work", "personal"):
            for it in d.get("work_items") or []:
                item_scope = str(it.get("scope") or "work")
                if scope != "all" and item_scope != scope:
                    continue
                blob = " ".join(
                    str(it.get(k) or "")
                    for k in ("project", "category", "tool", "session_name", "summary", "outputs", "next_step")
                )
                if matches_terms(blob, terms):
                    day_matched = True
                    hits.append(
                        {
                            "kind": "work_item",
                            "workday": wd,
                            "scope": item_scope,
                            "project": it.get("project", ""),
                            "tool": it.get("tool") or it.get("source") or "",
                            "session_name": it.get("session_name", ""),
                            "status": it.get("status", ""),
                            "summary": it.get("summary", ""),
                            "outputs": it.get("outputs", ""),
                            "next_step": it.get("next_step", ""),
                        }
                    )

        if scope in ("all", "personal", "life"):
            for li in d.get("life_items") or []:
                blob = " ".join(str(li.get(k) or "") for k in ("tool", "session_name", "topic", "summary"))
                if matches_terms(blob, terms):
                    day_matched = True
                    hits.append(
                        {
                            "kind": "life_item",
                            "workday": wd,
                            "scope": "life",
                            "project": li.get("topic", ""),
                            "tool": li.get("tool") or li.get("source") or "",
                            "session_name": li.get("session_name", ""),
                            "status": "-",
                            "summary": li.get("summary", ""),
                            "outputs": "-",
                            "next_step": "-",
                        }
                    )

        if not day_matched and scope == "all":
            day_blob = f"{d.get('headline', '')} {d.get('core_outputs', '')}"
            if matches_terms(day_blob, terms):
                hits.append(
                    {
                        "kind": "day_headline",
                        "workday": wd,
                        "scope": "all",
                        "project": d.get("headline", ""),
                        "tool": "-",
                        "session_name": "-",
                        "status": "-",
                        "summary": d.get("core_outputs", ""),
                        "outputs": "-",
                        "next_step": "-",
                    }
                )

        if include_raw and len(raw_hits) < 6:
            rm = extract_raw_matches(wd, terms, data_dir=data_dir)
            if rm:
                raw_hits.append(rm)

    return {
        "query": keyword,
        "scope": scope,
        "total_matches": len(hits),
        "hits": hits[:limit],
        "raw": raw_hits,
    }


def format_raw_section(raw_items: list[dict[str, Any]]) -> list[str]:
    if not raw_items:
        return []
    lines = ["", "### Raw 原始溯源（会话 ID / 工作区 / Git / 文件）"]
    for rm in raw_items:
        lines.append(f"\n#### `{rm['workday']}`")
        for s in rm.get("sessions") or []:
            ws = f" | workspace: `{s['workspace']}`" if s.get("workspace") else ""
            lines.append(f"- **[{s['source']}]** {s['title']} (id: `{s['id']}`{ws})")
            for p in (s.get("user_prompts") or [])[:2]:
                lines.append(f"  - 用户指令：{clean_cell(p, 120)}")
            for m in (s.get("assistant_milestones") or [])[:2]:
                lines.append(f"  - 里程碑：{clean_cell(m, 120)}")
        for g in rm.get("git") or []:
            lines.append(f"- **Git `{g['repo_name']}`** (`{g['repo']}`)")
            for c in g.get("commits") or []:
                f_preview = ", ".join(c["files"][:5])
                lines.append(f"  - `{c['hash']}` {c['time']} {c['subject']}" + (f" [{f_preview}]" if f_preview else ""))
        for fg in rm.get("files") or []:
            lines.append(f"- **本地文件 `{fg['group']}`** ({fg['latest_time']}): {', '.join(fg['sample_files'][:6])}")
    return lines


def print_projects_md(projs: list[dict[str, Any]], limit: int) -> None:
    sliced = projs[:limit]
    print(f"### 匹配项目（显示 {len(sliced)} / 共 {len(projs)} 项）\n")
    if not sliced:
        print("_无匹配结果_")
        return
    print("| 维度 | 项目名称 | 覆盖日期（天数） | 状态 | 下一步 / 卡点 | 最近进展 |")
    print("|---|---|---|---|---|---|")
    for p in sliced:
        scope_lbl = "工作" if p["scope"] == "work" else "个人"
        print(
            f"| {scope_lbl} | {clean_cell(p['project'], 44)} | `{p['first_date']}`~`{p['last_date']}` ({p['days']}d) "
            f"| {clean_cell(p['status'], 14)} | {clean_cell(p['next_step'], 50)} | {clean_cell(p['latest_summary'], 100)} |"
        )


def print_project_timeline_md(res: dict[str, Any]) -> None:
    print(f"### 项目时间线：`{res['query']}`（匹配项目 {len(res['matched_projects'])} 个，共 {res['total_matches']} 条记录）")
    if res["matched_projects"]:
        print(f"> 项目名：{', '.join(res['matched_projects'])}")
    print()
    if not res["entries"]:
        print("_无匹配记录_")
        return
    for e in res["entries"]:
        scope_lbl = "工作" if e["scope"] == "work" else "个人"
        print(
            f"- **`{e['workday']}`** [{scope_lbl} | {e['tool']} | {e['status']}] **{e['project']}** — `{e['session_name']}`"
        )
        if e["summary"]:
            print(f"  - 进展：{e['summary']}")
        if e["outputs"] and e["outputs"] != "-":
            print(f"  - 产出：{e['outputs']}")
        if e["next_step"] and e["next_step"] != "-":
            print(f"  - 下一步：{e['next_step']}")
    for line in format_raw_section(res.get("raw") or []):
        print(line)


def print_keyword_md(res: dict[str, Any]) -> None:
    print(f"### 关键词检索：`{res['query']}`（scope={res['scope']}，共 {res['total_matches']} 条，展示 {len(res['hits'])} 条）\n")
    if not res["hits"] and not res.get("raw"):
        print("_无匹配结果_")
        return
    for h in res["hits"]:
        tag = "随想" if h["kind"] == "life_item" else ("当日主线" if h["kind"] == "day_headline" else h["scope"])
        print(f"- **`{h['workday']}`** [{tag} | {h['tool']}] **{h['project']}** (`{h['session_name']}`)")
        if h["summary"]:
            print(f"  - 摘要：{h['summary']}")
        if h["outputs"] and h["outputs"] != "-":
            print(f"  - 产出：{h['outputs']}")
        if h["next_step"] and h["next_step"] != "-":
            print(f"  - 下一步：{h['next_step']}")
    for line in format_raw_section(res.get("raw") or []):
        print(line)


def print_day_md(day: dict[str, Any], raw: dict[str, Any] | None = None) -> None:
    print(f"### `{day.get('workday')}` ({day.get('weekday', '')}) ｜ 活跃时段：`{day.get('active_span', '-')}`")
    if day.get("headline"):
        print(f"- **主线**：{day['headline']}")
    if day.get("core_outputs"):
        print(f"- **核心产出**：{day['core_outputs']}")
    work_items = day.get("work_items") or []
    if work_items:
        print("\n#### 项目明细")
        for it in work_items:
            print(
                f"- [{it.get('scope', 'work')} | {it.get('tool', '')} | {it.get('status', '')}] "
                f"**{it.get('project', '')}** (`{it.get('session_name', '')}`): {it.get('summary', '')}"
            )
            if it.get("outputs") and it.get("outputs") != "-":
                print(f"  - 产出：{it['outputs']}")
    life_items = day.get("life_items") or []
    if life_items:
        print("\n#### 随想与探索")
        for li in life_items:
            print(f"- [{li.get('tool', '')}] **{li.get('topic', '')}** (`{li.get('session_name', '')}`): {li.get('summary', '')}")
    if raw:
        for line in format_raw_section([raw]):
            print(line)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI memory index generator and fast historical query CLI for daily-summary."
    )
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="Path to daily-summary data directory.")
    parser.add_argument("--rebuild-index", action="store_true", help="Rebuild MEMORY_INDEX.md from summary history.")
    parser.add_argument("--index-output", help="Override output path for --rebuild-index.")
    parser.add_argument("--projects", action="store_true", help="List aggregated projects sorted by latest activity.")
    parser.add_argument("--project", help="Show chronological timeline for matching project name(s).")
    parser.add_argument("--keyword", "-k", help="Search keyword(s) (space-separated AND terms) across history.")
    parser.add_argument("--date", help="Inspect a specific workday (YYYY-MM-DD).")
    parser.add_argument("--days", type=int, help="Limit search to the most recent N calendar days.")
    parser.add_argument("--from", dest="date_from", help="Start date inclusive (YYYY-MM-DD).")
    parser.add_argument("--to", dest="date_to", help="End date inclusive (YYYY-MM-DD).")
    parser.add_argument(
        "--scope",
        choices=["all", "work", "personal", "life"],
        default="all",
        help="Filter by scope (default: all).",
    )
    parser.add_argument("--status", help="Filter --projects by status substring (e.g. 进行中).")
    parser.add_argument("--raw", action="store_true", help="Also drill into data/raw/ for session UUIDs, Git, and files.")
    parser.add_argument("--asc", action="store_true", help="Sort timeline entries oldest-first.")
    parser.add_argument("--limit", type=int, default=15, help="Max results to return (default: 15).")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Output raw JSON instead of Markdown.")
    args = parser.parse_args()

    days = load_all_days(args.data_dir)
    if args.rebuild_index:
        cfg = load_config()
        stats = generate_memory_index(
            days,
            output_path=args.index_output or resolve_index_path(cfg),
            tz_name=str(cfg.get("timezone") or "Asia/Shanghai"),
        )
        if args.as_json:
            print(json.dumps(stats, ensure_ascii=False, indent=2))
        else:
            print(
                f"Generated {stats['path']} "
                f"({stats['days']} days, {stats['work_projects']} work + {stats['personal_projects']} personal projects, "
                f"{stats['bytes']} bytes)"
            )
        return

    filtered_days = filter_days_by_date(
        days,
        date_from=args.date_from,
        date_to=args.date_to,
        recent_days=args.days,
        exact_date=args.date,
    )

    if args.date:
        if not filtered_days:
            print(f"No summary found for {args.date}", file=sys.stderr)
            sys.exit(1)
        day = filtered_days[0]
        raw_m = extract_raw_matches(args.date, parse_terms(args.keyword), data_dir=args.data_dir) if args.raw else None
        if args.as_json:
            print(json.dumps({"day": day, "raw": raw_m}, ensure_ascii=False, indent=2))
        else:
            print_day_md(day, raw_m)
        return

    if args.project:
        res = query_project_timeline(
            filtered_days,
            args.project,
            scope=args.scope,
            limit=args.limit,
            asc=args.asc,
            include_raw=args.raw,
            data_dir=args.data_dir,
        )
        if args.as_json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print_project_timeline_md(res)
        return

    if args.keyword and not args.projects:
        res = query_keyword(
            filtered_days,
            args.keyword,
            scope=args.scope,
            limit=args.limit,
            include_raw=args.raw,
            data_dir=args.data_dir,
        )
        if args.as_json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print_keyword_md(res)
        return

    proj_scope = "all" if args.scope == "life" else args.scope
    projs = aggregate_projects(filtered_days, scope=proj_scope)
    if args.status:
        projs = [p for p in projs if args.status in p["status"]]
    if args.keyword:
        terms = parse_terms(args.keyword)
        projs = [
            p
            for p in projs
            if matches_terms(
                f"{p['project']} {p['category']} {p['latest_summary']} {p['next_step']}",
                terms,
            )
        ]
    if args.as_json:
        print(json.dumps({"total": len(projs), "projects": projs[: args.limit]}, ensure_ascii=False, indent=2))
    else:
        print_projects_md(projs, args.limit)


if __name__ == "__main__":
    main()
