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

import decisions as dec_mod
import live_memory
import multi_machine
import remote_client
import search_fts
from project_identity import ProjectCatalog, edit_registry, item_name, load_registry, name_key
from provenance import SOURCE_ARCHIVE_MISSING, SOURCE_PENDING, SOURCE_RECORDED, SourceIndex, format_sources, source_key

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("DAILY_MEMORY_CONFIG_PATH") or os.path.join(BASE_DIR, "config.json")
EXAMPLE_CONFIG_PATH = os.path.join(BASE_DIR, "config.example.json")
DEFAULT_DATA_DIR = os.environ.get("DAILY_MEMORY_DATA_DIR") or os.path.join(BASE_DIR, "data")
DEFAULT_INDEX_PATH = os.path.expanduser("~/Documents/DailyReport/MEMORY_INDEX.md")
ACTIVE_STATUSES = {"进行中", "待开启定时", "待验证", "阻塞", "待跟进"}


def load_config(config_path: str = CONFIG_PATH) -> dict[str, Any]:
    path = config_path if os.path.exists(config_path) else EXAMPLE_CONFIG_PATH
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


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


def project_candidates(data_dir: str) -> list[dict[str, Any]]:
    days = load_all_days(data_dir)
    catalog = ProjectCatalog(days, load_registry(data_dir))
    projects = aggregate_projects(days, catalog=catalog)
    seen = {p["project_id"] for p in projects}
    fields = ("project_id", "project", "aliases", "paths", "scope", "category", "last_date", "status", "next_step")
    candidates = [
        {**{k: p[k] for k in fields}, "sessions": "；".join(p["recent_sessions"])[:160]} for p in projects
    ]
    return candidates + [p for p in catalog.projects.values() if p["project_id"] not in seen]


def load_raw_day(workday: str, data_dir: str = DEFAULT_DATA_DIR) -> dict[str, Any] | None:
    try:
        if datetime.fromisoformat(workday).date().isoformat() != workday:
            return None
    except ValueError:
        return None
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


def format_paths_cell(paths: list[str] | None, limit: int = 110) -> str:
    if not paths:
        return "-"
    rendered = "；".join(f"`{clean_cell(p, 72)}`" for p in paths[:3])
    if len(paths) > 3:
        rendered += f" 等{len(paths)}处"
    if len(rendered) <= limit:
        return rendered
    return f"`{clean_cell(paths[0], limit - 8)}`" + (f" (+{len(paths) - 1})" if len(paths) > 1 else "")


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


def _matches_machine(item_machine: str | None, day_machines: Any, machine_filter: str | None) -> bool:
    if not machine_filter:
        return True
    target = multi_machine.normalize_machine_id(machine_filter)
    if item_machine:
        return multi_machine.normalize_machine_id(str(item_machine)) == target
    if isinstance(day_machines, list) and day_machines:
        return any(multi_machine.normalize_machine_id(str(m)) == target for m in day_machines)
    return False


def aggregate_projects(
    days: list[dict[str, Any]],
    scope: str = "all",
    machine_filter: str | None = None,
    catalog: ProjectCatalog | None = None,
) -> list[dict[str, Any]]:
    """Aggregate chronological day summaries into per-project current-state snapshots."""
    ordered_days = sorted(days, key=lambda d: str(d.get("workday", "")))
    catalog = catalog if catalog is not None else ProjectCatalog(ordered_days)
    by_proj: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)

    for day in ordered_days:
        day_machines = day.get("machines")
        for it in day.get("work_items") or []:
            item_scope = str(it.get("scope") or "work")
            if scope != "all" and item_scope != scope:
                continue
            if not _matches_machine(it.get("machine"), day_machines, machine_filter):
                continue
            identity = catalog.for_item(it)
            by_proj[identity["project_id"]].append((day, it))

    results: list[dict[str, Any]] = []
    for identity_id, pairs in by_proj.items():
        identity = catalog.projects.get(identity_id) or catalog.for_item(pairs[0][1])
        proj = identity["project"]
        dates = [str(d["workday"]) for d, _ in pairs]
        first_d = min(dates)
        last_d = max(dates)
        span_days = len(set(dates))

        cats: dict[str, int] = defaultdict(int)
        scopes: dict[str, int] = defaultdict(int)
        tools: list[str] = []
        seen_tools: set[str] = set()
        machines: list[str] = []
        seen_machines: set[str] = set()
        last_machine = ""
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
            mach = str(it.get("machine") or "").strip()
            if mach:
                last_machine = mach
                if mach not in seen_machines:
                    seen_machines.add(mach)
                    machines.append(mach)
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
        last_day = last_day_pairs[-1][0]
        briefs = catalog.briefs(last_day.get("project_briefs") or {})
        latest_brief = _extract_latest_brief(proj, {"project_briefs": briefs}, [it for _, it in last_day_pairs])

        results.append(
            {
                **identity,
                "scope": max(scopes.items(), key=lambda kv: kv[1])[0],
                "category": max(cats.items(), key=lambda kv: kv[1])[0],
                "first_date": first_d,
                "last_date": last_d,
                "days": span_days,
                "total_hours": round(total_h, 2),
                "status": latest_status,
                "next_step": latest_next,
                "tools": tools,
                "machines": machines,
                "last_machine": last_machine,
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
    catalog: ProjectCatalog | None = None,
    live_days: list[dict[str, Any]] | None = None,
    active_decisions: list[dict[str, Any]] | None = None,
) -> str:
    ordered_days = sorted(days, key=lambda d: str(d.get("workday", "")))
    archived_workdays = {str(d.get("workday", "")) for d in ordered_days}
    catalog = catalog if catalog is not None else ProjectCatalog(ordered_days)
    work_projs = aggregate_projects(ordered_days, scope="work", catalog=catalog)
    personal_projs = aggregate_projects(ordered_days, scope="personal", catalog=catalog)
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
        "# MEMORY_INDEX · 跨端 AI 记忆中枢（Agent Context Bus）全项目快照",
        "",
        "> 由 **跨端 AI 记忆中枢（Agent Context Bus）** 流水线自动生成，请勿手工编辑（下次运行会覆盖）。",
        f"> 覆盖范围：`{first_d}` ~ `{last_d}`（共 **{total_days} 天**）｜ 正式工作：**{len(work_projs)} 项** ｜ 个人技术与折腾：**{len(personal_projs)} 项** ｜ 进行中/待跟进：**{len(active_projs)} 项** ｜ 更新时间：`{now_str}`",
        "",
        "## 按需下钻检索（CLI）",
        "",
        "- 查某项目完整时间线与关键路径：`python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --project \"<项目关键词>\"`",
        "- 跨项目/随想搜关键词（加 `--raw` 可查会话 UUID、用户指令、Git 提交与改动文件）：`python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --keyword \"<关键词>\" --raw`",
        "- 为项目登记/更新物理目录与关键配置文件：`python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --add-path \"<项目名>\" \"<路径1>\" \"<路径2>\"`",
        "- 立即记一笔今日关键结论 / 强制刷新实时层：`python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --note \"<项目名>\" \"<关键结论>\"` ｜ `--live-sync`",
        "",
    ]

    active_live = [
        ld for ld in (live_days or [])
        if str(ld.get("workday") or "") not in archived_workdays
        and (ld.get("notes") or ld.get("sessions") or ld.get("git"))
    ]
    for ld in active_live[:2]:
        wd = str(ld.get("workday") or "")
        upd = str(ld.get("updated_at") or "")
        upd_short = upd[11:16] if len(upd) >= 16 else upd
        lines.append(f"## 今日实时动态（`{wd}` · 0 Token 实时层，压实前生效｜刷新 `{upd_short or '-'}`）")
        lines.append("")
        notes = list(ld.get("notes") or [])[:10]
        if notes:
            lines.append("**关键笔记（`--note`）**")
            for n in notes:
                p_lbl = clean_cell(n.get("project"), 40)
                t_lbl = clean_cell(n.get("time"), 8)
                txt = clean_cell(n.get("text"), 140)
                lines.append(f"- `{t_lbl}` **{p_lbl}**：{txt}")
            lines.append("")
        sessions = list(ld.get("sessions") or [])[:8]
        if sessions:
            lines.extend(
                [
                    "| 时间 | 客户端 | 会话标题 | 工作区 | 最新动态 / 摘要 |",
                    "|---|---|---|---|---|",
                ]
            )
            for s in sessions:
                summary = s.get("summary") or s.get("last_input") or "-"
                ws = f"`{clean_cell(s.get('workspace'), 40)}`" if s.get("workspace") else "-"
                lines.append(
                    f"| `{clean_cell(s.get('time'), 8)}` | {clean_cell(s.get('tool'), 14)} "
                    f"| {clean_cell(s.get('title'), 42)} | {ws} | {clean_cell(summary, 95)} |"
                )
            lines.append("")
        commits = list(ld.get("git") or [])[:8]
        if commits:
            lines.append("**今日 Git 提交**")
            for g in commits:
                lines.append(
                    f"- `{clean_cell(g.get('time'), 8)}` **{clean_cell(g.get('repo_name'), 28)}** "
                    f"`{clean_cell(g.get('hash'), 10)}` {clean_cell(g.get('subject'), 90)}"
                )
            lines.append("")

    if active_projs:
        lines.extend(
            [
                "## 活跃与待跟进项目（置顶）",
                "",
                "| 维度 | 项目名称 | 最近更新 | 状态 | 关键路径 | 下一步 / 卡点 | 最近进展摘要 |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for p in active_projs:
            scope_lbl = "正式工作" if p["scope"] == "work" else "个人技术"
            lines.append(
                f"| {scope_lbl} | {clean_cell(p['project'], 48)} | `{p['last_date']}` ({p['days']}d) "
                f"| {clean_cell(p['status'], 16)} | {format_paths_cell(p.get('paths'))} | {clean_cell(p['next_step'], 60)} "
                f"| {clean_cell(p['latest_summary'], 110)} |"
            )
        lines.append("")

    top_decisions = [d for d in (active_decisions or []) if str(d.get("status") or "active") == "active"][:12]
    if top_decisions:
        kind_badge = {"decision": "决策", "constraint": "硬约束", "pitfall": "避坑"}
        lines.extend(["## 关键约束与长期决策（有效置顶）", ""])
        for d in top_decisions:
            badge = kind_badge.get(str(d.get("kind") or "decision"), "决策")
            scope_lbl = "全局" if d.get("scope") == "global" else clean_cell(d.get("project"), 32)
            lines.append(f"- `{d.get('id', '')}` **{badge}** [{scope_lbl}]：{clean_cell(d.get('text'), 130)}")
        lines.append("")

    def append_scope_table(title: str, projs: list[dict[str, Any]]) -> None:
        lines.extend([f"## {title}（{len(projs)} 项，按最近更新倒序）", ""])
        if not projs:
            lines.extend(["_暂无记录_", ""])
            return
        lines.extend(
            [
                "| 项目名称 | 分类 | 覆盖日期（天数） | 状态 | 关键路径 | 下一步 / 卡点 | 最近进展与关键产出 |",
                "|---|---|---|---|---|---|---|",
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
                f"| {clean_cell(p['status'], 14)} | {format_paths_cell(p.get('paths'))} | {clean_cell(p['next_step'], 52)} "
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
    catalog: ProjectCatalog | None = None,
    data_dir: str | None = None,
    live_days: list[dict[str, Any]] | None = None,
    active_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    target = os.path.expanduser(output_path or resolve_index_path())
    ordered = sorted(days, key=lambda d: str(d.get("workday", "")))
    if live_days is None and data_dir:
        archived = {str(d.get("workday", "")) for d in ordered}
        live_days = multi_machine.load_merged_live_days(data_dir, archived_workdays=archived)
    if active_decisions is None and data_dir:
        active_decisions = dec_mod.query_decisions(data_dir, limit=12)
    content = render_memory_index(
        days,
        tz_name=tz_name,
        catalog=catalog,
        live_days=live_days,
        active_decisions=active_decisions,
    )
    out_dir = os.path.dirname(os.path.abspath(target))
    os.makedirs(out_dir, exist_ok=True)
    tmp_path = target + ".tmp.md"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp_path, target)

    work_cnt = len(aggregate_projects(ordered, scope="work", catalog=catalog))
    personal_cnt = len(aggregate_projects(ordered, scope="personal", catalog=catalog))
    return {
        "path": target,
        "days": len(ordered),
        "work_projects": work_cnt,
        "personal_projects": personal_cnt,
        "live_days": len(live_days or []),
        "bytes": len(content.encode("utf-8")),
    }


def source_fields(item: dict[str, Any]) -> dict[str, Any]:
    sources = item.get("sources") or []
    status = SOURCE_RECORDED if sources else SOURCE_PENDING
    if sources and item.get("source_status") == SOURCE_ARCHIVE_MISSING:
        status = SOURCE_ARCHIVE_MISSING
    return {"sources": sources, "source_status": status}


def resolve_item_sources(
    items_by_day: dict[str, list[dict[str, Any]]], data_dir: str, machine_filter: str | None,
) -> list[dict[str, Any]]:
    details = []
    for workday, items in items_by_day.items():
        if not any(item.get("sources") for item in items):
            continue
        raw = load_raw_day(workday, data_dir=data_dir) or {}
        index = SourceIndex(raw)
        sessions, repos, files = {}, {}, {}
        for item in items:
            expected = 0
            resolved_refs = []
            for ref in item.get("sources") or []:
                if not _matches_machine(ref.get("machine"), raw.get("machines"), machine_filter):
                    continue
                expected += 1
                match = index.lookup(ref)
                if match is None and item.get("source_status") == SOURCE_ARCHIVE_MISSING:
                    match = index.resolve(ref)
                if not match:
                    continue
                canonical, record = match
                resolved_refs.append(canonical)
                kind, identifier, machine, bucket, repo_path = source_key(canonical)
                if kind == "session":
                    sessions[source_key(canonical)] = {
                        "source": record.get("source") or bucket,
                        "machine": machine,
                        "id": identifier,
                        "title": record.get("display_title") or record.get("title") or "",
                        "workspace": canonical["workspace"],
                        "active_span": record.get("active_span", ""),
                        "user_prompts": (record.get("user_prompts") or [])[:4],
                        "assistant_milestones": (record.get("assistant_milestones") or [])[:3],
                    }
                elif kind == "commit":
                    repo, commit = record
                    repo_info = repos.setdefault((machine, repo_path), {
                        "repo": repo_path, "repo_name": repo.get("repo_name") or repo_path,
                        "machine": machine, "commits": {},
                    })
                    repo_info["commits"][identifier] = {
                        "hash": identifier, "time": commit.get("time", ""),
                        "subject": commit.get("subject") or commit.get("message") or "",
                        "files": canonical["files"],
                    }
                else:
                    group = record.get("group", "")
                    files[source_key(canonical)] = {
                        "group": group, "machine": machine, "latest_time": record.get("latest_time", ""),
                        "sample_files": [identifier],
                    }
            if len(resolved_refs) < expected:
                item["raw_sources_unavailable"] = expected - len(resolved_refs)
            elif resolved_refs and not machine_filter and item.get("source_status") == SOURCE_ARCHIVE_MISSING:
                item["sources"] = resolved_refs
                item["source_status"] = SOURCE_RECORDED
        if sessions or repos or files:
            details.append({
                "workday": workday,
                "sessions": list(sessions.values()),
                "git": [{**repo, "commits": list(repo["commits"].values())} for repo in repos.values()],
                "files": list(files.values()),
            })
    return details


def raw_for_results(
    items: list[dict[str, Any]], data_dir: str, machine_filter: str | None,
) -> list[dict[str, Any]]:
    items_by_day = defaultdict(list)
    for item in items:
        items_by_day[item["workday"]].append(item)
    return resolve_item_sources(items_by_day, data_dir, machine_filter)


def query_project_timeline(
    days: list[dict[str, Any]],
    project_query: str,
    scope: str = "all",
    limit: int = 30,
    asc: bool = False,
    include_raw: bool = False,
    data_dir: str = DEFAULT_DATA_DIR,
    status_filter: str | None = None,
    machine_filter: str | None = None,
    catalog: ProjectCatalog | None = None,
) -> dict[str, Any]:
    query_key = name_key(project_query)
    terms = query_key.split()
    catalog = catalog if catalog is not None else ProjectCatalog(days, load_registry(data_dir))
    exact_match = catalog.lookup(project_query) if query_key in catalog.confirmed_names or query_key in catalog.by_id else None
    entries: list[dict[str, Any]] = []
    matched_projects: dict[str, dict] = {}

    for d in sorted(days, key=lambda x: str(x.get("workday", "")), reverse=not asc):
        wd = str(d.get("workday", ""))
        day_machines = d.get("machines")
        for it in d.get("work_items") or []:
            item_scope = str(it.get("scope") or "work")
            if scope not in ("all", "life") and item_scope != scope:
                continue
            if status_filter and status_filter not in str(it.get("status") or ""):
                continue
            if not _matches_machine(it.get("machine"), day_machines, machine_filter):
                continue
            identity = catalog.for_item(it)
            if exact_match is not None:
                matched = identity["project_id"] == exact_match["project_id"]
            else:
                matched = any(matches_terms(name_key(label), terms)
                              for label in [identity["project"], *identity["aliases"]])
            if matched:
                matched_projects[identity["project_id"]] = identity
                entries.append(
                    {
                        "workday": wd,
                        "weekday": d.get("weekday", ""),
                        "scope": item_scope,
                        **identity,
                        "project_label": item_name(it),
                        "category": it.get("category", ""),
                        "tool": it.get("tool") or it.get("source") or "",
                        "machine": it.get("machine", ""),
                        "session_name": it.get("session_name", ""),
                        "status": it.get("status", ""),
                        "summary": it.get("summary", ""),
                        "outputs": it.get("outputs", ""),
                        "next_step": it.get("next_step", ""),
                        "active_span": it.get("active_span", ""),
                        "estimated_hours": it.get("estimated_hours", 0),
                        **source_fields(it),
                    }
                )

    sliced = entries[:limit]
    raw_details = raw_for_results(sliced, data_dir, machine_filter) if include_raw else []

    projects = sorted(matched_projects.values(), key=lambda p: p["project"])
    archived = {str(d.get("workday", "")) for d in days}
    uncompacted = [
        *multi_machine.load_merged_live_days(data_dir, archived_workdays=archived),
        *multi_machine.load_archived_live_note_days(data_dir, archived_workdays=archived),
    ]
    live_hits: list[dict[str, Any]] = []
    if uncompacted:
        if len(projects) == 1:
            live_hits = live_memory.search_live_entries(uncompacted, project_identity=projects[0])
        elif exact_match is not None:
            live_hits = live_memory.search_live_entries(uncompacted, project_identity=exact_match)
        else:
            live_hits = live_memory.search_live_entries(uncompacted, terms=terms)

    target_ident = projects[0] if len(projects) == 1 else exact_match
    proj_decs = (
        dec_mod.query_decisions(data_dir, project_identity=target_ident, include_inactive=True, limit=30)
        if target_ident is not None
        else dec_mod.query_decisions(data_dir, project=project_query, include_inactive=True, limit=30, catalog=catalog)
    )
    active_decs = [d for d in proj_decs if d.get("status") == "active"]
    superseded_decs = [d for d in proj_decs if d.get("status") == "superseded"]

    return {
        "query": project_query,
        "matched_projects": [p["project"] for p in projects],
        "matched_identities": projects,
        "total_matches": len(entries),
        "entries": sliced,
        "live": live_hits[:10],
        "decisions": active_decs[:15],
        "superseded_decisions": superseded_decs[:10],
        "raw": raw_details,
    }


def query_keyword(
    days: list[dict[str, Any]],
    keyword: str,
    scope: str = "all",
    limit: int = 15,
    include_raw: bool = False,
    data_dir: str = DEFAULT_DATA_DIR,
    status_filter: str | None = None,
    machine_filter: str | None = None,
    asc: bool = False,
    catalog: ProjectCatalog | None = None,
    use_fts: bool = True,
) -> dict[str, Any]:
    terms = parse_terms(keyword)
    catalog = catalog if catalog is not None else ProjectCatalog(days, load_registry(data_dir))
    exact_project = catalog.lookup(keyword)
    archived = {str(d.get("workday", "")) for d in days}

    # Prefer SQLite FTS5 (unicode61 + trigram + BM25 + RRF) when disk store exists for data_dir
    disk_master = os.path.join(data_dir, "summary_all_history.json") if data_dir else ""
    if use_fts and not asc and data_dir and os.path.exists(disk_master):
        try:
            date_bounds = sorted(str(d.get("workday") or "") for d in days if d.get("workday"))
            date_from = date_bounds[0] if date_bounds else None
            date_to = date_bounds[-1] if date_bounds else None
            fts_results = search_fts.search(
                data_dir,
                keyword,
                doc_types=("work_item", "life_item", "live_note", "live_session", "live_git", "decision"),
                scope=scope,
                status_filter=status_filter,
                machine_filter=machine_filter,
                date_from=date_from,
                date_to=date_to,
                limit=max(limit * 2, 30),
                auto_sync_structured=True,
            )
            hits_fts: list[dict[str, Any]] = []
            live_fts: list[dict[str, Any]] = []
            decs_fts: list[dict[str, Any]] = []
            allowed_days = {str(d.get("workday") or "") for d in days}
            for item in fts_results:
                dt = item["doc_type"]
                p = item["payload"]
                if dt == "decision":
                    if p.get("status") == "active":
                        decs_fts.append(p)
                elif dt in ("live_note", "live_session", "live_git"):
                    if str(p.get("workday") or "") not in archived:
                        live_fts.append(p)
                elif dt == "work_item":
                    if allowed_days and str(p.get("workday") or "") not in allowed_days:
                        continue
                    ident = catalog.for_item(p)
                    hits_fts.append(
                        {
                            "kind": "work_item",
                            "workday": p.get("workday", ""),
                            "scope": p.get("scope", "personal"),
                            **ident,
                            "project_label": p.get("project", ""),
                            "tool": p.get("tool", ""),
                            "machine": p.get("machine", ""),
                            "session_name": p.get("session_name", ""),
                            "status": p.get("status", ""),
                            "summary": p.get("summary", ""),
                            "outputs": p.get("outputs", ""),
                            "next_step": p.get("next_step", ""),
                            "score": item["score"],
                            **source_fields(p),
                        }
                    )
                elif dt == "life_item":
                    if allowed_days and str(p.get("workday") or "") not in allowed_days:
                        continue
                    hits_fts.append(
                        {
                            "kind": "life_item",
                            "workday": p.get("workday", ""),
                            "scope": "life",
                            "project": p.get("topic", ""),
                            "tool": p.get("tool", ""),
                            "machine": p.get("machine", ""),
                            "session_name": p.get("session_name", ""),
                            "status": "-",
                            "summary": p.get("summary", ""),
                            "outputs": "-",
                            "next_step": "-",
                            "score": item["score"],
                            **source_fields(p),
                        }
                    )
            if hits_fts or live_fts or decs_fts:
                sliced_fts = hits_fts[:limit]
                raw_hits = raw_for_results(sliced_fts, data_dir, machine_filter) if include_raw else []
                return {
                    "query": keyword,
                    "scope": scope,
                    "search_mode": "fts5",
                    "total_matches": len(hits_fts),
                    "hits": sliced_fts,
                    "live": live_fts[:10],
                    "decisions": decs_fts[:8],
                    "raw": raw_hits,
                }
        except Exception:
            pass

    hits: list[dict[str, Any]] = []

    for d in sorted(days, key=lambda x: str(x.get("workday", "")), reverse=not asc):
        wd = str(d.get("workday", ""))
        day_machines = d.get("machines")
        day_matched = False

        if scope in ("all", "work", "personal"):
            for it in d.get("work_items") or []:
                item_scope = str(it.get("scope") or "work")
                if scope != "all" and item_scope != scope:
                    continue
                if status_filter and status_filter not in str(it.get("status") or ""):
                    continue
                if not _matches_machine(it.get("machine"), day_machines, machine_filter):
                    continue
                identity = catalog.for_item(it)
                blob = " ".join(
                    str(it.get(k) or "")
                    for k in ("project", "category", "tool", "machine", "session_name", "summary", "outputs", "next_step")
                )
                names = " ".join([identity["project"], *identity["aliases"], *(identity.get("paths") or [])])
                if identity == exact_project or matches_terms(f"{blob} {names}", terms):
                    day_matched = True
                    hits.append(
                        {
                            "kind": "work_item",
                            "workday": wd,
                            "scope": item_scope,
                            **identity,
                            "project_label": item_name(it),
                            "tool": it.get("tool") or it.get("source") or "",
                            "machine": it.get("machine", ""),
                            "session_name": it.get("session_name", ""),
                            "status": it.get("status", ""),
                            "summary": it.get("summary", ""),
                            "outputs": it.get("outputs", ""),
                            "next_step": it.get("next_step", ""),
                            **source_fields(it),
                        }
                    )

        if scope in ("all", "personal", "life") and not status_filter:
            for li in d.get("life_items") or []:
                if not _matches_machine(li.get("machine"), day_machines, machine_filter):
                    continue
                blob = " ".join(str(li.get(k) or "") for k in ("tool", "machine", "session_name", "topic", "summary"))
                if matches_terms(blob, terms):
                    day_matched = True
                    hits.append(
                        {
                            "kind": "life_item",
                            "workday": wd,
                            "scope": "life",
                            "project": li.get("topic", ""),
                            "tool": li.get("tool") or li.get("source") or "",
                            "machine": li.get("machine", ""),
                            "session_name": li.get("session_name", ""),
                            "status": "-",
                            "summary": li.get("summary", ""),
                            **source_fields(li),
                            "outputs": "-",
                            "next_step": "-",
                        }
                    )

        if not day_matched and scope == "all" and not status_filter and not machine_filter:
            day_blob = f"{d.get('headline', '')} {d.get('core_outputs', '')}"
            if matches_terms(day_blob, terms):
                hits.append(
                    {
                        "kind": "day_headline",
                        "workday": wd,
                        "scope": "all",
                        "project": d.get("headline", ""),
                        "tool": "-",
                        "machine": "",
                        "session_name": "-",
                        "status": "-",
                        "summary": d.get("core_outputs", ""),
                        "outputs": "-",
                        "next_step": "-",
                    }
                )

    sliced = hits[:limit]
    raw_hits = raw_for_results(sliced, data_dir, machine_filter) if include_raw else []
    uncompacted = [
        *multi_machine.load_merged_live_days(data_dir, archived_workdays=archived),
        *multi_machine.load_archived_live_note_days(data_dir, archived_workdays=archived),
    ]
    live_hits = (
        live_memory.search_live_entries(uncompacted, terms=terms)
        if uncompacted and not status_filter and not machine_filter
        else []
    )
    matched_decs = dec_mod.query_decisions(data_dir, keyword=keyword, limit=8) if data_dir else []

    return {
        "query": keyword,
        "scope": scope,
        "search_mode": "exact",
        "total_matches": len(hits),
        "hits": sliced,
        "live": live_hits[:10],
        "decisions": matched_decs,
        "raw": raw_hits,
    }


def query_projects(
    days: list[dict[str, Any]],
    *,
    scope: str = "all",
    status_filter: str | None = None,
    machine_filter: str | None = None,
    keyword: str | None = None,
    catalog: ProjectCatalog | None = None,
) -> list[dict[str, Any]]:
    proj_scope = "all" if scope == "life" else scope
    projs = aggregate_projects(days, scope=proj_scope, machine_filter=machine_filter, catalog=catalog)
    if status_filter:
        projs = [p for p in projs if status_filter in p["status"]]
    if keyword:
        terms = parse_terms(keyword)
        projs = [
            p
            for p in projs
            if matches_terms(
                f"{p['project']} {' '.join(p['aliases'])} {' '.join(p.get('paths') or [])} {p['project_id']} "
                f"{p['category']} {p['latest_summary']} {p['next_step']}",
                terms,
            )
        ]
    return projs


def query_by_project(
    days: list[dict[str, Any]],
    project_query: str,
    *,
    scope: str = "all",
    status_filter: str | None = None,
    machine_filter: str | None = None,
    include_raw: bool = False,
    data_dir: str = DEFAULT_DATA_DIR,
    asc: bool = False,
    limit: int = 30,
    catalog: ProjectCatalog | None = None,
) -> dict[str, Any]:
    return query_project_timeline(
        days,
        project_query,
        scope=scope,
        limit=limit,
        asc=asc,
        include_raw=include_raw,
        data_dir=data_dir,
        status_filter=status_filter,
        machine_filter=machine_filter,
        catalog=catalog,
    )


def query_by_keyword(
    days: list[dict[str, Any]],
    keyword: str,
    *,
    scope: str = "all",
    status_filter: str | None = None,
    machine_filter: str | None = None,
    include_raw: bool = False,
    data_dir: str = DEFAULT_DATA_DIR,
    asc: bool = False,
    limit: int = 15,
    catalog: ProjectCatalog | None = None,
    use_fts: bool = True,
) -> dict[str, Any]:
    return query_keyword(
        days,
        keyword,
        scope=scope,
        limit=limit,
        include_raw=include_raw,
        data_dir=data_dir,
        status_filter=status_filter,
        machine_filter=machine_filter,
        asc=asc,
        catalog=catalog,
        use_fts=use_fts,
    )


def query_exact_date(
    days: list[dict[str, Any]],
    exact_date: str,
    *,
    include_raw: bool = False,
    data_dir: str = DEFAULT_DATA_DIR,
    machine_filter: str | None = None,
    keyword: str | None = None,
    catalog: ProjectCatalog | None = None,
) -> dict[str, Any]:
    day = next((d for d in days if str(d.get("workday")) == exact_date), None)
    if day is None:
        return {"workday": exact_date, "day": None, "raw": None}
    catalog = catalog if catalog is not None else ProjectCatalog(days, load_registry(data_dir))
    day = dict(day)
    for collection in ("work_items", "life_items"):
        day[collection] = [
            {**item, **source_fields(item)} for item in day.get(collection) or []
            if _matches_machine(item.get("machine"), day.get("machines"), machine_filter)
        ]
    day["work_items"] = [
        {**item, **catalog.for_item(item), "project_label": item_name(item)}
        for item in day["work_items"]
    ]
    day["project_briefs"] = catalog.briefs(day.get("project_briefs") or {})
    raw_m = None
    if include_raw:
        terms = parse_terms(keyword)
        items = [
            item for item in [*day["work_items"], *day["life_items"]]
            if matches_terms(" ".join(str(item.get(key) or "") for key in (
                "project", "topic", "session_name", "summary", "outputs",
            )), terms)
        ]
        raw_m = next(iter(resolve_item_sources({exact_date: items}, data_dir, machine_filter)), None)
    return {"workday": exact_date, "day": day, "raw": raw_m}


def format_raw_section(raw_items: list[dict[str, Any]]) -> list[str]:
    if not raw_items:
        return []
    lines = ["", "### Raw 原始溯源（会话 ID / 工作区 / Git / 文件）"]
    for rm in raw_items:
        lines.append(f"\n#### `{rm['workday']}`")
        for s in rm.get("sessions") or []:
            ws = f" | workspace: `{s['workspace']}`" if s.get("workspace") else ""
            mach = f"@{s['machine']}" if s.get("machine") else ""
            lines.append(f"- **[{s['source']}{mach}]** {s['title']} (id: `{s['id']}`{ws})")
            for p in (s.get("user_prompts") or [])[:2]:
                lines.append(f"  - 用户指令：{clean_cell(p, 120)}")
            for m in (s.get("assistant_milestones") or [])[:2]:
                lines.append(f"  - 里程碑：{clean_cell(m, 120)}")
        for g in rm.get("git") or []:
            mach = f"@{g['machine']}" if g.get("machine") else ""
            lines.append(f"- **Git{mach} `{g['repo_name']}`** (`{g['repo']}`)")
            for c in g.get("commits") or []:
                f_preview = ", ".join(c["files"][:5])
                lines.append(f"  - `{c['hash']}` {c['time']} {c['subject']}" + (f" [{f_preview}]" if f_preview else ""))
        for fg in rm.get("files") or []:
            mach = f"@{fg['machine']}" if fg.get("machine") else ""
            lines.append(f"- **本地文件{mach} `{fg['group']}`** ({fg['latest_time']}): {', '.join(fg['sample_files'][:6])}")
    return lines


def format_projects_md(projs: list[dict[str, Any]], limit: int = 50) -> str:
    sliced = projs[:limit]
    lines = [f"### 匹配项目（显示 {len(sliced)} / 共 {len(projs)} 项）", ""]
    if not sliced:
        lines.append("_无匹配结果_")
        return "\n".join(lines)
    lines.extend(
        [
            "| 维度 | 项目名称 | 覆盖日期（天数） | 状态 | 关键路径 | 下一步 / 卡点 | 最近进展 |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for p in sliced:
        scope_lbl = "工作" if p["scope"] == "work" else "个人"
        mach_suffix = f" `@{p['last_machine']}`" if p.get("last_machine") else ""
        lines.append(
            f"| {scope_lbl} | {clean_cell(p['project'], 44)}{mach_suffix} | `{p['first_date']}`~`{p['last_date']}` ({p['days']}d) "
            f"| {clean_cell(p['status'], 14)} | {format_paths_cell(p.get('paths'))} | {clean_cell(p['next_step'], 50)} | {clean_cell(p['latest_summary'], 100)} |"
        )
    return "\n".join(lines)


def format_item_sources(item: dict[str, Any]) -> str:
    citations = format_sources(item.get("sources") or [], separator="；", status=item.get("source_status", ""))
    unavailable = item.get("raw_sources_unavailable", 0)
    if unavailable:
        citations += f"（{unavailable} 条原始记录当前不可用）"
    return f"  - 出处：{citations}"


def format_live_section(live_items: list[dict[str, Any]]) -> list[str]:
    if not live_items:
        return []
    lines = ["", "### 今日实时动态（未压实）"]
    for item in live_items:
        kind = item.get("kind")
        wd = item.get("workday", "")
        tm = item.get("time", "")
        if kind == "live_note":
            lines.append(f"- **`{wd} {tm}`** [今日笔记] **{item.get('project', '')}**：{item.get('text', '')}")
        elif kind == "live_session":
            ws = f" (`{item['workspace']}`)" if item.get("workspace") else ""
            summary = item.get("summary") or item.get("last_input") or ""
            lines.append(
                f"- **`{wd} {tm}`** [今日实时 | {item.get('tool', '')}] **{item.get('title', '')}**{ws}"
                + (f" — {summary}" if summary else "")
            )
        elif kind == "live_git":
            lines.append(
                f"- **`{wd} {tm}`** [今日 Git | {item.get('repo_name', '')}] `{item.get('hash', '')}` {item.get('subject', '')}"
            )
    return lines


def format_decisions_section(
    active_decs: list[dict[str, Any]],
    superseded_decs: list[dict[str, Any]] | None = None,
) -> list[str]:
    if not active_decs and not superseded_decs:
        return []
    kind_badge = {"decision": "决策", "constraint": "硬约束", "pitfall": "避坑"}
    lines = ["", "### 有效长期决策与避坑约束"]
    for d in active_decs:
        badge = kind_badge.get(str(d.get("kind") or "decision"), "决策")
        scope_lbl = "全局" if d.get("scope") == "global" else str(d.get("project") or "")
        conf = " [候选]" if d.get("confidence") == "candidate" else ""
        lines.append(f"- `{d.get('id', '')}` **{badge}** [{scope_lbl}]{conf}：{d.get('text', '')}")
    if superseded_decs:
        sup_ids = ", ".join(f"`{d.get('id', '')}`" for d in superseded_decs[:5])
        lines.append(f"- _已淘汰历史方案 {len(superseded_decs)} 条（{sup_ids}，可用 `--decisions --all-status` 查看）_")
    return lines


def format_project_query_md(res: dict[str, Any]) -> str:
    lines = [f"### 项目时间线：`{res['query']}`（匹配项目 {len(res['matched_projects'])} 个，共 {res['total_matches']} 条记录）"]
    if res["matched_projects"]:
        lines.append(f"> 项目名：{', '.join(res['matched_projects'])}")
    if len(res.get("matched_identities") or []) == 1:
        project = res["matched_identities"][0]
        lines.append(f"> 项目 ID：`{project['project_id']}`")
        if project["aliases"]:
            lines.append(f"> 别名：{'、'.join(project['aliases'])}")
        if project.get("paths"):
            lines.append(f"> 关键路径：{' ； '.join(f'`{p}`' for p in project['paths'])}")
    lines.extend(format_decisions_section(res.get("decisions") or [], res.get("superseded_decisions") or []))
    lines.extend(format_live_section(res.get("live") or []))
    lines.append("")
    if not res["entries"]:
        if not res.get("live") and not res.get("decisions"):
            lines.append("_无匹配记录_")
        return "\n".join(lines)
    for e in res["entries"]:
        scope_lbl = "工作" if e["scope"] == "work" else "个人"
        mach = f" | @{e['machine']}" if e.get("machine") else ""
        lines.append(
            f"- **`{e['workday']}`** [{scope_lbl} | {e['tool']}{mach} | {e['status']}] **{e['project']}** — `{e['session_name']}`"
        )
        if e["summary"]:
            lines.append(f"  - 进展：{e['summary']}")
        if e["outputs"] and e["outputs"] != "-":
            lines.append(f"  - 产出：{e['outputs']}")
        if e["next_step"] and e["next_step"] != "-":
            lines.append(f"  - 下一步：{e['next_step']}")
        lines.append(format_item_sources(e))
    lines.extend(format_raw_section(res.get("raw") or []))
    return "\n".join(lines)


def format_keyword_query_md(res: dict[str, Any]) -> str:
    mode_tag = f" | {res['search_mode']}" if res.get("search_mode") else ""
    lines = [
        f"### 关键词检索：`{res['query']}`（scope={res['scope']}{mode_tag}，共 {res['total_matches']} 条，展示 {len(res['hits'])} 条）",
        "",
    ]
    if not res["hits"] and not res.get("raw") and not res.get("live") and not res.get("decisions"):
        lines.append("_无匹配结果_")
        return "\n".join(lines)
    if res.get("decisions"):
        lines.extend(format_decisions_section(res["decisions"]))
        lines.append("")
    for h in res["hits"]:
        tag = "随想" if h["kind"] == "life_item" else ("当日主线" if h["kind"] == "day_headline" else h["scope"])
        mach = f" | @{h['machine']}" if h.get("machine") else ""
        lines.append(f"- **`{h['workday']}`** [{tag} | {h['tool']}{mach}] **{h['project']}** (`{h['session_name']}`)")
        if h["summary"]:
            lines.append(f"  - 摘要：{h['summary']}")
        if h["outputs"] and h["outputs"] != "-":
            lines.append(f"  - 产出：{h['outputs']}")
        if h["next_step"] and h["next_step"] != "-":
            lines.append(f"  - 下一步：{h['next_step']}")
        if h["kind"] != "day_headline":
            lines.append(format_item_sources(h))
    lines.extend(format_live_section(res.get("live") or []))
    lines.extend(format_raw_section(res.get("raw") or []))
    return "\n".join(lines)


def format_exact_date_md(res: dict[str, Any]) -> str:
    day = res.get("day")
    if not day:
        return f"_无 `{res.get('workday', '')}` 记录_"
    raw = res.get("raw")
    mach_part = f" ｜ 主机：`{', '.join(day['machines'])}`" if day.get("machines") else ""
    lines = [f"### `{day.get('workday')}` ({day.get('weekday', '')}) ｜ 活跃时段：`{day.get('active_span', '-')}`{mach_part}"]
    if day.get("headline"):
        lines.append(f"- **主线**：{day['headline']}")
    if day.get("core_outputs"):
        lines.append(f"- **核心产出**：{day['core_outputs']}")
    work_items = day.get("work_items") or []
    if work_items:
        lines.extend(["", "#### 项目明细"])
        for it in work_items:
            mach = f" | @{it['machine']}" if it.get("machine") else ""
            lines.append(
                f"- [{it.get('scope', 'work')} | {it.get('tool', '')}{mach} | {it.get('status', '')}] "
                f"**{it.get('project', '')}** (`{it.get('session_name', '')}`): {it.get('summary', '')}"
            )
            if it.get("outputs") and it.get("outputs") != "-":
                lines.append(f"  - 产出：{it['outputs']}")
            lines.append(format_item_sources(it))
    life_items = day.get("life_items") or []
    if life_items:
        lines.extend(["", "#### 随想与探索"])
        for li in life_items:
            mach = f" | @{li['machine']}" if li.get("machine") else ""
            lines.append(f"- [{li.get('tool', '')}{mach}] **{li.get('topic', '')}** (`{li.get('session_name', '')}`): {li.get('summary', '')}")
            lines.append(format_item_sources(li))
    if raw:
        lines.extend(format_raw_section([raw]))
    return "\n".join(lines)


def print_projects_md(projs: list[dict[str, Any]], limit: int) -> None:
    print(format_projects_md(projs, limit))


def print_project_timeline_md(res: dict[str, Any]) -> None:
    print(format_project_query_md(res))


def print_keyword_md(res: dict[str, Any]) -> None:
    print(format_keyword_query_md(res))


def print_day_md(day: dict[str, Any], raw: dict[str, Any] | None = None) -> None:
    print(format_exact_date_md({"workday": day.get("workday", ""), "day": day, "raw": raw}))


def _run_remote_query(args: argparse.Namespace, rc: remote_client.RemoteConfig) -> str:
    fmt = "json" if args.as_json else "md"
    if args.index:
        md, _ = remote_client.request_text(rc, "GET", "/api/v1/index", write=False)
        return md
    params: dict[str, Any] = {
        "scope": args.scope,
        "format": fmt,
        "limit": args.limit,
    }
    if args.date:
        params["date"] = args.date
    if args.days:
        params["days"] = args.days
    if args.date_from:
        params["from"] = args.date_from
    if args.date_to:
        params["to"] = args.date_to
    if args.status:
        params["status"] = args.status
    if args.machine:
        params["machine"] = args.machine
    if args.raw:
        params["raw"] = "sources"
    if args.asc:
        params["asc"] = "1"
    if args.exact:
        params["exact"] = "1"

    if args.project:
        params["project"] = args.project
        render = format_project_query_md
    elif args.keyword and not args.projects:
        params["keyword"] = args.keyword
        render = format_keyword_query_md
    elif args.date:
        render = format_exact_date_md
    else:
        render = None

    if render:
        params["format"] = "json"
        result, _ = remote_client.request_json(rc, "GET", "/api/v1/query", params=params, write=False)
        day = result.get("day") or {}
        for items in (result.get("entries"), result.get("hits"), day.get("work_items"), day.get("life_items")):
            for item in items or []:
                if item.get("kind") != "day_headline" and "source_status" not in item:
                    item.update(source_fields({}))
        return json.dumps(result, ensure_ascii=False, indent=2) if args.as_json else render(result)
    if args.keyword:
        params["keyword"] = args.keyword
    text, _ = remote_client.request_text(rc, "GET", "/api/v1/projects", params=params, write=False)
    return text


def run_project_review(args: argparse.Namespace, rc: remote_client.RemoteConfig, use_remote: bool) -> None:
    if args.project_review:
        if use_remote:
            result, _ = remote_client.request_json(rc, "GET", "/api/v1/project-review")
            pending = result["suggestions"]
        else:
            data_dir = args.data_dir or DEFAULT_DATA_DIR
            days = load_all_days(data_dir)
            pending = ProjectCatalog(days, load_registry(data_dir)).suggestions(days)
        if args.as_json:
            print(json.dumps({"suggestions": pending}, ensure_ascii=False, indent=2))
        elif pending:
            for suggestion in pending[: args.limit]:
                target = suggestion["suggested"]["project"]
                print(f"- 「{suggestion['label']}」→「{target}」"
                      f"（{', '.join(suggestion['dates'])}） {suggestion['reason']}".rstrip())
        else:
            print("暂无待确认的项目归类建议。")
        return

    if args.merge_projects is not None:
        action = "merge"
        target, *labels = args.merge_projects
    elif args.split_project is not None:
        action = "split"
        target, *labels = args.split_project
    elif args.add_path is not None:
        action = "add_paths"
        target, *labels = args.add_path
    elif args.remove_path is not None:
        action = "remove_paths"
        target, *labels = args.remove_path
    else:
        action = "set_paths"
        target, *labels = args.set_paths
    if use_remote:
        result, _ = remote_client.request_json(
            rc, "POST", "/api/v1/project-review",
            json_body={"action": action, "target": target, "labels": labels}, write=True,
        )
        project = result["project"]
    else:
        data_dir = args.data_dir or DEFAULT_DATA_DIR
        project = edit_registry(data_dir, load_all_days(data_dir), action, target, labels)
    if args.as_json:
        print(json.dumps(project, ensure_ascii=False, indent=2))
    else:
        print(f"已保存「{project['project']}」({project['project_id']})")
        if project["aliases"]:
            print(f"别名 {'、'.join(project['aliases'])}")
        if project.get("paths"):
            print(f"关键路径 {' ； '.join(project['paths'])}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI memory index generator and fast historical query CLI for daily-summary."
    )
    parser.add_argument("--data-dir", default=None, help="Path to local daily-summary data directory.")
    parser.add_argument("--rebuild-index", action="store_true", help="Rebuild MEMORY_INDEX.md from summary history.")
    parser.add_argument("--index-output", help="Override output path for --rebuild-index.")
    parser.add_argument("--index", action="store_true", help="Print latest MEMORY_INDEX.md (from NAS or local/cache).")
    parser.add_argument("--projects", action="store_true", help="List aggregated projects sorted by latest activity.")
    parser.add_argument("--project", help="Show a project timeline by name, alias, or stable ID.")
    decisions = parser.add_mutually_exclusive_group()
    decisions.add_argument("--project-review", action="store_true", help="List AI suggestions awaiting a decision.")
    decisions.add_argument("--merge-projects", nargs="+", metavar="PROJECT",
                           help="Merge one or more names into the first project.")
    decisions.add_argument("--split-project", nargs="+", metavar="NAME",
                           help="Restore an alias as a project, or split named aliases into the first name.")
    decisions.add_argument("--add-path", nargs="+", metavar="ARG",
                           help="Attach one or more physical directories or key config paths to a project (PROJECT PATH ...).")
    decisions.add_argument("--remove-path", nargs="+", metavar="ARG",
                           help="Remove one or more paths from a project (PROJECT PATH ...).")
    decisions.add_argument("--set-paths", nargs="+", metavar="ARG",
                           help="Replace a project's registered paths (PROJECT [PATH ...]; omit PATH to clear).")
    decisions.add_argument("--note", nargs=2, metavar=("PROJECT", "TEXT"),
                           help="Record a real-time same-day note into the 0-token live WAL and refresh MEMORY_INDEX.md.")
    decisions.add_argument("--live-sync", action="store_true",
                           help="Run a 0-token incremental scan of today's 4 clients + Git and refresh MEMORY_INDEX.md.")
    decisions.add_argument("--decision", nargs=2, metavar=("PROJECT", "TEXT"),
                           help="Record a persistent project-scoped decision/constraint/pitfall.")
    decisions.add_argument("--global-decision", metavar="TEXT",
                           help="Record a persistent global cross-project constraint/pitfall.")
    decisions.add_argument("--decisions", action="store_true",
                           help="List long-term decisions/constraints (filter with --project/--scope/--kind/--keyword).")
    decisions.add_argument("--supersede", nargs=2, metavar=("OLD_ID", "NEW_TEXT"),
                           help="Supersede an existing decision with a new chain head.")
    decisions.add_argument("--forget-decision", metavar="DEC_ID",
                           help="Soft-delete (tombstone) a decision by ID.")
    decisions.add_argument("--reindex", action="store_true",
                           help="Force rebuild the derived SQLite FTS5 search index (structured + recent 30d sessions).")
    decisions.add_argument("--doctor", action="store_true",
                           help="Run full health & integrity diagnostics across hooks, JSON stores, FTS5, and NAS.")
    parser.add_argument("--kind", choices=list(dec_mod.VALID_KINDS), default="decision",
                        help="Kind for --decision/--global-decision (decision|constraint|pitfall).")
    parser.add_argument("--tags", help="Comma-separated tags for --decision/--global-decision.")
    parser.add_argument("--supersedes", nargs="+", metavar="OLD_ID",
                        help="Old decision ID(s) superseded by a new --decision/--global-decision.")
    parser.add_argument("--all-status", action="store_true",
                        help="Include superseded/forgotten records when running --decisions.")
    parser.add_argument("--exact", action="store_true",
                        help="Bypass SQLite FTS5 ranking and use legacy strict substring AND matching.")
    parser.add_argument("--fix", action="store_true",
                        help="Apply safe, non-destructive repairs when running --doctor.")
    parser.add_argument("--note-path", nargs="+", metavar="PATH",
                        help="Optional path(s) to register alongside --note.")
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
    parser.add_argument("--status", help="Filter by status substring (e.g. 进行中).")
    parser.add_argument("--machine", help="Filter results by machine_id (e.g. mac, win-pc).")
    parser.add_argument("--raw", action="store_true", help="Open the archived evidence cited by each returned item.")
    parser.add_argument("--asc", action="store_true", help="Sort timeline entries oldest-first.")
    parser.add_argument("--limit", type=int, default=15, help="Max results to return (default: 15).")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Output raw JSON instead of Markdown.")
    parser.add_argument("--remote", action="store_true", help="Force querying remote NAS hub.")
    parser.add_argument("--local", action="store_true", help="Force querying local data directory only.")
    parser.add_argument("--remote-config", help="Path to remote.json override.")
    parser.add_argument("--sync-cache", action="store_true", help="Download latest NAS bundle into local offline cache.")
    parser.add_argument("--probe", action="store_true", help="Probe all configured remote endpoints and print latency.")
    args = parser.parse_args()

    cfg = load_config()
    rc = remote_client.load_remote_config(args.remote_config, project_cfg=cfg)

    if args.probe:
        results = remote_client.probe_endpoints(rc)
        if args.as_json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            if not results:
                print("_未配置远程端点（请检查 ~/.daily-memory/remote.json 或 DAILY_MEMORY_URLS）_")
            for r in results:
                icon = "OK" if r["ok"] else "FAIL"
                print(f"- [{icon}] **{r['name']}** `{r['url']}` ({r['latency_ms']} ms, HTTP {r['status']}) — {r['detail']}")
        return

    if args.sync_cache:
        res = remote_client.sync_local_cache(rc)
        if args.as_json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(
                f"Synced local offline cache from {res['endpoint_name']} ({res['endpoint_url']}): "
                f"{res['total_days']} days -> {res['index_path']}"
            )
        return

    if args.doctor:
        import doctor as doctor_mod

        target_data_dir = args.data_dir or DEFAULT_DATA_DIR
        report = doctor_mod.run_doctor(
            target_data_dir,
            cfg=cfg,
            remote_config_path=args.remote_config,
            fix=args.fix,
            index_output=args.index_output,
        )
        if args.as_json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(doctor_mod.format_doctor_md(report))
        return

    if args.reindex:
        target_data_dir = args.data_dir or DEFAULT_DATA_DIR
        days = load_all_days(target_data_dir)
        catalog = ProjectCatalog(days, load_registry(target_data_dir))
        s_res = search_fts.rebuild_structured_index(target_data_dir, days=days, catalog=catalog)
        sess_res = search_fts.sync_sessions_to_fts(
            target_data_dir,
            days=30,
            tz_name=str(cfg.get("timezone") or "Asia/Shanghai"),
            day_start_hour=int(cfg.get("day_start_hour", 5)),
            cfg=cfg,
        )
        out = {"structured": s_res, "sessions": sess_res}
        if args.as_json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(
                f"Rebuilt FTS5 index {s_res['db_path']} (trigram={s_res['trigram_enabled']}): "
                f"{s_res['structured_docs']} structured docs, "
                f"{sess_res['updated_files']} session files ({sess_res['indexed_chunks']} chunks)"
            )
        return

    if args.decision or args.global_decision or args.decisions or args.supersede or args.forget_decision:
        use_remote_dec = (
            not args.local
            and args.data_dir is None
            and (args.remote or rc.configured)
        )
        target_data_dir = args.data_dir or DEFAULT_DATA_DIR
        tz_name = str(cfg.get("timezone") or "Asia/Shanghai")
        try:
            if args.decisions:
                if use_remote_dec:
                    res, _ = remote_client.list_remote_decisions(
                        rc,
                        project=args.project,
                        scope=None if args.scope in ("all", "work", "personal", "life") else args.scope,
                        kind=args.kind if "--kind" in sys.argv else None,
                        include_inactive=args.all_status,
                        keyword=args.keyword,
                        limit=args.limit,
                    )
                    items = res.get("decisions") or []
                else:
                    days = load_all_days(target_data_dir)
                    catalog = ProjectCatalog(days, load_registry(target_data_dir))
                    items = dec_mod.query_decisions(
                        target_data_dir,
                        project=args.project,
                        kind=args.kind if "--kind" in sys.argv else None,
                        include_inactive=args.all_status,
                        keyword=args.keyword,
                        limit=args.limit,
                        catalog=catalog,
                    )
                if args.as_json:
                    print(json.dumps({"total": len(items), "decisions": items}, ensure_ascii=False, indent=2))
                else:
                    print(dec_mod.format_decisions_md(items))
                return

            if args.decision or args.global_decision:
                is_global = bool(args.global_decision)
                proj_arg = "" if is_global else args.decision[0]
                text_arg = args.global_decision if is_global else args.decision[1]
                if use_remote_dec:
                    res, _ = remote_client.mutate_remote_decision(
                        rc,
                        {
                            "action": "add",
                            "scope": "global" if is_global else "project",
                            "project": proj_arg,
                            "kind": args.kind,
                            "text": text_arg,
                            "tags": args.tags,
                            "supersedes": args.supersedes or [],
                        },
                    )
                    saved_dec = res["decision"]
                else:
                    days = load_all_days(target_data_dir)
                    saved_dec = dec_mod.add_decision(
                        target_data_dir,
                        days,
                        text=text_arg,
                        scope="global" if is_global else "project",
                        project=proj_arg,
                        kind=args.kind,
                        tags=args.tags,
                        supersedes=args.supersedes,
                        tz_name=tz_name,
                        cfg=cfg,
                    )
                    generate_memory_index(
                        days,
                        output_path=args.index_output or resolve_index_path(cfg),
                        tz_name=tz_name,
                        catalog=ProjectCatalog(days, load_registry(target_data_dir)),
                        data_dir=target_data_dir,
                    )
                if args.as_json:
                    print(json.dumps(saved_dec, ensure_ascii=False, indent=2))
                else:
                    print(f"已写入决策 `{saved_dec['id']}` [{saved_dec['kind']}]: {saved_dec['text']}")
                return

            if args.supersede:
                old_id, new_text = args.supersede
                if use_remote_dec:
                    res, _ = remote_client.mutate_remote_decision(
                        rc,
                        {
                            "action": "supersede",
                            "old_id": old_id,
                            "text": new_text,
                            "kind": args.kind if "--kind" in sys.argv else None,
                            "tags": args.tags,
                        },
                    )
                    saved_dec = res["decision"]
                else:
                    days = load_all_days(target_data_dir)
                    saved_dec = dec_mod.supersede_decision(
                        target_data_dir,
                        days,
                        old_id,
                        new_text,
                        kind=args.kind if "--kind" in sys.argv else None,
                        tags=args.tags,
                        tz_name=tz_name,
                        cfg=cfg,
                    )
                    generate_memory_index(
                        days,
                        output_path=args.index_output or resolve_index_path(cfg),
                        tz_name=tz_name,
                        catalog=ProjectCatalog(days, load_registry(target_data_dir)),
                        data_dir=target_data_dir,
                    )
                if args.as_json:
                    print(json.dumps(saved_dec, ensure_ascii=False, indent=2))
                else:
                    print(f"已淘汰 `{old_id}` -> 新决策 `{saved_dec['id']}`: {saved_dec['text']}")
                return

            if args.forget_decision:
                if use_remote_dec:
                    res, _ = remote_client.mutate_remote_decision(
                        rc,
                        {"action": "forget", "id": args.forget_decision},
                    )
                    saved_dec = res["decision"]
                else:
                    days = load_all_days(target_data_dir)
                    saved_dec = dec_mod.forget_decision(target_data_dir, args.forget_decision, tz_name=tz_name)
                    generate_memory_index(
                        days,
                        output_path=args.index_output or resolve_index_path(cfg),
                        tz_name=tz_name,
                        catalog=ProjectCatalog(days, load_registry(target_data_dir)),
                        data_dir=target_data_dir,
                    )
                if args.as_json:
                    print(json.dumps(saved_dec, ensure_ascii=False, indent=2))
                else:
                    print(f"已软删除决策 `{saved_dec['id']}`")
                return
        except (ValueError, RuntimeError, OSError) as exc:
            parser.exit(2, f"ERROR: {exc}\n")

    if args.note:
        target_data_dir = args.data_dir or DEFAULT_DATA_DIR
        days = load_all_days(target_data_dir)
        try:
            saved = live_memory.add_note(
                target_data_dir,
                days,
                args.note[0],
                args.note[1],
                paths=args.note_path,
                cfg=cfg,
            )
        except ValueError as exc:
            parser.exit(2, f"ERROR: {exc}\n")
        stats = generate_memory_index(
            days,
            output_path=args.index_output or resolve_index_path(cfg),
            tz_name=str(cfg.get("timezone") or "Asia/Shanghai"),
            catalog=ProjectCatalog(days, load_registry(target_data_dir)),
            data_dir=target_data_dir,
        )
        remote_note_res = None
        if not args.local and args.data_dir is None and rc.configured and rc.write_token:
            try:
                remote_note_res, _ = remote_client.add_remote_note(
                    rc,
                    args.note[0],
                    saved["note"]["text"],
                    paths=args.note_path,
                    workday=saved["workday"],
                )
            except Exception:
                remote_note_res = None
        if args.as_json:
            print(json.dumps({"workday": saved["workday"], "note": saved["note"], "index": stats, "remote": remote_note_res}, ensure_ascii=False, indent=2))
        else:
            n = saved["note"]
            remote_suffix = " (已同步 NAS)" if remote_note_res else ""
            print(f"已记入今日实时动态 ({saved['workday']} {n['time']}) [{n['project']}]: {n['text']}{remote_suffix}")
            print(f"已刷新 {stats['path']} ({stats['bytes']} bytes)")
        return

    if args.live_sync:
        target_data_dir = args.data_dir or DEFAULT_DATA_DIR
        res = live_memory.sync_live(
            target_data_dir,
            cfg=cfg,
            force_scan=True,
            rebuild_index=True,
            index_output=args.index_output,
            push_remote=(not args.local and args.data_dir is None and rc.configured and bool(rc.write_token)),
            remote_config_path=args.remote_config,
        )
        if args.as_json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(
                f"Live synced {res['workday']}: "
                f"{res['notes_count']} notes, {res['sessions_count']} sessions, {res['git_count']} commits -> "
                f"{(res.get('index') or {}).get('path', '')}"
            )
        return

    use_remote = (
        not args.local
        and not args.rebuild_index
        and args.data_dir is None
        and (args.remote or rc.configured)
    )

    if (
        args.project_review
        or args.merge_projects
        or args.split_project
        or args.add_path
        or args.remove_path
        or args.set_paths
    ):
        try:
            run_project_review(args, rc, use_remote)
        except (OSError, ValueError, RuntimeError) as exc:
            parser.exit(2, f"ERROR: {exc}\n")
        return

    offline_notice = ""
    if use_remote:
        try:
            output = _run_remote_query(args, rc)
            print(output)
            return
        except remote_client.RemoteAuthError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(2)
        except remote_client.RemoteUnavailableError as exc:
            cache_data = os.path.join(rc.cache_dir, "data")
            if args.index and os.path.exists(os.path.join(rc.cache_dir, "MEMORY_INDEX.md")):
                print("<!-- [offline cache] NAS unreachable; serving cached MEMORY_INDEX.md -->", file=sys.stderr)
                with open(os.path.join(rc.cache_dir, "MEMORY_INDEX.md"), "r", encoding="utf-8") as fh:
                    print(fh.read())
                return
            if os.path.exists(os.path.join(cache_data, "summary_all_history.json")):
                args.data_dir = cache_data
                offline_notice = f"[offline cache] NAS unreachable ({exc}); fell back to {cache_data}"
                print(offline_notice, file=sys.stderr)
            elif os.path.exists(os.path.join(DEFAULT_DATA_DIR, "summary_all_history.json")):
                args.data_dir = DEFAULT_DATA_DIR
                offline_notice = f"[offline local] NAS unreachable ({exc}); fell back to {DEFAULT_DATA_DIR}"
                print(offline_notice, file=sys.stderr)
            else:
                print(f"ERROR: {exc}", file=sys.stderr)
                sys.exit(1)

    target_data_dir = args.data_dir or DEFAULT_DATA_DIR
    days = load_all_days(target_data_dir)
    catalog = ProjectCatalog(days, load_registry(target_data_dir))

    if args.index:
        archived = {str(d.get("workday", "")) for d in days}
        live_days = multi_machine.load_merged_live_days(target_data_dir, archived_workdays=archived)
        active_decs = dec_mod.query_decisions(target_data_dir, limit=12)
        print(
            render_memory_index(
                days,
                tz_name=str(cfg.get("timezone") or "Asia/Shanghai"),
                catalog=catalog,
                live_days=live_days,
                active_decisions=active_decs,
            )
        )
        return

    if args.rebuild_index:
        stats = generate_memory_index(
            days,
            output_path=args.index_output or resolve_index_path(cfg),
            tz_name=str(cfg.get("timezone") or "Asia/Shanghai"),
            catalog=catalog,
            data_dir=target_data_dir,
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

    if args.date and not args.project and not args.keyword:
        res = query_exact_date(
            days,
            args.date,
            include_raw=args.raw,
            data_dir=target_data_dir,
            machine_filter=args.machine,
            keyword=args.keyword,
            catalog=catalog,
        )
        if not res["day"]:
            print(f"No summary found for {args.date}", file=sys.stderr)
            sys.exit(1)
        if args.as_json:
            print(json.dumps({"day": res["day"], "raw": res["raw"]}, ensure_ascii=False, indent=2))
        else:
            print(format_exact_date_md(res))
        return

    if args.project:
        res = query_by_project(
            filtered_days,
            args.project,
            scope=args.scope,
            status_filter=args.status,
            machine_filter=args.machine,
            include_raw=args.raw,
            data_dir=target_data_dir,
            asc=args.asc,
            limit=args.limit,
            catalog=catalog,
        )
        if args.as_json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(format_project_query_md(res))
        return

    if args.keyword and not args.projects:
        res = query_by_keyword(
            filtered_days,
            args.keyword,
            scope=args.scope,
            status_filter=args.status,
            machine_filter=args.machine,
            include_raw=args.raw,
            data_dir=target_data_dir,
            asc=args.asc,
            limit=args.limit,
            catalog=catalog,
            use_fts=not args.exact,
        )
        if args.as_json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(format_keyword_query_md(res))
        return

    projs = query_projects(
        filtered_days,
        scope=args.scope,
        status_filter=args.status,
        machine_filter=args.machine,
        keyword=args.keyword,
        catalog=catalog,
    )
    if args.as_json:
        print(json.dumps({"total": len(projs), "projects": projs[: args.limit]}, ensure_ascii=False, indent=2))
    else:
        print(format_projects_md(projs, args.limit))


if __name__ == "__main__":
    main()
