#!/usr/bin/env python3
"""Multi-machine sharded storage, file locking, and conflict-free merge utilities."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import glob
import json
import os
import re
import socket
import tempfile
import threading
from typing import Any, Iterator

try:
    import fcntl  # type: ignore[attr-defined]
except ImportError:
    fcntl = None  # Windows fallback uses in-process threading lock


_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


def _get_thread_lock(path: str) -> threading.Lock:
    key = os.path.abspath(path)
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def file_lock(lock_path: str) -> Iterator[None]:
    """Process-safe and thread-safe exclusive lock for atomic data mutations."""
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    tlock = _get_thread_lock(lock_path)
    with tlock:
        if fcntl is None:
            yield
            return
        with open(lock_path, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def normalize_machine_id(raw: str | None = None) -> str:
    candidate = (raw or "").strip()
    if not candidate:
        candidate = (os.environ.get("DAILY_MEMORY_MACHINE_ID") or "").strip()
    if not candidate:
        candidate = socket.gethostname().split(".")[0].strip()
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", candidate).strip("-_").lower()
    return (cleaned or "default")[:48]


def atomic_write_text(path: str, content: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def atomic_write_json(path: str, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def _span_point_to_minutes(token: str) -> int | None:
    m = re.match(r"^(\d{1,2}):(\d{2})(\(\+1\))?$", (token or "").strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    extra = 1440 if m.group(3) else 0
    return extra + hh * 60 + mm


def _minutes_to_span_point(minutes: int) -> str:
    suffix = ""
    if minutes >= 1440:
        minutes -= 1440
        suffix = "(+1)"
    return f"{minutes // 60:02d}:{minutes % 60:02d}{suffix}"


def merge_active_spans(spans: list[str]) -> str:
    points: list[int] = []
    for sp in spans:
        s = (sp or "").strip()
        if not s or s == "-":
            continue
        if "-" in s:
            left, right = s.split("-", 1)
            for part in (left, right):
                val = _span_point_to_minutes(part)
                if val is not None:
                    points.append(val)
        else:
            val = _span_point_to_minutes(s)
            if val is not None:
                points.append(val)
    if not points:
        return ""
    lo, hi = min(points), max(points)
    return _minutes_to_span_point(lo) if lo == hi else f"{_minutes_to_span_point(lo)}-{_minutes_to_span_point(hi)}"


def tag_items_with_machine(items: list[dict[str, Any]], machine_id: str) -> list[dict[str, Any]]:
    tagged: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        copy = dict(it)
        if not copy.get("machine"):
            copy["machine"] = machine_id
        tagged.append(copy)
    return tagged


def merge_raw_payloads(workday: str, machine_payloads: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Merge per-machine raw payloads for the same workday without losing any device's traces."""
    machines = sorted(machine_payloads.keys())
    weekday = ""
    window_local = ""
    existing_projects: list[dict[str, Any]] = []
    seen_proj_keys: set[tuple[str, str]] = set()

    buckets: dict[str, list[dict[str, Any]]] = {
        "antigravity": [],
        "codex": [],
        "claude": [],
        "workbuddy": [],
        "git": [],
        "files": [],
        "live_notes": [],
    }
    seen_bucket_keys: dict[str, set[str]] = {k: set() for k in buckets}

    spans: list[str] = []
    uploaded_times: list[str] = []
    overall_hours = 0.0
    ag_hours = 0.0
    cx_hours = 0.0
    claude_hours = 0.0
    wb_hours = 0.0

    for mid in machines:
        p = machine_payloads[mid] or {}
        if p.get("uploaded_at"):
            uploaded_times.append(str(p["uploaded_at"]))
        if not weekday and p.get("weekday"):
            weekday = str(p["weekday"])
        if not window_local and p.get("window_local"):
            window_local = str(p["window_local"])
        if p.get("overall_active_span"):
            spans.append(str(p["overall_active_span"]))
        overall_hours += float(p.get("overall_estimated_hours") or 0.0)
        ag_hours += float(p.get("antigravity_hours") or 0.0)
        cx_hours += float(p.get("codex_hours") or 0.0)
        claude_hours += float(p.get("claude_hours") or 0.0)
        wb_hours += float(p.get("workbuddy_hours") or 0.0)

        for ep in p.get("existing_projects") or []:
            if not isinstance(ep, dict):
                continue
            pk = (str(ep.get("scope") or ""), str(ep.get("project") or ""))
            if pk[1] and pk not in seen_proj_keys:
                seen_proj_keys.add(pk)
                existing_projects.append(ep)

        for key in ("antigravity", "codex", "claude", "workbuddy"):
            for it in p.get(key) or []:
                if not isinstance(it, dict):
                    continue
                item = dict(it)
                item["machine"] = str(item.get("machine") or mid)
                dedup = f"{item['machine']}::{item.get('id') or item.get('title') or ''}::{item.get('active_span') or ''}"
                if dedup in seen_bucket_keys[key]:
                    continue
                seen_bucket_keys[key].add(dedup)
                buckets[key].append(item)

        for it in p.get("git") or []:
            if not isinstance(it, dict):
                continue
            item = dict(it)
            item["machine"] = str(item.get("machine") or mid)
            dedup = f"{item['machine']}::{item.get('repo') or item.get('path') or json.dumps(item, sort_keys=True, ensure_ascii=False)}"
            if dedup in seen_bucket_keys["git"]:
                continue
            seen_bucket_keys["git"].add(dedup)
            buckets["git"].append(item)

        for it in p.get("files") or []:
            if not isinstance(it, dict):
                continue
            item = dict(it)
            item["machine"] = str(item.get("machine") or mid)
            dedup = f"{item['machine']}::{item.get('group') or item.get('dir') or item.get('path') or json.dumps(item, sort_keys=True, ensure_ascii=False)}"
            if dedup in seen_bucket_keys["files"]:
                continue
            seen_bucket_keys["files"].add(dedup)
            buckets["files"].append(item)

        for it in p.get("live_notes") or []:
            if not isinstance(it, dict):
                continue
            item = dict(it)
            item["machine"] = str(item.get("machine") or mid)
            dedup = f"{item.get('time') or ''}::{item.get('project') or ''}::{item.get('text') or ''}"
            if not item.get("text") or dedup in seen_bucket_keys["live_notes"]:
                continue
            seen_bucket_keys["live_notes"].add(dedup)
            buckets["live_notes"].append(item)

    for key in ("antigravity", "codex", "claude", "workbuddy"):
        buckets[key].sort(key=lambda x: (str(x.get("active_span") or ""), str(x.get("machine") or "")))
    buckets["live_notes"].sort(key=lambda x: (str(x.get("created_at") or ""), str(x.get("time") or "")))

    merged_out: dict[str, Any] = {
        "workday": workday,
        "weekday": weekday,
        "window_local": window_local,
        "machines": machines,
        "overall_active_span": merge_active_spans(spans),
        "overall_estimated_hours": round(overall_hours, 2),
        "antigravity_hours": round(ag_hours, 2),
        "codex_hours": round(cx_hours, 2),
        "claude_hours": round(claude_hours, 2),
        "workbuddy_hours": round(wb_hours, 2),
        "existing_projects": existing_projects[:80],
        "counts": {
            "antigravity_sessions": len(buckets["antigravity"]),
            "codex_sessions": len(buckets["codex"]),
            "claude_sessions": len(buckets["claude"]),
            "workbuddy_sessions": len(buckets["workbuddy"]),
            "git_repos": len(buckets["git"]),
            "file_groups": len(buckets["files"]),
            "live_notes": len(buckets["live_notes"]),
        },
        "antigravity": buckets["antigravity"],
        "codex": buckets["codex"],
        "claude": buckets["claude"],
        "workbuddy": buckets["workbuddy"],
        "git": buckets["git"],
        "files": buckets["files"],
        "live_notes": buckets["live_notes"],
    }
    if uploaded_times:
        merged_out["latest_uploaded_at"] = max(uploaded_times)
    return merged_out


def merge_summary_payloads(workday: str, machine_summaries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Merge per-machine summary JSONs for the same workday into one canonical day object."""
    machines = sorted(machine_summaries.keys())
    weekday = ""
    headlines: list[str] = []
    core_outputs: list[str] = []
    spans: list[str] = []

    ag_h = cx_h = wb_h = est_h = work_h = pers_h = 0.0
    work_items: list[dict[str, Any]] = []
    life_items: list[dict[str, Any]] = []
    seen_work: set[str] = set()
    seen_life: set[str] = set()
    project_briefs: dict[str, dict[str, str]] = {}

    for mid in machines:
        d = machine_summaries[mid] or {}
        if not weekday and d.get("weekday"):
            weekday = str(d["weekday"])
        hl = str(d.get("headline") or "").strip()
        if hl and hl not in headlines:
            headlines.append(hl)
        co = str(d.get("core_outputs") or "").strip()
        if co and co != "-" and co not in core_outputs:
            core_outputs.append(co)
        if d.get("active_span"):
            spans.append(str(d["active_span"]))

        ag_h += float(d.get("antigravity_hours") or 0.0)
        cx_h += float(d.get("codex_hours") or 0.0)
        wb_h += float(d.get("workbuddy_hours") or 0.0)
        est_h += float(d.get("estimated_hours") or 0.0)
        work_h += float(d.get("work_estimated_hours") or 0.0)
        pers_h += float(d.get("personal_estimated_hours") or 0.0)

        for it in d.get("work_items") or []:
            if not isinstance(it, dict):
                continue
            item = dict(it)
            if mid not in ("__nas__", "__merged__") and not item.get("machine"):
                item["machine"] = mid
            key = "||".join(
                [
                    str(item.get("machine") or ""),
                    str(item.get("project") or ""),
                    str(item.get("tool") or ""),
                    str(item.get("session_name") or ""),
                    str(item.get("active_span") or ""),
                ]
            )
            if key in seen_work:
                continue
            seen_work.add(key)
            work_items.append(item)

        for it in d.get("life_items") or []:
            if not isinstance(it, dict):
                continue
            item = dict(it)
            if mid not in ("__nas__", "__merged__") and not item.get("machine"):
                item["machine"] = mid
            key = "||".join(
                [
                    str(item.get("machine") or ""),
                    str(item.get("tool") or ""),
                    str(item.get("session_name") or ""),
                    str(item.get("topic") or ""),
                ]
            )
            if key in seen_life:
                continue
            seen_life.add(key)
            life_items.append(item)

        pb = d.get("project_briefs")
        if isinstance(pb, dict):
            for proj, brief in pb.items():
                if not isinstance(brief, dict):
                    continue
                target = project_briefs.setdefault(str(proj), {})
                for bk, bv in brief.items():
                    text = str(bv or "").strip()
                    if not text or text == "-":
                        continue
                    existing = target.get(bk, "")
                    if not existing:
                        target[bk] = text
                    elif text not in existing:
                        target[bk] = f"{existing}；{text}"

    effective_machines = sorted(
        {
            str(m)
            for m in (
                [mid for mid in machines if mid not in ("__nas__", "__merged__")]
                + [str(it.get("machine")) for it in work_items if it.get("machine")]
                + [str(it.get("machine")) for it in life_items if it.get("machine")]
            )
            if m
        }
    )

    merged: dict[str, Any] = {
        "workday": workday,
        "weekday": weekday,
        "headline": " / ".join(headlines),
        "active_span": merge_active_spans(spans) or "-",
        "antigravity_hours": round(ag_h, 2),
        "codex_hours": round(cx_h, 2),
        "workbuddy_hours": round(wb_h, 2),
        "estimated_hours": round(est_h, 2),
        "work_estimated_hours": round(work_h, 2),
        "personal_estimated_hours": round(pers_h, 2),
        "core_outputs": "；".join(core_outputs) if core_outputs else "-",
        "work_items": work_items,
        "life_items": life_items,
        "project_briefs": project_briefs,
    }
    if effective_machines:
        merged["machines"] = effective_machines
    return merged


def _machine_raw_path(data_dir: str, machine_id: str, workday: str) -> str:
    return os.path.join(data_dir, "machines", machine_id, "raw", workday[:7], f"raw_{workday}.json")


def _machine_summary_path(data_dir: str, machine_id: str, workday: str) -> str:
    return os.path.join(data_dir, "machines", machine_id, "summary", workday[:7], f"summary_{workday}.json")


def _merged_raw_path(data_dir: str, workday: str) -> str:
    return os.path.join(data_dir, "raw", workday[:7], f"raw_{workday}.json")


def save_machine_raw(data_dir: str, machine_id: str, raw_payload: dict[str, Any]) -> dict[str, Any]:
    """Save one machine's raw payload under machines/<id>/raw/ and rebuild merged raw/<YYYY-MM>/raw_<day>.json."""
    mid = normalize_machine_id(machine_id)
    workday = str(raw_payload.get("workday") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", workday):
        raise ValueError(f"Invalid workday in raw payload: {workday!r}")

    stamped = dict(raw_payload)
    stamped["machine"] = mid
    stamped["uploaded_at"] = datetime.now(timezone.utc).isoformat()
    for key in ("antigravity", "codex", "claude", "workbuddy", "git", "files", "live_notes"):
        if isinstance(stamped.get(key), list):
            stamped[key] = tag_items_with_machine(stamped[key], mid)

    lock_path = os.path.join(data_dir, ".daily_memory.lock")
    with file_lock(lock_path):
        shard_path = _machine_raw_path(data_dir, mid, workday)
        atomic_write_json(shard_path, stamped)

        # Gather all machine shards for this workday
        by_machine: dict[str, dict[str, Any]] = {}
        pattern = os.path.join(data_dir, "machines", "*", "raw", workday[:7], f"raw_{workday}.json")
        for fp in sorted(glob.glob(pattern)):
            rel = os.path.relpath(fp, os.path.join(data_dir, "machines"))
            shard_mid = rel.split(os.sep)[0]
            with open(fp, "r", encoding="utf-8") as fh:
                by_machine[shard_mid] = json.load(fh)

        # Persist any legacy pre-migration raw file as a dedicated machine shard so later uploads never drop it
        merged_path = _merged_raw_path(data_dir, workday)
        if os.path.exists(merged_path):
            try:
                with open(merged_path, "r", encoding="utf-8") as fh:
                    existing = json.load(fh)
                if isinstance(existing, dict) and not existing.get("machines"):
                    legacy_mid = normalize_machine_id(existing.get("machine") or "legacy")
                    if legacy_mid not in by_machine and legacy_mid != mid:
                        legacy_stamped = dict(existing)
                        legacy_stamped["machine"] = legacy_mid
                        for key in ("antigravity", "codex", "claude", "workbuddy", "git", "files", "live_notes"):
                            if isinstance(legacy_stamped.get(key), list):
                                legacy_stamped[key] = tag_items_with_machine(legacy_stamped[key], legacy_mid)
                        atomic_write_json(_machine_raw_path(data_dir, legacy_mid, workday), legacy_stamped)
                        by_machine[legacy_mid] = legacy_stamped
            except Exception:
                pass

        merged = merge_raw_payloads(workday, by_machine)
        atomic_write_json(merged_path, merged)
        return {
            "workday": workday,
            "machine_id": mid,
            "machines": merged["machines"],
            "counts": merged["counts"],
            "merged_raw_path": merged_path,
        }


def save_machine_summary(data_dir: str, machine_id: str, summary_payload: dict[str, Any]) -> dict[str, Any]:
    """Save one machine's (or NAS-merged) summary shard and return the merged canonical day summary."""
    mid = "__nas__" if machine_id in ("__nas__", "__merged__") else normalize_machine_id(machine_id)
    workday = str(summary_payload.get("workday") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", workday):
        raise ValueError(f"Invalid workday in summary payload: {workday!r}")

    stamped = dict(summary_payload)
    if mid != "__nas__":
        stamped["machine"] = mid
        if isinstance(stamped.get("work_items"), list):
            stamped["work_items"] = tag_items_with_machine(stamped["work_items"], mid)
        if isinstance(stamped.get("life_items"), list):
            stamped["life_items"] = tag_items_with_machine(stamped["life_items"], mid)
    stamped["updated_at"] = datetime.now(timezone.utc).isoformat()

    lock_path = os.path.join(data_dir, ".daily_memory.lock")
    with file_lock(lock_path):
        shard_path = _machine_summary_path(data_dir, mid, workday)
        atomic_write_json(shard_path, stamped)

        if mid == "__nas__":
            # NAS LLM full-day fusion already covers all machines' raw shards for this workday.
            return stamped

        by_machine: dict[str, dict[str, Any]] = {}
        pattern = os.path.join(data_dir, "machines", "*", "summary", workday[:7], f"summary_{workday}.json")
        for fp in sorted(glob.glob(pattern)):
            rel = os.path.relpath(fp, os.path.join(data_dir, "machines"))
            shard_mid = rel.split(os.sep)[0]
            if shard_mid == "__nas__":
                continue
            with open(fp, "r", encoding="utf-8") as fh:
                by_machine[shard_mid] = json.load(fh)
        return merge_summary_payloads(workday, by_machine)


def list_machines(data_dir: str) -> list[dict[str, Any]]:
    """Summarize all machines that have uploaded raw, summary, or live WAL shards."""
    machines_root = os.path.join(data_dir, "machines")
    live_root = os.path.join(data_dir, "live", "machines")
    names: set[str] = set()
    for root in (machines_root, live_root):
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            if name.startswith(".") or name in ("__nas__", "__merged__"):
                continue
            if os.path.isdir(os.path.join(root, name)):
                names.add(name)
    out: list[dict[str, Any]] = []
    for name in sorted(names):
        mdir = os.path.join(machines_root, name)
        ldir = os.path.join(live_root, name)
        raw_files = sorted(glob.glob(os.path.join(mdir, "raw", "*", "raw_*.json"))) if os.path.isdir(mdir) else []
        sum_files = sorted(glob.glob(os.path.join(mdir, "summary", "*", "summary_*.json"))) if os.path.isdir(mdir) else []
        live_files = sorted(glob.glob(os.path.join(ldir, "live_*.json"))) if os.path.isdir(ldir) else []
        raw_dates = [os.path.basename(p)[4:-5] for p in raw_files]
        sum_dates = [os.path.basename(p)[8:-5] for p in sum_files]
        live_dates = [os.path.basename(p)[5:-5] for p in live_files]
        out.append(
            {
                "machine_id": name,
                "raw_days": len(raw_dates),
                "latest_raw_date": raw_dates[-1] if raw_dates else "",
                "summary_days": len(sum_dates),
                "latest_summary_date": sum_dates[-1] if sum_dates else "",
                "live_days": len(live_dates),
                "latest_live_date": live_dates[-1] if live_dates else "",
            }
        )
    return out


def _machine_live_path(data_dir: str, machine_id: str, workday: str) -> str:
    return os.path.join(data_dir, "live", "machines", machine_id, f"live_{workday}.json")


def merge_live_payloads(workday: str, machine_live: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Merge per-machine same-day live WALs (notes, sessions, git) preserving source machine tags."""
    machines = sorted(machine_live.keys())
    notes_by_key: dict[str, dict[str, Any]] = {}
    sessions_by_key: dict[str, dict[str, Any]] = {}
    git_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    latest_updated = ""

    for mid in machines:
        d = machine_live[mid] or {}
        upd = str(d.get("updated_at") or "")
        if upd > latest_updated:
            latest_updated = upd
        for n in d.get("notes") or []:
            if not isinstance(n, dict):
                continue
            item = dict(n)
            if mid != "__local__" and not item.get("machine"):
                item["machine"] = mid
            nk = f"{item.get('project_id') or item.get('project') or ''}|{str(item.get('text') or '').strip()}"
            prev = notes_by_key.get(nk)
            if prev is None or str(item.get("created_at") or "") >= str(prev.get("created_at") or ""):
                notes_by_key[nk] = item
        for s in d.get("sessions") or []:
            if not isinstance(s, dict):
                continue
            item = dict(s)
            if mid != "__local__" and not item.get("machine"):
                item["machine"] = mid
            sk = f"{item.get('machine') or ''}|{item.get('key') or item.get('id') or ''}"
            prev = sessions_by_key.get(sk)
            if prev is None or str(item.get("updated_at") or "") >= str(prev.get("updated_at") or ""):
                sessions_by_key[sk] = item
        for g in d.get("git") or []:
            if not isinstance(g, dict):
                continue
            item = dict(g)
            if mid != "__local__" and not item.get("machine"):
                item["machine"] = mid
            gk = (str(item.get("machine") or ""), str(item.get("repo") or item.get("repo_name") or ""), str(item.get("hash") or ""))
            git_by_key[gk] = item

    notes = sorted(notes_by_key.values(), key=lambda x: str(x.get("created_at") or ""), reverse=True)[:50]
    sessions = sorted(sessions_by_key.values(), key=lambda x: str(x.get("updated_at") or ""), reverse=True)[:32]
    git_items = sorted(git_by_key.values(), key=lambda x: (str(x.get("committed_at") or ""), str(x.get("time") or "")), reverse=True)[:24]
    effective_machines = sorted(m for m in machines if m != "__local__")
    out: dict[str, Any] = {
        "workday": workday,
        "updated_at": latest_updated or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "notes": notes,
        "sessions": sessions,
        "git": git_items,
    }
    if effective_machines:
        out["machines"] = effective_machines
    return out


def save_machine_live(data_dir: str, machine_id: str, live_payload: dict[str, Any]) -> dict[str, Any]:
    """Save one machine's same-day live WAL shard and rebuild merged live/<workday> view."""
    mid = normalize_machine_id(machine_id)
    workday = str(live_payload.get("workday") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", workday):
        raise ValueError(f"Invalid workday in live payload: {workday!r}")

    stamped = {
        "workday": workday,
        "machine": mid,
        "updated_at": str(live_payload.get("updated_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")),
        "notes": tag_items_with_machine(list(live_payload.get("notes") or []), mid),
        "sessions": tag_items_with_machine(list(live_payload.get("sessions") or []), mid),
        "git": tag_items_with_machine(list(live_payload.get("git") or []), mid),
    }

    lock_path = os.path.join(data_dir, "live", ".live.lock")
    with file_lock(lock_path):
        shard_path = _machine_live_path(data_dir, mid, workday)
        atomic_write_json(shard_path, stamped)
        merged = load_merged_live_day(data_dir, workday)
        return {
            "workday": workday,
            "machine_id": mid,
            "machines": merged.get("machines") or [mid],
            "notes_count": len(merged.get("notes") or []),
            "sessions_count": len(merged.get("sessions") or []),
            "git_count": len(merged.get("git") or []),
        }


def load_merged_live_day(data_dir: str, workday: str) -> dict[str, Any]:
    """Merge local live_<day>.json (if any) and all machine shards for a workday."""
    by_machine: dict[str, dict[str, Any]] = {}
    local_path = os.path.join(data_dir, "live", f"live_{workday}.json")
    if os.path.exists(local_path):
        try:
            with open(local_path, "r", encoding="utf-8") as fh:
                obj = json.load(fh)
            if isinstance(obj, dict):
                by_machine["__local__"] = obj
        except Exception:
            pass
    pattern = os.path.join(data_dir, "live", "machines", "*", f"live_{workday}.json")
    for fp in sorted(glob.glob(pattern)):
        mid = os.path.basename(os.path.dirname(fp))
        try:
            with open(fp, "r", encoding="utf-8") as fh:
                obj = json.load(fh)
            if isinstance(obj, dict):
                by_machine[mid] = obj
        except Exception:
            continue
    if not by_machine:
        return {"workday": workday, "updated_at": "", "notes": [], "sessions": [], "git": []}
    return merge_live_payloads(workday, by_machine)


def load_merged_live_days(
    data_dir: str,
    archived_workdays: set[str] | None = None,
    max_days: int = 2,
) -> list[dict[str, Any]]:
    """Return uncompacted merged live days across local + all machine shards, newest first."""
    archived = archived_workdays or set()
    workdays: set[str] = set()
    for pat in (
        os.path.join(data_dir, "live", "live_*.json"),
        os.path.join(data_dir, "live", "machines", "*", "live_*.json"),
    ):
        for fp in glob.glob(pat):
            m = re.match(r"^live_(\d{4}-\d{2}-\d{2})\.json$", os.path.basename(fp))
            if m and m.group(1) not in archived:
                workdays.add(m.group(1))
    results: list[dict[str, Any]] = []
    for wd in sorted(workdays, reverse=True):
        day = load_merged_live_day(data_dir, wd)
        if day.get("notes") or day.get("sessions") or day.get("git"):
            results.append(day)
        if len(results) >= max_days:
            break
    return results


def load_archived_live_note_days(
    data_dir: str,
    archived_workdays: set[str] | None = None,
    max_days: int = 365,
) -> list[dict[str, Any]]:
    """Return note-only live day records for archived workdays so --note entries remain searchable after compaction."""
    archived = archived_workdays or set()
    if not archived:
        return []
    workdays: set[str] = set()
    for pat in (
        os.path.join(data_dir, "live", "live_*.json"),
        os.path.join(data_dir, "live", "machines", "*", "live_*.json"),
    ):
        for fp in glob.glob(pat):
            m = re.match(r"^live_(\d{4}-\d{2}-\d{2})\.json$", os.path.basename(fp))
            if m and m.group(1) in archived:
                workdays.add(m.group(1))
    results: list[dict[str, Any]] = []
    for wd in sorted(workdays, reverse=True):
        day = load_merged_live_day(data_dir, wd)
        notes = list(day.get("notes") or [])
        if notes:
            results.append(
                {
                    "workday": wd,
                    "updated_at": day.get("updated_at") or "",
                    "machines": day.get("machines") or [],
                    "notes": notes,
                    "sessions": [],
                    "git": [],
                }
            )
        if len(results) >= max_days:
            break
    return results


def list_raw_workdays(data_dir: str) -> list[str]:
    """Return sorted workdays that have merged raw JSON files."""
    files = glob.glob(os.path.join(data_dir, "raw", "*", "raw_*.json"))
    dates = set()
    for fp in files:
        base = os.path.basename(fp)
        if base.startswith("raw_") and base.endswith(".json"):
            wd = base[4:-5]
            if re.match(r"^\d{4}-\d{2}-\d{2}$", wd):
                dates.add(wd)
    return sorted(dates)
