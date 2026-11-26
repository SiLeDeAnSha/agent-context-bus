#!/usr/bin/env python3
"""Zero-LLM real-time WAL layer for Agent Context Bus (跨端 AI 记忆中枢).

Captures same-day activity across Antigravity, Codex, Claude Code, WorkBuddy, and local
Git repositories with zero extra LLM tokens, plus manual/agent `--note` entries.
Once a workday is compacted into `summary_all_history.json`, its live block
automatically exits `MEMORY_INDEX.md`.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from typing import Any
from zoneinfo import ZoneInfo

from multi_machine import atomic_write_json, file_lock
from project_identity import ProjectCatalog, edit_registry, load_registry, name_key
import redact

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LIVE_DIRNAME = "live"
LIVE_LOCK_FILE = ".live.lock"
SCAN_STAMP_FILE = ".last_scan_ts"
REMOTE_PUSH_STAMP_FILE = ".last_remote_push_ts"
DEFAULT_DEBOUNCE_SECONDS = 12
DEFAULT_REMOTE_PUSH_DEBOUNCE = 30
TAIL_READ_MAX_BYTES = 65536
NOISE_CODEX_TITLES = {"guardian review"}


def _load_cfg(config_path: str | None = None) -> dict[str, Any]:
    candidates = [
        config_path,
        os.environ.get("DAILY_MEMORY_CONFIG_PATH"),
        os.path.join(BASE_DIR, "config.json"),
        os.path.join(BASE_DIR, "config.example.json"),
    ]
    for p in candidates:
        if p and os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                return {}
    return {}


def resolve_workday(cfg: dict[str, Any] | None = None, now: datetime | None = None) -> str:
    cfg = cfg if cfg is not None else _load_cfg()
    tz_name = str(cfg.get("timezone") or "Asia/Shanghai")
    day_start_hour = int(cfg.get("day_start_hour", 5))
    tz = ZoneInfo(tz_name)
    dt = now.astimezone(tz) if now is not None else datetime.now(tz)
    return (dt - timedelta(hours=day_start_hour)).strftime("%Y-%m-%d")


def workday_bounds(workday: str, tz_name: str = "Asia/Shanghai", day_start_hour: int = 5) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tz_name)
    start = datetime.strptime(workday, "%Y-%m-%d").replace(hour=day_start_hour, minute=0, second=0, tzinfo=tz)
    return start, start + timedelta(days=1)


def _shorten_home(path: str) -> str:
    if not path:
        return ""
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def _clean_text(text: Any, limit: int = 180) -> str:
    s = " ".join(str(text or "").split())
    if not s:
        return ""
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip(" ，、；;") + "…"


def _parse_iso(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def live_file_path(data_dir: str, workday: str) -> str:
    return os.path.join(data_dir, LIVE_DIRNAME, f"live_{workday}.json")


def empty_live_day(workday: str) -> dict[str, Any]:
    return {
        "workday": workday,
        "updated_at": "",
        "notes": [],
        "sessions": [],
        "git": [],
    }


def load_live_day(data_dir: str, workday: str) -> dict[str, Any]:
    path = live_file_path(data_dir, workday)
    if not os.path.exists(path):
        return empty_live_day(workday)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("workday", workday)
            data.setdefault("updated_at", "")
            data.setdefault("notes", [])
            data.setdefault("sessions", [])
            data.setdefault("git", [])
            return data
    except Exception:
        pass
    return empty_live_day(workday)


def load_uncompacted_live(
    data_dir: str,
    archived_workdays: set[str] | None = None,
    max_days: int = 2,
) -> list[dict[str, Any]]:
    live_dir = os.path.join(data_dir, LIVE_DIRNAME)
    if not os.path.isdir(live_dir):
        return []
    archived = archived_workdays or set()
    results: list[dict[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(live_dir, "live_*.json")), reverse=True):
        base = os.path.basename(path)
        m = re.match(r"^live_(\d{4}-\d{2}-\d{2})\.json$", base)
        if not m:
            continue
        wd = m.group(1)
        if wd in archived:
            continue
        day = load_live_day(data_dir, wd)
        if day.get("notes") or day.get("sessions") or day.get("git"):
            results.append(day)
        if len(results) >= max_days:
            break
    return results


def _merge_session(sessions: list[dict[str, Any]], incoming: dict[str, Any]) -> list[dict[str, Any]]:
    key = incoming.get("key")
    if not key:
        return sessions
    by_key = {str(s.get("key")): dict(s) for s in sessions if s.get("key")}
    prev = by_key.get(str(key), {})
    merged = dict(prev)
    for k, v in incoming.items():
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        merged[k] = v
    by_key[str(key)] = merged
    items = list(by_key.values())
    items.sort(key=lambda s: str(s.get("updated_at") or ""), reverse=True)
    return items[:24]


def _merge_git(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for item in [*existing, *incoming]:
        repo = str(item.get("repo") or item.get("repo_name") or "")
        h = str(item.get("hash") or "")
        if repo and h:
            by_key[(repo, h)] = item
    items = list(by_key.values())
    items.sort(key=lambda g: (str(g.get("committed_at") or ""), str(g.get("time") or "")), reverse=True)
    return items[:20]


def add_note(
    data_dir: str,
    days: list[dict[str, Any]],
    project: str,
    text: str,
    *,
    workday: str | None = None,
    source: str = "manual",
    paths: list[str] | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    project_clean = str(project or "").strip()
    text_clean = " ".join(str(text or "").split())
    if not project_clean or not text_clean:
        raise ValueError("项目名称与笔记内容不能为空。")
    cfg = cfg if cfg is not None else _load_cfg()
    text_clean = redact.redact_with_cfg(text_clean, cfg)
    tz_name = str(cfg.get("timezone") or "Asia/Shanghai")
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    wd = workday or resolve_workday(cfg, now=now)

    catalog = ProjectCatalog(days, load_registry(data_dir))
    identity = catalog.lookup(project_clean) or catalog.for_item({"project": project_clean})
    canonical_project = identity["project"]
    clean_paths = [p.strip() for p in (paths or []) if isinstance(p, str) and p.strip()]
    if clean_paths:
        identity = edit_registry(data_dir, days, "add_paths", canonical_project, clean_paths)

    live_dir = os.path.join(data_dir, LIVE_DIRNAME)
    os.makedirs(live_dir, exist_ok=True)
    note = {
        "time": now.strftime("%H:%M"),
        "created_at": now.isoformat(timespec="seconds"),
        "project": identity["project"],
        "project_id": identity["project_id"],
        "text": text_clean,
        "source": source,
        "paths": clean_paths,
    }
    with file_lock(os.path.join(live_dir, LIVE_LOCK_FILE)):
        day = load_live_day(data_dir, wd)
        notes = list(day.get("notes") or [])
        dup = any(
            name_key(str(n.get("project") or "")) == name_key(note["project"])
            and str(n.get("text") or "").strip() == text_clean
            for n in notes
        )
        if not dup:
            notes.insert(0, note)
        day["notes"] = notes[:40]
        day["updated_at"] = now.isoformat(timespec="seconds")
        atomic_write_json(live_file_path(data_dir, wd), day)
    return {"workday": wd, "note": note, "live_day": day}


def _open_sqlite_readonly(path: str) -> sqlite3.Connection | None:
    if not path or not os.path.exists(path):
        return None
    try:
        uri = f"file:{urllib.parse.quote(path)}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _read_antigravity_db_entry(conv_id: str, db_path: str | None = None) -> dict[str, Any] | None:
    path = db_path or os.path.expanduser("~/.gemini/antigravity/conversation_summaries.db")
    conn = _open_sqlite_readonly(path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            """
            SELECT conversation_id, title, preview, workspace_uris, last_modified_time,
                   parent_conversation_id, nesting_depth
            FROM conversation_summaries
            WHERE conversation_id = ?
            """,
            (conv_id,),
        ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None
    finally:
        conn.close()


def _fetch_antigravity_ls_conversation(conv_id: str) -> dict[str, Any] | None:
    addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
    csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
    if not addr or not csrf or not conv_id:
        return None
    url = f"http://{addr}/product.v1.ConversationService/GetConversation"
    req = urllib.request.Request(
        url,
        data=json.dumps({"conversationId": conv_id}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "x-antigravity-csrf-token": csrf,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            return json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception:
        return None


def _parse_workspace_uris(raw: str) -> str:
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list) and parsed:
            raw = str(parsed[0])
    except Exception:
        pass
    if raw.startswith("file://"):
        raw = urllib.parse.unquote(urllib.parse.urlparse(raw).path)
    return _shorten_home(raw.strip())


def _read_tail_lines(path: str, max_bytes: int = TAIL_READ_MAX_BYTES) -> list[str]:
    """Read up to max_bytes from the tail of a UTF-8 JSONL file in O(1) seek time."""
    if not path or not os.path.isfile(path):
        return []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                raw = fh.read()
                nl = raw.find(b"\n")
                if nl != -1:
                    raw = raw[nl + 1 :]
            else:
                raw = fh.read()
        return raw.decode("utf-8", errors="ignore").splitlines()
    except OSError:
        return []


_PATH_ARG_KEYS = ("path", "Path", "file_path", "target_file", "TargetFile", "AbsolutePath", "filePath")


def _extract_paths_from_obj(obj: Any, out: list[str], seen: set[str], limit: int = 5) -> None:
    if len(out) >= limit:
        return
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                obj = json.loads(s)
            except Exception:
                return
        else:
            return
    if isinstance(obj, dict):
        for k in _PATH_ARG_KEYS:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                cleaned_p = v.strip().strip("\"'`,;")
                short = _shorten_home(cleaned_p)
                if short and short not in seen:
                    seen.add(short)
                    out.append(short)
                    if len(out) >= limit:
                        return
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _extract_paths_from_obj(v, out, seen, limit)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                _extract_paths_from_obj(item, out, seen, limit)


def _first_meaningful_snippet(raw: str, limit: int = 160) -> str:
    txt = re.sub(r"<system-reminder[^>]*>.*?</system-reminder>", "", str(raw or ""), flags=re.S)
    txt = re.sub(r"```[\s\S]*?```", " ", txt)
    for line in txt.splitlines():
        s = line.strip().lstrip("#>-* ").strip()
        s = s.replace("**", "").strip()
        if len(s) >= 4:
            return _clean_text(s, limit)
    return _clean_text(txt.replace("**", ""), limit)


def _extract_transcript_tail(client: str, path: str) -> dict[str, Any]:
    """Extract 0-token high-density context from the tail of a session JSONL file."""
    lines = _read_tail_lines(path)
    if not lines:
        return {"last_user_prompt": "", "last_assistant_summary": "", "touched_files": []}
    last_user = ""
    last_asst = ""
    touched: list[str] = []
    seen_paths: set[str] = set()
    norm = client.lower()

    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if not isinstance(ev, dict):
            continue

        if norm == "antigravity":
            kind = ev.get("type")
            if kind == "PLANNER_RESPONSE":
                for call in ev.get("tool_calls") or []:
                    if isinstance(call, dict):
                        _extract_paths_from_obj(call.get("args"), touched, seen_paths)
                if not last_asst:
                    cand = _first_meaningful_snippet(str(ev.get("content") or ""), 160)
                    if cand:
                        last_asst = cand
            elif (kind == "USER_INPUT" or ev.get("source") == "USER_EXPLICIT") and not last_user:
                raw_u = str(ev.get("content") or "")
                m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", raw_u, flags=re.S)
                if m:
                    raw_u = m.group(1)
                cand = _first_meaningful_snippet(raw_u, 140)
                if cand:
                    last_user = cand
        elif norm == "codex":
            if ev.get("type") == "response_item":
                payload = ev.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                pk = payload.get("type")
                role = payload.get("role")
                if pk in ("function_call", "custom_tool_call"):
                    _extract_paths_from_obj(payload.get("arguments", payload.get("input")), touched, seen_paths)
                elif pk == "message" and role in ("user", "assistant"):
                    content = payload.get("content")
                    txt = ""
                    if isinstance(content, str):
                        txt = content
                    elif isinstance(content, list):
                        txt = " ".join(
                            str(p.get("text") or "")
                            for p in content
                            if isinstance(p, dict) and p.get("type") in ("input_text", "output_text", "text")
                        )
                    cand = _first_meaningful_snippet(txt, 160 if role == "assistant" else 140)
                    if role == "assistant" and not last_asst and cand:
                        last_asst = cand
                    elif role == "user" and not last_user and cand:
                        last_user = cand
        elif norm == "claude":
            if ev.get("isSidechain"):
                continue
            kind = ev.get("type")
            if kind not in ("user", "assistant"):
                continue
            msg = ev.get("message")
            content = msg.get("content") if isinstance(msg, dict) else ev.get("content")
            txt_parts: list[str] = []
            if isinstance(content, str):
                txt_parts.append(content)
            elif isinstance(content, list):
                for b in content:
                    if isinstance(b, dict):
                        if b.get("type") == "text":
                            txt_parts.append(str(b.get("text") or ""))
                        elif b.get("type") == "tool_use":
                            _extract_paths_from_obj(b.get("input"), touched, seen_paths)
                    elif isinstance(b, str):
                        txt_parts.append(b)
            cand = _first_meaningful_snippet(" ".join(txt_parts), 160 if kind == "assistant" else 140)
            if kind == "assistant" and not last_asst and cand:
                last_asst = cand
            elif kind == "user" and not last_user and cand:
                last_user = cand
        else:  # workbuddy
            role = ev.get("role") or ev.get("type")
            if role in ("function_call", "custom_tool_call", "tool_use"):
                _extract_paths_from_obj(ev.get("arguments", ev.get("input")), touched, seen_paths)
            elif role in ("user", "assistant"):
                msg = ev.get("message")
                content = msg.get("content") if isinstance(msg, dict) else ev.get("content")
                txt = content if isinstance(content, str) else ""
                if isinstance(content, list):
                    txt = " ".join(str(b.get("text") or "") for b in content if isinstance(b, dict))
                cand = _first_meaningful_snippet(txt, 160 if role == "assistant" else 140)
                if role == "assistant" and not last_asst and cand:
                    last_asst = cand
                elif role == "user" and not last_user and cand:
                    last_user = cand

        if last_user and last_asst and len(touched) >= 3:
            break

    return {
        "last_user_prompt": last_user,
        "last_assistant_summary": last_asst,
        "touched_files": touched[:5],
    }


def _choose_live_summary(title: str, explicit_summary: str, tail: dict[str, Any]) -> str:
    norm_title = _clean_text(title, 80).lower()
    asst = str(tail.get("last_assistant_summary") or "").strip()
    user = str(tail.get("last_user_prompt") or "").strip()
    touched = [os.path.basename(p.rstrip("/")) for p in (tail.get("touched_files") or []) if p]
    file_suffix = f"（文件: {', '.join(touched[:3])}）" if touched else ""

    primary = ""
    if asst and asst.lower() != norm_title:
        primary = asst
    elif explicit_summary and _clean_text(explicit_summary, 80).lower() != norm_title:
        primary = explicit_summary
    elif user and user.lower() != norm_title:
        primary = user
    else:
        primary = explicit_summary or asst or user

    if not primary and file_suffix:
        return _clean_text(file_suffix.strip("（）"), 160)
    if file_suffix and primary and file_suffix not in primary:
        return _clean_text(f"{primary} {file_suffix}", 180)
    return _clean_text(primary, 180)


def _find_antigravity_transcript(conv_id: str, brain_dir: str | None = None) -> str:
    root = brain_dir or os.path.expanduser("~/.gemini/antigravity/brain")
    logs = os.path.join(root, conv_id, ".system_generated", "logs")
    for name in ("transcript.jsonl", "transcript_full.jsonl"):
        p = os.path.join(logs, name)
        if os.path.isfile(p):
            return p
    return ""


def _find_codex_rollout(session_id: str, codex_home: str | None = None) -> str:
    if not session_id:
        return ""
    root = codex_home or os.path.expanduser("~/.codex")
    for pat in (
        os.path.join(root, "sessions", "*", "*", "*", f"*{session_id}*.jsonl"),
        os.path.join(root, "sessions", f"*{session_id}*.jsonl"),
    ):
        matches = sorted(glob.glob(pat), reverse=True)
        if matches:
            return matches[0]
    return ""


def scan_antigravity_today(
    start: datetime,
    end: datetime,
    tz: ZoneInfo,
    db_path: str | None = None,
    brain_dir: str | None = None,
) -> list[dict[str, Any]]:
    path = db_path or os.path.expanduser("~/.gemini/antigravity/conversation_summaries.db")
    conn = _open_sqlite_readonly(path)
    if conn is None:
        return []
    out: list[dict[str, Any]] = []
    try:
        rows = conn.execute(
            """
            SELECT conversation_id, title, preview, workspace_uris, last_modified_time,
                   parent_conversation_id, nesting_depth
            FROM conversation_summaries
            WHERE COALESCE(parent_conversation_id, '') = ''
              AND COALESCE(nesting_depth, 0) = 0
            ORDER BY last_modified_time DESC
            LIMIT 40
            """
        ).fetchall()
        for row in rows:
            dt = _parse_iso(row["last_modified_time"])
            if not dt:
                continue
            local_dt = dt.astimezone(tz)
            if not (start <= local_dt < end):
                continue
            cid = str(row["conversation_id"] or "")
            if not cid:
                continue
            title = _clean_text(row["title"] or "", 80)
            preview = _clean_text(row["preview"] or "", 140)
            ws = _parse_workspace_uris(str(row["workspace_uris"] or ""))
            tpath = _find_antigravity_transcript(cid, brain_dir=brain_dir)
            tail = _extract_transcript_tail("antigravity", tpath) if tpath else {"last_user_prompt": "", "last_assistant_summary": "", "touched_files": []}
            final_title = title or preview[:50] or cid[:8]
            summary = _choose_live_summary(final_title, preview, tail)
            out.append(
                {
                    "key": f"antigravity:{cid}",
                    "tool": "Antigravity",
                    "id": cid,
                    "title": final_title,
                    "summary": summary,
                    "last_user_prompt": tail["last_user_prompt"],
                    "last_assistant_summary": tail["last_assistant_summary"],
                    "touched_files": tail["touched_files"],
                    "workspace": ws,
                    "updated_at": local_dt.isoformat(timespec="seconds"),
                    "time": local_dt.strftime("%H:%M"),
                }
            )
    except Exception:
        pass
    finally:
        conn.close()
    return out


def load_codex_rollout_summaries(
    summaries_dir: str | None = None,
    *,
    since_ts: float | None = None,
    limit: int = 30,
) -> dict[str, dict[str, str]]:
    root = summaries_dir or os.path.expanduser("~/.codex/memories/rollout_summaries")
    if not os.path.isdir(root):
        return {}
    files: list[tuple[float, str]] = []
    try:
        for entry in os.scandir(root):
            if not entry.is_file() or not entry.name.endswith(".md"):
                continue
            st = entry.stat()
            if since_ts is not None and st.st_mtime < since_ts:
                continue
            files.append((st.st_mtime, entry.path))
    except OSError:
        return {}
    files.sort(reverse=True)
    by_tid: dict[str, dict[str, str]] = {}
    for _, fp in files[:limit]:
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                head = [fh.readline() for _ in range(24)]
        except OSError:
            continue
        tid = ""
        cwd = ""
        updated = ""
        title = ""
        context = ""
        for line in head:
            s = line.strip()
            if s.startswith("thread_id:"):
                tid = s.split(":", 1)[1].strip()
            elif s.startswith("cwd:"):
                cwd = _shorten_home(s.split(":", 1)[1].strip())
            elif s.startswith("updated_at:"):
                updated = s.split(":", 1)[1].strip()
            elif s.startswith("# ") and not title:
                title = _clean_text(s[2:].strip(), 80)
            elif s.startswith("Rollout context:") and not context:
                context = _clean_text(s.split(":", 1)[1].strip(), 150)
        if tid:
            by_tid[tid] = {"title": title, "summary": context, "cwd": cwd, "updated_at": updated}
    return by_tid


def load_codex_session_index(index_path: str | None = None) -> dict[str, dict[str, str]]:
    path = index_path or os.path.expanduser("~/.codex/session_index.jsonl")
    if not os.path.exists(path):
        return {}
    out: dict[str, dict[str, str]] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()[-200:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            sid = str(obj.get("id") or "")
            if sid:
                out[sid] = {
                    "title": str(obj.get("thread_name") or "").strip(),
                    "updated_at": str(obj.get("updated_at") or "").strip(),
                }
    except OSError:
        pass
    return out


def scan_codex_today(
    start: datetime,
    end: datetime,
    tz: ZoneInfo,
    *,
    codex_home: str | None = None,
) -> list[dict[str, Any]]:
    root = codex_home or os.path.expanduser("~/.codex")
    idx = load_codex_session_index(os.path.join(root, "session_index.jsonl"))
    summaries = load_codex_rollout_summaries(
        os.path.join(root, "memories", "rollout_summaries"),
        since_ts=start.timestamp() - 3600,
    )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 1. Start with today's official rollout summaries (highest quality, 0 tokens)
    for tid, info in summaries.items():
        dt = _parse_iso(info.get("updated_at"))
        if dt is None:
            continue
        local_dt = dt.astimezone(tz)
        if not (start <= local_dt < end):
            continue
        title = info.get("title") or idx.get(tid, {}).get("title") or tid[:8]
        if title.strip().lower() in NOISE_CODEX_TITLES:
            continue
        seen.add(tid)
        out.append(
            {
                "key": f"codex:{tid}",
                "tool": "Codex",
                "id": tid,
                "title": _clean_text(title, 80),
                "summary": _clean_text(info.get("summary") or "", 140),
                "workspace": info.get("cwd") or "",
                "updated_at": local_dt.isoformat(timespec="seconds"),
                "time": local_dt.strftime("%H:%M"),
            }
        )

    # 2. Supplement from session_index.jsonl for sessions not yet summarized by Codex
    for sid, info in idx.items():
        if sid in seen:
            continue
        dt = _parse_iso(info.get("updated_at"))
        if dt is None:
            continue
        local_dt = dt.astimezone(tz)
        if not (start <= local_dt < end):
            continue
        title = str(info.get("title") or "").strip()
        if not title or title.lower() in NOISE_CODEX_TITLES:
            continue
        rpath = _find_codex_rollout(sid, codex_home=root)
        tail = _extract_transcript_tail("codex", rpath) if rpath else {"last_user_prompt": "", "last_assistant_summary": "", "touched_files": []}
        final_title = _clean_text(title, 80)
        summary = _choose_live_summary(final_title, "", tail)
        out.append(
            {
                "key": f"codex:{sid}",
                "tool": "Codex",
                "id": sid,
                "title": final_title,
                "summary": summary,
                "last_user_prompt": tail["last_user_prompt"],
                "last_assistant_summary": tail["last_assistant_summary"],
                "touched_files": tail["touched_files"],
                "workspace": "",
                "updated_at": local_dt.isoformat(timespec="seconds"),
                "time": local_dt.strftime("%H:%M"),
            }
        )
    return out


def scan_workbuddy_today(
    start: datetime,
    end: datetime,
    tz: ZoneInfo,
    *,
    projects_dir: str | None = None,
) -> list[dict[str, Any]]:
    root = projects_dir or os.path.expanduser("~/.workbuddy/projects")
    if not os.path.isdir(root):
        return []
    start_ts = start.timestamp()
    out: list[dict[str, Any]] = []
    for fp in glob.glob(os.path.join(root, "*", "*.jsonl")):
        try:
            st = os.stat(fp)
        except OSError:
            continue
        if st.st_mtime < start_ts:
            continue
        local_dt = datetime.fromtimestamp(st.st_mtime, tz=tz)
        if not (start <= local_dt < end):
            continue
        sid = os.path.splitext(os.path.basename(fp))[0]
        proj_slug = os.path.basename(os.path.dirname(fp))
        tail = _extract_transcript_tail("workbuddy", fp)
        final_title = _clean_text(tail["last_user_prompt"] or proj_slug, 80)
        summary = _choose_live_summary(final_title, tail["last_user_prompt"], tail)
        out.append(
            {
                "key": f"workbuddy:{sid}",
                "tool": "WorkBuddy",
                "id": sid,
                "title": final_title,
                "summary": summary,
                "last_user_prompt": tail["last_user_prompt"],
                "last_assistant_summary": tail["last_assistant_summary"],
                "touched_files": tail["touched_files"],
                "workspace": proj_slug,
                "updated_at": local_dt.isoformat(timespec="seconds"),
                "time": local_dt.strftime("%H:%M"),
            }
        )
    return out


def _read_claude_session_meta(fp: str) -> dict[str, str]:
    title = ""
    cwd = ""
    last_prompt = ""
    try:
        with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()
        for line in lines:
            if '"custom-title"' in line or '"ai-title"' in line or '"workingDirectory"' in line or '"cwd"' in line or '"last-prompt"' in line:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict) or obj.get("isSidechain"):
                    continue
                t = obj.get("type")
                if t == "custom-title" and obj.get("customTitle"):
                    title = _clean_text(obj["customTitle"], 80)
                elif t == "ai-title" and obj.get("aiTitle") and not title:
                    title = _clean_text(obj["aiTitle"], 80)
                elif t == "last-prompt" and obj.get("lastPrompt"):
                    last_prompt = _clean_text(obj["lastPrompt"], 140)
                elif t == "attachment" and not cwd:
                    snap = (obj.get("attachment") or {}).get("snapshot") if isinstance(obj.get("attachment"), dict) else None
                    if isinstance(snap, dict) and snap.get("workingDirectory"):
                        cwd = _shorten_home(str(snap["workingDirectory"]))
                if obj.get("cwd") and not cwd:
                    cwd = _shorten_home(str(obj["cwd"]))
        if not last_prompt:
            for line in reversed(lines[-80:]):
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict) or obj.get("isSidechain") or obj.get("type") != "user":
                    continue
                msg = obj.get("message")
                content = msg.get("content") if isinstance(msg, dict) else obj.get("content")
                txt = ""
                if isinstance(content, str):
                    txt = content
                elif isinstance(content, list):
                    txt = " ".join(
                        str(b.get("text") or "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                txt = re.sub(r"<system-reminder[^>]*>.*?</system-reminder>", "", txt, flags=re.S).strip()
                if txt:
                    last_prompt = _clean_text(txt, 140)
                    break
    except OSError:
        pass
    return {"title": title, "cwd": cwd, "last_prompt": last_prompt}


def scan_claude_today(
    start: datetime,
    end: datetime,
    tz: ZoneInfo,
    *,
    projects_dir: str | None = None,
) -> list[dict[str, Any]]:
    root = projects_dir or os.path.expanduser("~/.claude/projects")
    if not os.path.isdir(root):
        return []
    start_ts = start.timestamp()
    out: list[dict[str, Any]] = []
    for fp in glob.glob(os.path.join(root, "*", "*.jsonl")):
        try:
            st = os.stat(fp)
        except OSError:
            continue
        if st.st_mtime < start_ts:
            continue
        local_dt = datetime.fromtimestamp(st.st_mtime, tz=tz)
        if not (start <= local_dt < end):
            continue
        sid = os.path.splitext(os.path.basename(fp))[0]
        meta = _read_claude_session_meta(fp)
        tail = _extract_transcript_tail("claude", fp)
        ws = meta["cwd"]
        title = meta["title"] or _clean_text((meta["last_prompt"] or tail["last_user_prompt"])[:50], 80) or _clean_text(os.path.basename(ws.rstrip("/")), 80) or sid[:8]
        summary = _choose_live_summary(title, meta["last_prompt"], tail)
        out.append(
            {
                "key": f"claude:{sid}",
                "tool": "Claude Code",
                "id": sid,
                "title": title,
                "summary": summary,
                "last_user_prompt": tail["last_user_prompt"] or meta["last_prompt"],
                "last_assistant_summary": tail["last_assistant_summary"],
                "touched_files": tail["touched_files"],
                "workspace": ws,
                "updated_at": local_dt.isoformat(timespec="seconds"),
                "time": local_dt.strftime("%H:%M"),
            }
        )
    return out


def scan_git_today(
    start: datetime,
    end: datetime,
    tz: ZoneInfo,
    *,
    data_dir: str,
    cfg: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    cfg = cfg if cfg is not None else _load_cfg()
    candidates: list[str] = []
    for root in cfg.get("git_roots") or []:
        exp = os.path.expanduser(str(root))
        if os.path.isdir(os.path.join(exp, ".git")):
            candidates.append(exp)
        elif os.path.isdir(exp):
            try:
                for entry in os.scandir(exp):
                    if entry.is_dir() and os.path.isdir(os.path.join(entry.path, ".git")):
                        candidates.append(entry.path)
            except OSError:
                pass
    for proj in load_registry(data_dir).get("projects", []):
        for p in proj.get("paths") or []:
            exp = os.path.expanduser(str(p))
            if os.path.isdir(os.path.join(exp, ".git")):
                candidates.append(exp)

    seen_repos: set[str] = set()
    commits: list[dict[str, Any]] = []
    since_arg = start.isoformat()
    until_arg = end.isoformat()
    for repo in candidates:
        real = os.path.realpath(repo)
        if real in seen_repos:
            continue
        seen_repos.add(real)
        try:
            res = subprocess.run(
                [
                    "git",
                    "-C",
                    repo,
                    "log",
                    "--all",
                    f"--since={since_arg}",
                    f"--until={until_arg}",
                    "--pretty=format:%h\t%aI\t%s",
                    "-n",
                    "6",
                ],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if res.returncode != 0 or not res.stdout.strip():
                continue
            for line in res.stdout.splitlines():
                parts = line.split("\t", 2)
                if len(parts) != 3:
                    continue
                h, iso_ts, subj = parts
                dt = _parse_iso(iso_ts)
                local_dt = dt.astimezone(tz) if dt else start
                commits.append(
                    {
                        "repo_name": os.path.basename(repo.rstrip(os.sep)),
                        "repo": _shorten_home(repo),
                        "hash": h.strip(),
                        "time": local_dt.strftime("%H:%M"),
                        "committed_at": local_dt.isoformat(timespec="seconds"),
                        "subject": _clean_text(subj, 100),
                    }
                )
        except Exception:
            continue
    return commits


def _extract_hook_session(
    client: str,
    payload: dict[str, Any],
    now: datetime,
    *,
    ag_db_path: str | None = None,
    codex_home: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    norm_client = str(client or "").strip().lower()
    if norm_client == "antigravity":
        cid = str(payload.get("conversationId") or payload.get("conversation_id") or "").strip()
        if not cid:
            return None
        db_row = _read_antigravity_db_entry(cid, db_path=ag_db_path)
        if db_row:
            if str(db_row.get("parent_conversation_id") or "").strip() or int(db_row.get("nesting_depth") or 0) > 0:
                return None
        ls_conv = _fetch_antigravity_ls_conversation(cid)
        ls_obj = ls_conv.get("conversation") if isinstance(ls_conv, dict) else None
        ls_title = ""
        ls_summary = ""
        if isinstance(ls_obj, dict):
            if str((ls_obj.get("metadata") or {}).get("parentConversationId") or "").strip():
                return None
            ls_title = str((ls_obj.get("annotations") or {}).get("title") or "").strip()
            ls_summary = str((ls_obj.get("state") or {}).get("summary") or "").strip()

        ws_list = payload.get("workspacePaths")
        ws = ""
        if isinstance(ws_list, list) and ws_list:
            ws = _shorten_home(str(ws_list[0]))
        elif payload.get("cwd"):
            ws = _shorten_home(str(payload["cwd"]))
        elif db_row:
            ws = _parse_workspace_uris(str(db_row.get("workspace_uris") or ""))

        last_in = _clean_text(payload.get("lastUserInput") or "", 140)
        tpath = _find_antigravity_transcript(cid)
        tail = _extract_transcript_tail("antigravity", tpath) if tpath else {"last_user_prompt": "", "last_assistant_summary": "", "touched_files": []}
        title = _clean_text(ls_title or (db_row.get("title") if db_row else "") or last_in[:50] or cid[:8], 80)
        summary = _choose_live_summary(title, ls_summary or (db_row.get("preview") if db_row else "") or last_in, tail)
        return {
            "key": f"antigravity:{cid}",
            "tool": "Antigravity",
            "id": cid,
            "title": title,
            "summary": summary,
            "last_input": last_in or tail["last_user_prompt"],
            "last_user_prompt": tail["last_user_prompt"] or last_in,
            "last_assistant_summary": tail["last_assistant_summary"],
            "touched_files": tail["touched_files"],
            "workspace": ws,
            "updated_at": now.isoformat(timespec="seconds"),
            "time": now.strftime("%H:%M"),
        }

    if norm_client == "codex":
        sid = str(payload.get("session_id") or payload.get("thread_id") or payload.get("sessionId") or "").strip()
        if not sid:
            return None
        root = codex_home or os.path.expanduser("~/.codex")
        idx = load_codex_session_index(os.path.join(root, "session_index.jsonl"))
        summaries = load_codex_rollout_summaries(os.path.join(root, "memories", "rollout_summaries"), limit=30)
        info = summaries.get(sid, {})
        idx_title = idx.get(sid, {}).get("title", "")
        title = _clean_text(info.get("title") or idx_title or payload.get("title") or sid[:8], 80)
        if title.strip().lower() in NOISE_CODEX_TITLES:
            return None
        last_msg = _clean_text(
            payload.get("last_assistant_message")
            or payload.get("lastUserInput")
            or payload.get("prompt")
            or "",
            140,
        )
        ws = _shorten_home(str(payload.get("cwd") or info.get("cwd") or ""))
        summary = _clean_text(info.get("summary") or last_msg, 140)
        return {
            "key": f"codex:{sid}",
            "tool": "Codex",
            "id": sid,
            "title": title,
            "summary": summary,
            "last_input": last_msg,
            "workspace": ws,
            "updated_at": now.isoformat(timespec="seconds"),
            "time": now.strftime("%H:%M"),
        }

    if norm_client == "claude":
        sid = str(payload.get("session_id") or payload.get("sessionId") or "").strip()
        if not sid:
            return None
        tpath = str(payload.get("transcript_path") or "").strip()
        meta = _read_claude_session_meta(tpath) if tpath and os.path.isfile(tpath) else {"title": "", "cwd": "", "last_prompt": ""}
        ws = _shorten_home(str(payload.get("cwd") or "")) or meta["cwd"]
        summary = _clean_text(
            payload.get("last_assistant_message") or payload.get("prompt") or meta["last_prompt"] or "",
            140,
        )
        title = meta["title"] or _clean_text(os.path.basename(ws.rstrip("/")) or summary[:50] or sid[:8], 80)
        return {
            "key": f"claude:{sid}",
            "tool": "Claude Code",
            "id": sid,
            "title": title,
            "summary": summary,
            "workspace": ws,
            "updated_at": now.isoformat(timespec="seconds"),
            "time": now.strftime("%H:%M"),
        }

    return None


def _try_push_live_remote(
    data_dir: str,
    day: dict[str, Any],
    *,
    cfg: dict[str, Any] | None = None,
    remote_config_path: str | None = None,
    debounce_seconds: int = DEFAULT_REMOTE_PUSH_DEBOUNCE,
    force: bool = False,
) -> dict[str, Any] | None:
    """Best-effort, debounced remote push of same-day live WAL to NAS hub."""
    try:
        import remote_client

        rc = remote_client.load_remote_config(remote_config_path, project_cfg=cfg)
        if not rc.configured or not rc.write_token:
            return None
        stamp = os.path.join(data_dir, LIVE_DIRNAME, REMOTE_PUSH_STAMP_FILE)
        if not force and os.path.exists(stamp):
            try:
                last_ts = float(open(stamp, "r", encoding="utf-8").read().strip() or "0")
                if time.time() - last_ts < debounce_seconds:
                    return None
            except Exception:
                pass
        short_eps = [
            remote_client.Endpoint(name=ep.name, url=ep.url, timeout=min(ep.timeout, 2.5))
            for ep in rc.endpoints
        ]
        short_rc = remote_client.RemoteConfig(
            endpoints=short_eps,
            read_token=rc.read_token,
            write_token=rc.write_token,
            machine_id=rc.machine_id,
            cache_dir=rc.cache_dir,
        )
        res, ep = remote_client.push_live(short_rc, day, machine_id=rc.machine_id)
        try:
            with open(stamp, "w", encoding="utf-8") as fh:
                fh.write(f"{time.time():.3f}")
        except OSError:
            pass
        return {"endpoint": ep.name, **res}
    except Exception:
        return None


def sync_live(
    data_dir: str,
    *,
    client: str | None = None,
    payload: dict[str, Any] | None = None,
    cfg: dict[str, Any] | None = None,
    debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS,
    force_scan: bool = False,
    skip_scan: bool = False,
    rebuild_index: bool = True,
    index_output: str | None = None,
    ag_db_path: str | None = None,
    codex_home: str | None = None,
    claude_dir: str | None = None,
    workbuddy_dir: str | None = None,
    push_remote: bool = False,
    remote_config_path: str | None = None,
) -> dict[str, Any]:
    cfg = cfg if cfg is not None else _load_cfg()
    tz_name = str(cfg.get("timezone") or "Asia/Shanghai")
    day_start_hour = int(cfg.get("day_start_hour", 5))
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    wd = resolve_workday(cfg, now=now)
    start, end = workday_bounds(wd, tz_name=tz_name, day_start_hour=day_start_hour)

    live_dir = os.path.join(data_dir, LIVE_DIRNAME)
    os.makedirs(live_dir, exist_ok=True)
    stamp_path = os.path.join(live_dir, SCAN_STAMP_FILE)

    scanned = False
    hook_session = (
        _extract_hook_session(client, payload, now, ag_db_path=ag_db_path, codex_home=codex_home)
        if client and payload
        else None
    )

    with file_lock(os.path.join(live_dir, LIVE_LOCK_FILE)):
        day = load_live_day(data_dir, wd)
        sessions = list(day.get("sessions") or [])
        git_items = list(day.get("git") or [])

        if hook_session:
            sessions = _merge_session(sessions, hook_session)

        should_scan = False
        if not skip_scan:
            if force_scan:
                should_scan = True
            else:
                last_ts = 0.0
                if os.path.exists(stamp_path):
                    try:
                        last_ts = float(open(stamp_path, "r", encoding="utf-8").read().strip() or "0")
                    except Exception:
                        last_ts = 0.0
                if time.time() - last_ts >= debounce_seconds:
                    should_scan = True

        if should_scan:
            scanned = True
            for s in scan_antigravity_today(start, end, tz, db_path=ag_db_path):
                sessions = _merge_session(sessions, s)
            for s in scan_codex_today(start, end, tz, codex_home=codex_home):
                sessions = _merge_session(sessions, s)
            for s in scan_claude_today(start, end, tz, projects_dir=claude_dir):
                sessions = _merge_session(sessions, s)
            for s in scan_workbuddy_today(start, end, tz, projects_dir=workbuddy_dir):
                sessions = _merge_session(sessions, s)
            git_items = _merge_git(
                git_items,
                scan_git_today(start, end, tz, data_dir=data_dir, cfg=cfg),
            )
            try:
                with open(stamp_path, "w", encoding="utf-8") as fh:
                    fh.write(f"{time.time():.3f}")
            except OSError:
                pass

        day["sessions"] = redact.redact_with_cfg(sessions, cfg)
        day["git"] = redact.redact_with_cfg(git_items, cfg)
        day["notes"] = redact.redact_with_cfg(list(day.get("notes") or []), cfg)
        day["updated_at"] = now.isoformat(timespec="seconds")
        atomic_write_json(live_file_path(data_dir, wd), day)

    index_stats = None
    if rebuild_index:
        import query_memory

        days = query_memory.load_all_days(data_dir)
        catalog = ProjectCatalog(days, load_registry(data_dir))
        index_stats = query_memory.generate_memory_index(
            days,
            output_path=index_output or query_memory.resolve_index_path(cfg),
            tz_name=tz_name,
            catalog=catalog,
            data_dir=data_dir,
        )

    try:
        import search_fts

        if os.path.exists(search_fts.index_db_path(data_dir)):
            search_fts.sync_live_to_fts(data_dir, day)
    except Exception:
        pass

    remote_res = None
    if push_remote:
        remote_res = _try_push_live_remote(
            data_dir,
            day,
            cfg=cfg,
            remote_config_path=remote_config_path,
            force=force_scan,
        )

    return {
        "workday": wd,
        "scanned": scanned,
        "hook_recorded": hook_session is not None,
        "notes_count": len(day["notes"]),
        "sessions_count": len(day["sessions"]),
        "git_count": len(day["git"]),
        "index": index_stats,
        "remote_push": remote_res,
    }


def search_live_entries(
    live_days: list[dict[str, Any]],
    *,
    project_identity: dict[str, Any] | None = None,
    terms: list[str] | None = None,
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    norm_terms = [t.lower() for t in (terms or []) if t.strip()]
    proj_names: set[str] = set()
    proj_paths: list[str] = []
    proj_id = ""
    if project_identity:
        proj_id = str(project_identity.get("project_id") or "")
        for lbl in [project_identity.get("project"), *(project_identity.get("aliases") or [])]:
            if lbl:
                proj_names.add(name_key(str(lbl)))
        proj_paths = [str(p).lower() for p in (project_identity.get("paths") or []) if p]

    for day in live_days:
        wd = str(day.get("workday") or "")
        for n in day.get("notes") or []:
            matched = False
            if project_identity:
                if proj_id and n.get("project_id") == proj_id:
                    matched = True
                elif name_key(str(n.get("project") or "")) in proj_names:
                    matched = True
            elif norm_terms:
                blob = f"{n.get('project', '')} {n.get('text', '')} {' '.join(n.get('paths') or [])}".lower()
                matched = all(t in blob for t in norm_terms)
            if matched:
                hits.append({"kind": "live_note", "workday": wd, **n})

        for s in day.get("sessions") or []:
            blob = f"{s.get('tool', '')} {s.get('title', '')} {s.get('summary', '')} {s.get('workspace', '')} {s.get('last_input', '')}".lower()
            matched = False
            if project_identity:
                ws = str(s.get("workspace") or "").lower()
                if ws and any(ws in p or p in ws for p in proj_paths):
                    matched = True
                elif any(lbl and lbl in blob for lbl in proj_names):
                    matched = True
            elif norm_terms:
                matched = all(t in blob for t in norm_terms)
            if matched:
                hits.append({"kind": "live_session", "workday": wd, **s})

        for g in day.get("git") or []:
            blob = f"{g.get('repo_name', '')} {g.get('repo', '')} {g.get('subject', '')} {g.get('hash', '')}".lower()
            matched = False
            if project_identity:
                repo = str(g.get("repo") or "").lower()
                if repo and any(repo in p or p in repo for p in proj_paths):
                    matched = True
                elif any(lbl and lbl in blob for lbl in proj_names):
                    matched = True
            elif norm_terms:
                matched = all(t in blob for t in norm_terms)
            if matched:
                hits.append({"kind": "live_git", "workday": wd, **g})

    return hits


def main() -> None:
    parser = argparse.ArgumentParser(description="Zero-LLM real-time WAL updater for Agent Context Bus.")
    parser.add_argument("--data-dir", default=os.environ.get("DAILY_MEMORY_DATA_DIR") or os.path.join(BASE_DIR, "data"))
    parser.add_argument("--client", default=None, help="Hook client name (antigravity|codex|claude).")
    parser.add_argument("--payload-file", default=None, help="Optional temp JSON file containing hook payload (deleted after read).")
    parser.add_argument("--force", action="store_true", help="Ignore debounce window and scan all 4 clients + git.")
    parser.add_argument("--no-rebuild-index", action="store_true", help="Do not rewrite MEMORY_INDEX.md.")
    parser.add_argument("--index-output", default=None, help="Override MEMORY_INDEX.md output path.")
    parser.add_argument("--push-remote", action="store_true", help="Debounced push of live WAL to configured NAS hub.")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Print JSON summary.")
    args = parser.parse_args()

    payload: dict[str, Any] | None = None
    if args.payload_file and os.path.exists(args.payload_file):
        try:
            with open(args.payload_file, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                payload = raw
        except Exception:
            payload = None
        finally:
            try:
                os.remove(args.payload_file)
            except OSError:
                pass

    res = sync_live(
        args.data_dir,
        client=args.client,
        payload=payload,
        force_scan=args.force,
        rebuild_index=not args.no_rebuild_index,
        index_output=args.index_output,
        push_remote=args.push_remote,
    )
    if args.as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(
            f"Live synced {res['workday']}: "
            f"{res['notes_count']} notes, {res['sessions_count']} sessions, {res['git_count']} commits "
            f"(scanned={res['scanned']})"
        )


if __name__ == "__main__":
    main()
