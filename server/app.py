#!/usr/bin/env python3
"""Daily Memory Hub for fnOS / Docker: REST API + Remote MCP (Streamable HTTP) + Auto LLM Summarizer."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import glob
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import ssl
import sys
import threading
import time
from typing import Any
from urllib import parse as urlparse
from zoneinfo import ZoneInfo

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import decisions as dec_mod
import live_memory
import multi_machine
import query_memory
import redact
import search_fts
import summarizer
try:
    import write_excel
except ImportError:  # Allow running hub API/MCP/UI in pure-stdlib environments without openpyxl
    write_excel = None  # type: ignore[assignment]
from project_identity import ProjectCatalog, edit_registry, load_registry
from provenance import verify_day_sources

UI_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")


MCP_PROTOCOL_VERSION = "2025-03-26"

MCP_TOOLS = [
    {
        "name": "memory_get_index",
        "description": "Read the L2 current-state project snapshot (MEMORY_INDEX.md) across all synced machines.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_list_projects",
        "description": "List and filter projects across work/personal scopes, recent days, status, or machine.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["all", "work", "personal", "life"], "default": "all"},
                "days": {"type": "integer", "description": "Limit to recent N days"},
                "status": {"type": "string", "description": "Filter by status substring, e.g. 进行中"},
                "machine": {"type": "string", "description": "Filter by machine_id"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_query",
        "description": "Query historical project timelines, keyword matches, or specific workdays; set raw=true for session UUIDs, paths, and Git commits.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project name or keyword"},
                "keyword": {"type": "string", "description": "Space-separated AND keywords"},
                "date": {"type": "string", "description": "Exact workday YYYY-MM-DD"},
                "days": {"type": "integer", "description": "Recent N days"},
                "date_from": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "End date YYYY-MM-DD"},
                "scope": {"type": "string", "enum": ["all", "work", "personal", "life"], "default": "all"},
                "status": {"type": "string", "description": "Filter by status substring"},
                "machine": {"type": "string", "description": "Filter by machine_id"},
                "raw": {"type": "boolean", "default": False, "description": "Include raw session UUIDs, workspace paths, and Git commits"},
                "asc": {"type": "boolean", "default": False, "description": "Sort chronologically ascending"},
                "limit": {"type": "integer", "default": 20},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_get_raw_day",
        "description": "Fetch raw cross-machine sessions, Git commits, and file changes for a specific YYYY-MM-DD workday.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workday": {"type": "string", "description": "Workday YYYY-MM-DD"},
                "machine": {"type": "string", "description": "Optional machine_id filter"},
            },
            "required": ["workday"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_server_status",
        "description": "Inspect NAS memory hub statistics, synced devices, latest workday, and auto-summarizer status.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_add_note",
        "description": "Write a 0-token real-time same-day note (optionally registering physical paths) into the hub live WAL. Requires write_token.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project name or alias"},
                "text": {"type": "string", "description": "Key conclusion, decision, or change summary"},
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Optional project file/dir paths"},
                "machine": {"type": "string", "description": "Optional source machine ID"},
            },
            "required": ["project", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_register_path",
        "description": "Add, remove, or replace registered physical directories/config paths for a project. Requires write_token.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project name, alias, or ID"},
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Paths to register"},
                "mode": {"type": "string", "enum": ["add", "remove", "set"], "default": "add"},
            },
            "required": ["project", "paths"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_add_decision",
        "description": "Record a persistent project or global decision/constraint/pitfall, optionally superseding older decision IDs. Requires write_token.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["project", "global"], "default": "project"},
                "project": {"type": "string", "description": "Project name (required when scope=project)"},
                "kind": {"type": "string", "enum": ["decision", "constraint", "pitfall"], "default": "decision"},
                "text": {"type": "string", "description": "Reusable decision, constraint, or pitfall"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "supersedes": {"type": "array", "items": {"type": "string"}, "description": "Old decision IDs replaced by this one"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
]

MCP_WRITE_TOOLS = {"memory_add_note", "memory_register_path", "memory_add_decision"}


class RateLimiter:
    """Simple per-IP sliding window rate limiter to protect public IPv4/IPv6 endpoints."""

    def __init__(self, max_requests: int = 120, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def allow(self, client_ip: str) -> bool:
        if self.max_requests <= 0:
            return True
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            bucket = [t for t in self._hits[client_ip] if t >= cutoff]
            if len(bucket) >= self.max_requests:
                self._hits[client_ip] = bucket
                return False
            bucket.append(now)
            self._hits[client_ip] = bucket
            return True


class HubState:
    """Holds NAS storage paths, auth tokens, write pipeline lock, and scheduler state."""

    def __init__(
        self,
        *,
        storage_root: str,
        read_token: str = "",
        write_token: str = "",
        timezone_name: str = "Asia/Shanghai",
        day_start_hour: int = 5,
        monthly_folders: bool = True,
        rate_limit_per_min: int = 180,
        enable_auto_summarize: bool = False,
        summarize_hour: int = 9,
        summarize_minute: int = 5,
        llm_base_url: str = "",
        llm_api_key: str = "",
        llm_model: str = "gpt-4o-mini",
        allow_fallback_summarize: bool = False,
        push_resummarize_debounce_sec: float = 120.0,
    ) -> None:
        self.storage_root = os.path.abspath(os.path.expanduser(storage_root))
        self.data_dir = os.path.join(self.storage_root, "data")
        self.report_dir = os.path.join(self.storage_root, "DailyReport")
        self.work_excel_path = os.path.join(self.report_dir, "每日工作汇总.xlsx")
        self.personal_excel_path = os.path.join(self.report_dir, "个人技术与折腾汇总.xlsx")
        self.memory_index_path = os.path.join(self.report_dir, "MEMORY_INDEX.md")

        self.read_token = read_token.strip()
        self.write_token = write_token.strip()
        self.timezone_name = timezone_name
        self.day_start_hour = day_start_hour
        self.monthly_folders = monthly_folders
        self.rate_limiter = RateLimiter(max_requests=rate_limit_per_min, window_seconds=60)

        self.enable_auto_summarize = enable_auto_summarize
        self.summarize_hour = summarize_hour
        self.summarize_minute = summarize_minute
        self.llm_base_url = llm_base_url.strip()
        self.llm_api_key = llm_api_key.strip()
        self.llm_model = llm_model.strip() or "gpt-4o-mini"
        self.allow_fallback_summarize = allow_fallback_summarize
        self.push_resummarize_debounce_sec = max(0.0, float(push_resummarize_debounce_sec))

        self.last_summarize_run: dict[str, Any] = {}
        self._write_lock = threading.Lock()
        self._summarize_lock = threading.Lock()
        self._debounce_lock = threading.Lock()
        self._pending_resummarize: set[str] = set()
        self._debounce_timer: threading.Timer | None = None
        self._stop_event = threading.Event()
        self._scheduler_thread: threading.Thread | None = None

        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.report_dir, exist_ok=True)

    def check_auth(self, auth_header: str, *, require_write: bool = False) -> bool:
        # If no tokens configured at all, allow (for isolated LAN / unit tests)
        if not self.read_token and not self.write_token:
            return True
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header[7:].strip()
        if not token:
            return False
        if require_write:
            expected = self.write_token or self.read_token
            return bool(expected) and hmac.compare_digest(token, expected)
        # Read permission accepts either read_token or write_token
        candidates = [t for t in (self.read_token, self.write_token) if t]
        return any(hmac.compare_digest(token, c) for c in candidates)

    def rebuild_index_locked(self) -> dict[str, Any]:
        days = query_memory.load_all_days(self.data_dir)
        archived = {str(d.get("workday") or "") for d in days}
        live_days = multi_machine.load_merged_live_days(self.data_dir, archived_workdays=archived, max_days=2)
        active_decs = dec_mod.query_decisions(self.data_dir, limit=12)
        return query_memory.generate_memory_index(
            days,
            output_path=self.memory_index_path,
            tz_name=self.timezone_name,
            catalog=ProjectCatalog(days, load_registry(self.data_dir)),
            data_dir=self.data_dir,
            live_days=live_days,
            active_decisions=active_decs,
        )

    def get_index_markdown(self) -> str:
        if not os.path.exists(self.memory_index_path):
            with self._write_lock:
                if not os.path.exists(self.memory_index_path):
                    self.rebuild_index_locked()
        with open(self.memory_index_path, "r", encoding="utf-8") as fh:
            return fh.read()

    def apply_summaries(self, machine_id: str, days_payload: list[dict[str, Any]]) -> dict[str, Any]:
        if not days_payload:
            raise ValueError("Empty summary payload.")
        with self._write_lock:
            merged_days = [
                multi_machine.save_machine_summary(
                    self.data_dir,
                    machine_id,
                    verify_day_sources(
                        day_obj,
                        query_memory.load_raw_day(str(day_obj.get("workday") or ""), data_dir=self.data_dir),
                    ),
                )
                for day_obj in days_payload
            ]
            excel_res = write_excel.update_split_workbooks(
                merged_days,
                self.work_excel_path,
                self.personal_excel_path,
                split_months=self.monthly_folders,
            )
            idx_res = write_excel.sync_json_indexes(merged_days, data_dir=self.data_dir)
            mem_res = self.rebuild_index_locked()
            return {
                "workdays": [str(d.get("workday")) for d in merged_days],
                "excel": {
                    "work": excel_res.get("work"),
                    "personal": excel_res.get("personal"),
                    "months": sorted((excel_res.get("months") or {}).keys()),
                },
                "json_index": idx_res,
                "memory_index": mem_res,
            }

    def run_summarize(
        self,
        workdays: list[str] | None = None,
        *,
        allow_fallback: bool | None = None,
    ) -> dict[str, Any]:
        with self._summarize_lock:
            fallback = self.allow_fallback_summarize if allow_fallback is None else allow_fallback
            target_dates = workdays or summarizer.find_unsummarized_raw_days(
                self.data_dir,
                tz_name=self.timezone_name,
                day_start_hour=self.day_start_hour,
            )
            if not target_dates:
                res = {"status": "noop", "workdays": [], "ran_at": datetime.now(timezone.utc).isoformat()}
                self.last_summarize_run = res
                return res

            generated: list[dict[str, Any]] = []
            errors: list[dict[str, str]] = []
            for wd in target_dates:
                try:
                    day_summary = summarizer.summarize_workday(
                        wd,
                        self.data_dir,
                        llm_base_url=self.llm_base_url,
                        llm_api_key=self.llm_api_key,
                        llm_model=self.llm_model,
                        allow_fallback=fallback,
                    )
                    generated.append(day_summary)
                except Exception as exc:
                    errors.append({"workday": wd, "error": str(exc)})

            applied = self.apply_summaries("__nas__", generated) if generated else {}
            res = {
                "status": "ok" if not errors else ("partial" if generated else "error"),
                "workdays": [d["workday"] for d in generated],
                "errors": errors,
                "applied": applied,
                "ran_at": datetime.now(timezone.utc).isoformat(),
            }
            self.last_summarize_run = res
            return res

    def schedule_resummarize_for_workday(self, workday: str) -> bool:
        """Debounce auto-resummarize when a late device pushes a completed workday's raw shard."""
        wd = str(workday or "").strip()
        if (
            not wd
            or not self.enable_auto_summarize
            or self.push_resummarize_debounce_sec <= 0
            or self._stop_event.is_set()
        ):
            return False
        if not (self.llm_base_url and self.llm_api_key) and not self.allow_fallback_summarize:
            return False
        cur_wd = live_memory.resolve_workday(
            {"timezone": self.timezone_name, "day_start_hour": self.day_start_hour}
        )
        # Never auto-compact the still-active workday before day_start_hour rollover
        if wd >= cur_wd:
            return False
        with self._debounce_lock:
            self._pending_resummarize.add(wd)
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            timer = threading.Timer(self.push_resummarize_debounce_sec, self._flush_pending_resummarize)
            timer.daemon = True
            timer.name = "daily-memory-push-resummarize"
            self._debounce_timer = timer
            timer.start()
        return True

    def _flush_pending_resummarize(self) -> None:
        if self._stop_event.is_set():
            return
        with self._debounce_lock:
            pending = sorted(self._pending_resummarize)
            self._pending_resummarize.clear()
            self._debounce_timer = None
        if not pending:
            return
        try:
            self.run_summarize(workdays=pending)
        except Exception as exc:
            self.last_summarize_run = {
                "status": "error",
                "workdays": pending,
                "error": str(exc),
                "ran_at": datetime.now(timezone.utc).isoformat(),
            }

    def get_status(self) -> dict[str, Any]:
        days = query_memory.load_all_days(self.data_dir)
        latest = str(days[-1]["workday"]) if days else ""
        active_decs = dec_mod.query_decisions(self.data_dir, limit=500)
        return {
            "service": "daily-memory-hub",
            "storage_root": self.storage_root,
            "total_days": len(days),
            "first_date": str(days[0]["workday"]) if days else "",
            "latest_date": latest,
            "latest_workday": latest,
            "raw_days": len(multi_machine.list_raw_workdays(self.data_dir)),
            "machines": multi_machine.list_machines(self.data_dir),
            "active_decisions": len(active_decs),
            "fts_index": search_fts.index_stats(self.data_dir),
            "auto_summarize": {
                "enabled": self.enable_auto_summarize,
                "schedule": f"{self.summarize_hour:02d}:{self.summarize_minute:02d} ({self.timezone_name})",
                "push_debounce_sec": self.push_resummarize_debounce_sec,
                "pending_workdays": sorted(self._pending_resummarize),
                "llm_configured": bool(self.llm_base_url and self.llm_api_key),
                "llm_model": self.llm_model,
                "last_run": self.last_summarize_run,
            },
        }

    def start_scheduler(self) -> None:
        if not self.enable_auto_summarize or self._scheduler_thread is not None:
            return
        tz = ZoneInfo(self.timezone_name)
        last_fired_date = ""

        def _loop() -> None:
            nonlocal last_fired_date
            while not self._stop_event.wait(30.0):
                now = datetime.now(tz)
                today_key = now.strftime("%Y-%m-%d")
                if (
                    today_key != last_fired_date
                    and (now.hour > self.summarize_hour or (now.hour == self.summarize_hour and now.minute >= self.summarize_minute))
                ):
                    last_fired_date = today_key
                    try:
                        self.run_summarize()
                    except Exception as exc:
                        self.last_summarize_run = {
                            "status": "error",
                            "error": str(exc),
                            "ran_at": datetime.now(timezone.utc).isoformat(),
                        }

        self._scheduler_thread = threading.Thread(target=_loop, name="daily-memory-scheduler", daemon=True)
        self._scheduler_thread.start()

    def stop_scheduler(self) -> None:
        self._stop_event.set()
        with self._debounce_lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
                self._debounce_timer = None
            self._pending_resummarize.clear()


def _filter_raw_by_machine(raw_payload: dict[str, Any], machine: str | None) -> dict[str, Any]:
    if not machine:
        return raw_payload
    target = multi_machine.normalize_machine_id(machine)
    out = dict(raw_payload)
    for key in ("antigravity", "codex", "claude", "workbuddy", "git", "files"):
        items = raw_payload.get(key) or []
        out[key] = [it for it in items if multi_machine.normalize_machine_id(it.get("machine") or raw_payload.get("machine") or "legacy") == target]
    out["filtered_machine"] = target
    return out


def _execute_query(state: HubState, args: dict[str, Any]) -> tuple[str, Any]:
    days = query_memory.load_all_days(state.data_dir)
    catalog = ProjectCatalog(days, load_registry(state.data_dir))
    date_from = args.get("date_from") or args.get("from")
    date_to = args.get("date_to") or args.get("to")
    recent_days = int(args["days"]) if args.get("days") not in (None, "") else None
    exact_date = args.get("date")
    scope = str(args.get("scope") or "all")
    status_filter = args.get("status")
    machine_filter = args.get("machine")
    include_raw = bool(args.get("raw"))
    asc = bool(args.get("asc"))
    limit = int(args.get("limit") or 20)

    filtered = query_memory.filter_days_by_date(
        days,
        date_from=date_from,
        date_to=date_to,
        recent_days=recent_days,
        exact_date=exact_date,
    )

    if args.get("mode") == "projects":
        res = query_memory.query_projects(
            filtered,
            scope=scope,
            status_filter=status_filter,
            machine_filter=machine_filter,
            keyword=args.get("keyword"),
            catalog=catalog,
        )
        return query_memory.format_projects_md(res), res
    if exact_date and not args.get("project") and not args.get("keyword"):
        res = query_memory.query_exact_date(
            days,
            str(exact_date),
            include_raw=include_raw,
            data_dir=state.data_dir,
            machine_filter=machine_filter,
            catalog=catalog,
        )
        return query_memory.format_exact_date_md(res), res
    if args.get("project"):
        res = query_memory.query_by_project(
            filtered,
            str(args["project"]),
            scope=scope,
            status_filter=status_filter,
            machine_filter=machine_filter,
            include_raw=include_raw,
            data_dir=state.data_dir,
            asc=asc,
            limit=limit,
            catalog=catalog,
        )
        return query_memory.format_project_query_md(res), res
    if args.get("keyword"):
        res = query_memory.query_by_keyword(
            filtered,
            str(args["keyword"]),
            scope=scope,
            status_filter=status_filter,
            machine_filter=machine_filter,
            include_raw=include_raw,
            data_dir=state.data_dir,
            asc=asc,
            limit=limit,
            catalog=catalog,
            use_fts=not bool(args.get("exact")),
        )
        return query_memory.format_keyword_query_md(res), res
    res = query_memory.query_projects(
        filtered,
        scope=scope,
        status_filter=status_filter,
        machine_filter=machine_filter,
        catalog=catalog,
    )
    return query_memory.format_projects_md(res), res


def _handle_mcp_rpc(
    state: HubState,
    payload: dict[str, Any],
    *,
    can_write: bool = False,
) -> tuple[int, dict[str, Any] | None]:
    method = str(payload.get("method") or "")
    rpc_id = payload.get("id")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}

    # Notifications have no id and return 202 Accepted with empty body
    if rpc_id is None and method.startswith("notifications/"):
        return 202, None

    def ok(result: Any) -> tuple[int, dict[str, Any]]:
        return 200, {"jsonrpc": "2.0", "id": rpc_id, "result": result}

    def err(code: int, message: str) -> tuple[int, dict[str, Any]]:
        return 200, {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        req_ver = str(params.get("protocolVersion") or MCP_PROTOCOL_VERSION)
        return ok(
            {
                "protocolVersion": req_ver,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "daily-memory-hub", "version": "1.1.0"},
            }
        )
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": MCP_TOOLS})
    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if name in MCP_WRITE_TOOLS and not can_write:
            return err(-32001, f"Write token required for tool: {name}")
        try:
            if name == "memory_get_index":
                text = state.get_index_markdown()
            elif name == "memory_list_projects":
                text, _ = _execute_query(state, {**args, "mode": "projects"})
            elif name == "memory_query":
                text, _ = _execute_query(state, args)
            elif name == "memory_get_raw_day":
                wd = str(args.get("workday") or "").strip()
                raw = query_memory.load_raw_day(wd, data_dir=state.data_dir)
                if raw is None:
                    text = f"No raw data found for workday {wd}."
                else:
                    text = json.dumps(_filter_raw_by_machine(raw, args.get("machine")), ensure_ascii=False, indent=2)
            elif name == "memory_server_status":
                text = json.dumps(state.get_status(), ensure_ascii=False, indent=2)
            elif name == "memory_add_note":
                with state._write_lock:
                    days = query_memory.load_all_days(state.data_dir)
                    saved = live_memory.add_note(
                        state.data_dir,
                        days,
                        str(args.get("project") or ""),
                        str(args.get("text") or ""),
                        source=f"mcp:{args.get('machine') or 'remote'}",
                        paths=args.get("paths") if isinstance(args.get("paths"), list) else None,
                        cfg={"timezone": state.timezone_name, "day_start_hour": state.day_start_hour},
                    )
                    state.rebuild_index_locked()
                text = json.dumps({"ok": True, "workday": saved["workday"], "note": saved["note"]}, ensure_ascii=False, indent=2)
            elif name == "memory_register_path":
                mode_map = {"add": "add_paths", "remove": "remove_paths", "set": "set_paths"}
                act = mode_map.get(str(args.get("mode") or "add").lower(), "add_paths")
                with state._write_lock:
                    days = query_memory.load_all_days(state.data_dir)
                    proj = edit_registry(
                        state.data_dir,
                        days,
                        act,
                        str(args.get("project") or ""),
                        list(args.get("paths") or []),
                    )
                    state.rebuild_index_locked()
                text = json.dumps({"ok": True, "project": proj}, ensure_ascii=False, indent=2)
            elif name == "memory_add_decision":
                with state._write_lock:
                    days = query_memory.load_all_days(state.data_dir)
                    dec = dec_mod.add_decision(
                        state.data_dir,
                        days,
                        text=str(args.get("text") or ""),
                        scope=str(args.get("scope") or "project"),
                        project=str(args.get("project") or ""),
                        kind=str(args.get("kind") or "decision"),
                        tags=args.get("tags"),
                        supersedes=args.get("supersedes") if isinstance(args.get("supersedes"), list) else None,
                        source="mcp",
                        tz_name=state.timezone_name,
                    )
                    state.rebuild_index_locked()
                text = json.dumps({"ok": True, "decision": dec}, ensure_ascii=False, indent=2)
            else:
                return err(-32601, f"Unknown tool: {name}")
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:
            return ok({"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True})

    return err(-32601, f"Method not found: {method}")


def make_handler(state: HubState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            if os.environ.get("DAILY_MEMORY_QUIET_LOG") == "1":
                return
            sys.stderr.write(f"[daily-memory-hub] {self.address_string()} - {format % args}\n")

        def _client_ip(self) -> str:
            xff = self.headers.get("X-Forwarded-For") or self.headers.get("X-Real-IP") or ""
            if xff:
                return xff.split(",")[0].strip()
            return self.client_address[0] if self.client_address else "unknown"

        def _send_bytes(
            self,
            status: int,
            data: bytes,
            content_type: str,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _send_json(self, status: int, obj: Any, extra_headers: dict[str, str] | None = None) -> None:
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self._send_bytes(status, raw, "application/json; charset=utf-8", extra_headers)

        def _send_text(self, status: int, text: str, content_type: str = "text/markdown; charset=utf-8", extra_headers: dict[str, str] | None = None) -> None:
            self._send_bytes(status, text.encode("utf-8"), content_type, extra_headers)

        def _read_json_body(self, max_bytes: int = 20 * 1024 * 1024) -> Any:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            if length > max_bytes:
                raise ValueError("Payload too large")
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))

        def _guard(self, *, require_write: bool = False) -> bool:
            if not state.rate_limiter.allow(self._client_ip()):
                self._send_json(429, {"error": "rate_limit_exceeded"})
                return False
            auth = self.headers.get("Authorization") or ""
            if not state.check_auth(auth, require_write=require_write):
                self._send_json(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self) -> None:
            parsed = urlparse.urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = {k: v[-1] for k, v in urlparse.parse_qs(parsed.query).items()}

            if path == "/healthz":
                self._send_json(200, {"status": "ok", "service": "daily-memory-hub"})
                return

            if path == "/":
                self._send_bytes(302, b"", "text/plain; charset=utf-8", {"Location": "/ui"})
                return

            if path == "/ui":
                if not os.path.exists(UI_HTML_PATH):
                    self._send_json(404, {"error": "ui_not_found"})
                    return
                with open(UI_HTML_PATH, "rb") as fh:
                    html = fh.read()
                self._send_bytes(
                    200,
                    html,
                    "text/html; charset=utf-8",
                    {
                        "X-Content-Type-Options": "nosniff",
                        "Referrer-Policy": "no-referrer",
                        "Content-Security-Policy": "default-src 'self' 'unsafe-inline'; connect-src 'self'",
                    },
                )
                return

            if not self._guard(require_write=False):
                return

            if path == "/api/v1/status":
                self._send_json(200, state.get_status())
                return

            if path == "/api/v1/live":
                days = query_memory.load_all_days(state.data_dir)
                archived = {str(d.get("workday") or "") for d in days}
                live_days = multi_machine.load_merged_live_days(state.data_dir, archived_workdays=archived, max_days=2)
                self._send_json(200, {"live_days": live_days})
                return

            if path == "/api/v1/decisions":
                days = query_memory.load_all_days(state.data_dir)
                catalog = ProjectCatalog(days, load_registry(state.data_dir))
                items = dec_mod.query_decisions(
                    state.data_dir,
                    project=qs.get("project"),
                    scope=qs.get("scope"),
                    kind=qs.get("kind"),
                    include_inactive=qs.get("all_status", "").lower() in ("1", "true", "yes"),
                    keyword=qs.get("keyword"),
                    limit=int(qs.get("limit") or 50),
                    catalog=catalog,
                )
                self._send_json(200, {"total": len(items), "decisions": items})
                return

            if path == "/api/v1/index":
                md = state.get_index_markdown()
                etag = '"' + hashlib.sha256(md.encode("utf-8")).hexdigest()[:16] + '"'
                if self.headers.get("If-None-Match") == etag:
                    self._send_bytes(304, b"", "text/markdown; charset=utf-8", {"ETag": etag})
                    return
                self._send_text(200, md, "text/markdown; charset=utf-8", {"ETag": etag})
                return

            if path == "/api/v1/bundle":
                md = state.get_index_markdown()
                days = query_memory.load_all_days(state.data_dir)
                archived = {str(d.get("workday") or "") for d in days}
                self._send_json(200, {
                    "memory_index_md": md, "days": days, "total_days": len(days),
                    "project_registry": load_registry(state.data_dir),
                    "decisions": dec_mod.load_decisions_store(state.data_dir),
                    "live_days": multi_machine.load_merged_live_days(state.data_dir, archived_workdays=archived, max_days=2),
                })
                return

            if path == "/api/v1/project-review":
                days = query_memory.load_all_days(state.data_dir)
                catalog = ProjectCatalog(days, load_registry(state.data_dir))
                self._send_json(200, {"suggestions": catalog.suggestions(days)})
                return

            if path == "/api/v1/projects":
                args = {
                    "mode": "projects",
                    "scope": qs.get("scope", "all"),
                    "days": qs.get("days"),
                    "date_from": qs.get("from"),
                    "date_to": qs.get("to"),
                    "status": qs.get("status"),
                    "machine": qs.get("machine"),
                    "keyword": qs.get("keyword"),
                }
                md, data = _execute_query(state, args)
                if qs.get("format") == "json":
                    self._send_json(200, data)
                else:
                    self._send_text(200, md)
                return

            if path == "/api/v1/query":
                args = {
                    "project": qs.get("project"),
                    "keyword": qs.get("keyword"),
                    "date": qs.get("date"),
                    "days": qs.get("days"),
                    "date_from": qs.get("from"),
                    "date_to": qs.get("to"),
                    "scope": qs.get("scope", "all"),
                    "status": qs.get("status"),
                    "machine": qs.get("machine"),
                    "raw": qs.get("raw", "").lower() in ("1", "true", "yes", "sources"),
                    "asc": qs.get("asc", "").lower() in ("1", "true", "yes"),
                    "exact": qs.get("exact", "").lower() in ("1", "true", "yes"),
                    "limit": qs.get("limit") or 20,
                }
                md, data = _execute_query(state, args)
                if qs.get("format") == "json":
                    self._send_json(200, data)
                else:
                    self._send_text(200, md)
                return

            if path.startswith("/api/v1/raw/"):
                workday = path[len("/api/v1/raw/") :].strip()
                raw = query_memory.load_raw_day(workday, data_dir=state.data_dir)
                if raw is None:
                    self._send_json(404, {"error": f"raw_not_found:{workday}"})
                    return
                self._send_json(200, _filter_raw_by_machine(raw, qs.get("machine")))
                return

            if path == "/mcp":
                self._send_json(
                    200,
                    {
                        "name": "daily-memory-hub",
                        "transport": "streamable-http",
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "tools": [t["name"] for t in MCP_TOOLS],
                    },
                )
                return

            self._send_json(404, {"error": "not_found", "path": path})

        def do_POST(self) -> None:
            parsed = urlparse.urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path == "/mcp":
                if not self._guard(require_write=False):
                    return
                can_write = state.check_auth(self.headers.get("Authorization") or "", require_write=True)
                try:
                    body = self._read_json_body()
                except Exception as exc:
                    self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}})
                    return
                if isinstance(body, list):
                    responses = []
                    for item in body:
                        if isinstance(item, dict):
                            _, resp_obj = _handle_mcp_rpc(state, item, can_write=can_write)
                            if resp_obj is not None:
                                responses.append(resp_obj)
                    if not responses:
                        self._send_bytes(202, b"", "application/json; charset=utf-8")
                    else:
                        self._send_json(200, responses)
                    return
                if not isinstance(body, dict):
                    self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}})
                    return
                status, resp_obj = _handle_mcp_rpc(state, body, can_write=can_write)
                if resp_obj is None:
                    self._send_bytes(status, b"", "application/json; charset=utf-8")
                else:
                    self._send_json(status, resp_obj)
                return

            if not self._guard(require_write=True):
                return

            try:
                body = self._read_json_body()
            except Exception as exc:
                self._send_json(400, {"error": f"invalid_json:{exc}"})
                return

            if path == "/api/v1/live/push":
                if not isinstance(body, dict):
                    self._send_json(400, {"error": "body_must_be_object"})
                    return
                machine_id = str(body.get("machine_id") or body.get("machine") or "")
                live_payload = body.get("payload") if isinstance(body.get("payload"), dict) else body
                live_payload = redact.redact_obj(live_payload)
                try:
                    with state._write_lock:
                        res = multi_machine.save_machine_live(state.data_dir, machine_id, live_payload)
                        state.rebuild_index_locked()
                        try:
                            if os.path.exists(search_fts.index_db_path(state.data_dir)):
                                search_fts.sync_live_to_fts(
                                    state.data_dir,
                                    multi_machine.load_merged_live_day(state.data_dir, res["workday"]),
                                )
                        except Exception:
                            pass
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                self._send_json(200, {"ok": True, **res})
                return

            if path == "/api/v1/notes":
                if not isinstance(body, dict):
                    self._send_json(400, {"error": "body_must_be_object"})
                    return
                try:
                    with state._write_lock:
                        days = query_memory.load_all_days(state.data_dir)
                        saved = live_memory.add_note(
                            state.data_dir,
                            days,
                            str(body.get("project") or ""),
                            str(body.get("text") or ""),
                            workday=body.get("workday"),
                            source=f"api:{body.get('machine_id') or 'web'}",
                            paths=body.get("paths") if isinstance(body.get("paths"), list) else None,
                            cfg={"timezone": state.timezone_name, "day_start_hour": state.day_start_hour},
                        )
                        state.rebuild_index_locked()
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                self._send_json(200, {"ok": True, "workday": saved["workday"], "note": saved["note"]})
                return

            if path == "/api/v1/decisions":
                if not isinstance(body, dict):
                    self._send_json(400, {"error": "body_must_be_object"})
                    return
                action = str(body.get("action") or "add").strip().lower()
                try:
                    with state._write_lock:
                        days = query_memory.load_all_days(state.data_dir)
                        if action == "add":
                            dec = dec_mod.add_decision(
                                state.data_dir,
                                days,
                                text=str(body.get("text") or ""),
                                scope=str(body.get("scope") or "project"),
                                project=str(body.get("project") or ""),
                                kind=str(body.get("kind") or "decision"),
                                tags=body.get("tags"),
                                supersedes=body.get("supersedes") if isinstance(body.get("supersedes"), list) else None,
                                source="api",
                                tz_name=state.timezone_name,
                            )
                        elif action == "supersede":
                            dec = dec_mod.supersede_decision(
                                state.data_dir,
                                days,
                                str(body.get("old_id") or ""),
                                str(body.get("text") or ""),
                                kind=body.get("kind"),
                                tags=body.get("tags"),
                                source="api",
                                tz_name=state.timezone_name,
                            )
                        elif action == "forget":
                            dec = dec_mod.forget_decision(
                                state.data_dir,
                                str(body.get("id") or ""),
                                reason=str(body.get("reason") or ""),
                                tz_name=state.timezone_name,
                            )
                        else:
                            raise ValueError(f"Unsupported decision action: {action!r}")
                        state.rebuild_index_locked()
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                self._send_json(200, {"ok": True, "decision": dec})
                return

            if path == "/api/v1/project-review":
                if not isinstance(body, dict):
                    self._send_json(400, {"error": "body_must_be_object"})
                    return
                try:
                    with state._write_lock:
                        days = query_memory.load_all_days(state.data_dir)
                        project = edit_registry(
                            state.data_dir, days, body.get("action"), body.get("target"), body.get("labels", []),
                        )
                        state.rebuild_index_locked()
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                self._send_json(200, {"ok": True, "project": project})
                return

            if path == "/api/v1/ingest/raw":
                if not isinstance(body, dict):
                    self._send_json(400, {"error": "body_must_be_object"})
                    return
                machine_id = str(body.get("machine_id") or body.get("machine") or "")
                raw_payload = body.get("payload") if isinstance(body.get("payload"), dict) else body
                raw_payload = redact.redact_obj(raw_payload)
                try:
                    res = multi_machine.save_machine_raw(state.data_dir, machine_id, raw_payload)
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                queued = state.schedule_resummarize_for_workday(str(res.get("workday") or ""))
                self._send_json(200, {"ok": True, "auto_resummarize_queued": queued, **res})
                return

            if path == "/api/v1/ingest/summary":
                if not isinstance(body, dict):
                    self._send_json(400, {"error": "body_must_be_object"})
                    return
                machine_id = str(body.get("machine_id") or body.get("machine") or "default")
                raw_sub = body.get("payload") if "payload" in body else body
                days_payload = write_excel.normalize_days_payload(raw_sub)
                try:
                    res = state.apply_summaries(machine_id, days_payload)
                except Exception as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                self._send_json(200, {"ok": True, "machine_id": multi_machine.normalize_machine_id(machine_id), **res})
                return

            if path == "/api/v1/summarize":
                workdays = None
                if isinstance(body, dict):
                    if isinstance(body.get("workdays"), list):
                        workdays = [str(x) for x in body["workdays"] if x]
                    elif body.get("workday"):
                        workdays = [str(body["workday"])]
                fallback = bool(body.get("allow_fallback")) if isinstance(body, dict) and "allow_fallback" in body else None
                res = state.run_summarize(workdays=workdays, allow_fallback=fallback)
                status = 200 if res.get("status") != "error" else 500
                self._send_json(status, res)
                return

            if path == "/api/v1/rebuild-index":
                with state._write_lock:
                    res = state.rebuild_index_locked()
                self._send_json(200, {"ok": True, "memory_index": res})
                return

            self._send_json(404, {"error": "not_found", "path": path})

    return Handler


class DualStackThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], RequestHandlerClass: type[BaseHTTPRequestHandler]) -> None:
        host, _ = server_address
        if ":" in host:
            self.address_family = socket.AF_INET6
        super().__init__(server_address, RequestHandlerClass)

    def server_bind(self) -> None:
        if self.address_family == socket.AF_INET6:
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except OSError:
                pass
        super().server_bind()


def resolve_tls_files(cert_file: str = "", key_file: str = "", cert_dir: str = "") -> tuple[str, str]:
    """Locate TLS cert & key either explicitly or from the newest subdir of a fnOS SSL folder."""
    if cert_file and key_file and os.path.exists(cert_file) and os.path.exists(key_file):
        return cert_file, key_file
    if cert_dir and os.path.isdir(cert_dir):
        candidates: list[tuple[float, str, str]] = []
        for root, _, files in os.walk(cert_dir):
            crt = next((os.path.join(root, f) for f in files if f in ("fullchain.crt", "fullchain.pem")), "")
            if not crt:
                crt = next((os.path.join(root, f) for f in files if f.endswith((".crt", ".pem")) and "issuer" not in f), "")
            key = next((os.path.join(root, f) for f in files if f.endswith(".key")), "")
            if crt and key:
                candidates.append((os.path.getmtime(crt), crt, key))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1], candidates[0][2]
    return "", ""


def create_server(
    host: str,
    port: int,
    state: HubState,
    *,
    tls_cert: str = "",
    tls_key: str = "",
) -> ThreadingHTTPServer:
    server = DualStackThreadingHTTPServer((host, port), make_handler(state))
    server.daemon_threads = True
    if tls_cert and tls_key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=tls_cert, keyfile=tls_key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Daily Memory Hub server (REST + Remote MCP + Auto Summarizer).")
    parser.add_argument("--host", default=os.environ.get("DAILY_MEMORY_HOST", "::"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("DAILY_MEMORY_PORT", "8765")))
    parser.add_argument("--tls-port", type=int, default=int(os.environ.get("DAILY_MEMORY_TLS_PORT", "0")))
    parser.add_argument("--tls-cert", default=os.environ.get("DAILY_MEMORY_TLS_CERT", ""))
    parser.add_argument("--tls-key", default=os.environ.get("DAILY_MEMORY_TLS_KEY", ""))
    parser.add_argument("--tls-dir", default=os.environ.get("DAILY_MEMORY_TLS_DIR", ""))
    parser.add_argument("--storage-root", default=os.environ.get("DAILY_MEMORY_STORAGE_ROOT", "/srv/daily-memory"))
    args = parser.parse_args()

    fallback_token = os.environ.get("DAILY_MEMORY_TOKEN", "")
    state = HubState(
        storage_root=args.storage_root,
        read_token=os.environ.get("DAILY_MEMORY_READ_TOKEN", "") or fallback_token,
        write_token=os.environ.get("DAILY_MEMORY_WRITE_TOKEN", "") or fallback_token,
        timezone_name=os.environ.get("TZ", "Asia/Shanghai"),
        day_start_hour=int(os.environ.get("DAY_START_HOUR", "5")),
        monthly_folders=os.environ.get("MONTHLY_FOLDERS", "true").lower() in ("1", "true", "yes"),
        rate_limit_per_min=int(os.environ.get("RATE_LIMIT_PER_MIN", "180")),
        enable_auto_summarize=os.environ.get("ENABLE_AUTO_SUMMARIZE", "true").lower() in ("1", "true", "yes"),
        summarize_hour=int(os.environ.get("SUMMARIZE_CRON_HOUR", "9")),
        summarize_minute=int(os.environ.get("SUMMARIZE_CRON_MINUTE", "5")),
        llm_base_url=os.environ.get("LLM_BASE_URL", ""),
        llm_api_key=os.environ.get("LLM_API_KEY", ""),
        llm_model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        allow_fallback_summarize=os.environ.get("ALLOW_FALLBACK_SUMMARIZE", "false").lower() in ("1", "true", "yes"),
        push_resummarize_debounce_sec=float(os.environ.get("SUMMARIZE_PUSH_DEBOUNCE_SEC", "120")),
    )
    state.start_scheduler()
    try:
        server = create_server(args.host, args.port, state)
    except OSError:
        server = create_server("0.0.0.0", args.port, state)

    tls_server: ThreadingHTTPServer | None = None
    if args.tls_port > 0:
        cert_path, key_path = resolve_tls_files(args.tls_cert, args.tls_key, args.tls_dir)
        if cert_path and key_path:
            try:
                tls_server = create_server(args.host, args.tls_port, state, tls_cert=cert_path, tls_key=key_path)
            except OSError:
                tls_server = create_server("0.0.0.0", args.tls_port, state, tls_cert=cert_path, tls_key=key_path)
            threading.Thread(target=tls_server.serve_forever, name="daily-memory-https", daemon=True).start()
            print(f"Daily Memory Hub HTTPS listening on [{args.host}]:{args.tls_port} (cert={cert_path})", flush=True)
        else:
            print(f"WARNING: DAILY_MEMORY_TLS_PORT={args.tls_port} set but no cert/key found.", file=sys.stderr, flush=True)

    print(f"Daily Memory Hub listening on http://[{args.host}]:{args.port} (storage={state.storage_root})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.stop_scheduler()
        if tls_server is not None:
            tls_server.shutdown()
            tls_server.server_close()
        server.server_close()


if __name__ == "__main__":
    main()
