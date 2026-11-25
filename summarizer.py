#!/usr/bin/env python3
"""NAS-side LLM daily summarizer: fuses multi-machine raw traces into canonical summary JSON."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta
from hashlib import sha256
import json
import ntpath
import posixpath
import os
import re
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

import multi_machine
import query_memory
from provenance import SESSION_BUCKETS, verify_day_sources
from project_identity import name_key


SYSTEM_PROMPT = """你是一个跨设备、跨端（Antigravity / Codex / WorkBuddy / Git / 本地文件）工作与个人技术记忆汇总助手。
输入是某一天（workday）从多台设备合并后的原始轨迹 raw JSON，以及历史已有项目名列表。
请严格输出**纯 JSON 对象**（不要 markdown 代码块、不要多余说明），格式必须完全符合以下 schema：
{
  "workday": "YYYY-MM-DD",
  "weekday": "周X",
  "headline": "一句精炼概括当天全部设备核心进展",
  "active_span": "HH:MM-HH:MM",
  "antigravity_hours": 0.0,
  "codex_hours": 0.0,
  "workbuddy_hours": 0.0,
  "estimated_hours": 0.0,
  "work_estimated_hours": 0.0,
  "personal_estimated_hours": 0.0,
  "core_outputs": "当天关键产出，分号分隔",
  "work_items": [
    {
      "project": "本条进展的项目名称，保留证据中的名称或别名；新项目起简洁中文名",
      "suggested_project_id": "可选，仅在本条名称与历史项目不同但有归类依据时提供 existing_projects 中的 ID",
      "project_suggestion_reason": "可选，一句话说明归类依据",
      "category": "分类如 效率工具开发 / 文档与工作资料 / AI应用开发 / 系统与运维",
      "tool": "Antigravity | Codex | WorkBuddy | Git | 本地文件",
      "machine": "来源机器 ID（与 raw 中一致）",
      "session_name": "会话标题或仓库/文件名",
      "summary": "1-2句客观描述做了什么",
      "outputs": "产出物/提交/文件",
      "sources": [{"kind": "session | commit | file", "id": "对应原始值", "machine": "原记录机器", "bucket": "会话所属 raw 数组（仅 session）", "repo": "原始仓库路径（仅 commit）"}],
      "active_span": "HH:MM-HH:MM",
      "estimated_hours": 0.0,
      "status": "已完成 | 阶段完成 | 进行中 | 待验证 | 阻塞",
      "next_step": "下一步或 -",
      "scope": "work 或 personal（正式单位业务为 work，技术折腾/工具开发为 personal）"
    }
  ],
  "life_items": [
    {
      "tool": "Antigravity | Codex | WorkBuddy",
      "machine": "来源机器 ID",
      "session_name": "会话标题",
      "topic": "主题/关键词",
      "summary": "简明摘要",
      "sources": [{"kind": "session", "id": "原始会话 ID", "machine": "原记录机器", "bucket": "会话所属 raw 数组"}],
      "active_span": "HH:MM-HH:MM"
    }
  ],
  "project_briefs": {
    "项目名称": {
      "ag": "Antigravity 当天一句话总结或 -",
      "codex": "Codex 当天一句话总结或 -",
      "wb": "WorkBuddy 当天一句话总结或 -"
    }
  }
}
规则：
1. existing_projects 提供既有项目名、别名、固定 ID 和 scope（work vs personal）。
   project 必须保留这条进展自身的项目名称。同一项目用了新名字时，保留新名字并建议历史项目 ID，
   由人工决定是否合并。遇到已知的项目名或别名，沿用其身份。证据不足时省略建议；不得自行添加别名。
2. 保留每条记录的 machine 字段，使跨电脑接力可追溯。
3. 纯生活闲聊、非项目探索归入 life_items，项目研发/业务归入 work_items。
4. 忽略极短无意义测试或纯空对话。
5. 每条进展和随想填写支持本条描述的 sources。session 的 id 必须逐字取自对应会话的 id，
   commit 的 id 必须逐字取自对应仓库 commits 中的 hash，file 的 id 必须逐字取自文件组的 paths。
   仅引用能明确支持本条内容的记录；不能确认时用空数组。同名项目、同日或同工作目录本身不足以证明关联。
   工作目录和 Git 改动文件由程序从原始记录补全，无需提供。"""


def _compact_raw_for_llm(raw_payload: dict[str, Any], existing_projects: list[dict[str, Any]]) -> dict[str, Any]:
    """Trim verbose fields to keep LLM prompts focused and well within token budgets."""
    def trim_sessions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for it in items[:50]:
            out.append(
                {
                    "id": it.get("id", ""),
                    "machine": it.get("machine", raw_payload.get("machine", "")),
                    "title": it.get("title", ""),
                    "client": it.get("client", ""),
                    "workspace": it.get("workspace") or it.get("cwd") or "",
                    "active_span": it.get("active_span", ""),
                    "estimated_hours": it.get("estimated_hours", 0.0),
                    "user_prompts": (it.get("user_prompts") or [])[:12],
                    "assistant_milestones": (it.get("assistant_milestones") or [])[:4],
                }
            )
        return out

    return {
        "workday": raw_payload.get("workday", ""),
        "weekday": raw_payload.get("weekday", ""),
        "machines": raw_payload.get("machines") or ([raw_payload["machine"]] if raw_payload.get("machine") else []),
        "overall_active_span": raw_payload.get("overall_active_span", ""),
        "overall_estimated_hours": raw_payload.get("overall_estimated_hours", 0.0),
        "antigravity_hours": raw_payload.get("antigravity_hours", 0.0),
        "codex_hours": raw_payload.get("codex_hours", 0.0),
        "workbuddy_hours": raw_payload.get("workbuddy_hours", 0.0),
        "existing_projects": existing_projects[:60],
        "antigravity": trim_sessions(raw_payload.get("antigravity") or []),
        "codex": trim_sessions(raw_payload.get("codex") or []),
        "workbuddy": trim_sessions(raw_payload.get("workbuddy") or []),
        "git": (raw_payload.get("git") or [])[:30],
        "files": (raw_payload.get("files") or [])[:30],
    }


def collect_existing_projects_from_history(data_dir: str) -> list[dict[str, Any]]:
    return query_memory.project_candidates(data_dir)[:60]


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        parsed = json.loads(text[start : end + 1])
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("LLM response did not contain a valid JSON object.")


def normalize_summary_schema(
    summary: dict[str, Any], raw_payload: dict[str, Any], *, valid_project_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Ensure required keys and numeric invariants match write_excel.py expectations."""
    workday = str(raw_payload.get("workday") or summary.get("workday") or "")
    weekday = str(summary.get("weekday") or raw_payload.get("weekday") or "")
    if not weekday and re.match(r"^\d{4}-\d{2}-\d{2}$", workday):
        weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][
            datetime.strptime(workday, "%Y-%m-%d").weekday()
        ]

    work_items: list[dict[str, Any]] = []
    for it in summary.get("work_items") or []:
        if not isinstance(it, dict):
            continue
        scope = str(it.get("scope") or "personal").strip().lower()
        if scope not in ("work", "personal"):
            scope = "personal"
        entry = {
            "project": str(it.get("project") or "未分类项目").strip(),
            "category": str(it.get("category") or "其他").strip(),
            "tool": str(it.get("tool") or "Antigravity").strip(),
            "session_name": str(it.get("session_name") or "-").strip(),
            "summary": str(it.get("summary") or "-").strip(),
            "outputs": str(it.get("outputs") or "-").strip(),
            "sources": it.get("sources"),
            "active_span": str(it.get("active_span") or "-").strip(),
            "estimated_hours": round(float(it.get("estimated_hours") or 0.0), 2),
            "status": str(it.get("status") or "阶段完成").strip(),
            "next_step": str(it.get("next_step") or "-").strip(),
            "scope": scope,
        }
        if it.get("machine"):
            entry["machine"] = str(it["machine"]).strip()
        suggestion = it.get("suggested_project_id")
        if isinstance(suggestion, str) and valid_project_ids is not None and suggestion in valid_project_ids:
            entry["suggested_project_id"] = suggestion
            reason = it.get("project_suggestion_reason")
            if isinstance(reason, str):
                entry["project_suggestion_reason"] = reason.strip()[:300]
        work_items.append(entry)

    life_items: list[dict[str, Any]] = []
    for it in summary.get("life_items") or []:
        if not isinstance(it, dict):
            continue
        entry = {
            "tool": str(it.get("tool") or "Antigravity").strip(),
            "session_name": str(it.get("session_name") or "-").strip(),
            "topic": str(it.get("topic") or "随想记录").strip(),
            "summary": str(it.get("summary") or "-").strip(),
            "active_span": str(it.get("active_span") or "-").strip(),
            "sources": it.get("sources"),
        }
        if it.get("machine"):
            entry["machine"] = str(it["machine"]).strip()
        life_items.append(entry)

    work_hours = round(sum(it["estimated_hours"] for it in work_items if it["scope"] == "work"), 2)
    personal_hours = round(sum(it["estimated_hours"] for it in work_items if it["scope"] != "work"), 2)
    est_hours = round(float(summary.get("estimated_hours") or raw_payload.get("overall_estimated_hours") or (work_hours + personal_hours)), 2)

    raw_machines = raw_payload.get("machines") or ([raw_payload["machine"]] if raw_payload.get("machine") else [])
    item_machines = [it["machine"] for it in work_items if it.get("machine")] + [
        it["machine"] for it in life_items if it.get("machine")
    ]
    machines = sorted({str(m) for m in (list(raw_machines) + item_machines) if m})

    pb = summary.get("project_briefs") if isinstance(summary.get("project_briefs"), dict) else {}
    clean_pb: dict[str, dict[str, str]] = {}
    for proj, val in pb.items():
        if isinstance(val, dict):
            clean_pb[str(proj)] = {
                "ag": str(val.get("ag") or "-"),
                "codex": str(val.get("codex") or "-"),
                "wb": str(val.get("wb") or val.get("workbuddy") or "-"),
            }

    out: dict[str, Any] = {
        "workday": workday,
        "weekday": weekday,
        "headline": str(summary.get("headline") or f"{workday} 跨端工作汇总").strip(),
        "active_span": str(summary.get("active_span") or raw_payload.get("overall_active_span") or "-").strip(),
        "antigravity_hours": round(float(summary.get("antigravity_hours") if summary.get("antigravity_hours") is not None else raw_payload.get("antigravity_hours") or 0.0), 2),
        "codex_hours": round(float(summary.get("codex_hours") if summary.get("codex_hours") is not None else raw_payload.get("codex_hours") or 0.0), 2),
        "workbuddy_hours": round(float(summary.get("workbuddy_hours") if summary.get("workbuddy_hours") is not None else raw_payload.get("workbuddy_hours") or 0.0), 2),
        "estimated_hours": est_hours,
        "work_estimated_hours": round(float(summary.get("work_estimated_hours") if summary.get("work_estimated_hours") is not None else work_hours), 2),
        "personal_estimated_hours": round(float(summary.get("personal_estimated_hours") if summary.get("personal_estimated_hours") is not None else personal_hours), 2),
        "core_outputs": str(summary.get("core_outputs") or "-").strip(),
        "work_items": work_items,
        "life_items": life_items,
        "project_briefs": clean_pb,
    }
    if machines:
        out["machines"] = machines
    return verify_day_sources(out, raw_payload)


def _import_workspace(workspace: str) -> str:
    if workspace.startswith("["):
        try:
            paths = json.loads(workspace)
        except json.JSONDecodeError:
            paths = []
        workspace = str(paths[0]) if isinstance(paths, list) and paths else ""
    if workspace.startswith("file://"):
        url = urlsplit(workspace)
        workspace = unquote(url.path)
        if url.netloc and url.netloc.lower() != "localhost":
            workspace = f"//{url.netloc}{workspace}"
        elif re.match(r"^/[a-zA-Z]:/", workspace):
            workspace = workspace[1:]
    if not workspace:
        return ""
    if ntpath.splitdrive(workspace)[0]:
        return ntpath.normcase(ntpath.normpath(workspace))
    return posixpath.normpath(workspace)


def _import_project(session: dict[str, Any], raw: dict[str, Any]) -> tuple[str, str, str]:
    workspace = _import_workspace(str(session.get("workspace") or session.get("cwd") or "").strip())
    machine = str(session.get("machine") or raw.get("machine") or "").strip()
    title = str(session.get("title") or session.get("id") or "未命名会话").strip()
    name = str(session.get("folder") or "").strip() or ntpath.basename(workspace) or title
    return name, machine, workspace


def first_import_project_labels(raw_days: list[dict[str, Any]]) -> dict[tuple[str, str, str], str]:
    projects: dict[str, dict[tuple[str, str], str]] = defaultdict(dict)
    for raw in raw_days:
        for bucket in SESSION_BUCKETS:
            for session in raw.get(bucket) or []:
                if isinstance(session, dict):
                    name, machine, workspace = _import_project(session, raw)
                    projects[name_key(name)].setdefault((machine, workspace), name)
    labels = {}
    for key, locations in projects.items():
        if len(locations) < 2:
            continue
        qualified = defaultdict(list)
        for (machine, workspace), name in locations.items():
            label = f"{name} [{workspace or '工作区未知'} · {machine or '机器未知'}]"
            qualified[name_key(label)].append(((key, machine, workspace), label))
        for matches in qualified.values():
            for identity, label in matches:
                if len(matches) > 1:
                    fingerprint = sha256(json.dumps(identity).encode("utf-8")).hexdigest()[:12]
                    label = f"{label} #{fingerprint}"
                labels[identity] = label
    return labels


def fallback_rule_summarize(
    raw_payload: dict[str, Any], *, first_import: bool = False,
    import_labels: dict[tuple[str, str, str], str] | None = None,
) -> dict[str, Any]:
    """Deterministic fallback summarizer when no LLM API key is configured (useful for tests/offline)."""
    work_items: list[dict[str, Any]] = []
    project_briefs: dict[str, dict[str, str]] = defaultdict(lambda: {"ag": "-", "codex": "-", "wb": "-"})
    tool_map = (
        ("antigravity", "Antigravity", "ag"),
        ("codex", "Codex", "codex"),
        ("workbuddy", "WorkBuddy", "wb"),
    )
    if import_labels is None:
        import_labels = first_import_project_labels([raw_payload]) if first_import else {}
    for bucket, tool_label, pb_key in tool_map:
        for it in raw_payload.get(bucket) or []:
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or it.get("id") or "未命名会话").strip()
            ws = str(it.get("workspace") or it.get("cwd") or "").strip()
            proj = ntpath.basename(ws.rstrip("/\\")) if ws else title[:24]
            if first_import:
                proj, host, workspace = _import_project(it, raw_payload)
                proj = import_labels.get((name_key(proj), host, workspace), proj)
            prompts = it.get("user_prompts") or []
            milestones = it.get("assistant_milestones") or []
            snippet = str(milestones[-1] if milestones else (prompts[0] if prompts else title))[:140]
            machine = str(it.get("machine") or raw_payload.get("machine") or "").strip()
            entry: dict[str, Any] = {
                "project": proj or "未分类项目",
                "category": "自动归档",
                "tool": (it.get("source") or tool_label) if first_import else tool_label,
                "session_name": title,
                "summary": snippet,
                "outputs": ws or "-",
                "sources": [{"kind": "session", "id": it.get("id"), "bucket": bucket, "machine": machine}],
                "active_span": str(it.get("active_span") or "-"),
                "estimated_hours": round(float(it.get("estimated_hours") or 0.0), 2),
                "status": "已导入会话" if first_import else "阶段完成",
                "next_step": "-",
                "scope": "personal",
            }
            if machine:
                entry["machine"] = machine
            work_items.append(entry)
            if project_briefs[entry["project"]][pb_key] == "-":
                project_briefs[entry["project"]][pb_key] = snippet[:90]

    for g in raw_payload.get("git") or []:
        if not isinstance(g, dict):
            continue
        repo = str(g.get("repo") or g.get("path") or "git-repo").strip()
        commits = g.get("commits") or []
        subjects = [c.get("subject") or c.get("message") if isinstance(c, dict) else c for c in commits[:3]]
        msg = "; ".join(str(subject) for subject in subjects if subject) or "Git 提交"
        entry = {
            "project": ntpath.basename(repo.rstrip("/\\")) or repo,
            "category": "代码提交",
            "tool": "Git",
            "session_name": repo,
            "summary": msg[:140],
            "outputs": f"{len(commits)} commits",
            "sources": [
                {"kind": "commit", "id": c.get("hash"), "machine": g.get("machine"), "repo": repo}
                for c in commits
                if isinstance(c, dict)
            ],
            "active_span": str(g.get("active_span") or "-"),
            "estimated_hours": 0.0,
            "status": "阶段完成",
            "next_step": "-",
            "scope": "personal",
        }
        if g.get("machine"):
            entry["machine"] = str(g["machine"])
        work_items.append(entry)

    projects = sorted({it["project"] for it in work_items})
    headline = "、".join(projects[:4]) if projects else f"{raw_payload.get('workday', '')} 无新增项目活动"
    return normalize_summary_schema(
        {
            "workday": raw_payload.get("workday", ""),
            "weekday": raw_payload.get("weekday", ""),
            "headline": headline,
            "core_outputs": "；".join(projects[:6]) if projects else "-",
            "work_items": work_items,
            "life_items": [],
            "project_briefs": dict(project_briefs),
        },
        raw_payload,
    )


def call_openai_compatible_llm(
    raw_payload: dict[str, Any],
    existing_projects: list[dict[str, Any]],
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Call an OpenAI-compatible /chat/completions endpoint using stdlib urllib."""
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint = endpoint + "/chat/completions"

    compact = _compact_raw_for_llm(raw_payload, existing_projects)
    body = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(compact, ensure_ascii=False)},
        ],
    }
    req = urlrequest.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "daily-memory-summarizer/1.0",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:300]
        # Retry once without response_format if the provider rejects json_object mode
        if exc.code == 400 and "response_format" in err_body:
            body.pop("response_format", None)
            req2 = urlrequest.Request(
                endpoint,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": "daily-memory-summarizer/1.0",
                },
                method="POST",
            )
            with urlrequest.urlopen(req2, timeout=timeout) as resp2:
                resp_data = json.loads(resp2.read().decode("utf-8"))
        else:
            raise RuntimeError(f"LLM HTTP {exc.code}: {err_body}") from exc

    choices = resp_data.get("choices") or []
    if not choices:
        raise RuntimeError(f"LLM returned no choices: {str(resp_data)[:240]}")
    content = choices[0].get("message", {}).get("content", "")
    parsed = _extract_json_object(content)
    return normalize_summary_schema(
        parsed, raw_payload,
        valid_project_ids={p["project_id"] for p in existing_projects[:60] if p.get("project_id")},
    )


def summarize_workday(
    workday: str,
    data_dir: str,
    *,
    llm_base_url: str | None = None,
    llm_api_key: str | None = None,
    llm_model: str | None = None,
    llm_timeout: float = 120.0,
    allow_fallback: bool = False,
) -> dict[str, Any]:
    """Summarize merged raw_<workday>.json using configured LLM (or deterministic fallback)."""
    raw_payload = query_memory.load_raw_day(workday, data_dir=data_dir)
    if not raw_payload:
        raise FileNotFoundError(f"No merged raw file found for workday {workday} under {data_dir}")

    base_url = (llm_base_url or os.environ.get("LLM_BASE_URL") or "").strip()
    api_key = (llm_api_key or os.environ.get("LLM_API_KEY") or "").strip()
    model = (llm_model or os.environ.get("LLM_MODEL") or "gpt-4o-mini").strip()

    if base_url and api_key:
        existing_projects = collect_existing_projects_from_history(data_dir)
        return call_openai_compatible_llm(
            raw_payload,
            existing_projects,
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=llm_timeout,
        )
    if allow_fallback:
        return fallback_rule_summarize(raw_payload)
    raise RuntimeError("LLM_BASE_URL / LLM_API_KEY not configured (pass allow_fallback=True for rule-based fallback).")


def find_unsummarized_raw_days(data_dir: str, lookback_days: int = 14, tz_name: str = "Asia/Shanghai", day_start_hour: int = 5) -> list[str]:
    """Find completed workdays that have raw traces but are not yet in summary_all_history.json."""
    tz = ZoneInfo(tz_name)
    logical_today = (datetime.now(tz) - timedelta(hours=day_start_hour)).date()
    yesterday = (logical_today - timedelta(days=1)).strftime("%Y-%m-%d")
    cutoff = (logical_today - timedelta(days=max(1, lookback_days))).strftime("%Y-%m-%d")

    raw_days = set(multi_machine.list_raw_workdays(data_dir))
    summarized = {str(d.get("workday")) for d in query_memory.load_all_days(data_dir) if d.get("workday")}
    return sorted(wd for wd in raw_days if cutoff <= wd <= yesterday and wd not in summarized)


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize multi-machine raw JSON into canonical daily summary JSON.")
    parser.add_argument("--date", help="Target workday YYYY-MM-DD.")
    parser.add_argument("--yesterday", action="store_true", help="Summarize previous completed workday.")
    parser.add_argument("--missing", action="store_true", help="Summarize unsummarized recent raw workdays.")
    parser.add_argument("--data-dir", default=os.environ.get("DAILY_MEMORY_DATA_DIR") or query_memory.DEFAULT_DATA_DIR)
    parser.add_argument("--fallback-rule", action="store_true", help="Allow deterministic non-LLM fallback when no LLM key is set.")
    parser.add_argument("--output", help="Write resulting summary JSON to path (default: print to stdout).")
    args = parser.parse_args()

    tz_name = os.environ.get("TZ") or "Asia/Shanghai"
    day_start = int(os.environ.get("DAY_START_HOUR") or "5")

    if args.missing:
        dates = find_unsummarized_raw_days(args.data_dir, tz_name=tz_name, day_start_hour=day_start)
    elif args.date:
        dates = [args.date]
    else:
        logical_today = (datetime.now(ZoneInfo(tz_name)) - timedelta(hours=day_start)).date()
        dates = [(logical_today - timedelta(days=1 if args.yesterday else 0)).strftime("%Y-%m-%d")]

    results = [
        summarize_workday(wd, args.data_dir, allow_fallback=args.fallback_rule)
        for wd in dates
    ]
    payload: Any = results[0] if len(results) == 1 else {"days": results}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        multi_machine.atomic_write_text(args.output, text)
        print(f"Wrote {len(results)} summarized day(s) to {args.output}")
    else:
        print(text)


if __name__ == "__main__":
    main()
