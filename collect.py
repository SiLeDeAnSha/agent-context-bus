#!/usr/bin/env python3
"""Collect daily work traces from Antigravity, Codex, Git, and local project directories."""

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import glob
import io
import json
import os
import re
import sqlite3
import subprocess
from typing import Any
from zoneinfo import ZoneInfo

import multi_machine
import remote_client

try:
    from compression import zstd as _stdlib_zstd  # Python 3.14+
except ImportError:
    _stdlib_zstd = None

try:
    import zstandard as _thirdparty_zstd  # type: ignore[import-not-found]
except ImportError:
    _thirdparty_zstd = None


def open_zstd_text(path: str):
    """Open a .zst file as UTF-8 text on both Python 3.14+ and older Python versions."""
    if _stdlib_zstd is not None:
        return _stdlib_zstd.open(path, "rt", encoding="utf-8")
    if _thirdparty_zstd is not None:
        raw = open(path, "rb")
        dctx = _thirdparty_zstd.ZstdDecompressor()
        reader = dctx.stream_reader(raw, closefd=True)
        return io.TextIOWrapper(reader, encoding="utf-8")
    raise RuntimeError("zstd decompression requires Python 3.14+ or `pip install zstandard`.")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DATA_DIR = os.path.join(BASE_DIR, "data")


EXAMPLE_CONFIG_PATH = os.path.join(BASE_DIR, "config.example.json")


def load_config() -> dict[str, Any]:
    path = CONFIG_PATH if os.path.exists(CONFIG_PATH) else EXAMPLE_CONFIG_PATH
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_iso_dt(value: str) -> datetime | None:
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def workday_bounds(workday: str, tz_name: str, day_start_hour: int) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tz_name)
    d = datetime.strptime(workday, "%Y-%m-%d").date()
    start = datetime(d.year, d.month, d.day, day_start_hour, 0, 0, tzinfo=tz)
    end = start + timedelta(days=1)
    return start, end


def current_workday(tz_name: str, day_start_hour: int) -> str:
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    logical_date = (now - timedelta(hours=day_start_hour)).date()
    return logical_date.strftime("%Y-%m-%d")


def format_span(timestamps: list[datetime], tz: ZoneInfo, workday: str) -> str:
    if not timestamps:
        return ""
    local_ts = sorted(t.astimezone(tz) for t in timestamps)
    base_date = datetime.strptime(workday, "%Y-%m-%d").date()

    def fmt(dt: datetime) -> str:
        suffix = "(+1)" if dt.date() > base_date else ""
        return f"{dt.strftime('%H:%M')}{suffix}"

    if len(local_ts) == 1:
        return fmt(local_ts[0])
    start_s = fmt(local_ts[0])
    end_s = fmt(local_ts[-1])
    return start_s if start_s == end_s else f"{start_s}-{end_s}"


def estimate_active_hours(timestamps: list[datetime], merge_gap_minutes: int = 30) -> float:
    """Merge nearby timestamps into active clusters to avoid overcounting idle gaps."""
    if not timestamps:
        return 0.0
    ts = sorted(timestamps)
    gap = timedelta(minutes=merge_gap_minutes)
    clusters: list[list[datetime]] = [[ts[0], ts[0]]]
    for t in ts[1:]:
        if t - clusters[-1][1] <= gap:
            clusters[-1][1] = t
        else:
            clusters.append([t, t])
    total_minutes = 0.0
    for c_start, c_end in clusters:
        span_min = (c_end - c_start).total_seconds() / 60.0
        # Give each active burst a minimum of 6 min (0.1h) plus 5 min tail buffer.
        total_minutes += max(6.0, span_min + 5.0)
    return round(total_minutes / 60.0, 2)


def compress_turn_history(
    msgs: list[str],
    max_items: int = 80,
    per_item_chars: int = 140,
) -> list[str]:
    """Keep all meaningful user prompts across turns 1..N without truncating to the first 10."""
    trivial_acks = {"1", "2", "3", "好", "好的", "可以", "继续", "开始", "确认", "行", "ok", "OK", "yes", "是", "是的", "动手", "开始执行"}
    use_ack_filter = len(msgs) > 10
    cleaned_list: list[str] = []
    seen: set[str] = set()
    for raw in msgs:
        s = re.sub(r"\s+", " ", str(raw or "")).strip()
        if not s:
            continue
        if use_ack_filter and s in trivial_acks:
            continue
        short = s if len(s) <= per_item_chars else (s[: per_item_chars - 1].rstrip(" ，、；;") + "…")
        if short in seen:
            continue
        seen.add(short)
        cleaned_list.append(short)
    if len(cleaned_list) <= max_items:
        return cleaned_list
    step = (len(cleaned_list) - 1) / float(max_items - 1)
    return [cleaned_list[round(i * step)] for i in range(max_items)]


def sample_assistant_milestones(
    replies: list[str],
    max_items: int = 5,
    per_item_chars: int = 160,
) -> list[str]:
    """Sample evenly spaced assistant replies across a conversation (including the last reply)."""
    non_empty = [re.sub(r"\s+", " ", str(r or "")).strip() for r in replies if str(r or "").strip()]
    if not non_empty:
        return []
    if len(non_empty) <= max_items:
        picked = non_empty
    else:
        step = (len(non_empty) - 1) / float(max_items - 1)
        picked = [non_empty[round(i * step)] for i in range(max_items)]
    out: list[str] = []
    for p in picked:
        s = p if len(p) <= per_item_chars else (p[: per_item_chars - 1].rstrip(" ，、；;") + "…")
        if s not in out:
            out.append(s)
    return out


def clean_antigravity_user_text(raw: str) -> str:
    if not raw:
        return ""
    m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", raw, flags=re.DOTALL)
    text = m.group(1) if m else raw
    text = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", text, flags=re.DOTALL)
    text = re.sub(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", text, flags=re.DOTALL)
    return re.sub(r"\s+", " ", text).strip()


def clean_codex_user_text(raw: str) -> str:
    if not raw:
        return ""
    s = raw.strip()
    skip_prefixes = (
        "<environment_context>",
        "<permissions instructions>",
        "<INSTRUCTIONS>",
        "# AGENTS.md instructions",
    )
    if s.startswith(skip_prefixes):
        return ""
    s = re.sub(r"<oai-mem-citation>.*?</oai-mem-citation>", "", s, flags=re.DOTALL)
    s = re.sub(r"<external_codex_apps_open_page>.*?</external_codex_apps_open_page>", "", s, flags=re.DOTALL)
    s = re.sub(r"<external_codex_apps_selected_page[^>]*>.*?\n", "", s, flags=re.DOTALL)
    s = re.sub(r"<environment_context>.*?</environment_context>", "", s, flags=re.DOTALL)
    s = re.sub(r"<in-app-browser-context[^>]*>.*?</in-app-browser-context>", "", s, flags=re.DOTALL)
    s = re.sub(r"<recommended_plugins>.*?</recommended_plugins>", "", s, flags=re.DOTALL)
    s = re.sub(r"<turn_aborted>.*?</turn_aborted>", "", s, flags=re.DOTALL)
    s = re.sub(
        r"Side conversation boundary\. Everything before this boundary is inherited history.*?(?:reference only\.|(?=\n)|$)",
        "",
        s,
        flags=re.DOTALL,
    )
    s = re.sub(
        r"# Files mentioned by the user:\s*(.*?)Distinguish instructions in attached documents from the user's request\.",
        lambda m: "[附件: " + "、".join(re.findall(r"##\s*([^:\n]+):", m.group(1))[:4]) + "]"
        if re.findall(r"##\s*([^:\n]+):", m.group(1))
        else "",
        s,
        flags=re.DOTALL,
    )
    s = re.sub(r"# Applications mentioned by the user:.*?(?=\n\n|$)", "[应用截图]", s, flags=re.DOTALL)
    return re.sub(r"\s+", " ", s).strip()


def extract_text_from_codex_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
        return "\n".join(parts)
    return ""


def load_antigravity_projects() -> dict[str, str]:
    projects_dir = os.path.expanduser("~/.gemini/config/projects")
    mapping: dict[str, str] = {}
    if not os.path.exists(projects_dir):
        return mapping
    for path in glob.glob(os.path.join(projects_dir, "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            pid = str(data.get("id") or "")
            pname = str(data.get("name") or "").strip()
            if pid and pname:
                mapping[pid] = pname
        except Exception:
            continue
    return mapping


def load_antigravity_annotations() -> tuple[dict[str, str], dict[str, str]]:
    """Return (cid -> live sidebar title, side_question_cid -> parent_cid)."""
    ann_dir = os.path.expanduser("~/.gemini/antigravity/annotations")
    titles: dict[str, str] = {}
    side_parent: dict[str, str] = {}
    if not os.path.exists(ann_dir):
        return titles, side_parent
    for path in glob.glob(os.path.join(ann_dir, "*.pbtxt")):
        cid = os.path.basename(path)[:-6]
        try:
            txt = open(path, "r", encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        m_title = re.search(r'title:"([^"]*)"', txt)
        if m_title and m_title.group(1).strip():
            titles[cid] = m_title.group(1).strip()
        for sq_id in re.findall(r'side_question_cascade_ids:"([^"]+)"', txt):
            side_parent[sq_id] = cid
    return titles, side_parent


def collect_antigravity(
    start_dt: datetime,
    end_dt: datetime,
    tz: ZoneInfo,
    workday: str,
    cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[datetime]]:
    db_path = os.path.expanduser("~/.gemini/antigravity/conversation_summaries.db")
    brain_dir = os.path.expanduser("~/.gemini/antigravity/brain")
    markers = cfg.get("self_filter_markers", [])
    proj_names = load_antigravity_projects()
    ann_titles, side_parent = load_antigravity_annotations()

    meta_map: dict[str, dict[str, Any]] = {}
    if os.path.exists(db_path):
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = conn.execute(
            """
            SELECT conversation_id, title, workspace_uris, parent_conversation_id, nesting_depth, project_id
            FROM conversation_summaries
            """
        ).fetchall()
        conn.close()
        for cid, title, ws, parent_id, depth, project_id in rows:
            meta_map[cid] = {
                "title": title or "",
                "workspace_uris": ws or "",
                "parent_id": parent_id or "",
                "depth": depth or 0,
                "project_id": project_id or "",
            }

    conv_data: dict[str, dict[str, Any]] = {}
    side_extra_ts: dict[str, list[datetime]] = defaultdict(list)
    side_extra_prompts: dict[str, list[str]] = defaultdict(list)
    side_extra_replies: dict[str, list[str]] = defaultdict(list)
    all_ts: list[datetime] = []

    for tpath in glob.glob(os.path.join(brain_dir, "*", ".system_generated", "logs", "transcript.jsonl")):
        cid = tpath.split(os.sep)[-4]
        meta = meta_map.get(cid, {})
        if meta.get("parent_id") or meta.get("depth", 0) > 0:
            continue
        title = ann_titles.get(cid) or meta.get("title", "")
        if any(m in title for m in markers):
            continue

        # Fast mtime skip: if file wasn't modified since start_dt, no events can be >= start_dt.
        if datetime.fromtimestamp(os.path.getmtime(tpath), tz=timezone.utc) < start_dt:
            continue

        step_ts: list[datetime] = []
        user_msgs: list[str] = []
        assistant_msgs: list[str] = []
        is_self_auto = False

        try:
            with open(tpath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    raw_content = str(obj.get("content") or "")
                    dt = parse_iso_dt(str(obj.get("created_at") or ""))
                    if not dt or not (start_dt <= dt < end_dt):
                        continue
                    stype = obj.get("type")
                    source = obj.get("source")
                    if stype == "USER_INPUT" or source == "USER_EXPLICIT":
                        if any(m in raw_content for m in markers):
                            is_self_auto = True
                            break
                        cleaned = clean_antigravity_user_text(raw_content)
                        if cleaned:
                            user_msgs.append(cleaned[:300])
                        step_ts.append(dt)
                    elif stype == "PLANNER_RESPONSE" and raw_content.strip():
                        cleaned = re.sub(r"\s+", " ", raw_content).strip()
                        assistant_msgs.append(cleaned[:300])
                        step_ts.append(dt)
                    elif stype == "PLANNER_RESPONSE":
                        step_ts.append(dt)
        except OSError:
            continue

        if is_self_auto or not step_ts or not user_msgs:
            continue

        all_ts.extend(step_ts)
        if cid in side_parent:
            parent_cid = side_parent[cid]
            side_extra_ts[parent_cid].extend(step_ts)
            side_extra_prompts[parent_cid].extend(user_msgs)
            side_extra_replies[parent_cid].extend(assistant_msgs)
            continue

        folder = proj_names.get(meta.get("project_id", ""), "")
        final_title = title or user_msgs[0][:60]
        display_title = f"{final_title} [目录: {folder}]" if folder else final_title

        conv_data[cid] = {
            "source": "Antigravity",
            "id": cid,
            "title": final_title,
            "folder": folder,
            "display_title": display_title,
            "workspace": meta.get("workspace_uris", ""),
            "turn_count": len(user_msgs),
            "step_ts": step_ts,
            "user_prompts": compress_turn_history(user_msgs),
            "assistant_milestones": sample_assistant_milestones(assistant_msgs),
            "last_assistant_reply": assistant_msgs[-1] if assistant_msgs else "",
        }

    # Fold side-question activity into parent conversations (synthesizing parent entry if main chat was idle today).
    for parent_cid, extra_ts in side_extra_ts.items():
        folded_prompts = [f"[侧边追问] {p}" for p in compress_turn_history(side_extra_prompts.get(parent_cid, []), max_items=10)]
        if parent_cid in conv_data:
            conv_data[parent_cid]["step_ts"].extend(extra_ts)
            conv_data[parent_cid]["user_prompts"].extend(folded_prompts)
            if not conv_data[parent_cid].get("last_assistant_reply") and side_extra_replies.get(parent_cid):
                conv_data[parent_cid]["last_assistant_reply"] = side_extra_replies[parent_cid][-1]
        elif folded_prompts:
            p_meta = meta_map.get(parent_cid, {})
            p_folder = proj_names.get(p_meta.get("project_id", ""), "")
            p_title = ann_titles.get(parent_cid) or p_meta.get("title") or side_extra_prompts[parent_cid][0][:60]
            p_display = f"{p_title} [目录: {p_folder}]" if p_folder else p_title
            p_replies = side_extra_replies.get(parent_cid, [])
            conv_data[parent_cid] = {
                "source": "Antigravity",
                "id": parent_cid,
                "title": p_title,
                "folder": p_folder,
                "display_title": p_display,
                "workspace": p_meta.get("workspace_uris", ""),
                "turn_count": len(side_extra_prompts.get(parent_cid, [])),
                "step_ts": list(extra_ts),
                "user_prompts": folded_prompts,
                "assistant_milestones": sample_assistant_milestones(p_replies),
                "last_assistant_reply": p_replies[-1] if p_replies else "",
            }

    results: list[dict[str, Any]] = []
    for item in conv_data.values():
        ts_list = item.pop("step_ts")
        item["active_span"] = format_span(ts_list, tz, workday)
        item["estimated_hours"] = estimate_active_hours(ts_list, cfg["merge_gap_minutes"])
        results.append(item)

    results.sort(key=lambda x: x["active_span"])
    return results, all_ts


def load_codex_thread_names() -> dict[str, str]:
    index_path = os.path.expanduser("~/.codex/session_index.jsonl")
    names: dict[str, str] = {}
    if not os.path.exists(index_path):
        return names
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = str(obj.get("id") or "")
                tname = str(obj.get("thread_name") or "").strip()
                if sid and tname:
                    names[sid] = tname
    except OSError:
        pass
    return names


def extract_codex_uuid(filename: str) -> str:
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", filename)
    return m.group(1) if m else ""


def list_codex_rollout_paths(roots: list[str] | None = None) -> list[str]:
    """List Codex rollout files keeping active sessions ahead of archived_sessions."""
    target_roots = roots or [
        os.path.expanduser("~/.codex/sessions"),
        os.path.expanduser("~/.codex/archived_sessions"),
    ]
    ordered: list[str] = []
    for root in target_roots:
        if not os.path.exists(root):
            continue
        found = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
        found.extend(glob.glob(os.path.join(root, "**", "*.jsonl.zst"), recursive=True))
        ordered.extend(sorted(found))
    return ordered


def collect_codex(
    start_dt: datetime,
    end_dt: datetime,
    tz: ZoneInfo,
    workday: str,
    cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[datetime]]:
    markers = cfg.get("self_filter_markers", [])
    thread_names = load_codex_thread_names()
    results: list[dict[str, Any]] = []
    all_ts: list[datetime] = []
    seen_stems: set[str] = set()

    for path in list_codex_rollout_paths():
        if datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc) < start_dt:
            continue

        base = os.path.basename(path)
        stem = base[:-10] if base.endswith(".jsonl.zst") else base[:-6]
        if stem in seen_stems:
            continue
        seen_stems.add(stem)

        session_id = base
        session_uuid = extract_codex_uuid(base)
        thread_name = thread_names.get(session_uuid, "")
        if thread_name == "Guardian review":
            continue

        is_archived = "/archived_sessions/" in path
        cwd = ""
        step_ts: list[datetime] = []
        user_msgs: list[str] = []
        assistant_msgs: list[str] = []
        is_self_auto = False

        try:
            opener = (
                (lambda p: open_zstd_text(p))
                if path.endswith(".zst")
                else (lambda p: open(p, "r", encoding="utf-8"))
            )
            with opener(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rtype = obj.get("type")
                    payload = obj.get("payload") or {}
                    if rtype == "session_meta" and isinstance(payload, dict) and not session_uuid:
                        session_uuid = str(payload.get("session_id") or "")
                        thread_name = thread_names.get(session_uuid, thread_name)
                    if rtype == "turn_context" and isinstance(payload, dict) and payload.get("cwd"):
                        cwd = payload["cwd"]
                    dt = parse_iso_dt(str(obj.get("timestamp") or ""))
                    if not dt or not (start_dt <= dt < end_dt):
                        continue

                    if rtype == "response_item" and isinstance(payload, dict) and payload.get("type") == "message":
                        role = payload.get("role")
                        text = extract_text_from_codex_content(payload.get("content"))
                        if role == "user":
                            if any(m in text for m in markers):
                                is_self_auto = True
                                break
                            cleaned = clean_codex_user_text(text)
                            if cleaned:
                                user_msgs.append(cleaned[:300])
                                step_ts.append(dt)
                        elif role == "assistant":
                            cleaned = re.sub(r"<oai-mem-citation>.*?</oai-mem-citation>", "", text, flags=re.DOTALL)
                            cleaned = re.sub(r"\s+", " ", cleaned).strip()
                            if cleaned:
                                assistant_msgs.append(cleaned[:300])
                                step_ts.append(dt)
        except OSError:
            continue

        if is_self_auto or not step_ts or not user_msgs:
            continue

        all_ts.extend(step_ts)
        results.append(
            {
                "source": "Codex",
                "id": session_uuid or session_id,
                "archived": is_archived,
                "cwd": cwd,
                "title": thread_name or user_msgs[0][:60],
                "turn_count": len(user_msgs),
                "active_span": format_span(step_ts, tz, workday),
                "estimated_hours": estimate_active_hours(step_ts, cfg["merge_gap_minutes"]),
                "user_prompts": compress_turn_history(user_msgs),
                "assistant_milestones": sample_assistant_milestones(assistant_msgs),
                "last_assistant_reply": assistant_msgs[-1] if assistant_msgs else "",
            }
        )

    results.sort(key=lambda x: x["active_span"])
    return results, all_ts


def clean_workbuddy_user_text(raw: str) -> str:
    if not raw:
        return ""
    s = re.sub(r"<system-reminder\b[^>]*>.*?</system-reminder>", "", raw, flags=re.DOTALL)
    s = re.sub(r"<local-command-caveat>.*?</local-command-caveat>", "", s, flags=re.DOTALL)
    s = re.sub(r"<task-notification>.*?</task-notification>", "", s, flags=re.DOTALL)
    s = re.sub(r"</?(?:user_query|long_text_quote|cb_summary)>", "", s)
    s = re.sub(r"<image_list>.*?</image_list>", "", s, flags=re.DOTALL)
    return re.sub(r"\s+", " ", s).strip()


def parse_workbuddy_ts(raw_ts: Any) -> datetime | None:
    if raw_ts is None or raw_ts == "":
        return None
    if isinstance(raw_ts, (int, float)):
        sec = raw_ts / 1000.0 if raw_ts > 1e12 else float(raw_ts)
        return datetime.fromtimestamp(sec, tz=timezone.utc)
    if isinstance(raw_ts, str):
        if raw_ts.isdigit():
            val = float(raw_ts)
            sec = val / 1000.0 if val > 1e12 else val
            return datetime.fromtimestamp(sec, tz=timezone.utc)
        return parse_iso_dt(raw_ts)
    return None


def load_workbuddy_metadata() -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Return (session_id -> metadata, session_id -> jsonl_path) excluding background cron runs."""
    db_path = os.path.expanduser("~/.workbuddy/workbuddy.db")
    projects_dir = os.path.expanduser("~/.workbuddy/projects")
    meta: dict[str, dict[str, Any]] = {}

    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            spaces = dict(conn.execute("SELECT id, name FROM spaces").fetchall())
            rows = conn.execute(
                """
                SELECT id, COALESCE(custom_title, title), space_id, cwd, is_background_automation, deleted_at
                FROM sessions
                """
            ).fetchall()
            conn.close()
            for sid, title, space_id, cwd, is_bg, deleted_at in rows:
                if is_bg:
                    continue
                meta[str(sid)] = {
                    "title": str(title or "").strip(),
                    "space": str(spaces.get(space_id, "") or "").strip(),
                    "cwd": str(cwd or "").strip(),
                    "archived": deleted_at is not None,
                }
        except Exception:
            pass

    jsonl_map: dict[str, str] = {}
    if os.path.exists(projects_dir):
        for p in sorted(glob.glob(os.path.join(projects_dir, "**", "*.jsonl"), recursive=True)):
            sid = os.path.basename(p)[:-6]
            jsonl_map[sid] = p
    return meta, jsonl_map


def parse_workbuddy_jsonl_by_day(
    jpath: str,
    markers: list[str],
    tz: ZoneInfo,
    day_start_hour: int,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
    end_workday: str | None = None,
) -> tuple[dict[str, list[datetime]], dict[str, list[str]], dict[str, list[str]], bool]:
    steps_day: dict[str, list[datetime]] = defaultdict(list)
    prompts_day: dict[str, list[str]] = defaultdict(list)
    replies_day: dict[str, list[str]] = defaultdict(list)
    is_self = False

    try:
        with open(jpath, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "message":
                    continue
                dt = parse_workbuddy_ts(obj.get("timestamp"))
                if not dt:
                    continue
                if start_dt is not None and end_dt is not None and not (start_dt <= dt < end_dt):
                    continue
                wd = (dt.astimezone(tz) - timedelta(hours=day_start_hour)).strftime("%Y-%m-%d")
                if end_workday and wd > end_workday:
                    continue
                role = obj.get("role")
                text = extract_text_from_codex_content(obj.get("content"))
                if role == "user":
                    if any(m in text for m in markers):
                        is_self = True
                        break
                    cleaned = clean_workbuddy_user_text(text)
                    if cleaned:
                        prompts_day[wd].append(cleaned[:300])
                        steps_day[wd].append(dt)
                elif role == "assistant":
                    cleaned = re.sub(r"\s+", " ", text).strip()
                    if cleaned:
                        replies_day[wd].append(cleaned[:300])
                        steps_day[wd].append(dt)
    except OSError:
        pass
    return steps_day, prompts_day, replies_day, is_self


def build_workbuddy_record(
    sid: str,
    info: dict[str, Any],
    ts: list[datetime],
    prompts: list[str],
    replies: list[str],
    tz: ZoneInfo,
    workday: str,
    gap_min: int,
) -> dict[str, Any]:
    raw_title = info.get("title") or prompts[0][:60]
    space = info.get("space") or ""
    cwd = info.get("cwd") or ""
    folder_label = space
    if not folder_label and cwd and not cwd.startswith(os.path.expanduser("~/WorkBuddy")):
        folder_label = os.path.basename(cwd.rstrip(os.sep))
    display_title = f"{raw_title} [目录: {folder_label}]" if folder_label else raw_title
    return {
        "source": "WorkBuddy",
        "id": sid,
        "archived": bool(info.get("archived")),
        "cwd": cwd,
        "space": space,
        "title": raw_title,
        "display_title": display_title,
        "turn_count": len(prompts),
        "active_span": format_span(ts, tz, workday),
        "estimated_hours": estimate_active_hours(ts, gap_min),
        "user_prompts": compress_turn_history(prompts),
        "assistant_milestones": sample_assistant_milestones(replies),
        "last_assistant_reply": replies[-1] if replies else "",
    }


def collect_workbuddy(
    start_dt: datetime,
    end_dt: datetime,
    tz: ZoneInfo,
    workday: str,
    cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[datetime]]:
    markers = cfg.get("self_filter_markers", [])
    gap_min = int(cfg["merge_gap_minutes"])
    day_start_hour = int(cfg["day_start_hour"])
    meta, jsonl_map = load_workbuddy_metadata()

    results: list[dict[str, Any]] = []
    all_ts: list[datetime] = []

    for sid, info in meta.items():
        jpath = jsonl_map.get(sid)
        if not jpath or not os.path.exists(jpath):
            continue
        if datetime.fromtimestamp(os.path.getmtime(jpath), tz=timezone.utc) < start_dt:
            continue
        if any(m in info.get("title", "") for m in markers):
            continue

        steps_day, prompts_day, replies_day, is_self = parse_workbuddy_jsonl_by_day(
            jpath, markers, tz, day_start_hour, start_dt=start_dt, end_dt=end_dt
        )
        if is_self:
            continue
        ts = steps_day.get(workday, [])
        prompts = prompts_day.get(workday, [])
        if not ts or not prompts:
            continue
        all_ts.extend(ts)
        results.append(build_workbuddy_record(sid, info, ts, prompts, replies_day.get(workday, []), tz, workday, gap_min))

    results.sort(key=lambda x: x["active_span"])
    return results, all_ts


def clean_t3_user_text(raw: str) -> str:
    if not raw:
        return ""
    s = re.sub(r"<t3_code_instructions>.*?</t3_code_instructions>", "", raw, flags=re.DOTALL)
    m = re.search(r"<user_request>\s*(.*?)\s*</user_request>", s, flags=re.DOTALL)
    if m:
        s = m.group(1)
    return re.sub(r"\s+", " ", s).strip()


def map_t3_provider_bucket(provider: str, instance_id: str) -> tuple[str, str]:
    """Return (bucket, display_tool) for a T3 Code thread provider."""
    p = (instance_id or provider or "").strip()
    pl = p.lower()
    if "antigravity" in pl:
        return "antigravity", "Antigravity"
    if "codex" in pl:
        return "codex", "Codex"
    if "codebuddy" in pl or "workbuddy" in pl:
        return "workbuddy", "WorkBuddy"
    if "claude" in pl:
        return "other", "Claude"
    if "cursor" in pl:
        return "other", "Cursor"
    if "grok" in pl:
        return "other", "Grok"
    if "opencode" in pl:
        return "other", "OpenCode"
    return "other", "T3 Code"


def load_t3_code_data(
    db_path: str | None = None,
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, Any]], dict[str, set[str]], list[tuple[str, str, str, str]]]:
    """Read T3 Code statev2.sqlite projects, top-level threads, provider session IDs, and messages."""
    path = os.path.expanduser(db_path or "~/.t3/userdata/statev2.sqlite")
    if not os.path.exists(path):
        return {}, {}, {}, []

    projects: dict[str, dict[str, str]] = {}
    threads: dict[str, dict[str, Any]] = {}
    provider_sids: dict[str, set[str]] = defaultdict(set)
    messages: list[tuple[str, str, str, str]] = []

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        for pid, title, ws_root in conn.execute(
            "SELECT project_id, title, workspace_root FROM projection_projects"
        ):
            projects[str(pid)] = {
                "title": str(title or "").strip(),
                "workspace_root": str(ws_root or "").strip(),
            }

        for tid, pid, title, def_prov, inst_id, archived_at, deleted_at, payload_raw in conn.execute(
            """
            SELECT thread_id, project_id, title, default_provider, provider_instance_id,
                   archived_at, deleted_at, payload_json
            FROM orchestration_v2_projection_threads
            """
        ):
            tid_s = str(tid or "")
            if deleted_at is not None or tid_s.startswith("thread:subagent:"):
                continue
            try:
                p_obj = json.loads(payload_raw) if payload_raw else {}
            except json.JSONDecodeError:
                p_obj = {}
            lineage = p_obj.get("lineage") or {}
            if lineage.get("relationshipToParent") == "subagent":
                continue
            threads[tid_s] = {
                "thread_id": tid_s,
                "project_id": str(pid or ""),
                "title": str(title or "").strip(),
                "default_provider": str(def_prov or "").strip(),
                "provider_instance_id": str(inst_id or p_obj.get("providerInstanceId") or "").strip(),
                "archived": archived_at is not None,
                "creation_source": str(p_obj.get("creationSource") or ""),
            }

        for tid, prov_sid in conn.execute(
            "SELECT thread_id, provider_session_id FROM orchestration_v2_projection_provider_threads WHERE thread_id IS NOT NULL AND provider_session_id IS NOT NULL"
        ):
            if prov_sid:
                provider_sids[str(tid)].add(str(prov_sid))

        messages = conn.execute(
            """
            SELECT thread_id, role, created_at, payload_json
            FROM orchestration_v2_projection_messages
            ORDER BY created_at ASC
            """
        ).fetchall()
        conn.close()
    except Exception:
        return {}, {}, {}, []

    return projects, threads, provider_sids, messages


def build_t3_record(
    tid: str,
    tinfo: dict[str, Any],
    proj_info: dict[str, str],
    bucket: str,
    tool_label: str,
    ts: list[datetime],
    prompts: list[str],
    replies: list[str],
    tz: ZoneInfo,
    workday: str,
    gap_min: int,
) -> dict[str, Any]:
    raw_title = tinfo.get("title") or prompts[0][:60]
    proj_title = proj_info.get("title", "")
    ws_root = proj_info.get("workspace_root", "")

    if bucket == "workbuddy":
        tag = f"T3·Codebuddy/{proj_title}" if proj_title else "T3·Codebuddy"
    else:
        tag = f"T3·{proj_title}" if proj_title else "T3"
    display_title = f"{raw_title} [{tag}]"

    rec: dict[str, Any] = {
        "source": tool_label,
        "client": "T3 Code",
        "id": tid,
        "archived": bool(tinfo.get("archived")),
        "title": raw_title,
        "folder": proj_title,
        "display_title": display_title,
        "workspace": ws_root,
        "cwd": ws_root,
        "turn_count": len(prompts),
        "active_span": format_span(ts, tz, workday),
        "estimated_hours": estimate_active_hours(ts, gap_min),
        "user_prompts": compress_turn_history(prompts),
        "assistant_milestones": sample_assistant_milestones(replies),
        "last_assistant_reply": replies[-1] if replies else "",
    }
    if bucket == "codex":
        rec["title"] = display_title
    return rec


def collect_t3_code(
    start_dt: datetime,
    end_dt: datetime,
    tz: ZoneInfo,
    workday: str,
    cfg: dict[str, Any],
    existing_codex_ids: set[str] | None = None,
    db_path: str | None = None,
) -> tuple[
    list[dict[str, Any]],
    list[datetime],
    list[dict[str, Any]],
    list[datetime],
    list[dict[str, Any]],
    list[datetime],
    list[dict[str, Any]],
    list[datetime],
    dict[str, str],
]:
    """Collect active T3 Code threads for one workday, routed by provider and deduplicated against Codex."""
    markers = cfg.get("self_filter_markers", [])
    gap_min = int(cfg["merge_gap_minutes"])
    codex_seen = existing_codex_ids or set()

    projects, threads, provider_sids, messages = load_t3_code_data(db_path=db_path)
    if not threads:
        return [], [], [], [], [], [], [], [], {}

    steps_by_t: dict[str, list[datetime]] = defaultdict(list)
    prompts_by_t: dict[str, list[str]] = defaultdict(list)
    replies_by_t: dict[str, list[str]] = defaultdict(list)
    self_threads: set[str] = set()

    for tid, role, created_at, payload_raw in messages:
        tid_s = str(tid or "")
        if tid_s not in threads or tid_s in self_threads:
            continue
        dt = parse_iso_dt(str(created_at or ""))
        if not dt or not (start_dt <= dt < end_dt):
            continue
        try:
            p_obj = json.loads(payload_raw) if payload_raw else {}
        except json.JSONDecodeError:
            continue
        raw_text = str(p_obj.get("text") or "")
        if role == "user":
            if any(m in raw_text for m in markers):
                self_threads.add(tid_s)
                continue
            # Exclude agent-to-agent MCP handoffs or subagent system prompts; keep real human inputs.
            created_by = str(p_obj.get("createdBy") or "")
            creation_src = str(p_obj.get("creationSource") or "")
            if created_by and created_by != "user":
                continue
            if not created_by and creation_src in ("mcp", "provider"):
                continue
            cleaned = clean_t3_user_text(raw_text)
            if cleaned:
                prompts_by_t[tid_s].append(cleaned[:300])
                steps_by_t[tid_s].append(dt)
        elif role == "assistant":
            cleaned = re.sub(r"\s+", " ", raw_text).strip()
            if cleaned:
                replies_by_t[tid_s].append(cleaned[:300])
                steps_by_t[tid_s].append(dt)

    ag_items: list[dict[str, Any]] = []
    ag_ts: list[datetime] = []
    cx_items: list[dict[str, Any]] = []
    cx_ts: list[datetime] = []
    wb_items: list[dict[str, Any]] = []
    wb_ts: list[datetime] = []
    other_items: list[dict[str, Any]] = []
    other_ts: list[datetime] = []
    codex_title_hints: dict[str, str] = {}

    for tid, tinfo in threads.items():
        if tid in self_threads:
            continue
        if any(m in tinfo.get("title", "") for m in markers):
            continue
        prompts = prompts_by_t.get(tid, [])
        ts = steps_by_t.get(tid, [])
        if not prompts or not ts:
            continue

        proj_info = projects.get(tinfo.get("project_id", ""), {})
        bucket, tool_label = map_t3_provider_bucket(
            tinfo.get("default_provider", ""), tinfo.get("provider_instance_id", "")
        )

        if bucket == "codex":
            matched_sids = provider_sids.get(tid, set()) & codex_seen
            if matched_sids:
                proj_title = proj_info.get("title", "")
                hint_title = tinfo.get("title", "")
                if hint_title:
                    full_hint = f"{hint_title} [T3·{proj_title}]" if proj_title else f"{hint_title} [T3]"
                    for sid in matched_sids:
                        codex_title_hints[sid] = full_hint
                continue

        rec = build_t3_record(
            tid, tinfo, proj_info, bucket, tool_label, ts, prompts, replies_by_t.get(tid, []), tz, workday, gap_min
        )
        if bucket == "antigravity":
            ag_items.append(rec)
            ag_ts.extend(ts)
        elif bucket == "codex":
            cx_items.append(rec)
            cx_ts.extend(ts)
        elif bucket == "workbuddy":
            wb_items.append(rec)
            wb_ts.extend(ts)
        else:
            other_items.append(rec)
            other_ts.extend(ts)

    for lst in (ag_items, cx_items, wb_items, other_items):
        lst.sort(key=lambda x: x["active_span"])
    return ag_items, ag_ts, cx_items, cx_ts, wb_items, wb_ts, other_items, other_ts, codex_title_hints


def discover_git_repos(cfg: dict[str, Any]) -> list[str]:
    excludes = [os.path.realpath(os.path.expanduser(p)) for p in cfg.get("exclude_paths", [])]
    exclude_names = set(cfg.get("exclude_dir_names", []))
    repos: list[str] = []

    for root_raw in cfg.get("git_search_roots", []):
        root = os.path.realpath(os.path.expanduser(root_raw))
        if not os.path.exists(root):
            continue
        root_depth = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, _ in os.walk(root):
            real_dir = os.path.realpath(dirpath)
            if any(real_dir == ex or real_dir.startswith(ex + os.sep) for ex in excludes):
                dirnames[:] = []
                continue
            if ".git" in dirnames:
                repos.append(real_dir)
                dirnames[:] = []
                continue
            depth = real_dir.rstrip(os.sep).count(os.sep) - root_depth
            if depth >= 3:
                dirnames[:] = []
                continue
            dirnames[:] = [
                d for d in dirnames
                if d not in exclude_names and not d.startswith(".")
            ]
    return sorted(set(repos))


def collect_git(
    start_dt: datetime,
    end_dt: datetime,
    tz: ZoneInfo,
    cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[datetime]]:
    repos = discover_git_repos(cfg)
    since_arg = start_dt.isoformat()
    until_arg = (end_dt - timedelta(seconds=1)).isoformat()
    results: list[dict[str, Any]] = []
    all_ts: list[datetime] = []

    for repo in repos:
        try:
            proc = subprocess.run(
                [
                    "git", "-C", repo, "log", "--all",
                    f"--since={since_arg}", f"--until={until_arg}",
                    "--format=%H%x1f%aI%x1f%s", "--name-only"
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception:
            continue
        if proc.returncode != 0 or not proc.stdout.strip():
            continue

        commits: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for line in proc.stdout.splitlines():
            if "\x1f" in line:
                if current:
                    commits.append(current)
                parts = line.split("\x1f", 2)
                dt = parse_iso_dt(parts[1])
                if dt:
                    all_ts.append(dt)
                current = {
                    "hash": parts[0],
                    "time": dt.astimezone(tz).strftime("%H:%M") if dt else parts[1],
                    "subject": parts[2].strip(),
                    "files": [],
                }
            elif line.strip() and current is not None:
                if len(current["files"]) < 15:
                    current["files"].append(line.strip())
        if current:
            commits.append(current)

        if commits:
            results.append(
                {
                    "repo": repo,
                    "repo_name": os.path.basename(repo),
                    "commit_count": len(commits),
                    "commits": commits[:20],
                }
            )
    return results, all_ts


def collect_local_files(
    start_dt: datetime,
    end_dt: datetime,
    tz: ZoneInfo,
    cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    excludes = [os.path.realpath(os.path.expanduser(p)) for p in cfg.get("exclude_paths", [])]
    exclude_names = set(cfg.get("exclude_dir_names", []))
    exclude_prefixes = tuple(cfg.get("exclude_dir_prefixes", []))
    start_ts = start_dt.timestamp()
    end_ts = end_dt.timestamp()

    jianying_root = os.path.realpath(
        os.path.expanduser("~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft")
    )

    grouped: dict[str, list[tuple[float, str]]] = defaultdict(list)

    for raw_dir in cfg.get("scan_dirs", []):
        base = os.path.realpath(os.path.expanduser(raw_dir))
        if not os.path.exists(base):
            continue
        if any(base == ex or base.startswith(ex + os.sep) for ex in excludes):
            continue

        is_jianying = (base == jianying_root)

        for dirpath, dirnames, filenames in os.walk(base):
            real_dir = os.path.realpath(dirpath)
            if any(real_dir == ex or real_dir.startswith(ex + os.sep) for ex in excludes):
                dirnames[:] = []
                continue

            dirnames[:] = [
                d for d in dirnames
                if d not in exclude_names
                and not d.startswith(".")
                and (not exclude_prefixes or not d.startswith(exclude_prefixes))
            ]

            for fname in filenames:
                if fname.startswith(".") or fname.endswith((".tmp", ".log", ".pyc", ".lock", ".DS_Store")):
                    continue
                fpath = os.path.join(real_dir, fname)
                try:
                    st = os.stat(fpath)
                except OSError:
                    continue
                mtime = st.st_mtime
                if not (start_ts <= mtime < end_ts):
                    continue

                rel = os.path.relpath(fpath, base)
                top = rel.split(os.sep)[0] if os.sep in rel else "(根目录)"
                if is_jianying:
                    if top == "(根目录)" or fname == "root_meta_info.json":
                        continue
                    group_key = f"剪映工程/{top}"
                else:
                    base_label = os.path.basename(base)
                    group_key = f"{base_label}/{top}" if top != "(根目录)" else base_label

                grouped[group_key].append((mtime, fpath))

    summaries: list[dict[str, Any]] = []
    for group_key, items in sorted(grouped.items()):
        items.sort(key=lambda x: x[0], reverse=True)
        unique_names: list[str] = []
        seen = set()
        paths = list(dict.fromkeys(path for _, path in items))[:12]
        for _, path in items:
            name = os.path.basename(path)
            if name not in seen:
                seen.add(name)
                unique_names.append(name)
            if len(unique_names) >= 12:
                break
        latest_dt = datetime.fromtimestamp(items[0][0], tz=tz).strftime("%H:%M")
        summaries.append(
            {
                "group": group_key,
                "file_count": len(items),
                "latest_time": latest_dt,
                "sample_files": unique_names,
                "paths": paths,
            }
        )
    return summaries


def load_existing_projects(cfg: dict[str, Any]) -> list[dict[str, str]]:
    sources = [
        ("work", os.path.expanduser(cfg.get("excel_path", ""))),
        ("personal", os.path.expanduser(cfg.get("personal_excel_path", ""))),
    ]
    projects: list[dict[str, str]] = []
    try:
        from openpyxl import load_workbook
    except Exception:
        return []

    for scope, xlsx_path in sources:
        if not xlsx_path or not os.path.exists(xlsx_path):
            continue
        try:
            wb = load_workbook(xlsx_path, read_only=True, data_only=True)
            if "项目总览" not in wb.sheetnames:
                wb.close()
                continue
            ws = wb["项目总览"]
            rows = ws.iter_rows(values_only=True)
            header = None
            for row in rows:
                if row and row[0] == "项目名称":
                    header = row
                    break
            if not header:
                wb.close()
                continue
            sess_col = header.index("关联会话名称清单") if "关联会话名称清单" in header else (len(header) - 2)
            for r in rows:
                if not r or not r[0]:
                    continue
                projects.append(
                    {
                        "scope": scope,
                        "project": str(r[0]),
                        "category": str(r[1] or ""),
                        "last_date": str(r[3] or ""),
                        "status": str(r[5] or ""),
                        "next_step": str(r[6] or ""),
                        "sessions": str(r[sess_col] or "")[:160] if len(r) > sess_col else "",
                    }
                )
            wb.close()
        except Exception:
            continue
    return projects[:60]


def collect_day(workday: str, cfg: dict[str, Any], machine_id: str | None = None) -> dict[str, Any]:
    tz_name = cfg["timezone"]
    tz = ZoneInfo(tz_name)
    start_dt, end_dt = workday_bounds(workday, tz_name, int(cfg["day_start_hour"]))
    mid = multi_machine.normalize_machine_id(machine_id or cfg.get("machine_id"))

    ag_items, ag_ts = collect_antigravity(start_dt, end_dt, tz, workday, cfg)
    codex_items, codex_ts = collect_codex(start_dt, end_dt, tz, workday, cfg)
    wb_items, wb_ts = collect_workbuddy(start_dt, end_dt, tz, workday, cfg)

    existing_codex_ids = {str(it.get("id") or "") for it in codex_items if it.get("id")}
    t3_ag, t3_ag_ts, t3_cx, t3_cx_ts, t3_wb, t3_wb_ts, t3_other, t3_other_ts, cx_hints = collect_t3_code(
        start_dt, end_dt, tz, workday, cfg, existing_codex_ids=existing_codex_ids
    )
    if cx_hints:
        for it in codex_items:
            sid = str(it.get("id") or "")
            if sid in cx_hints:
                it["title"] = cx_hints[sid]
                it["client"] = "T3 Code"

    ag_items = sorted(ag_items + t3_ag, key=lambda x: x["active_span"])
    codex_items = sorted(codex_items + t3_cx, key=lambda x: x["active_span"])
    wb_items = sorted(wb_items + t3_wb, key=lambda x: x["active_span"])
    ag_ts = ag_ts + t3_ag_ts
    codex_ts = codex_ts + t3_cx_ts
    wb_ts = wb_ts + t3_wb_ts

    git_items, git_ts = collect_git(start_dt, end_dt, tz, cfg)
    file_groups = collect_local_files(start_dt, end_dt, tz, cfg)

    all_ts = ag_ts + codex_ts + wb_ts + t3_other_ts + git_ts
    gap_min = int(cfg["merge_gap_minutes"])
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][
        datetime.strptime(workday, "%Y-%m-%d").weekday()
    ]

    payload = {
        "workday": workday,
        "weekday": weekday_cn,
        "machine": mid,
        "window_local": f"{start_dt.strftime('%Y-%m-%d %H:%M')} ~ {end_dt.strftime('%Y-%m-%d %H:%M')} ({tz_name})",
        "overall_active_span": format_span(all_ts, tz, workday),
        "overall_estimated_hours": estimate_active_hours(all_ts, gap_min),
        "antigravity_hours": estimate_active_hours(ag_ts, gap_min),
        "codex_hours": estimate_active_hours(codex_ts, gap_min),
        "workbuddy_hours": estimate_active_hours(wb_ts, gap_min),
        "t3_other_hours": estimate_active_hours(t3_other_ts, gap_min),
        "existing_projects": load_existing_projects(cfg),
        "counts": {
            "antigravity_sessions": len(ag_items),
            "codex_sessions": len(codex_items),
            "workbuddy_sessions": len(wb_items),
            "t3_other_sessions": len(t3_other),
            "git_repos": len(git_items),
            "file_groups": len(file_groups),
        },
        "antigravity": multi_machine.tag_items_with_machine(ag_items, mid),
        "codex": multi_machine.tag_items_with_machine(codex_items, mid),
        "workbuddy": multi_machine.tag_items_with_machine(wb_items, mid),
        "t3_other": multi_machine.tag_items_with_machine(t3_other, mid),
        "git": multi_machine.tag_items_with_machine(git_items, mid),
        "files": multi_machine.tag_items_with_machine(file_groups, mid),
    }

    raw_month_dir = os.path.join(DATA_DIR, "raw", workday[:7])
    os.makedirs(raw_month_dir, exist_ok=True)
    out_path = os.path.join(raw_month_dir, f"raw_{workday}.json")
    multi_machine.atomic_write_json(out_path, payload)
    return payload


def collect_all_history_single_pass(cfg: dict[str, Any], end_workday: str) -> list[str]:
    """Scan all Antigravity, Codex, and Git history in a single pass and write monthly raw JSONs."""
    tz_name = cfg["timezone"]
    tz = ZoneInfo(tz_name)
    day_start_hour = int(cfg["day_start_hour"])
    gap_min = int(cfg["merge_gap_minutes"])
    markers = cfg.get("self_filter_markers", [])

    def dt_to_workday(dt: datetime) -> str:
        return (dt.astimezone(tz) - timedelta(hours=day_start_hour)).strftime("%Y-%m-%d")

    ag_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ag_ts_by_day: dict[str, list[datetime]] = defaultdict(list)

    ann_titles, side_parent = load_antigravity_annotations()
    proj_names = load_antigravity_projects()
    db_path = os.path.expanduser("~/.gemini/antigravity/conversation_summaries.db")
    meta_map: dict[str, dict[str, Any]] = {}
    if os.path.exists(db_path):
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        for cid, title, ws, parent_id, depth, project_id in conn.execute(
            "SELECT conversation_id, title, workspace_uris, parent_conversation_id, nesting_depth, project_id FROM conversation_summaries"
        ):
            meta_map[cid] = {
                "title": title or "",
                "workspace_uris": ws or "",
                "parent_id": parent_id or "",
                "depth": depth or 0,
                "project_id": project_id or "",
            }
        conn.close()

    brain_dir = os.path.expanduser("~/.gemini/antigravity/brain")
    raw_ag: dict[str, tuple[dict[str, Any], str, dict[str, list[datetime]], dict[str, list[str]], dict[str, list[str]]]] = {}
    side_ts: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    side_prompts: dict[tuple[str, str], list[str]] = defaultdict(list)
    side_replies: dict[tuple[str, str], list[str]] = defaultdict(list)

    for tpath in glob.glob(os.path.join(brain_dir, "*", ".system_generated", "logs", "transcript.jsonl")):
        cid = tpath.split(os.sep)[-4]
        meta = meta_map.get(cid, {})
        if meta.get("parent_id") or meta.get("depth", 0) > 0:
            continue
        title = ann_titles.get(cid) or meta.get("title", "")
        if any(m in title for m in markers):
            continue

        steps_day: dict[str, list[datetime]] = defaultdict(list)
        prompts_day: dict[str, list[str]] = defaultdict(list)
        replies_day: dict[str, list[str]] = defaultdict(list)
        is_self = False

        try:
            with open(tpath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    dt = parse_iso_dt(str(obj.get("created_at") or ""))
                    if not dt:
                        continue
                    wd = dt_to_workday(dt)
                    if wd > end_workday:
                        continue
                    raw_content = str(obj.get("content") or "")
                    stype = obj.get("type")
                    source = obj.get("source")
                    if stype == "USER_INPUT" or source == "USER_EXPLICIT":
                        if any(m in raw_content for m in markers):
                            is_self = True
                            break
                        cleaned = clean_antigravity_user_text(raw_content)
                        if cleaned:
                            prompts_day[wd].append(cleaned[:300])
                        steps_day[wd].append(dt)
                    elif stype == "PLANNER_RESPONSE":
                        steps_day[wd].append(dt)
                        if raw_content.strip():
                            replies_day[wd].append(re.sub(r"\s+", " ", raw_content).strip()[:300])
        except OSError:
            continue

        if is_self:
            continue
        if cid in side_parent:
            p = side_parent[cid]
            for wd, ts in steps_day.items():
                side_ts[(p, wd)].extend(ts)
                side_prompts[(p, wd)].extend(prompts_day[wd])
                side_replies[(p, wd)].extend(replies_day[wd])
            continue
        raw_ag[cid] = (meta, title, steps_day, prompts_day, replies_day)

    all_ag_pairs = {
        (cid, wd) for cid, (_, _, sday, _, _) in raw_ag.items() for wd in sday
    } | set(side_ts.keys())

    for cid, wd in sorted(all_ag_pairs):
        if cid in raw_ag:
            meta, title, steps_day, prompts_day, replies_day = raw_ag[cid]
            ts = steps_day.get(wd, [])
            main_prompts = prompts_day.get(wd, [])
            main_replies = replies_day.get(wd, [])
        else:
            meta = meta_map.get(cid, {})
            title = ann_titles.get(cid) or meta.get("title", "")
            ts = []
            main_prompts = []
            main_replies = []

        merged_ts = list(ts) + side_ts.get((cid, wd), [])
        prompts = compress_turn_history(main_prompts)
        for sp in compress_turn_history(side_prompts.get((cid, wd), []), max_items=10):
            prompts.append(f"[侧边追问] {sp}")
        if not prompts or not merged_ts:
            continue
        all_replies = main_replies or side_replies.get((cid, wd), [])
        folder = proj_names.get(meta.get("project_id", ""), "")
        final_title = title or (main_prompts or side_prompts.get((cid, wd), [""]))[0][:60]
        display_title = f"{final_title} [目录: {folder}]" if folder else final_title
        ag_ts_by_day[wd].extend(merged_ts)
        ag_by_day[wd].append(
            {
                "source": "Antigravity",
                "id": cid,
                "title": final_title,
                "folder": folder,
                "display_title": display_title,
                "workspace": meta.get("workspace_uris", ""),
                "turn_count": len(main_prompts) + len(side_prompts.get((cid, wd), [])),
                "active_span": format_span(merged_ts, tz, wd),
                "estimated_hours": estimate_active_hours(merged_ts, gap_min),
                "user_prompts": prompts,
                "assistant_milestones": sample_assistant_milestones(all_replies),
                "last_assistant_reply": all_replies[-1] if all_replies else "",
            }
        )

    cx_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cx_ts_by_day: dict[str, list[datetime]] = defaultdict(list)
    thread_names = load_codex_thread_names()
    seen_stems: set[str] = set()

    for path in list_codex_rollout_paths():
        base = os.path.basename(path)
        stem = base[:-10] if base.endswith(".jsonl.zst") else base[:-6]
        if stem in seen_stems:
            continue
        seen_stems.add(stem)
        suuid = extract_codex_uuid(base)
        tname = thread_names.get(suuid, "")
        if tname == "Guardian review":
            continue
        is_archived = "/archived_sessions/" in path
        cwd = ""
        steps_day = defaultdict(list)
        prompts_day = defaultdict(list)
        replies_day = defaultdict(list)
        is_self = False
        try:
            opener = (
                (lambda p: open_zstd_text(p))
                if path.endswith(".zst")
                else (lambda p: open(p, "r", encoding="utf-8"))
            )
            with opener(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rtype = obj.get("type")
                    payload = obj.get("payload") or {}
                    if rtype == "session_meta" and isinstance(payload, dict) and not suuid:
                        suuid = str(payload.get("session_id") or "")
                        tname = thread_names.get(suuid, tname)
                    if rtype == "turn_context" and isinstance(payload, dict) and payload.get("cwd"):
                        cwd = payload["cwd"]
                    dt = parse_iso_dt(str(obj.get("timestamp") or ""))
                    if not dt:
                        continue
                    wd = dt_to_workday(dt)
                    if wd > end_workday:
                        continue
                    if rtype == "response_item" and isinstance(payload, dict) and payload.get("type") == "message":
                        role = payload.get("role")
                        text = extract_text_from_codex_content(payload.get("content"))
                        if role == "user":
                            if any(m in text for m in markers):
                                is_self = True
                                break
                            cleaned = clean_codex_user_text(text)
                            if cleaned:
                                prompts_day[wd].append(cleaned[:300])
                                steps_day[wd].append(dt)
                        elif role == "assistant":
                            cleaned = re.sub(r"<oai-mem-citation>.*?</oai-mem-citation>", "", text, flags=re.DOTALL)
                            cleaned = re.sub(r"\s+", " ", cleaned).strip()
                            if cleaned:
                                replies_day[wd].append(cleaned[:300])
                                steps_day[wd].append(dt)
        except OSError:
            continue

        if is_self:
            continue
        for wd, ts in steps_day.items():
            prompts = prompts_day[wd]
            if not prompts:
                continue
            cx_ts_by_day[wd].extend(ts)
            cx_by_day[wd].append(
                {
                    "source": "Codex",
                    "id": suuid or base,
                    "archived": is_archived,
                    "cwd": cwd,
                    "title": tname or prompts[0][:60],
                    "turn_count": len(prompts),
                    "active_span": format_span(ts, tz, wd),
                    "estimated_hours": estimate_active_hours(ts, gap_min),
                    "user_prompts": compress_turn_history(prompts),
                    "assistant_milestones": sample_assistant_milestones(replies_day[wd]),
                    "last_assistant_reply": replies_day[wd][-1] if replies_day[wd] else "",
                }
            )

    wb_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    wb_ts_by_day: dict[str, list[datetime]] = defaultdict(list)
    wb_meta, wb_jsonl_map = load_workbuddy_metadata()
    for sid, info in wb_meta.items():
        jpath = wb_jsonl_map.get(sid)
        if not jpath or not os.path.exists(jpath):
            continue
        if any(m in info.get("title", "") for m in markers):
            continue
        steps_day, prompts_day, replies_day, is_self = parse_workbuddy_jsonl_by_day(
            jpath, markers, tz, day_start_hour, end_workday=end_workday
        )
        if is_self:
            continue
        for wd, ts in steps_day.items():
            prompts = prompts_day[wd]
            if not prompts:
                continue
            wb_ts_by_day[wd].extend(ts)
            wb_by_day[wd].append(
                build_workbuddy_record(sid, info, ts, prompts, replies_day[wd], tz, wd, gap_min)
            )

    t3_other_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    t3_other_ts_by_day: dict[str, list[datetime]] = defaultdict(list)
    t3_projects, t3_threads, t3_prov_sids, t3_messages = load_t3_code_data()
    if t3_threads:
        t3_steps: dict[tuple[str, str], list[datetime]] = defaultdict(list)
        t3_prompts: dict[tuple[str, str], list[str]] = defaultdict(list)
        t3_replies: dict[tuple[str, str], list[str]] = defaultdict(list)
        t3_self: set[str] = set()
        for tid, role, created_at, payload_raw in t3_messages:
            tid_s = str(tid or "")
            if tid_s not in t3_threads or tid_s in t3_self:
                continue
            dt = parse_iso_dt(str(created_at or ""))
            if not dt:
                continue
            wd = dt_to_workday(dt)
            if wd > end_workday:
                continue
            try:
                p_obj = json.loads(payload_raw) if payload_raw else {}
            except json.JSONDecodeError:
                continue
            raw_text = str(p_obj.get("text") or "")
            if role == "user":
                if any(m in raw_text for m in markers):
                    t3_self.add(tid_s)
                    continue
                created_by = str(p_obj.get("createdBy") or "")
                creation_src = str(p_obj.get("creationSource") or "")
                if created_by and created_by != "user":
                    continue
                if not created_by and creation_src in ("mcp", "provider"):
                    continue
                cleaned = clean_t3_user_text(raw_text)
                if cleaned:
                    t3_prompts[(tid_s, wd)].append(cleaned[:300])
                    t3_steps[(tid_s, wd)].append(dt)
            elif role == "assistant":
                cleaned = re.sub(r"\s+", " ", raw_text).strip()
                if cleaned:
                    t3_replies[(tid_s, wd)].append(cleaned[:300])
                    t3_steps[(tid_s, wd)].append(dt)

        cx_ids_by_day: dict[str, set[str]] = {
            wd: {str(it.get("id") or "") for it in items if it.get("id")}
            for wd, items in cx_by_day.items()
        }
        for (tid_s, wd), ts in sorted(t3_steps.items()):
            if tid_s in t3_self:
                continue
            tinfo = t3_threads[tid_s]
            if any(m in tinfo.get("title", "") for m in markers):
                continue
            prompts = t3_prompts.get((tid_s, wd), [])
            if not prompts or not ts:
                continue
            proj_info = t3_projects.get(tinfo.get("project_id", ""), {})
            bucket, tool_label = map_t3_provider_bucket(
                tinfo.get("default_provider", ""), tinfo.get("provider_instance_id", "")
            )
            if bucket == "codex":
                matched_sids = t3_prov_sids.get(tid_s, set()) & cx_ids_by_day.get(wd, set())
                if matched_sids:
                    proj_title = proj_info.get("title", "")
                    hint_title = tinfo.get("title", "")
                    if hint_title:
                        full_hint = f"{hint_title} [T3·{proj_title}]" if proj_title else f"{hint_title} [T3]"
                        for it in cx_by_day[wd]:
                            if str(it.get("id") or "") in matched_sids:
                                it["title"] = full_hint
                                it["client"] = "T3 Code"
                    continue
            rec = build_t3_record(
                tid_s, tinfo, proj_info, bucket, tool_label, ts, prompts, t3_replies.get((tid_s, wd), []), tz, wd, gap_min
            )
            if bucket == "antigravity":
                ag_by_day[wd].append(rec)
                ag_ts_by_day[wd].extend(ts)
            elif bucket == "codex":
                cx_by_day[wd].append(rec)
                cx_ts_by_day[wd].extend(ts)
            elif bucket == "workbuddy":
                wb_by_day[wd].append(rec)
                wb_ts_by_day[wd].extend(ts)
            else:
                t3_other_by_day[wd].append(rec)
                t3_other_ts_by_day[wd].extend(ts)

    existing_projects = load_existing_projects(cfg)
    active_dates = sorted(
        set(ag_by_day.keys()) | set(cx_by_day.keys()) | set(wb_by_day.keys()) | set(t3_other_by_day.keys())
    )
    for wd in active_dates:
        ag_items = sorted(ag_by_day[wd], key=lambda x: x["active_span"])
        cx_items = sorted(cx_by_day[wd], key=lambda x: x["active_span"])
        wb_items = sorted(wb_by_day[wd], key=lambda x: x["active_span"])
        t3o_items = sorted(t3_other_by_day[wd], key=lambda x: x["active_span"])
        all_ts = ag_ts_by_day[wd] + cx_ts_by_day[wd] + wb_ts_by_day[wd] + t3_other_ts_by_day[wd]
        start_dt, end_dt = workday_bounds(wd, tz_name, day_start_hour)
        weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][
            datetime.strptime(wd, "%Y-%m-%d").weekday()
        ]
        payload = {
            "workday": wd,
            "weekday": weekday_cn,
            "window_local": f"{start_dt.strftime('%Y-%m-%d %H:%M')} ~ {end_dt.strftime('%Y-%m-%d %H:%M')} ({tz_name})",
            "overall_active_span": format_span(all_ts, tz, wd),
            "overall_estimated_hours": estimate_active_hours(all_ts, gap_min),
            "antigravity_hours": estimate_active_hours(ag_ts_by_day[wd], gap_min),
            "codex_hours": estimate_active_hours(cx_ts_by_day[wd], gap_min),
            "workbuddy_hours": estimate_active_hours(wb_ts_by_day[wd], gap_min),
            "t3_other_hours": estimate_active_hours(t3_other_ts_by_day[wd], gap_min),
            "existing_projects": existing_projects,
            "counts": {
                "antigravity_sessions": len(ag_items),
                "codex_sessions": len(cx_items),
                "workbuddy_sessions": len(wb_items),
                "t3_other_sessions": len(t3o_items),
                "git_repos": 0,
                "file_groups": 0,
            },
            "antigravity": ag_items,
            "codex": cx_items,
            "workbuddy": wb_items,
            "t3_other": t3o_items,
            "git": [],
            "files": [],
        }
        raw_month_dir = os.path.join(DATA_DIR, "raw", wd[:7])
        os.makedirs(raw_month_dir, exist_ok=True)
        with open(os.path.join(raw_month_dir, f"raw_{wd}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    return active_dates


def load_recorded_overview_dates(cfg: dict[str, Any]) -> set[str]:
    xlsx_path = os.path.expanduser(cfg.get("excel_path", ""))
    if not xlsx_path or not os.path.exists(xlsx_path):
        return set()
    try:
        from openpyxl import load_workbook
        wb = load_workbook(xlsx_path, read_only=True, data_only=True)
        if "每日概览" not in wb.sheetnames:
            wb.close()
            return set()
        ws = wb["每日概览"]
        rows = ws.iter_rows(values_only=True)
        for row in rows:
            if row and row[0] == "日期":
                break
        recorded = {str(r[0]).strip() for r in rows if r and r[0]}
        wb.close()
        return recorded
    except Exception:
        return set()


def find_missing_workdays(cfg: dict[str, Any], end_workday: str, lookback_days: int) -> list[str]:
    recorded = load_recorded_overview_dates(cfg)
    first_recorded = min(recorded) if recorded else None
    end_d = datetime.strptime(end_workday, "%Y-%m-%d").date()
    missing: list[str] = []
    for offset in range(lookback_days - 1, -1, -1):
        d_str = (end_d - timedelta(days=offset)).strftime("%Y-%m-%d")
        if first_recorded and d_str < first_recorded:
            continue
        if d_str not in recorded:
            missing.append(d_str)
    return missing


def main() -> None:
    cfg = load_config()
    parser = argparse.ArgumentParser(description="Collect daily activity traces.")
    parser.add_argument("--date", help="Target workday YYYY-MM-DD (default: current workday).")
    parser.add_argument("--yesterday", action="store_true", help="Collect the previous completed workday.")
    parser.add_argument("--missing", action="store_true", help="Also collect any missed workdays in the lookback window.")
    parser.add_argument("--lookback-days", type=int, help="Lookback days for --missing (default from config).")
    parser.add_argument("--days", type=int, help="Collect N workdays ending at --date (or current workday).")
    parser.add_argument("--all-history", action="store_true", help="Single-pass extract all historical AG/Codex days through end workday.")
    parser.add_argument("--machine", help="Machine identifier for multi-device sharding (default: env/config/hostname).")
    parser.add_argument("--push", action="store_true", help="Push collected raw payload(s) to the configured remote NAS hub.")
    parser.add_argument("--remote-config", help="Path to remote.json override when using --push.")
    args = parser.parse_args()

    rc = remote_client.load_remote_config(args.remote_config, project_cfg=cfg) if args.push else None
    machine_id = multi_machine.normalize_machine_id(args.machine or (rc.machine_id if rc else cfg.get("machine_id")))

    cur_wd = current_workday(cfg["timezone"], int(cfg["day_start_hour"]))
    cur_d = datetime.strptime(cur_wd, "%Y-%m-%d").date()
    yesterday_wd = (cur_d - timedelta(days=1)).strftime("%Y-%m-%d")

    if args.date:
        end_workday = args.date
    elif args.yesterday or args.all_history:
        end_workday = yesterday_wd
    else:
        end_workday = cur_wd

    if args.all_history:
        dates = collect_all_history_single_pass(cfg, end_workday)
        print(f"ALL_HISTORY_DATES:{len(dates)} ({dates[0] if dates else '-'} ~ {dates[-1] if dates else '-'})")
        return

    if args.missing:
        lookback = args.lookback_days or int(cfg.get("catchup_days", 7))
        target_end = args.date or yesterday_wd
        dates = find_missing_workdays(cfg, target_end, lookback)
        if args.yesterday and yesterday_wd not in dates:
            dates.append(yesterday_wd)
        dates = sorted(set(dates))
    else:
        days = args.days or 1
        end_d = datetime.strptime(end_workday, "%Y-%m-%d").date()
        dates = [(end_d - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(days - 1, -1, -1)]

    if not dates:
        print("NO_MISSING")
        return

    print("TARGET_DATES:" + ",".join(dates))
    for d in dates:
        p = collect_day(d, cfg, machine_id=machine_id)
        c = p["counts"]
        t3o = c.get("t3_other_sessions", 0)
        t3o_part = f", T3Other={t3o}" if t3o else ""
        push_part = ""
        if args.push and rc is not None:
            res, ep = remote_client.push_raw(rc, p, machine_id=machine_id)
            push_part = f" | PUSHED->{ep.name} (machines={','.join(res.get('machines', []))})"
        print(
            f"{d} ({p['weekday']}) [{machine_id}]: AG={c['antigravity_sessions']}, Codex={c['codex_sessions']}, "
            f"WB={c.get('workbuddy_sessions', 0)}{t3o_part}, Git={c['git_repos']}, Files={c['file_groups']}, "
            f"Span={p['overall_active_span'] or '-'}, EstHours={p['overall_estimated_hours']}{push_part}"
        )


if __name__ == "__main__":
    main()
