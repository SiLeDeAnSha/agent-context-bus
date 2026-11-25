"""Bind each progress item to explicit evidence from its workday's raw archive."""

from collections import defaultdict
from typing import Any

SESSION_BUCKETS = ("antigravity", "codex", "workbuddy")
SOURCE_PENDING = "来源待核对"
SOURCE_RECORDED = "已记录"
SOURCE_ARCHIVE_MISSING = "原始档案暂不可用"


def source_key(ref: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(ref.get(k) or "") for k in ("kind", "id", "machine", "bucket", "repo"))


class SourceIndex:
    def __init__(self, raw: dict[str, Any]):
        self.records = defaultdict(dict)
        default_machine = raw.get("machine") or ""
        for bucket in SESSION_BUCKETS:
            for session in raw.get(bucket) or []:
                if not isinstance(session, dict):
                    continue
                ref = {
                    "kind": "session",
                    "id": session.get("id") or "",
                    "bucket": bucket,
                    "machine": session.get("machine") or default_machine,
                    "workspace": session.get("workspace") or session.get("cwd") or "",
                }
                self._add(ref, session)
        for repo in raw.get("git") or []:
            if not isinstance(repo, dict):
                continue
            path = repo.get("repo") or repo.get("path") or ""
            if not path:
                continue
            for commit in repo.get("commits") or []:
                if not isinstance(commit, dict):
                    continue
                ref = {
                    "kind": "commit",
                    "id": commit.get("hash") or "",
                    "repo": path,
                    "machine": repo.get("machine") or default_machine,
                    "files": commit.get("files") or [],
                }
                self._add(ref, (repo, commit))
        for group in raw.get("files") or []:
            if not isinstance(group, dict):
                continue
            for path in group.get("paths") or []:
                ref = {"kind": "file", "id": path, "machine": group.get("machine") or default_machine}
                self._add(ref, group)

    def _add(self, ref: dict[str, Any], raw_record: Any) -> None:
        key = source_key(ref)
        if key[1]:
            self.records[key[:2]].setdefault((*key, ref.get("workspace") or ""), (ref, raw_record))

    def resolve(self, claimed: dict[str, Any]) -> tuple[dict[str, Any], Any] | None:
        if not isinstance(claimed, dict):
            return None
        kind, identifier, *_ = source_key(claimed)
        matches = [
            (ref, record)
            for ref, record in self.records.get((kind, identifier), {}).values()
            if all(not claimed.get(k) or claimed[k] == ref.get(k) for k in ("machine", "bucket", "repo"))
        ]
        return matches[0] if len(matches) == 1 else None

    def verify(self, claims: Any) -> list[dict[str, Any]]:
        verified = {}
        for claim in claims if isinstance(claims, list) else []:
            match = self.resolve(claim)
            if match:
                ref, _ = match
                verified[source_key(ref)] = ref
        return list(verified.values())

    def lookup(self, ref: dict[str, Any]) -> tuple[dict[str, Any], Any] | None:
        key = source_key(ref)
        return self.records.get(key[:2], {}).get((*key, ref.get("workspace") or ""))


def saved_source_refs(claims: Any) -> list[dict[str, Any]]:
    refs = {}
    for claim in claims if isinstance(claims, list) else []:
        if not isinstance(claim, dict):
            continue
        kind, identifier = claim.get("kind"), claim.get("id")
        if kind not in ("session", "commit", "file") or not isinstance(identifier, str) or not identifier:
            continue
        ref = {"kind": kind, "id": identifier}
        for key in ("machine", "bucket", "workspace", "repo"):
            if isinstance(claim.get(key), str):
                ref[key] = claim[key]
        if kind == "commit" and isinstance(claim.get("files"), list):
            ref["files"] = [name for name in claim["files"] if isinstance(name, str)]
        refs.setdefault(source_key(ref), ref)
    return list(refs.values())


def verify_day_sources(day: dict[str, Any], raw: dict[str, Any] | None) -> dict[str, Any]:
    """Validate incoming citations, copying source metadata from the raw archive."""
    sources = SourceIndex(raw) if raw is not None else None
    result = dict(day)
    for collection in ("work_items", "life_items"):
        if collection not in day:
            continue
        result[collection] = []
        for item in day[collection] or []:
            if not isinstance(item, dict):
                continue
            claims = item.get("sources")
            refs = sources.verify(claims) if sources is not None else saved_source_refs(claims)
            record = {**item, "sources": refs}
            record.pop("source_status", None)
            if sources is None and refs:
                record["source_status"] = SOURCE_ARCHIVE_MISSING
            result[collection].append(record)
    return result


def format_sources(sources: list[dict[str, Any]], separator: str = "\n", status: str = "") -> str:
    lines = []
    for ref in sources:
        machine = f" @{ref['machine']}" if ref.get("machine") else ""
        kind = ref["kind"]
        if kind == "session":
            workspace = f" · {ref['workspace']}" if ref.get("workspace") else ""
            lines.append(f"会话 {ref['id']}{machine}{workspace}")
        elif kind == "commit":
            files = f" · {', '.join(ref['files'])}" if ref.get("files") else ""
            repo = f" · {ref['repo']}" if ref.get("repo") else ""
            lines.append(f"Git {ref['id']}{machine}{repo}{files}")
        elif kind == "file":
            lines.append(f"文件 {ref['id']}{machine}")
    if lines and status == SOURCE_ARCHIVE_MISSING:
        lines.append(SOURCE_ARCHIVE_MISSING)
    return separator.join(lines) or SOURCE_PENDING
