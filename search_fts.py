#!/usr/bin/env python3
"""Zero-dependency SQLite FTS5 (unicode61 + trigram + BM25 + RRF) derived search index.

Source of truth always remains pure JSON/Markdown files (`summary_all_history.json`,
`decisions.json`, `live_*.json`, and client session transcripts).
`<data_dir>/search_index.db` is purely a derived acceleration index that can be
deleted and rebuilt at any time with zero data loss.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import glob
import json
import math
import os
import re
import sqlite3
import time
from typing import Any
from zoneinfo import ZoneInfo

from multi_machine import file_lock
from project_identity import ProjectCatalog, load_registry
import redact

INDEX_DB_NAME = "search_index.db"
INDEX_LOCK_NAME = ".search_index.lock"
SCHEMA_VERSION = "2"
DEFAULT_SESSION_DAYS = 30
MAX_CHUNKS_PER_SESSION = 160
MAX_CHUNK_CHARS = 1200


class VectorProvider:
    """Optional extension seam for future semantic embeddings (unused by default)."""

    def available(self) -> bool:
        return False

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        return None


class NullVectorProvider(VectorProvider):
    """Default zero-dependency provider."""


_DEFAULT_VECTOR_PROVIDER: VectorProvider = NullVectorProvider()


def set_vector_provider(provider: VectorProvider | None) -> None:
    global _DEFAULT_VECTOR_PROVIDER
    _DEFAULT_VECTOR_PROVIDER = provider if provider is not None else NullVectorProvider()


def index_db_path(data_dir: str) -> str:
    return os.path.join(data_dir, INDEX_DB_NAME)


def _fts_quote(term: str) -> str:
    cleaned = term.replace('"', '""').strip()
    return f'"{cleaned}"' if cleaned else ""


def _tokenize_query(query: str) -> list[str]:
    raw = [t.strip() for t in re.split(r"\s+", str(query or "").strip()) if t.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for t in raw:
        low = t.lower()
        if low not in seen:
            seen.add(low)
            out.append(t)
    return out[:12]


def _has_trigram(conn: sqlite3.Connection) -> bool:
    try:
        probe = sqlite3.connect(":memory:")
        try:
            probe.execute("CREATE VIRTUAL TABLE _fts_tri_probe USING fts5(x, tokenize='trigram')")
            return True
        finally:
            probe.close()
    except sqlite3.OperationalError:
        return False


def _connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_schema(conn: sqlite3.Connection) -> bool:
    trigram_ok = _has_trigram(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS docs (
            doc_id TEXT PRIMARY KEY,
            doc_type TEXT NOT NULL,
            workday TEXT DEFAULT '',
            scope TEXT DEFAULT '',
            project_id TEXT DEFAULT '',
            project TEXT DEFAULT '',
            status TEXT DEFAULT '',
            machine TEXT DEFAULT '',
            client TEXT DEFAULT '',
            session_id TEXT DEFAULT '',
            path TEXT DEFAULT '',
            line_num INTEGER DEFAULT 0,
            title TEXT DEFAULT '',
            body TEXT DEFAULT '',
            payload_json TEXT NOT NULL,
            updated_at TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_docs_type ON docs(doc_type);
        CREATE INDEX IF NOT EXISTS idx_docs_workday ON docs(workday);
        CREATE INDEX IF NOT EXISTS idx_docs_project ON docs(project_id);
        CREATE INDEX IF NOT EXISTS idx_docs_session ON docs(client, session_id);

        CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts_u61 USING fts5(
            title, body, project,
            content='docs',
            content_rowid='rowid',
            tokenize='unicode61 remove_diacritics 2'
        );

        CREATE TABLE IF NOT EXISTS session_watermarks (
            file_path TEXT PRIMARY KEY,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL,
            indexed_at TEXT NOT NULL
        );
        """
    )
    if trigram_ok:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts_tri USING fts5(
                title, body, project,
                content='docs',
                content_rowid='rowid',
                tokenize='trigram'
            )
            """
        )
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)", (SCHEMA_VERSION,))
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('trigram_enabled', ?)", ("1" if trigram_ok else "0",))
    conn.commit()
    return trigram_ok


def _open_or_reset(data_dir: str) -> tuple[sqlite3.Connection, bool]:
    db_path = index_db_path(data_dir)
    try:
        conn = _connect(db_path)
        row = None
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except sqlite3.DatabaseError:
            row = None
        if row and row["value"] == SCHEMA_VERSION:
            tri_row = conn.execute("SELECT value FROM meta WHERE key='trigram_enabled'").fetchone()
            return conn, (tri_row is not None and tri_row["value"] == "1")
        trigram_ok = _init_schema(conn)
        return conn, trigram_ok
    except sqlite3.DatabaseError:
        for ext in ("", "-wal", "-shm"):
            p = db_path + ext
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        conn = _connect(db_path)
        trigram_ok = _init_schema(conn)
        return conn, trigram_ok


def _rebuild_fts_tables(conn: sqlite3.Connection, trigram_ok: bool) -> None:
    conn.execute("INSERT INTO docs_fts_u61(docs_fts_u61) VALUES('rebuild')")
    if trigram_ok:
        conn.execute("INSERT INTO docs_fts_tri(docs_fts_tri) VALUES('rebuild')")


def _structured_fingerprint(data_dir: str) -> str:
    parts: list[str] = []
    candidates = [
        os.path.join(data_dir, "summary_all_history.json"),
        os.path.join(data_dir, "project_registry.json"),
        os.path.join(data_dir, "decisions.json"),
    ]
    candidates.extend(sorted(glob.glob(os.path.join(data_dir, "live", "live_*.json"))))
    candidates.extend(sorted(glob.glob(os.path.join(data_dir, "live", "machines", "*", "live_*.json"))))
    for fp in candidates:
        try:
            st = os.stat(fp)
            parts.append(f"{os.path.relpath(fp, data_dir)}:{int(st.st_mtime)}:{st.st_size}")
        except OSError:
            continue
    return "|".join(parts)


def _upsert_docs(conn: sqlite3.Connection, rows: list[tuple[Any, ...]]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO docs(
            doc_id, doc_type, workday, scope, project_id, project, status,
            machine, client, session_id, path, line_num, title, body, payload_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _build_live_doc_rows(live_day: dict[str, Any]) -> list[tuple[Any, ...]]:
    wd = str(live_day.get("workday") or "")
    upd = str(live_day.get("updated_at") or "")
    rows: list[tuple[Any, ...]] = []
    for idx, n in enumerate(live_day.get("notes") or []):
        if not isinstance(n, dict):
            continue
        proj = str(n.get("project") or "")
        text = str(n.get("text") or "")
        paths_s = " ".join(n.get("paths") or [])
        rows.append(
            (
                f"live_note:{wd}:{idx}:{n.get('created_at', '')}",
                "live_note",
                wd,
                "live",
                str(n.get("project_id") or ""),
                proj,
                "active",
                str(n.get("machine") or ""),
                "note",
                "",
                paths_s,
                0,
                proj,
                f"{text} {paths_s}".strip(),
                json.dumps({"kind": "live_note", "workday": wd, **n}, ensure_ascii=False),
                str(n.get("created_at") or upd),
            )
        )
    for s in live_day.get("sessions") or []:
        if not isinstance(s, dict):
            continue
        key = str(s.get("key") or s.get("id") or "")
        title = str(s.get("title") or "")
        body = " ".join(
            str(x or "")
            for x in (
                s.get("summary"),
                s.get("last_user_prompt"),
                s.get("last_assistant_summary"),
                s.get("last_input"),
                s.get("workspace"),
                " ".join(s.get("touched_files") or []),
            )
            if x
        )
        rows.append(
            (
                f"live_session:{wd}:{s.get('machine', '')}:{key}",
                "live_session",
                wd,
                "live",
                "",
                str(s.get("workspace") or ""),
                "active",
                str(s.get("machine") or ""),
                str(s.get("tool") or "").lower(),
                str(s.get("id") or ""),
                str(s.get("workspace") or ""),
                0,
                title,
                body,
                json.dumps({"kind": "live_session", "workday": wd, **s}, ensure_ascii=False),
                str(s.get("updated_at") or upd),
            )
        )
    for g in live_day.get("git") or []:
        if not isinstance(g, dict):
            continue
        repo = str(g.get("repo_name") or g.get("repo") or "")
        h = str(g.get("hash") or "")
        rows.append(
            (
                f"live_git:{wd}:{g.get('machine', '')}:{repo}:{h}",
                "live_git",
                wd,
                "live",
                "",
                repo,
                "active",
                str(g.get("machine") or ""),
                "git",
                h,
                str(g.get("repo") or ""),
                0,
                f"{repo} {h}",
                f"{g.get('subject', '')} {g.get('repo', '')}",
                json.dumps({"kind": "live_git", "workday": wd, **g}, ensure_ascii=False),
                str(g.get("committed_at") or upd),
            )
        )
    return rows


def sync_live_to_fts(data_dir: str, live_day: dict[str, Any]) -> int:
    """Fast incremental update for one live workday inside search_index.db."""
    wd = str(live_day.get("workday") or "")
    if not wd:
        return 0
    lock_path = os.path.join(data_dir, INDEX_LOCK_NAME)
    with file_lock(lock_path):
        conn, trigram_ok = _open_or_reset(data_dir)
        try:
            conn.execute(
                "DELETE FROM docs WHERE doc_type IN ('live_note', 'live_session', 'live_git') AND workday = ?",
                (wd,),
            )
            rows = _build_live_doc_rows(live_day)
            _upsert_docs(conn, rows)
            _rebuild_fts_tables(conn, trigram_ok)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('structured_fp', ?)",
                (_structured_fingerprint(data_dir),),
            )
            conn.commit()
            return len(rows)
        finally:
            conn.close()


def rebuild_structured_index(
    data_dir: str,
    *,
    days: list[dict[str, Any]] | None = None,
    catalog: ProjectCatalog | None = None,
) -> dict[str, Any]:
    """Rebuild all structured documents (work_items, life_items, live, decisions) in search_index.db."""
    import decisions as dec_mod
    import multi_machine
    import query_memory

    if days is None:
        days = query_memory.load_all_days(data_dir)
    if catalog is None:
        catalog = ProjectCatalog(days, load_registry(data_dir))
    archived = {str(d.get("workday") or "") for d in days}
    live_days = [
        *multi_machine.load_merged_live_days(data_dir, archived_workdays=archived, max_days=2),
        *multi_machine.load_archived_live_note_days(data_dir, archived_workdays=archived),
    ]
    dec_store = dec_mod.load_decisions_store(data_dir)

    rows: list[tuple[Any, ...]] = []
    for d in days:
        wd = str(d.get("workday") or "")
        for idx, it in enumerate(d.get("work_items") or []):
            if not isinstance(it, dict):
                continue
            ident = catalog.for_item(it)
            proj = ident["project"]
            aliases_s = " ".join(ident.get("aliases") or [])
            paths_s = " ".join(ident.get("paths") or [])
            title = f"{proj} {it.get('session_name', '')}".strip()
            body = " ".join(
                str(x or "")
                for x in (
                    proj,
                    aliases_s,
                    paths_s,
                    it.get("category"),
                    it.get("tool"),
                    it.get("session_name"),
                    it.get("summary"),
                    it.get("outputs"),
                    it.get("next_step"),
                )
                if x
            )
            payload = {
                "workday": wd,
                "weekday": d.get("weekday", ""),
                "scope": it.get("scope", "personal"),
                "project": proj,
                "project_id": ident["project_id"],
                "project_paths": ident.get("paths") or [],
                "category": it.get("category", ""),
                "tool": it.get("tool", ""),
                "machine": it.get("machine", ""),
                "session_name": it.get("session_name", ""),
                "summary": it.get("summary", ""),
                "outputs": it.get("outputs", ""),
                "status": it.get("status", ""),
                "next_step": it.get("next_step", ""),
                "active_span": it.get("active_span", ""),
                "sources": it.get("sources"),
            }
            rows.append(
                (
                    f"work:{wd}:{idx}",
                    "work_item",
                    wd,
                    str(it.get("scope") or "personal"),
                    ident["project_id"],
                    proj,
                    str(it.get("status") or ""),
                    str(it.get("machine") or ""),
                    str(it.get("tool") or "").lower(),
                    "",
                    paths_s,
                    0,
                    title,
                    body,
                    json.dumps(payload, ensure_ascii=False),
                    wd,
                )
            )
        for idx, it in enumerate(d.get("life_items") or []):
            if not isinstance(it, dict):
                continue
            topic = str(it.get("topic") or "")
            body = " ".join(
                str(x or "")
                for x in (topic, it.get("tool"), it.get("session_name"), it.get("summary"))
                if x
            )
            payload = {
                "workday": wd,
                "weekday": d.get("weekday", ""),
                "scope": "life",
                "tool": it.get("tool", ""),
                "machine": it.get("machine", ""),
                "session_name": it.get("session_name", ""),
                "topic": topic,
                "summary": it.get("summary", ""),
                "active_span": it.get("active_span", ""),
                "sources": it.get("sources"),
            }
            rows.append(
                (
                    f"life:{wd}:{idx}",
                    "life_item",
                    wd,
                    "life",
                    "",
                    topic,
                    "",
                    str(it.get("machine") or ""),
                    str(it.get("tool") or "").lower(),
                    "",
                    "",
                    0,
                    topic,
                    body,
                    json.dumps(payload, ensure_ascii=False),
                    wd,
                )
            )

    for ld in live_days:
        rows.extend(_build_live_doc_rows(ld))

    for dec in dec_store.get("decisions") or []:
        if not isinstance(dec, dict) or not dec.get("id"):
            continue
        tags_s = " ".join(dec.get("tags") or [])
        proj = str(dec.get("project") or "")
        rows.append(
            (
                f"dec:{dec['id']}",
                "decision",
                str((dec.get("evidence") or {}).get("workday") or str(dec.get("updated_at") or "")[:10]),
                str(dec.get("scope") or "project"),
                str(dec.get("project_id") or ""),
                proj,
                str(dec.get("status") or "active"),
                "",
                str(dec.get("kind") or "decision"),
                str(dec["id"]),
                "",
                0,
                f"{proj} {dec.get('kind', '')}".strip(),
                f"{dec.get('text', '')} {tags_s}".strip(),
                json.dumps(dec, ensure_ascii=False),
                str(dec.get("updated_at") or dec.get("created_at") or ""),
            )
        )

    lock_path = os.path.join(data_dir, INDEX_LOCK_NAME)
    with file_lock(lock_path):
        conn, trigram_ok = _open_or_reset(data_dir)
        try:
            conn.execute("DELETE FROM docs WHERE doc_type != 'session_turn'")
            _upsert_docs(conn, rows)
            _rebuild_fts_tables(conn, trigram_ok)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('structured_fp', ?)",
                (_structured_fingerprint(data_dir),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('structured_updated_at', ?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
            )
            conn.commit()
            return {
                "db_path": index_db_path(data_dir),
                "structured_docs": len(rows),
                "trigram_enabled": trigram_ok,
            }
        finally:
            conn.close()


def ensure_structured_index(
    data_dir: str,
    *,
    days: list[dict[str, Any]] | None = None,
    catalog: ProjectCatalog | None = None,
) -> bool:
    """Fast O(1) stat check; rebuild structured docs only when source files changed."""
    fp_now = _structured_fingerprint(data_dir)
    db_path = index_db_path(data_dir)
    if os.path.exists(db_path):
        try:
            conn = _connect(db_path)
            try:
                ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                fp = conn.execute("SELECT value FROM meta WHERE key='structured_fp'").fetchone()
                if ver and ver["value"] == SCHEMA_VERSION and fp and fp["value"] == fp_now:
                    return False
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            pass
    rebuild_structured_index(data_dir, days=days, catalog=catalog)
    return True


def sync_sessions_to_fts(
    data_dir: str,
    *,
    days: int = DEFAULT_SESSION_DAYS,
    clients: tuple[str, ...] = ("antigravity", "codex", "claude", "workbuddy"),
    tz_name: str = "Asia/Shanghai",
    day_start_hour: int = 5,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Incrementally index local client transcripts modified within the last N days."""
    import search_sessions

    tz = ZoneInfo(tz_name)
    cutoff_ts = time.time() - max(1, days) * 86400
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    lock_path = os.path.join(data_dir, INDEX_LOCK_NAME)
    with file_lock(lock_path):
        conn, trigram_ok = _open_or_reset(data_dir)
        try:
            existing_wm = {
                row["file_path"]: (float(row["mtime"]), int(row["size"]))
                for row in conn.execute("SELECT file_path, mtime, size FROM session_watermarks").fetchall()
            }
            updated_files = 0
            indexed_chunks = 0
            active_paths: set[str] = set()

            for client in clients:
                for sid, path_obj in search_sessions.session_files(client, set()):
                    fpath = str(path_obj)
                    try:
                        st = os.stat(fpath)
                    except OSError:
                        continue
                    if st.st_mtime < cutoff_ts:
                        continue
                    active_paths.add(fpath)
                    prev = existing_wm.get(fpath)
                    if prev and abs(prev[0] - st.st_mtime) < 1e-3 and prev[1] == st.st_size:
                        continue

                    conn.execute("DELETE FROM docs WHERE doc_type='session_turn' AND path = ?", (fpath,))
                    errors: list[str] = []
                    chunk_rows: list[tuple[Any, ...]] = []
                    for _, _, line_num, dt, kind, value in search_sessions.jsonl_records(client, {sid}, errors):
                        if len(chunk_rows) >= MAX_CHUNKS_PER_SESSION:
                            break
                        parts = [p.strip() for p in search_sessions.tool_texts(value) if isinstance(p, str) and p.strip()]
                        if not parts:
                            continue
                        raw_text = " ".join(parts)
                        if len(raw_text) > MAX_CHUNK_CHARS:
                            raw_text = raw_text[:MAX_CHUNK_CHARS]
                        clean_text = redact.redact_with_cfg(raw_text, cfg)
                        local_dt = dt.astimezone(tz) if dt else None
                        wd = (local_dt - timedelta(hours=day_start_hour)).date().isoformat() if local_dt else ""
                        ts_iso = local_dt.isoformat(timespec="seconds") if local_dt else ""
                        payload = {
                            "client": client,
                            "session_id": sid,
                            "path": fpath,
                            "position": line_num,
                            "timestamp": ts_iso,
                            "workday": wd,
                            "kind": kind,
                            "excerpt": clean_text,
                        }
                        chunk_rows.append(
                            (
                                f"sess:{client}:{sid}:{line_num}:{kind}",
                                "session_turn",
                                wd,
                                "session",
                                "",
                                sid,
                                kind,
                                "",
                                client,
                                sid,
                                fpath,
                                int(line_num),
                                f"{client} {kind} {sid[:8]}",
                                clean_text,
                                json.dumps(payload, ensure_ascii=False),
                                ts_iso,
                            )
                        )
                    _upsert_docs(conn, chunk_rows)
                    conn.execute(
                        "INSERT OR REPLACE INTO session_watermarks(file_path, mtime, size, indexed_at) VALUES(?, ?, ?, ?)",
                        (fpath, float(st.st_mtime), int(st.st_size), now_iso),
                    )
                    updated_files += 1
                    indexed_chunks += len(chunk_rows)

            # Prune expired session watermarks older than window
            stale_paths = [p for p in existing_wm if p not in active_paths]
            for sp in stale_paths:
                conn.execute("DELETE FROM docs WHERE doc_type='session_turn' AND path = ?", (sp,))
                conn.execute("DELETE FROM session_watermarks WHERE file_path = ?", (sp,))

            if updated_files > 0 or stale_paths:
                _rebuild_fts_tables(conn, trigram_ok)
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('sessions_updated_at', ?)",
                    (now_iso,),
                )
            conn.commit()
            return {
                "updated_files": updated_files,
                "indexed_chunks": indexed_chunks,
                "pruned_files": len(stale_paths),
                "window_days": days,
            }
        finally:
            conn.close()


def _filter_clauses(
    *,
    doc_types: tuple[str, ...] | None = None,
    scope: str | None = None,
    status_filter: str | None = None,
    machine_filter: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    exact_date: str | None = None,
    clients: tuple[str, ...] | None = None,
    session_ids: set[str] | None = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if doc_types:
        ph = ",".join("?" for _ in doc_types)
        clauses.append(f"d.doc_type IN ({ph})")
        params.extend(doc_types)
    if scope and scope != "all":
        clauses.append("(d.scope = ? OR d.doc_type IN ('live_note', 'live_session', 'live_git', 'decision'))")
        params.append(scope)
    if status_filter:
        clauses.append("(d.status LIKE ? OR d.doc_type != 'work_item')")
        params.append(f"%{status_filter}%")
    if machine_filter:
        clauses.append("(LOWER(d.machine) = LOWER(?) OR d.machine = '')")
        params.append(machine_filter)
    if exact_date:
        clauses.append("d.workday = ?")
        params.append(exact_date)
    else:
        if date_from:
            clauses.append("(d.workday >= ? OR d.doc_type = 'decision')")
            params.append(date_from)
        if date_to:
            clauses.append("(d.workday <= ? OR d.doc_type = 'decision')")
            params.append(date_to)
    if clients:
        ph = ",".join("?" for _ in clients)
        clauses.append(f"d.client IN ({ph})")
        params.extend(clients)
    if session_ids:
        ph = ",".join("?" for _ in session_ids)
        clauses.append(f"d.session_id IN ({ph})")
        params.extend(sorted(session_ids))
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _recency_boost(workday: str, doc_type: str, status: str) -> float:
    boost = 0.0
    if doc_type == "decision":
        boost += 0.015 if status == "active" else -0.02
    elif doc_type in ("live_note", "live_session", "live_git"):
        boost += 0.012
    if workday and re.match(r"^\d{4}-\d{2}-\d{2}$", workday):
        try:
            age_days = max(0, (datetime.now().date() - datetime.strptime(workday, "%Y-%m-%d").date()).days)
            boost += 0.008 * math.exp(-age_days / 30.0)
        except ValueError:
            pass
    return boost


def search(
    data_dir: str,
    query: str,
    *,
    doc_types: tuple[str, ...] | None = None,
    scope: str | None = None,
    status_filter: str | None = None,
    machine_filter: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    exact_date: str | None = None,
    clients: tuple[str, ...] | None = None,
    session_ids: set[str] | None = None,
    limit: int = 20,
    auto_sync_structured: bool = True,
    vector_provider: VectorProvider | None = None,
) -> list[dict[str, Any]]:
    """Hybrid FTS5 (unicode61 + trigram + short-term LIKE) + RRF ranked search."""
    terms = _tokenize_query(query)
    if not terms:
        return []
    if auto_sync_structured and (not doc_types or any(t != "session_turn" for t in doc_types)):
        ensure_structured_index(data_dir)

    db_path = index_db_path(data_dir)
    if not os.path.exists(db_path):
        return []

    where_sql, base_params = _filter_clauses(
        doc_types=doc_types,
        scope=scope,
        status_filter=status_filter,
        machine_filter=machine_filter,
        date_from=date_from,
        date_to=date_to,
        exact_date=exact_date,
        clients=clients,
        session_ids=session_ids,
    )
    pool_limit = max(60, limit * 4)
    rrf_scores: dict[str, float] = {}
    docs_by_id: dict[str, dict[str, Any]] = {}

    def _add_rank_list(rows: list[sqlite3.Row], weight: float = 1.0) -> None:
        for rank, row in enumerate(rows, 1):
            did = str(row["doc_id"])
            rrf_scores[did] = rrf_scores.get(did, 0.0) + weight / (60.0 + rank)
            if did not in docs_by_id:
                docs_by_id[did] = dict(row)

    conn = _connect(db_path)
    try:
        tri_row = conn.execute("SELECT value FROM meta WHERE key='trigram_enabled'").fetchone()
        trigram_ok = tri_row is not None and tri_row["value"] == "1"

        # Track 1: unicode61 BM25 (AND first, OR fallback)
        u61_and = " AND ".join(_fts_quote(t) for t in terms if _fts_quote(t))
        if u61_and:
            try:
                rows = conn.execute(
                    f"""
                    SELECT d.*, bm25(docs_fts_u61, 3.0, 1.0, 2.0) AS score
                    FROM docs_fts_u61 f
                    JOIN docs d ON d.rowid = f.rowid
                    WHERE docs_fts_u61 MATCH ?{where_sql}
                    ORDER BY score ASC
                    LIMIT ?
                    """,
                    [u61_and, *base_params, pool_limit],
                ).fetchall()
                _add_rank_list(rows, weight=1.3)
            except sqlite3.OperationalError:
                pass

        # Track 2: trigram BM25 for terms >= 3 chars (great for CJK substrings & fuzzy code fragments)
        tri_terms = [t for t in terms if len(t) >= 3]
        if trigram_ok and tri_terms:
            tri_and = " AND ".join(_fts_quote(t) for t in tri_terms)
            try:
                rows = conn.execute(
                    f"""
                    SELECT d.*, bm25(docs_fts_tri, 3.0, 1.0, 2.0) AS score
                    FROM docs_fts_tri f
                    JOIN docs d ON d.rowid = f.rowid
                    WHERE docs_fts_tri MATCH ?{where_sql}
                    ORDER BY score ASC
                    LIMIT ?
                    """,
                    [tri_and, *base_params, pool_limit],
                ).fetchall()
                _add_rank_list(rows, weight=1.4)
            except sqlite3.OperationalError:
                pass

        # Track 3: exact substring AND via LIKE (crucial for 1-2 char CJK terms like 算力 / 刷机 / 报销)
        like_clauses = []
        like_params: list[Any] = []
        for t in terms:
            pat = f"%{t}%"
            like_clauses.append("(d.title LIKE ? OR d.body LIKE ? OR d.project LIKE ?)")
            like_params.extend([pat, pat, pat])
        if like_clauses:
            rows = conn.execute(
                f"""
                SELECT d.*
                FROM docs d
                WHERE {' AND '.join(like_clauses)}{where_sql}
                ORDER BY d.workday DESC, d.updated_at DESC
                LIMIT ?
                """,
                [*like_params, *base_params, pool_limit],
            ).fetchall()
            _add_rank_list(rows, weight=1.5)

        # Track 4: relaxed OR fallback when strict AND yielded few hits on multi-term queries
        if len(rrf_scores) < min(limit, 5) and len(terms) > 1:
            u61_or = " OR ".join(_fts_quote(t) for t in terms if _fts_quote(t))
            if u61_or:
                try:
                    rows = conn.execute(
                        f"""
                        SELECT d.*, bm25(docs_fts_u61, 3.0, 1.0, 2.0) AS score
                        FROM docs_fts_u61 f
                        JOIN docs d ON d.rowid = f.rowid
                        WHERE docs_fts_u61 MATCH ?{where_sql}
                        ORDER BY score ASC
                        LIMIT ?
                        """,
                        [u61_or, *base_params, pool_limit],
                    ).fetchall()
                    _add_rank_list(rows, weight=0.6)
                except sqlite3.OperationalError:
                    pass
            if trigram_ok and tri_terms:
                tri_or = " OR ".join(_fts_quote(t) for t in tri_terms)
                try:
                    rows = conn.execute(
                        f"""
                        SELECT d.*, bm25(docs_fts_tri, 3.0, 1.0, 2.0) AS score
                        FROM docs_fts_tri f
                        JOIN docs d ON d.rowid = f.rowid
                        WHERE docs_fts_tri MATCH ?{where_sql}
                        ORDER BY score ASC
                        LIMIT ?
                        """,
                        [tri_or, *base_params, pool_limit],
                    ).fetchall()
                    _add_rank_list(rows, weight=0.65)
                except sqlite3.OperationalError:
                    pass
            # Multi-term partial LIKE fallback (matches any term for short CJK words)
            or_clauses = []
            or_params: list[Any] = []
            for t in terms:
                pat = f"%{t}%"
                or_clauses.append("(d.title LIKE ? OR d.body LIKE ? OR d.project LIKE ?)")
                or_params.extend([pat, pat, pat])
            rows = conn.execute(
                f"""
                SELECT d.*
                FROM docs d
                WHERE ({' OR '.join(or_clauses)}){where_sql}
                ORDER BY d.workday DESC, d.updated_at DESC
                LIMIT ?
                """,
                [*or_params, *base_params, pool_limit],
            ).fetchall()
            _add_rank_list(rows, weight=0.45)

        # Future vector hook (no-op unless external provider explicitly configured)
        vp = vector_provider or _DEFAULT_VECTOR_PROVIDER
        if vp.available() and docs_by_id:
            pass  # Extension point for optional dense reranking

        ranked: list[tuple[float, dict[str, Any]]] = []
        for did, base_score in rrf_scores.items():
            doc = docs_by_id[did]
            # Skip forgotten decisions by default
            if doc["doc_type"] == "decision" and doc["status"] == "forgotten":
                continue
            final_score = base_score + _recency_boost(str(doc.get("workday") or ""), str(doc["doc_type"]), str(doc.get("status") or ""))
            try:
                payload = json.loads(doc["payload_json"])
            except Exception:
                payload = {}
            ranked.append(
                (
                    final_score,
                    {
                        "doc_id": did,
                        "doc_type": doc["doc_type"],
                        "score": round(final_score, 5),
                        "workday": doc["workday"],
                        "scope": doc["scope"],
                        "project_id": doc["project_id"],
                        "project": doc["project"],
                        "status": doc["status"],
                        "payload": payload,
                    },
                )
            )
        ranked.sort(key=lambda pair: (pair[0], pair[1]["workday"]), reverse=True)
        return [item for _, item in ranked[: max(1, limit)]]
    finally:
        conn.close()


def index_stats(data_dir: str) -> dict[str, Any]:
    db_path = index_db_path(data_dir)
    if not os.path.exists(db_path):
        return {"exists": False, "db_path": db_path, "size_bytes": 0, "counts": {}, "stale": True}
    try:
        conn = _connect(db_path)
        try:
            counts = {
                row["doc_type"]: int(row["cnt"])
                for row in conn.execute("SELECT doc_type, COUNT(*) AS cnt FROM docs GROUP BY doc_type").fetchall()
            }
            meta = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta").fetchall()}
            wm_count = int(conn.execute("SELECT COUNT(*) FROM session_watermarks").fetchone()[0])
            stale = meta.get("structured_fp") != _structured_fingerprint(data_dir)
            return {
                "exists": True,
                "db_path": db_path,
                "size_bytes": os.path.getsize(db_path),
                "schema_version": meta.get("schema_version", ""),
                "trigram_enabled": meta.get("trigram_enabled") == "1",
                "structured_updated_at": meta.get("structured_updated_at", ""),
                "sessions_updated_at": meta.get("sessions_updated_at", ""),
                "indexed_session_files": wm_count,
                "counts": counts,
                "stale": stale,
            }
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return {"exists": True, "db_path": db_path, "error": str(exc), "stale": True}
