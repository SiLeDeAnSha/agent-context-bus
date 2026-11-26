#!/usr/bin/env python3
"""Persistent long-term decisions, constraints, and pitfall registry with supersede chains.

Source of truth: `<data_dir>/decisions.json`.
Supports:
- Project-scoped (`scope="project"`) and global cross-project (`scope="global"`) records
- Kinds: `decision` | `constraint` | `pitfall`
- Non-destructive evolution: `supersede` links new chain head to old IDs (cycle-checked)
- Soft deletion: `forget` sets status="forgotten" while keeping audit trail
- Auto-ingested candidate decisions from summarizer.py (`source="auto"`, `confidence="candidate"`)
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
import re
from typing import Any
from zoneinfo import ZoneInfo

from multi_machine import atomic_write_json, file_lock
from project_identity import ProjectCatalog, load_registry, name_key
import redact

DECISIONS_FILE = "decisions.json"
DECISIONS_LOCK_FILE = ".decisions.lock"
VALID_SCOPES = ("project", "global")
VALID_KINDS = ("decision", "constraint", "pitfall")
VALID_STATUSES = ("active", "superseded", "forgotten")


def decisions_path(data_dir: str) -> str:
    return os.path.join(data_dir, DECISIONS_FILE)


def _norm_text(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def _dedup_key(scope: str, project_id: str, text: str) -> str:
    norm = re.sub(r"\s+", " ", str(text or "").strip().lower())
    return f"{scope}|{project_id}|{norm}"


def _make_decision_id(now: datetime, scope: str, project_id: str, text: str, existing_ids: set[str]) -> str:
    day_part = now.strftime("%Y%m%d")
    seed = f"{now.isoformat()}|{scope}|{project_id}|{text}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]
    cand = f"dec_{day_part}_{digest}"
    idx = 1
    while cand in existing_ids:
        cand = f"dec_{day_part}_{digest}_{idx}"
        idx += 1
    return cand


def _clean_tags(tags: list[str] | str | None) -> list[str]:
    if tags is None:
        return []
    raw_items: list[str] = []
    if isinstance(tags, str):
        raw_items = re.split(r"[,，;\s]+", tags)
    elif isinstance(tags, list):
        for item in tags:
            if isinstance(item, str):
                raw_items.extend(re.split(r"[,，;]+", item))
    out: list[str] = []
    seen: set[str] = set()
    for t in raw_items:
        cleaned = t.strip().lower()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned[:32])
    return out[:12]


def load_decisions_store(data_dir: str) -> dict[str, Any]:
    path = decisions_path(data_dir)
    if not os.path.exists(path):
        return {"version": 1, "decisions": []}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("decisions"), list):
            return {"version": int(data.get("version") or 1), "decisions": list(data["decisions"])}
    except Exception:
        pass
    return {"version": 1, "decisions": []}


def save_decisions_store(data_dir: str, store: dict[str, Any]) -> None:
    os.makedirs(data_dir, exist_ok=True)
    atomic_write_json(
        decisions_path(data_dir),
        {"version": int(store.get("version") or 1), "decisions": list(store.get("decisions") or [])},
    )


def _would_create_cycle(by_id: dict[str, dict[str, Any]], new_id: str, supersedes: list[str]) -> bool:
    for old_id in supersedes:
        if old_id == new_id:
            return True
        cur = old_id
        visited: set[str] = {new_id}
        while cur:
            if cur in visited:
                return True
            visited.add(cur)
            node = by_id.get(cur)
            if not node:
                break
            cur = str(node.get("superseded_by") or "")
    return False


def add_decision(
    data_dir: str,
    days: list[dict[str, Any]],
    *,
    text: str,
    scope: str = "project",
    project: str = "",
    kind: str = "decision",
    tags: list[str] | str | None = None,
    supersedes: list[str] | None = None,
    source: str = "manual",
    confidence: str = "confirmed",
    evidence: dict[str, Any] | None = None,
    tz_name: str = "Asia/Shanghai",
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_scope = str(scope or "project").strip().lower()
    if clean_scope not in VALID_SCOPES:
        raise ValueError(f"Invalid scope: {scope!r} (expected project|global)")
    clean_kind = str(kind or "decision").strip().lower()
    if clean_kind not in VALID_KINDS:
        raise ValueError(f"Invalid kind: {kind!r} (expected decision|constraint|pitfall)")

    clean_text = _norm_text(redact.redact_with_cfg(str(text or ""), cfg))
    if not clean_text:
        raise ValueError("决策内容不能为空。")

    canonical_project = ""
    proj_id = ""
    if clean_scope == "project":
        proj_clean = str(project or "").strip()
        if not proj_clean:
            raise ValueError("项目级决策必须提供项目名称（或使用 --global）。")
        catalog = ProjectCatalog(days, load_registry(data_dir))
        identity = catalog.lookup(proj_clean) or catalog.for_item({"project": proj_clean})
        canonical_project = identity["project"]
        proj_id = identity["project_id"]

    now = datetime.now(ZoneInfo(tz_name))
    now_iso = now.isoformat(timespec="seconds")
    clean_tags = _clean_tags(tags)
    clean_evidence = redact.redact_with_cfg(evidence or {}, cfg) if isinstance(evidence, dict) else {}
    sup_ids = [str(s).strip() for s in (supersedes or []) if str(s).strip()]

    lock_path = os.path.join(data_dir, DECISIONS_LOCK_FILE)
    with file_lock(lock_path):
        store = load_decisions_store(data_dir)
        items: list[dict[str, Any]] = list(store["decisions"])
        by_id = {str(d.get("id")): d for d in items if d.get("id")}

        for old_id in sup_ids:
            if old_id not in by_id:
                raise ValueError(f"找不到要淘汰的旧决策 ID: {old_id}")

        # Dedup identical active entry if no explicit supersede requested
        if not sup_ids:
            target_dk = _dedup_key(clean_scope, proj_id, clean_text)
            for existing in items:
                if existing.get("status") != "active":
                    continue
                if _dedup_key(
                    str(existing.get("scope") or "project"),
                    str(existing.get("project_id") or ""),
                    str(existing.get("text") or ""),
                ) == target_dk:
                    # Upgrade candidate -> confirmed if user explicitly added it
                    changed = False
                    if confidence == "confirmed" and existing.get("confidence") != "confirmed":
                        existing["confidence"] = "confirmed"
                        existing["source"] = source
                        changed = True
                    if clean_tags:
                        merged_tags = _clean_tags([*(existing.get("tags") or []), *clean_tags])
                        if merged_tags != existing.get("tags"):
                            existing["tags"] = merged_tags
                            changed = True
                    if changed:
                        existing["updated_at"] = now_iso
                        save_decisions_store(data_dir, {"version": store["version"], "decisions": items})
                    return existing

        new_id = _make_decision_id(now, clean_scope, proj_id, clean_text, set(by_id.keys()))
        if sup_ids and _would_create_cycle(by_id, new_id, sup_ids):
            raise ValueError("supersede 链存在循环引用。")

        entry: dict[str, Any] = {
            "id": new_id,
            "scope": clean_scope,
            "project_id": proj_id,
            "project": canonical_project,
            "kind": clean_kind,
            "text": clean_text,
            "tags": clean_tags,
            "status": "active",
            "superseded_by": None,
            "supersedes": sup_ids,
            "source": str(source or "manual"),
            "confidence": str(confidence or "confirmed"),
            "evidence": clean_evidence,
            "created_at": now_iso,
            "updated_at": now_iso,
        }

        for old_id in sup_ids:
            old = by_id[old_id]
            old["status"] = "superseded"
            old["superseded_by"] = new_id
            old["updated_at"] = now_iso

        items.insert(0, entry)
        save_decisions_store(data_dir, {"version": store["version"], "decisions": items})
        return entry


def supersede_decision(
    data_dir: str,
    days: list[dict[str, Any]],
    old_id: str,
    new_text: str,
    *,
    kind: str | None = None,
    tags: list[str] | str | None = None,
    source: str = "manual",
    tz_name: str = "Asia/Shanghai",
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store = load_decisions_store(data_dir)
    old = next((d for d in store["decisions"] if str(d.get("id")) == str(old_id).strip()), None)
    if old is None:
        raise ValueError(f"找不到决策 ID: {old_id}")
    old_status = str(old.get("status") or "active")
    if old_status != "active":
        hint = f"，当前接任版本为 `{old.get('superseded_by')}`" if old_status == "superseded" and old.get("superseded_by") else ""
        raise ValueError(f"决策 `{old['id']}` 状态为 `{old_status}`，不能重复替代{hint}")
    merged_tags = _clean_tags(tags) if tags is not None else list(old.get("tags") or [])
    return add_decision(
        data_dir,
        days,
        text=new_text,
        scope=str(old.get("scope") or "project"),
        project=str(old.get("project") or ""),
        kind=kind or str(old.get("kind") or "decision"),
        tags=merged_tags,
        supersedes=[str(old["id"])],
        source=source,
        confidence="confirmed",
        evidence=old.get("evidence") if isinstance(old.get("evidence"), dict) else {},
        tz_name=tz_name,
        cfg=cfg,
    )


def forget_decision(
    data_dir: str,
    dec_id: str,
    *,
    reason: str = "",
    tz_name: str = "Asia/Shanghai",
) -> dict[str, Any]:
    target = str(dec_id or "").strip()
    if not target:
        raise ValueError("必须提供决策 ID。")
    now_iso = datetime.now(ZoneInfo(tz_name)).isoformat(timespec="seconds")
    lock_path = os.path.join(data_dir, DECISIONS_LOCK_FILE)
    with file_lock(lock_path):
        store = load_decisions_store(data_dir)
        items = list(store["decisions"])
        found = next((d for d in items if str(d.get("id")) == target), None)
        if found is None:
            raise ValueError(f"找不到决策 ID: {target}")
        found["status"] = "forgotten"
        if reason.strip():
            found["forget_reason"] = reason.strip()[:200]
        found["updated_at"] = now_iso
        save_decisions_store(data_dir, {"version": store["version"], "decisions": items})
        return found


def query_decisions(
    data_dir: str,
    *,
    project_identity: dict[str, Any] | None = None,
    project: str | None = None,
    scope: str | None = None,
    kind: str | None = None,
    include_inactive: bool = False,
    keyword: str | None = None,
    limit: int = 50,
    catalog: ProjectCatalog | None = None,
) -> list[dict[str, Any]]:
    store = load_decisions_store(data_dir)
    items = list(store["decisions"])

    proj_id = ""
    proj_names: set[str] = set()
    if project_identity:
        proj_id = str(project_identity.get("project_id") or "")
        for lbl in [project_identity.get("project"), *(project_identity.get("aliases") or [])]:
            if lbl:
                proj_names.add(name_key(str(lbl)))
    elif project:
        if catalog:
            ident = catalog.lookup(project)
            if ident:
                proj_id = str(ident.get("project_id") or "")
                for lbl in [ident.get("project"), *(ident.get("aliases") or [])]:
                    if lbl:
                        proj_names.add(name_key(str(lbl)))
        if not proj_names:
            proj_names.add(name_key(project))

    terms = [t.lower() for t in (keyword or "").strip().split() if t.strip()]
    out: list[dict[str, Any]] = []
    for d in items:
        st = str(d.get("status") or "active")
        if not include_inactive and st != "active":
            continue
        if scope and scope != "all" and str(d.get("scope") or "") != scope:
            continue
        if kind and str(d.get("kind") or "") != kind:
            continue
        if proj_id or proj_names:
            d_pid = str(d.get("project_id") or "")
            d_name = name_key(str(d.get("project") or ""))
            if not ((proj_id and d_pid == proj_id) or (d_name and d_name in proj_names)):
                continue
        if terms:
            blob = " ".join(
                [
                    str(d.get("project") or ""),
                    str(d.get("kind") or ""),
                    str(d.get("text") or ""),
                    " ".join(d.get("tags") or []),
                ]
            ).lower()
            if not all(t in blob for t in terms):
                continue
        out.append(d)

    # Order: active confirmed first, then active candidate, then superseded, newest updated_at first
    status_rank = {"active": 0, "superseded": 1, "forgotten": 2}
    conf_rank = {"confirmed": 0, "candidate": 1}
    out.sort(
        key=lambda d: (
            status_rank.get(str(d.get("status") or "active"), 9),
            conf_rank.get(str(d.get("confidence") or "confirmed"), 9),
            str(d.get("updated_at") or ""),
        ),
    )
    # Within same (status, confidence), newest first
    out.sort(key=lambda d: str(d.get("updated_at") or ""), reverse=True)
    out.sort(
        key=lambda d: (
            status_rank.get(str(d.get("status") or "active"), 9),
            conf_rank.get(str(d.get("confidence") or "confirmed"), 9),
        )
    )
    return out[: max(1, limit)]


def ingest_candidate_decisions(
    data_dir: str,
    days: list[dict[str, Any]],
    day_summary: dict[str, Any],
    *,
    tz_name: str = "Asia/Shanghai",
    cfg: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Ingest auto-extracted `candidate_decisions` from summarizer.py without clobbering manual decisions."""
    raw_list = day_summary.get("candidate_decisions")
    if not isinstance(raw_list, list) or not raw_list:
        return []
    workday = str(day_summary.get("workday") or "")
    created: list[dict[str, Any]] = []
    for raw in raw_list[:8]:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if len(text) < 6:
            continue
        scope = str(raw.get("scope") or "project").strip().lower()
        if scope not in VALID_SCOPES:
            scope = "project"
        project = str(raw.get("project") or "").strip()
        if scope == "project" and not project:
            scope = "global"
        kind = str(raw.get("kind") or "decision").strip().lower()
        if kind not in VALID_KINDS:
            kind = "decision"
        try:
            item = add_decision(
                data_dir,
                days,
                text=text,
                scope=scope,
                project=project,
                kind=kind,
                tags=raw.get("tags"),
                source="auto",
                confidence="candidate",
                evidence={"workday": workday, "session_id": str(raw.get("session_id") or "")},
                tz_name=tz_name,
                cfg=cfg,
            )
            created.append(item)
        except ValueError:
            continue
    return created


def validate_decisions_integrity(data_dir: str) -> list[str]:
    """Check decisions.json for broken links, cycles, or invalid statuses (used by --doctor)."""
    path = decisions_path(data_dir)
    if not os.path.exists(path):
        return []
    issues: list[str] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        return [f"decisions.json 解析失败: {exc}"]
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
        return ["decisions.json 顶层结构不是 {version, decisions: [...]}"]
    items = data["decisions"]
    by_id = {str(d.get("id")): d for d in items if isinstance(d, dict) and d.get("id")}
    for d in items:
        if not isinstance(d, dict):
            issues.append("decisions.json 存在非对象条目")
            continue
        did = str(d.get("id") or "")
        st = str(d.get("status") or "")
        if st not in VALID_STATUSES:
            issues.append(f"决策 {did} 状态异常: {st!r}")
        sup_by = d.get("superseded_by")
        if sup_by and str(sup_by) not in by_id:
            issues.append(f"决策 {did} 的 superseded_by 指向不存在的 ID: {sup_by}")
        if st == "superseded" and not sup_by:
            issues.append(f"决策 {did} 标记为 superseded 但缺少 superseded_by")
        # Cycle detection
        cur = str(sup_by or "")
        seen = {did}
        while cur:
            if cur in seen:
                issues.append(f"决策 {did} 的 supersede 链存在循环引用")
                break
            seen.add(cur)
            nxt = by_id.get(cur)
            cur = str(nxt.get("superseded_by") or "") if nxt else ""
    return issues


def format_decisions_md(items: list[dict[str, Any]], *, title: str = "长期决策与避坑约束") -> str:
    if not items:
        return f"## {title}\n\n_暂无匹配决策记录。_\n"
    kind_badge = {"decision": "决策", "constraint": "硬约束", "pitfall": "避坑"}
    lines = [f"## {title}（{len(items)} 条）", ""]
    for d in items:
        st = str(d.get("status") or "active")
        badge = kind_badge.get(str(d.get("kind") or "decision"), "决策")
        scope_lbl = "全局" if d.get("scope") == "global" else str(d.get("project") or "未命名项目")
        conf = " [候选]" if d.get("confidence") == "candidate" else ""
        tags = " ".join(f"`#{t}`" for t in (d.get("tags") or []))
        status_note = ""
        if st == "superseded":
            status_note = f" ~~已废弃 -> `{d.get('superseded_by')}`~~"
        elif st == "forgotten":
            status_note = " ~~已软删~~"
        text = str(d.get("text") or "")
        if st != "active":
            text = f"~~{text}~~"
        date_str = str(d.get("updated_at") or d.get("created_at") or "")[:10]
        tag_part = f" {tags}" if tags else ""
        lines.append(f"- `{d.get('id')}` **{badge}** [{scope_lbl}]{conf} ({date_str}): {text}{tag_part}{status_note}")
    lines.append("")
    return "\n".join(lines)
