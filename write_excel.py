#!/usr/bin/env python3
"""Write or idempotently update polished Work (3-sheet) & Personal Tech (4-sheet) Excel workbooks."""

import argparse
from collections import defaultdict
import glob
import json
import math
import os
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import query_memory
from project_identity import ProjectCatalog, load_registry
from provenance import SOURCE_PENDING, format_sources, verify_day_sources

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("DAILY_MEMORY_CONFIG_PATH") or os.path.join(BASE_DIR, "config.json")
EXAMPLE_CONFIG_PATH = os.path.join(BASE_DIR, "config.example.json")

SHEET_OVERVIEW = "每日概览"
SHEET_PROJECTS = "项目总览"
SHEET_DETAILS = "项目流程明细"
SHEET_LIFE = "生活与随想记录"

HEADER_ROW = 4
DATA_START_ROW = 5

THEMES = {
    "work": {
        "book_title": "每日工作汇总 · 正式业务台账",
        "banner_bg": "0F172A",
        "banner_fg": "FFFFFF",
        "kpi_bg": "F1F5F9",
        "kpi_fg": "334155",
        "header_bg": "1E293B",
        "header_fg": "F8FAFC",
        "alt_bg": "F8FAFC",
        "group_line": "94A3B8",
        "hair_line": "E2E8F0",
    },
    "personal": {
        "book_title": "个人技术与折腾汇总 · 探索日志",
        "banner_bg": "0B3B3C",
        "banner_fg": "FFFFFF",
        "kpi_bg": "F0FDFA",
        "kpi_fg": "134E4A",
        "header_bg": "115E59",
        "header_fg": "F0FDFA",
        "alt_bg": "F6FCFA",
        "group_line": "6E9E9B",
        "hair_line": "DCECE9",
    },
}

HEADERS = {
    SHEET_OVERVIEW: [
        "日期", "星期", "项目名称",
        "Antigravity 干了啥", "Codex 干了啥", "WorkBuddy 干了啥", "对应对话名称",
        "项目跨度(h)", "当日总时长(h)", "阶段状态",
        "随想数", "当日活跃时段", "人工备注",
    ],
    SHEET_PROJECTS: [
        "项目名称", "分类", "起始日期", "最近更新", "天数",
        "当前状态", "下一步 / 卡点",
        "Antigravity 干了啥", "Codex 干了啥", "WorkBuddy 干了啥", "本地/其他改动",
        "AG 跨度(h)", "Codex 跨度(h)", "WB 跨度(h)", "项目总计(h)",
        "关联会话名称清单", "人工备注",
    ],
    SHEET_DETAILS: [
        "项目名称", "日期", "星期", "分类", "工具",
        "会话名称 / 来源标识", "做了什么", "核心产出 / 改动",
        "阶段状态", "遗留 / 下一步",
        "活跃时段", "单对话跨度(h)", "人工备注", "出处",
    ],
    SHEET_LIFE: [
        "日期", "工具", "会话名称 / 来源标识", "主题 / 关键词",
        "内容摘要", "活跃时段", "人工备注",
    ],
}

HEADER_COMMENTS = {
    (SHEET_OVERVIEW, 4): "当天该项目下 Antigravity 的精简总结（1~2 句）；完整逐场对话流水请切到「项目流程明细」查看。",
    (SHEET_OVERVIEW, 5): "当天该项目下 Codex 的精简总结（1~2 句）；完整逐场对话流水请切到「项目流程明细」查看。",
    (SHEET_OVERVIEW, 6): "当天该项目下 WorkBuddy 的精简总结（1~2 句）；完整逐场对话流水请切到「项目流程明细」查看。",
    (SHEET_OVERVIEW, 8): "该项目当天所有独立对话跨度相加（多窗口并行时不扣重）。",
    (SHEET_OVERVIEW, 9): "本表内当日全部项目活跃时长汇总（仅在每天第 1 行显示，避免重复加总）。",
    (SHEET_PROJECTS, 12): "该项目下所有 Antigravity 独立对话跨度之和（多窗口并发时不扣重）。",
    (SHEET_PROJECTS, 13): "该项目下所有 Codex 独立对话跨度之和（多窗口并发时不扣重）。",
    (SHEET_PROJECTS, 14): "该项目下所有 WorkBuddy 独立对话跨度之和（多窗口并发时不扣重）。",
    (SHEET_PROJECTS, 15): "该项目全部明细行「单对话跨度(h)」累加总计（反映跨任务并行累计投入）。",
    (SHEET_DETAILS, 12): "单场对话自身的活跃跨度（间隔≤30分钟合并；若同时开多个窗口并行干活，各行相加会大于实际墙钟时长）。",
    (SHEET_DETAILS, 14): "写入日报时核验的会话 ID、工作目录、Git 提交或文件路径。",
}

COL_WIDTHS = {
    SHEET_OVERVIEW: [15, 10, 28, 38, 36, 36, 38, 14, 16, 13, 10, 18, 22],
    SHEET_PROJECTS: [27, 16, 14, 14, 10, 13, 34, 38, 36, 36, 26, 14, 15, 14, 14, 38, 22],
    SHEET_DETAILS: [26, 14, 10, 16, 14, 38, 46, 36, 13, 30, 18, 15, 22, 52],
    SHEET_LIFE: [14, 14, 38, 24, 52, 18, 22],
}

HOUR_COLS = {
    SHEET_OVERVIEW: {8, 9},
    SHEET_PROJECTS: {12, 13, 14, 15},
    SHEET_DETAILS: {12},
    SHEET_LIFE: set(),
}


def load_config() -> dict[str, Any]:
    path = CONFIG_PATH if os.path.exists(CONFIG_PATH) else EXAMPLE_CONFIG_PATH
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def span_sort_key(span: str) -> tuple[int, str]:
    s = (span or "").strip()
    if not s or s == "-":
        return (3, "")
    if ":" not in s:
        return (2, s)
    start_tok = s.split("-", 1)[0].strip()
    is_next_day = 1 if "(+1)" in start_tok else 0
    hhmm = start_tok.replace("(+1)", "").strip()
    return (is_next_day, hhmm)


def shorten_text(text: str, limit: int = 58) -> str:
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip(" ，、；;") + "…"


def estimate_row_height(row_vals: list[Any], widths: list[int]) -> float:
    max_lines = 1
    for val, col_w in zip(row_vals, widths):
        if val is None:
            continue
        text = str(val)
        if not text:
            continue
        effective_w = max(8, col_w - 4)
        lines = 0
        for part in text.splitlines() or [""]:
            weighted_len = sum(2 if ord(ch) > 127 else 1 for ch in part)
            lines += max(1, math.ceil(weighted_len / effective_w))
        max_lines = max(max_lines, lines)
    return max(26, min(240, max_lines * 17 + 10))


def load_existing_rows(xlsx_path: str) -> dict[str, list[list[Any]]]:
    existing: dict[str, list[list[Any]]] = {k: [] for k in HEADERS}
    if not os.path.exists(xlsx_path):
        return existing

    wb = load_workbook(xlsx_path, data_only=True)
    for sheet_name, cols in HEADERS.items():
        if sheet_name not in wb.sheetnames:
            continue
        ws = wb[sheet_name]
        rows_iter = ws.iter_rows(values_only=True)
        header = None
        for row in rows_iter:
            if row and row[0] in ("日期", "项目名称"):
                header = row
                break
        if not header:
            continue
        header_list = [str(h or "").strip() for h in header]
        if len(header_list) == len(cols):
            idx_map = list(range(len(cols)))
        else:
            # Preserve fields (such as 人工备注) by header name across column-schema upgrades.
            idx_map = [header_list.index(c) if c in header_list else None for c in cols]
        for row in rows_iter:
            if not any(cell is not None and str(cell).strip() != "" for cell in row):
                continue
            if len(header_list) == len(cols):
                padded = list(row[: len(cols)]) + [""] * max(0, len(cols) - len(row))
            else:
                padded = [(row[idx] if idx is not None and idx < len(row) else "") for idx in idx_map]
            existing[sheet_name].append(padded)
    wb.close()
    return existing


def normalize_days_payload(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "days" in data and isinstance(data["days"], list):
        return data["days"]
    if isinstance(data, dict) and "workday" in data:
        return [data]
    raise ValueError("Unsupported summary JSON structure.")


def summarize_tool_for_project_day(
    items: list[dict[str, Any]],
    target_tool: str,
    explicit_brief: str = "",
) -> str:
    if explicit_brief:
        return explicit_brief
    matched = [it for it in items if (it.get("tool") or it.get("source")) == target_tool]
    if not matched:
        return "-"
    if len(matched) == 1:
        return shorten_text(str(matched[0].get("summary") or matched[0].get("outputs") or ""), 75)
    parts = [shorten_text(str(it.get("summary") or it.get("outputs") or ""), 38) for it in matched[:3]]
    if len(matched) > 3:
        parts.append(f"等共{len(matched)}场对话")
    return "；".join(p for p in parts if p)


def split_day_by_scope(day: dict[str, Any], target_scope: str) -> dict[str, Any]:
    all_work_items = day.get("work_items") or []
    scoped_items = [
        it for it in all_work_items
        if (it.get("scope") or "work") == target_scope
    ]

    if target_scope == "work":
        scoped_life: list[dict[str, Any]] = []
        scoped_hours = day.get("work_estimated_hours")
        empty_label = "无正式工作记录"
    else:
        scoped_life = day.get("life_items") or []
        scoped_hours = day.get("personal_estimated_hours")
        empty_label = "无技术折腾记录 / 休息"

    if scoped_hours is None:
        scoped_hours = round(sum(float(it.get("estimated_hours") or 0) for it in scoped_items), 2)

    return {
        "workday": day["workday"],
        "weekday": day.get("weekday", ""),
        "headline": empty_label if (not scoped_items and not scoped_life) else empty_label,
        "active_span": day.get("active_span") or "-",
        "estimated_hours": scoped_hours,
        "project_briefs": day.get("project_briefs") or {},
        "work_items": scoped_items,
        "life_items": scoped_life,
        "manual_note": day.get("manual_note", ""),
    }


def build_rows_from_day(day: dict[str, Any]) -> tuple[list[list[Any]], list[list[Any]], list[list[Any]]]:
    workday = str(day["workday"])
    weekday = str(day.get("weekday", ""))
    work_items = day.get("work_items") or []
    life_items = day.get("life_items") or []
    briefs = day.get("project_briefs") or {}

    is_empty = len(work_items) == 0
    active_span = day.get("active_span") or ("-" if not (work_items or life_items) else "")
    est_hours = day.get("estimated_hours")
    if est_hours is None:
        est_hours = round(sum(float(w.get("estimated_hours") or 0) for w in work_items), 2)

    overview_rows: list[list[Any]] = []
    if is_empty:
        overview_rows.append(
            [
                workday,
                weekday,
                day.get("headline") or "无记录 / 休息",
                "-",
                "-",
                "-",
                "-",
                0.0,
                float(est_hours),
                "-",
                len(life_items),
                active_span if life_items else "-",
                day.get("manual_note", ""),
            ]
        )
    else:
        by_proj: dict[str, list[dict[str, Any]]] = defaultdict(list)
        proj_order: list[str] = []
        for item in work_items:
            p = str(item.get("project") or "未命名项目")
            if p not in by_proj:
                proj_order.append(p)
            by_proj[p].append(item)

        for idx, proj in enumerate(proj_order):
            p_items = by_proj[proj]
            p_brief = briefs.get(proj, {}) if isinstance(briefs, dict) else {}

            ag_brief = summarize_tool_for_project_day(p_items, "Antigravity", str(p_brief.get("ag") or ""))
            codex_brief = summarize_tool_for_project_day(p_items, "Codex", str(p_brief.get("codex") or ""))
            wb_brief = summarize_tool_for_project_day(p_items, "WorkBuddy", str(p_brief.get("wb") or p_brief.get("workbuddy") or ""))
            if ag_brief == "-" and codex_brief == "-" and wb_brief == "-":
                other_tools = [
                    str(it.get("tool") or it.get("source") or "")
                    for it in p_items
                    if str(it.get("tool") or it.get("source") or "") not in ("", "本地文件", "本地", "Git")
                ]
                prefix = other_tools[0] if other_tools else "本地"
                local_parts = [shorten_text(str(it.get("summary") or it.get("outputs") or ""), 60) for it in p_items]
                ag_brief = f"[{prefix}] {'；'.join(local_parts)}"

            chats: list[str] = []
            seen_chat = set()
            for it in p_items:
                t = str(it.get("tool") or it.get("source") or "")
                sn = str(it.get("session_name") or "").strip()
                if sn:
                    lbl = f"[{t}] {sn}" if t in ("Antigravity", "Codex", "WorkBuddy") else sn
                    if lbl not in seen_chat:
                        seen_chat.add(lbl)
                        chats.append(lbl)

            proj_hours = round(sum(float(it.get("estimated_hours") or 0) for it in p_items), 2)
            latest_status = str(p_items[-1].get("status") or "已完成")

            overview_rows.append(
                [
                    workday,
                    weekday,
                    proj,
                    ag_brief,
                    codex_brief,
                    wb_brief,
                    "\n".join(chats) if chats else "-",
                    proj_hours,
                    float(est_hours) if idx == 0 else "",
                    latest_status,
                    len(life_items) if idx == 0 else "",
                    active_span if idx == 0 else "",
                    day.get("manual_note", "") if idx == 0 else "",
                ]
            )

    detail_rows: list[list[Any]] = []
    for item in work_items:
        tool = item.get("tool") or item.get("source") or ""
        session_name = item.get("session_name") or ""
        detail_rows.append(
            [
                item.get("project", ""),
                workday,
                weekday,
                item.get("category", "其他"),
                tool,
                session_name,
                item.get("summary", ""),
                item.get("outputs", ""),
                item.get("status", "已完成"),
                item.get("next_step", "-"),
                item.get("active_span", ""),
                float(item.get("estimated_hours") or 0.0),
                item.get("manual_note", ""),
                format_sources(item.get("sources") or [], status=item.get("source_status", "")),
            ]
        )

    life_rows: list[list[Any]] = []
    for item in life_items:
        tool = item.get("tool") or item.get("source") or ""
        session_name = item.get("session_name") or ""
        life_rows.append(
            [
                workday,
                tool,
                session_name,
                item.get("topic", ""),
                item.get("summary", ""),
                item.get("active_span", ""),
                item.get("manual_note", ""),
            ]
        )

    return overview_rows, detail_rows, life_rows


def sort_project_details(detail_rows: list[list[Any]]) -> list[list[Any]]:
    by_proj: dict[str, list[list[Any]]] = defaultdict(list)
    for r in detail_rows:
        by_proj[str(r[0])].append(r)

    proj_order = sorted(
        by_proj.keys(),
        key=lambda p: (max(str(r[1]) for r in by_proj[p]), p),
        reverse=True,
    )

    ordered: list[list[Any]] = []
    for proj in proj_order:
        items = by_proj[proj]
        items.sort(key=lambda r: (str(r[1]), span_sort_key(str(r[10]))))
        ordered.extend(items)
    return ordered


def compact_tool_by_day(items: list[list[Any]], tool_name: str, overview_rows: list[list[Any]], proj: str) -> str:
    brief_col = 3 if tool_name == "Antigravity" else (4 if tool_name == "Codex" else 5)
    day_briefs: dict[str, str] = {}
    for ov in overview_rows:
        if str(ov[2]) == proj:
            val = str(ov[brief_col] or "").strip()
            if val and val != "-" and not val.startswith("[本地]"):
                day_briefs[str(ov[0])] = val

    matched_days: dict[str, list[list[Any]]] = defaultdict(list)
    for r in items:
        if str(r[4]) == tool_name:
            matched_days[str(r[1])].append(r)

    if not matched_days:
        return "-"

    lines: list[str] = []
    for d in sorted(matched_days.keys()):
        if d in day_briefs:
            lines.append(f"• {d[5:]}：{day_briefs[d]}")
        else:
            day_items = matched_days[d]
            if len(day_items) == 1:
                lines.append(f"• {d[5:]}：{shorten_text(str(day_items[0][6]), 55)}")
            else:
                snippets = [shorten_text(str(x[6]), 28) for x in day_items[:2]]
                more = f"等{len(day_items)}项" if len(day_items) > 2 else ""
                lines.append(f"• {d[5:]}：{'；'.join(snippets)}{more}")
    return "\n".join(lines)


def build_project_summary_rows(
    ordered_details: list[list[Any]],
    overview_rows: list[list[Any]],
    project_notes: dict[str, Any],
) -> list[list[Any]]:
    by_proj: dict[str, list[list[Any]]] = defaultdict(list)
    for r in ordered_details:
        by_proj[str(r[0])].append(r)

    summary_rows: list[list[Any]] = []
    for proj, items in by_proj.items():
        dates = [str(r[1]) for r in items]
        first_d = min(dates)
        last_d = max(dates)
        span_days = len(set(dates))

        cats: dict[str, int] = defaultdict(int)
        for r in items:
            cats[str(r[3])] += 1
        main_cat = max(cats.items(), key=lambda kv: kv[1])[0]

        ag_h = round(sum(float(r[11] or 0) for r in items if str(r[4]) == "Antigravity"), 2)
        codex_h = round(sum(float(r[11] or 0) for r in items if str(r[4]) == "Codex"), 2)
        wb_h = round(sum(float(r[11] or 0) for r in items if str(r[4]) == "WorkBuddy"), 2)
        total_h = round(sum(float(r[11] or 0) for r in items), 2)

        latest_status = "已完成"
        latest_next = "-"
        for r in items:
            st = str(r[8] or "").strip()
            nx = str(r[9] or "").strip()
            if st:
                latest_status = st
            if nx and nx != "-":
                latest_next = nx
            elif st in ("已完成", "阶段完成", "已搁置") and nx == "-":
                latest_next = "-"

        sessions: list[str] = []
        seen_sess = set()
        other_lines: list[str] = []
        for r in items:
            d_short = str(r[1])[5:]
            tool = str(r[4])
            sname = str(r[5]).strip()
            if sname:
                label = f"[{tool}] {sname}"
                if label not in seen_sess:
                    seen_sess.add(label)
                    sessions.append(label)
            if tool not in ("Antigravity", "Codex", "WorkBuddy"):
                other_lines.append(f"• {d_short} [{tool}]：{shorten_text(str(r[7] or r[6]), 45)}")

        ag_txt = compact_tool_by_day(items, "Antigravity", overview_rows, proj)
        codex_txt = compact_tool_by_day(items, "Codex", overview_rows, proj)
        wb_txt = compact_tool_by_day(items, "WorkBuddy", overview_rows, proj)

        summary_rows.append(
            [
                proj,
                main_cat,
                first_d,
                last_d,
                span_days,
                latest_status,
                latest_next,
                ag_txt,
                codex_txt,
                wb_txt,
                "\n".join(other_lines) if other_lines else "-",
                ag_h,
                codex_h,
                wb_h,
                total_h,
                "\n".join(sessions),
                project_notes.get(proj, ""),
            ]
        )

    summary_rows.sort(key=lambda r: (str(r[3]), float(r[14])), reverse=True)
    return summary_rows


def build_kpi_subtitle(sheet_name: str, rows: list[list[Any]]) -> str:
    if sheet_name == SHEET_OVERVIEW:
        days_cnt = len({str(r[0]) for r in rows if r and r[0]})
        active_projs = sum(1 for r in rows if str(r[2]) not in ("无正式工作记录", "无技术折腾记录 / 休息", "无记录 / 休息"))
        total_h = round(sum(float(r[8] or 0) for r in rows if r[8] not in ("", None)), 2)
        return f"换日线：05:00–次日05:00 (Asia/Shanghai)    ｜    覆盖天数：{days_cnt} 天    ｜    活跃项目行：{active_projs} 条    ｜    累计投入：{total_h:.2f} h"
    if sheet_name == SHEET_PROJECTS:
        proj_cnt = len(rows)
        ongoing_cnt = sum(1 for r in rows if str(r[5]) in ("进行中", "待开启定时", "待验证"))
        total_h = round(sum(float(r[14] or 0) for r in rows if r[14] not in ("", None)), 2)
        return f"累计项目数：{proj_cnt} 个    ｜    进行中/待跟进：{ongoing_cnt} 个    ｜    跨度总计：{total_h:.2f} h    ｜    按最近推进时间倒序"
    if sheet_name == SHEET_DETAILS:
        ag_cnt = sum(1 for r in rows if str(r[4]) == "Antigravity")
        cx_cnt = sum(1 for r in rows if str(r[4]) == "Codex")
        wb_cnt = sum(1 for r in rows if str(r[4]) == "WorkBuddy")
        return f"1 对话 = 1 行（侧边栏 1:1 对应）    ｜    总记录：{len(rows)} 条    ｜    Antigravity：{ag_cnt} 条    ｜    Codex：{cx_cnt} 条    ｜    WorkBuddy：{wb_cnt} 条"
    return f"独立收录日常闲聊、生活探索与零碎问答    ｜    总记录：{len(rows)} 条    ｜    不混入工作明细"


def style_sheet(ws: Any, sheet_name: str, rows: list[list[Any]], theme_key: str) -> None:
    theme = THEMES[theme_key]
    cols = HEADERS[sheet_name]
    widths = COL_WIDTHS[sheet_name]
    last_col_letter = get_column_letter(len(cols))

    ws.views.sheetView[0].showGridLines = False

    # Row 1: Executive Title Banner
    ws.merge_cells(f"A1:{last_col_letter}1")
    ws["A1"] = f"{theme['book_title']}  /  {sheet_name}"
    ws.row_dimensions[1].height = 38
    banner_fill = PatternFill(start_color=theme["banner_bg"], end_color=theme["banner_bg"], fill_type="solid")
    banner_font = Font(name="PingFang SC", size=14, bold=True, color=theme["banner_fg"])
    for col_idx in range(1, len(cols) + 1):
        c = ws.cell(row=1, column=col_idx)
        c.fill = banner_fill
        c.font = banner_font
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)

    # Row 2: KPI & Rule Summary Strip
    ws.merge_cells(f"A2:{last_col_letter}2")
    ws["A2"] = build_kpi_subtitle(sheet_name, rows)
    ws.row_dimensions[2].height = 25
    kpi_fill = PatternFill(start_color=theme["kpi_bg"], end_color=theme["kpi_bg"], fill_type="solid")
    kpi_font = Font(name="PingFang SC", size=10, bold=False, color=theme["kpi_fg"])
    kpi_bottom = Side(border_style="thin", color=theme["hair_line"])
    for col_idx in range(1, len(cols) + 1):
        c = ws.cell(row=2, column=col_idx)
        c.fill = kpi_fill
        c.font = kpi_font
        c.border = Border(bottom=kpi_bottom)
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)

    # Row 3: Thin Spacer
    ws.row_dimensions[3].height = 9

    # Row 4: Table Header
    header_fill = PatternFill(start_color=theme["header_bg"], end_color=theme["header_bg"], fill_type="solid")
    header_font = Font(name="PingFang SC", size=10.5, bold=True, color=theme["header_fg"])
    header_border = Border(
        left=Side(border_style="thin", color=theme["header_bg"]),
        right=Side(border_style="thin", color=theme["header_bg"]),
        top=Side(border_style="thin", color=theme["header_bg"]),
        bottom=Side(border_style="medium", color=theme["banner_bg"]),
    )

    ws.row_dimensions[HEADER_ROW].height = 32
    for col_idx, col_title in enumerate(cols, start=1):
        cell = ws.cell(row=HEADER_ROW, column=col_idx, value=col_title)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = header_border
        if (sheet_name, col_idx) in HEADER_COMMENTS:
            cell.comment = Comment(HEADER_COMMENTS[(sheet_name, col_idx)], "DailySummary")
        ws.column_dimensions[get_column_letter(col_idx)].width = widths[col_idx - 1]

    # Data row styling
    alt_fill = PatternFill(start_color=theme["alt_bg"], end_color=theme["alt_bg"], fill_type="solid")
    white_fill = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")
    note_fill = PatternFill(start_color="FFFDF5", end_color="FFFDF5", fill_type="solid")

    badge_fills = {
        "Antigravity": PatternFill(start_color="DBEAFE", end_color="DBEAFE", fill_type="solid"),
        "Codex": PatternFill(start_color="DCFCE7", end_color="DCFCE7", fill_type="solid"),
        "WorkBuddy": PatternFill(start_color="EDE9FE", end_color="EDE9FE", fill_type="solid"),
        "T3 Code": PatternFill(start_color="CFFAFE", end_color="CFFAFE", fill_type="solid"),
        "Claude": PatternFill(start_color="FFEDD5", end_color="FFEDD5", fill_type="solid"),
        "Cursor": PatternFill(start_color="E0E7FF", end_color="E0E7FF", fill_type="solid"),
        "Grok": PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid"),
        "OpenCode": PatternFill(start_color="E0F2FE", end_color="E0F2FE", fill_type="solid"),
        "本地文件": PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid"),
        "进行中": PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid"),
        "待开启定时": PatternFill(start_color="DBEAFE", end_color="DBEAFE", fill_type="solid"),
        "待验证": PatternFill(start_color="E0E7FF", end_color="E0E7FF", fill_type="solid"),
        "已完成": PatternFill(start_color="DCFCE7", end_color="DCFCE7", fill_type="solid"),
        "阶段完成": PatternFill(start_color="DCFCE7", end_color="DCFCE7", fill_type="solid"),
        "已搁置": PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid"),
    }
    badge_fonts = {
        "Antigravity": Font(name="PingFang SC", size=10, bold=True, color="1E40AF"),
        "Codex": Font(name="PingFang SC", size=10, bold=True, color="166534"),
        "WorkBuddy": Font(name="PingFang SC", size=10, bold=True, color="6D28D9"),
        "T3 Code": Font(name="PingFang SC", size=10, bold=True, color="155E75"),
        "Claude": Font(name="PingFang SC", size=10, bold=True, color="9A3412"),
        "Cursor": Font(name="PingFang SC", size=10, bold=True, color="3730A3"),
        "Grok": Font(name="PingFang SC", size=10, bold=True, color="334155"),
        "OpenCode": Font(name="PingFang SC", size=10, bold=True, color="0369A1"),
        "本地文件": Font(name="PingFang SC", size=10, bold=True, color="92400E"),
        "进行中": Font(name="PingFang SC", size=10, bold=True, color="92400E"),
        "待开启定时": Font(name="PingFang SC", size=10, bold=True, color="1E40AF"),
        "待验证": Font(name="PingFang SC", size=10, bold=True, color="3730A3"),
        "已完成": Font(name="PingFang SC", size=10, bold=True, color="166534"),
        "阶段完成": Font(name="PingFang SC", size=10, bold=True, color="166534"),
        "已搁置": Font(name="PingFang SC", size=10, bold=True, color="475569"),
    }

    body_font = Font(name="PingFang SC", size=10, color="1E293B")
    bold_font = Font(name="PingFang SC", size=10, bold=True, color="0F172A")
    muted_font = Font(name="PingFang SC", size=10, color="94A3B8")
    rest_font = Font(name="PingFang SC", size=10, italic=True, color="94A3B8")

    hair_side = Side(border_style="thin", color=theme["hair_line"])
    group_top_side = Side(border_style="medium", color=theme["group_line"])
    default_border = Border(left=hair_side, right=hair_side, top=hair_side, bottom=hair_side)
    group_border = Border(left=hair_side, right=hair_side, top=group_top_side, bottom=hair_side)

    group_toggle = False
    prev_group = None
    badge_cols = set()
    bold_cols = set()
    if sheet_name == SHEET_OVERVIEW:
        badge_cols = {10}
        bold_cols = {3}
    elif sheet_name == SHEET_PROJECTS:
        badge_cols = {6}
        bold_cols = {1}
    elif sheet_name == SHEET_DETAILS:
        badge_cols = {5, 9}
        bold_cols = {1}
    elif sheet_name == SHEET_LIFE:
        badge_cols = {2}

    note_col_idx = cols.index("人工备注") + 1
    hour_col_set = HOUR_COLS[sheet_name]

    for offset, row_vals in enumerate(rows):
        r_idx = DATA_START_ROW + offset
        is_new_group = False
        if sheet_name in (SHEET_OVERVIEW, SHEET_DETAILS):
            cur_group = str(row_vals[0])
            if cur_group != prev_group:
                if prev_group is not None:
                    group_toggle = not group_toggle
                    is_new_group = True
                prev_group = cur_group
            fill = alt_fill if group_toggle else white_fill
        else:
            fill = alt_fill if offset % 2 == 1 else white_fill

        is_rest_row = (
            sheet_name == SHEET_OVERVIEW
            and str(row_vals[2]) in ("无正式工作记录", "无技术折腾记录 / 休息", "无记录 / 休息")
        )

        ws.row_dimensions[r_idx].height = estimate_row_height(row_vals, widths)
        for col_idx, val in enumerate(row_vals, start=1):
            cell = ws.cell(row=r_idx, column=col_idx, value=val)
            val_str = str(val) if val is not None else ""

            if col_idx in badge_cols and val_str in badge_fills:
                cell.fill = badge_fills[val_str]
                cell.font = badge_fonts[val_str]
            elif col_idx == note_col_idx and not is_rest_row:
                cell.fill = note_fill
                cell.font = body_font
            else:
                cell.fill = fill
                if is_rest_row and col_idx >= 3:
                    cell.font = rest_font
                elif val_str.strip() == "-":
                    cell.font = muted_font
                elif col_idx in bold_cols:
                    cell.font = bold_font
                else:
                    cell.font = body_font

            cell.border = group_border if is_new_group else default_border

            if col_idx in hour_col_set and isinstance(val, (int, float)):
                cell.number_format = '0.00" h"'

            is_dash = val_str.strip() == "-"
            is_center = (
                is_dash
                or (sheet_name == SHEET_OVERVIEW and col_idx in (1, 2, 8, 9, 10, 11, 12))
                or (sheet_name == SHEET_PROJECTS and col_idx in (2, 3, 4, 5, 6, 12, 13, 14, 15))
                or (sheet_name == SHEET_DETAILS and col_idx in (2, 3, 4, 5, 9, 11, 12))
                or (sheet_name == SHEET_LIFE and col_idx in (1, 2, 6))
            )
            is_mid_v = is_center or col_idx in bold_cols
            cell.alignment = Alignment(
                horizontal="center" if is_center else "left",
                vertical="center" if is_mid_v else "top",
                indent=0 if is_center else 1,
                wrap_text=True,
            )

    ws.freeze_panes = f"D{DATA_START_ROW}" if sheet_name == SHEET_OVERVIEW else f"B{DATA_START_ROW}" if sheet_name in (SHEET_PROJECTS, SHEET_DETAILS) else f"A{DATA_START_ROW}"
    last_row = max(HEADER_ROW, DATA_START_ROW + len(rows) - 1)
    ws.auto_filter.ref = f"A{HEADER_ROW}:{last_col_letter}{last_row}"


def update_excel(
    days: list[dict[str, Any]],
    xlsx_path: str,
    include_life: bool = True,
    theme_key: str = "work",
) -> dict[str, int]:
    existing = load_existing_rows(xlsx_path)

    overview_notes = {
        (str(r[0]), str(r[2])): r[12]
        for r in existing[SHEET_OVERVIEW]
        if len(r) > 12 and str(r[12]).strip()
    }
    project_notes = {str(r[0]): r[16] for r in existing[SHEET_PROJECTS] if len(r) > 16 and str(r[16]).strip()}
    detail_notes = {
        (str(r[0]), str(r[1]), str(r[4]), str(r[5])): r[12]
        for r in existing[SHEET_DETAILS]
        if len(r) > 12 and str(r[12]).strip()
    }
    life_notes = {
        (str(r[0]), str(r[1]), str(r[3])): r[6]
        for r in existing[SHEET_LIFE]
        if len(r) > 6 and str(r[6]).strip()
    }

    incoming_dates = {str(d["workday"]) for d in days}

    kept_overview = [r for r in existing[SHEET_OVERVIEW] if str(r[0]) not in incoming_dates]
    kept_details = [r for r in existing[SHEET_DETAILS] if str(r[1]) not in incoming_dates]
    for row in kept_details:
        row[-1] = row[-1] or SOURCE_PENDING
    kept_life = [r for r in existing[SHEET_LIFE] if str(r[0]) not in incoming_dates]

    for day in days:
        ovs, dets, lfs = build_rows_from_day(day)
        for r in ovs:
            key = (str(r[0]), str(r[2]))
            if not str(r[12]).strip() and key in overview_notes:
                r[12] = overview_notes[key]
            kept_overview.append(r)

        for r in dets:
            key = (str(r[0]), str(r[1]), str(r[4]), str(r[5]))
            if not str(r[12]).strip() and key in detail_notes:
                r[12] = detail_notes[key]
            kept_details.append(r)

        if include_life:
            for r in lfs:
                key = (str(r[0]), str(r[1]), str(r[3]))
                if not str(r[6]).strip() and key in life_notes:
                    r[6] = life_notes[key]
                kept_life.append(r)

    by_date_ov: dict[str, list[list[Any]]] = defaultdict(list)
    for r in kept_overview:
        by_date_ov[str(r[0])].append(r)
    sorted_overview: list[list[Any]] = []
    for d in sorted(by_date_ov.keys(), reverse=True):
        sorted_overview.extend(by_date_ov[d])

    ordered_details = sort_project_details(kept_details)
    project_rows = build_project_summary_rows(ordered_details, sorted_overview, project_notes)
    kept_life.sort(key=lambda r: (str(r[0]), span_sort_key(str(r[5]))), reverse=True)

    wb = Workbook()
    ws_ov = wb.active
    ws_ov.title = SHEET_OVERVIEW
    style_sheet(ws_ov, SHEET_OVERVIEW, sorted_overview, theme_key)

    ws_proj = wb.create_sheet(title=SHEET_PROJECTS)
    style_sheet(ws_proj, SHEET_PROJECTS, project_rows, theme_key)

    ws_det = wb.create_sheet(title=SHEET_DETAILS)
    style_sheet(ws_det, SHEET_DETAILS, ordered_details, theme_key)

    if include_life:
        ws_lf = wb.create_sheet(title=SHEET_LIFE)
        style_sheet(ws_lf, SHEET_LIFE, kept_life, theme_key)

    out_dir = os.path.dirname(os.path.abspath(xlsx_path))
    os.makedirs(out_dir, exist_ok=True)
    lock_path = os.path.join(out_dir, f".~{os.path.basename(xlsx_path)}")
    if os.path.exists(lock_path):
        print(
            f"[WARN] Detected open office lock file ({lock_path}); "
            "close/reopen the workbook instead of saving the stale window."
        )
    tmp_path = xlsx_path + ".tmp.xlsx"
    wb.save(tmp_path)
    os.replace(tmp_path, xlsx_path)

    stats = {
        "overview_rows": len(sorted_overview),
        "project_rows": len(project_rows),
        "detail_rows": len(ordered_details),
    }
    if include_life:
        stats["life_rows"] = len(kept_life)
    return stats


def update_split_workbooks(
    days: list[dict[str, Any]],
    work_xlsx_path: str,
    personal_xlsx_path: str,
    split_months: bool = False,
) -> dict[str, Any]:
    work_days = [split_day_by_scope(d, "work") for d in days]
    personal_days = [split_day_by_scope(d, "personal") for d in days]
    res: dict[str, Any] = {
        "work": update_excel(work_days, work_xlsx_path, include_life=False, theme_key="work"),
        "personal": update_excel(personal_days, personal_xlsx_path, include_life=True, theme_key="personal"),
    }
    if not split_months:
        return res

    work_dir = os.path.dirname(os.path.abspath(work_xlsx_path))
    work_stem = os.path.splitext(os.path.basename(work_xlsx_path))[0]
    personal_dir = os.path.dirname(os.path.abspath(personal_xlsx_path))
    personal_stem = os.path.splitext(os.path.basename(personal_xlsx_path))[0]

    by_month_work: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_month_personal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d_w, d_p in zip(work_days, personal_days):
        ym = str(d_w["workday"])[:7]
        by_month_work[ym].append(d_w)
        by_month_personal[ym].append(d_p)

    months_stats: dict[str, dict[str, Any]] = {}
    for ym in sorted(by_month_work.keys()):
        m_work_days = by_month_work[ym]
        m_personal_days = by_month_personal[ym]
        m_work_path = os.path.join(work_dir, ym, f"{work_stem}_{ym}.xlsx")
        m_personal_path = os.path.join(personal_dir, ym, f"{personal_stem}_{ym}.xlsx")

        m_stats: dict[str, Any] = {}
        has_work = any(d.get("work_items") for d in m_work_days)
        if has_work or os.path.exists(m_work_path):
            m_stats["work"] = update_excel(m_work_days, m_work_path, include_life=False, theme_key="work")

        has_personal = any(d.get("work_items") or d.get("life_items") for d in m_personal_days)
        if has_personal or os.path.exists(m_personal_path):
            m_stats["personal"] = update_excel(m_personal_days, m_personal_path, include_life=True, theme_key="personal")

        if m_stats:
            months_stats[ym] = m_stats

    res["months"] = months_stats
    return res


def sync_json_indexes(days: list[dict[str, Any]], data_dir: str | None = None) -> dict[str, int]:
    """Merge incoming day payloads into monthly summary_<YYYY-MM>.json and summary_all_history.json."""
    target_data = data_dir or os.path.join(BASE_DIR, "data")
    months_dir = os.path.join(target_data, "months")
    os.makedirs(months_dir, exist_ok=True)
    master_path = os.path.join(target_data, "summary_all_history.json")

    by_day: dict[str, dict[str, Any]] = {}
    if os.path.exists(master_path):
        with open(master_path, "r", encoding="utf-8") as f:
            for d in normalize_days_payload(json.load(f)):
                by_day[str(d["workday"])] = d
    for m_path in sorted(glob.glob(os.path.join(months_dir, "summary_*.json"))):
        with open(m_path, "r", encoding="utf-8") as f:
            for d in normalize_days_payload(json.load(f)):
                by_day[str(d["workday"])] = d

    touched_months: set[str] = set()
    for d in days:
        wd = str(d["workday"])
        by_day[wd] = d
        touched_months.add(wd[:7])

    by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for wd in sorted(by_day.keys()):
        by_month[wd[:7]].append(by_day[wd])

    for ym in sorted(touched_months):
        m_fp = os.path.join(months_dir, f"summary_{ym}.json")
        with open(m_fp, "w", encoding="utf-8") as f:
            json.dump({"month": ym, "days": by_month.get(ym, [])}, f, ensure_ascii=False, indent=2)

    ordered_all = [by_day[wd] for wd in sorted(by_day.keys())]
    with open(master_path, "w", encoding="utf-8") as f:
        json.dump({"days": ordered_all}, f, ensure_ascii=False, indent=2)
    return {"total_days": len(ordered_all), "updated_months": len(touched_months)}


def main() -> None:
    cfg = load_config()
    parser = argparse.ArgumentParser(description="Write polished Work (3-sheet) & Personal (4-sheet) Excel files.")
    parser.add_argument("--input", nargs="+", required=True, help="One or more summary JSON files.")
    parser.add_argument("--work-output", help="Override Work Excel output path.")
    parser.add_argument("--personal-output", help="Override Personal Excel output path.")
    parser.add_argument("--data-dir", default=os.environ.get("DAILY_MEMORY_DATA_DIR"), help="Override JSON data directory.")
    parser.add_argument("--no-monthly", action="store_true", help="Disable per-month YYYY-MM subfolder workbooks.")
    parser.add_argument("--no-index-sync", action="store_true", help="Skip syncing monthly/master JSON indexes.")
    parser.add_argument("--no-memory-index", action="store_true", help="Skip regenerating MEMORY_INDEX.md.")
    args = parser.parse_args()

    all_days: list[dict[str, Any]] = []
    for path in args.input:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        all_days.extend(normalize_days_payload(data))

    target_data_dir = args.data_dir or os.path.join(BASE_DIR, "data")
    all_days = [
        verify_day_sources(day, query_memory.load_raw_day(str(day["workday"]), data_dir=target_data_dir))
        for day in all_days
    ]
    work_path = os.path.expanduser(args.work_output or cfg["excel_path"])
    personal_path = os.path.expanduser(args.personal_output or cfg["personal_excel_path"])
    split_months = bool(cfg.get("monthly_folders", True)) and not args.no_monthly
    res = update_split_workbooks(all_days, work_path, personal_path, split_months=split_months)
    print(f"Updated Work ({work_path}): {res['work']}")
    print(f"Updated Personal ({personal_path}): {res['personal']}")
    if res.get("months"):
        print(f"Updated monthly workbooks: {', '.join(sorted(res['months'].keys()))}")
    if not args.work_output and not args.personal_output and not args.no_index_sync:
        idx_res = sync_json_indexes(all_days, data_dir=target_data_dir)
        print(f"Synced JSON indexes: {idx_res}")
        if not args.no_memory_index:
            history = query_memory.load_all_days(target_data_dir)
            mem_res = query_memory.generate_memory_index(
                history,
                output_path=query_memory.resolve_index_path(cfg),
                tz_name=str(cfg.get("timezone") or "Asia/Shanghai"),
                catalog=ProjectCatalog(history, load_registry(target_data_dir)),
            )
            print(f"Updated memory index: {mem_res}")


if __name__ == "__main__":
    main()
