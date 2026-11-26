#!/usr/bin/env python3
"""Find verbatim excerpts in the AI clients' local session stores."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import heapq
import json
import os
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

from collect import (
    extract_codex_uuid,
    extract_text_from_codex_content,
    list_codex_rollout_paths,
    load_antigravity_annotations,
    open_zstd_text,
    parse_iso_dt,
    parse_workbuddy_ts,
)
from query_memory import DEFAULT_DATA_DIR, load_config


CLIENTS = ("antigravity", "codex", "claude", "workbuddy")
MAX_EXCERPT = 800
EARLIEST = datetime.min.replace(tzinfo=timezone.utc)


def json_object(raw):
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def tool_texts(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for part in value:
            yield from tool_texts(part)
    elif isinstance(value, dict):
        if value.get("type") in ("image", "input_image", "reasoning", "reasoning_text"):
            return
        for key, part in value.items():
            if key not in ("reasoning", "thinking", "encrypted_content"):
                yield from tool_texts(part)


def tool_arguments(value):
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
        if isinstance(decoded, (dict, list)):
            return decoded
    return value


def message_or_tool(event):
    kind = event.get("type")
    role = event.get("role")
    if kind == "message" and role in ("user", "assistant"):
        yield role, extract_text_from_codex_content(event.get("content"))
    elif kind in ("function_call", "custom_tool_call"):
        value = event.get("arguments", event.get("input"))
        yield "tool_call", tool_arguments(value)
    elif kind in ("function_call_output", "custom_tool_call_output", "function_call_result"):
        yield "tool_output", event.get("output")


def antigravity_texts(event):
    kind = event.get("type")
    if kind == "USER_INPUT" or event.get("source") == "USER_EXPLICIT":
        yield "user", event.get("content")
    elif kind == "PLANNER_RESPONSE":
        yield "assistant", event.get("content")
        for call in event.get("tool_calls") or []:
            yield "tool_call", tool_arguments(call.get("args"))
    elif kind == "GENERIC":
        yield "tool_output", event.get("content")
    if event.get("error"):
        yield "error", event["error"]


def codex_texts(event):
    payload = event.get("payload") or {}
    if event.get("type") == "response_item":
        yield from message_or_tool(payload)
    elif event.get("type") == "event_msg" and payload.get("type") == "error":
        yield "error", payload.get("message")


def claude_texts(event):
    if event.get("isSidechain"):
        return
    kind = event.get("type")
    if kind not in ("user", "assistant"):
        return
    msg = event.get("message")
    content = msg.get("content") if isinstance(msg, dict) else event.get("content")
    if isinstance(content, str):
        yield kind, content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                yield kind, block
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    yield kind, block.get("text")
                elif btype == "tool_use":
                    yield "tool_call", tool_arguments(block.get("input"))
                elif btype == "tool_result":
                    yield "tool_output", block.get("content")


def session_files(client, ids):
    if client == "antigravity":
        brain = Path.home() / ".gemini/antigravity/brain"
        if ids:
            _, parents = load_antigravity_annotations()
            ids = ids | {child for child, parent in parents.items() if parent in ids}
        for logs in sorted(brain.glob("*/.system_generated/logs")):
            sid = logs.parent.parent.name
            if not ids or sid in ids:
                full = logs / "transcript_full.jsonl"
                path = full if full.is_file() else logs / "transcript.jsonl"
                if path.is_file():
                    yield sid, path
    elif client == "codex":
        seen = set()
        for filename in list_codex_rollout_paths():
            path = Path(filename)
            sid = extract_codex_uuid(path.name) or path.name.removesuffix(".zst").removesuffix(".jsonl")
            if (not ids or sid in ids) and sid not in seen:
                seen.add(sid)
                yield sid, path
    elif client == "claude":
        root = Path.home() / ".claude/projects"
        for path in sorted(root.glob("*/*.jsonl")):
            if path.is_file() and (not ids or path.stem in ids):
                yield path.stem, path
    elif client == "workbuddy":
        root = Path.home() / ".workbuddy/projects"
        for path in sorted(root.rglob("*.jsonl")):
            if not ids or path.stem in ids:
                yield path.stem, path


def jsonl_records(client, ids, errors):
    for sid, path in session_files(client, ids):
        try:
            stream = open_zstd_text(str(path)) if path.suffix == ".zst" else path.open(encoding="utf-8")
            with stream:
                for line_num, line in enumerate(stream, 1):
                    event = json_object(line)
                    if client == "antigravity":
                        dt = parse_iso_dt(event.get("created_at") or "")
                        fields = antigravity_texts(event)
                    elif client == "codex":
                        dt = parse_iso_dt(event.get("timestamp") or "")
                        fields = codex_texts(event)
                    elif client == "claude":
                        dt = parse_iso_dt(event.get("timestamp") or "")
                        fields = claude_texts(event)
                    else:
                        dt = parse_workbuddy_ts(event.get("timestamp"))
                        fields = message_or_tool(event)
                    for kind, value in fields:
                        yield sid, path, line_num, dt, kind, value
        except (OSError, UnicodeError, RuntimeError) as exc:
            errors.append(f"{path}: {exc}")


def excerpts(text, pattern):
    last_end = 0
    for match in pattern.finditer(text):
        if match.start() < last_end:
            continue
        start = text.rfind("\n", 0, match.start()) + 1
        end = text.find("\n", match.end())
        if end < 0:
            end = len(text)
        if end - start > MAX_EXCERPT:
            # Keep the match visible even if it occurs late in a long log line.
            left_context = min(120, max(0, MAX_EXCERPT - len(match.group())))
            start = max(start, match.start() - left_context)
            end = min(end, start + MAX_EXCERPT)
        last_end = end
        prefix = "…" if start and text[start - 1] != "\n" else ""
        suffix = "…" if end < len(text) and text[end] != "\n" else ""
        yield prefix + text[start:end] + suffix


def find_matches(clients, ids, pattern, start, end, tz, day_start, errors):
    for client in clients:
        records = jsonl_records(client, ids, errors)
        for sid, path, position, dt, kind, value in records:
            if start and (dt is None or dt < start):
                continue
            if end and (dt is None or dt >= end):
                continue
            for field in tool_texts(value):
                for snippet in excerpts(field, pattern):
                    local_dt = dt.astimezone(tz) if dt else None
                    yield dt or EARLIEST, {
                        "client": client,
                        "session_id": sid,
                        "path": str(path),
                        "position": position,
                        "timestamp": local_dt.isoformat(timespec="seconds") if local_dt else "",
                        "workday": (local_dt - timedelta(hours=day_start)).date().isoformat() if local_dt else "",
                        "kind": kind,
                        "excerpt": snippet,
                    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--keyword", "-k", required=True, help="Case-insensitive literal text to find.")
    parser.add_argument("--client", choices=CLIENTS, help="Search one client (default: all clients).")
    parser.add_argument("--session-id", action="append", help="Limit to a session ID. Repeat to search several.")
    parser.add_argument("--date", type=date.fromisoformat, help="Search one workday (YYYY-MM-DD).")
    parser.add_argument("--from", dest="date_from", type=date.fromisoformat, help="First workday, inclusive.")
    parser.add_argument("--to", dest="date_to", type=date.fromisoformat, help="Last workday, inclusive.")
    parser.add_argument("--limit", type=int, default=5, help="Maximum excerpts, newest first (1-50, default: 5).")
    parser.add_argument("--no-index", action="store_true", help="Bypass SQLite FTS5 session index and scan JSONL files directly.")
    parser.add_argument("--data-dir", default=None, help="Override data directory for derived FTS5 index.")
    parser.add_argument("--json", action="store_true", help="Return excerpt records as JSON.")
    args = parser.parse_args()
    if not args.keyword.strip():
        parser.error("--keyword must contain text")
    if not 1 <= args.limit <= 50:
        parser.error("--limit must be between 1 and 50")
    if args.date and (args.date_from or args.date_to):
        parser.error("use --date or --from/--to")
    if args.date_from and args.date_to and args.date_from > args.date_to:
        parser.error("--from must be on or before --to")

    cfg = load_config()
    tz_name = cfg.get("timezone", "Asia/Shanghai")
    tz = ZoneInfo(tz_name)
    day_start = int(cfg.get("day_start_hour", 5))
    first, last = args.date or args.date_from, args.date or args.date_to
    offset = timedelta(hours=day_start)
    start = datetime.combine(first, datetime.min.time(), tz) + offset if first else None
    end = datetime.combine(last, datetime.min.time(), tz) + offset + timedelta(days=1) if last else None
    clients = (args.client,) if args.client else CLIENTS
    ids = set(args.session_id or [])
    errors: list[str] = []

    results: list[dict] = []
    cutoff_date = (datetime.now(tz) - timedelta(days=30)).date()
    within_fts_window = (first is None or first >= cutoff_date)
    data_dir = args.data_dir or os.environ.get("DAILY_MEMORY_DATA_DIR") or DEFAULT_DATA_DIR

    if not args.no_index and within_fts_window:
        try:
            import search_fts

            search_fts.sync_sessions_to_fts(
                data_dir,
                days=30,
                clients=clients,
                tz_name=tz_name,
                day_start_hour=day_start,
                cfg=cfg,
            )
            fts_hits = search_fts.search(
                data_dir,
                args.keyword,
                doc_types=("session_turn",),
                clients=clients,
                session_ids=ids or None,
                exact_date=args.date.isoformat() if args.date else None,
                date_from=args.date_from.isoformat() if args.date_from else None,
                date_to=args.date_to.isoformat() if args.date_to else None,
                limit=args.limit,
                auto_sync_structured=False,
            )
            results = [h["payload"] for h in fts_hits if isinstance(h.get("payload"), dict)]
        except Exception:
            results = []

    if not results:
        pattern = re.compile(re.escape(args.keyword), re.IGNORECASE)
        hits = find_matches(clients, ids, pattern, start, end, tz, day_start, errors)
        results = [hit for _, hit in heapq.nlargest(args.limit, hits, key=lambda pair: pair[0])]
    for error in errors:
        print(f"读取原会话失败：{error}", file=sys.stderr)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    elif results:
        for hit in results:
            print(f"[{hit['client']}] {hit['timestamp']} {hit['kind']} {hit['session_id']}")
            print(f"{hit['path']}:{hit['position']}")
            print(hit["excerpt"] + "\n")
    else:
        print("未找到匹配的本地原会话片段。")


if __name__ == "__main__":
    main()
