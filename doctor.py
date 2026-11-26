#!/usr/bin/env python3
"""Health, integrity, and non-destructive self-repair diagnostics (`--doctor`) for Agent Context Bus."""

from __future__ import annotations

from datetime import datetime
import glob
import json
import os
import sqlite3
import sys
import time
from typing import Any

import decisions as dec_mod
import multi_machine
import query_memory
import remote_client
import search_fts
from project_identity import ProjectCatalog, load_registry


def _check_sqlite_fts() -> dict[str, Any]:
    ver = sqlite3.sqlite_version
    u61_ok = False
    tri_ok = False
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE _t1 USING fts5(x, tokenize='unicode61')")
            u61_ok = True
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("CREATE VIRTUAL TABLE _t2 USING fts5(x, tokenize='trigram')")
            tri_ok = True
        except sqlite3.OperationalError:
            pass
        conn.close()
    except Exception:
        pass
    return {
        "python": sys.version.split()[0],
        "sqlite": ver,
        "fts5_unicode61": u61_ok,
        "fts5_trigram": tri_ok,
        "ok": u61_ok,
    }


def _check_global_rules_and_hooks(data_dir: str) -> dict[str, Any]:
    targets = {
        "antigravity_rule": os.path.expanduser("~/.gemini/GEMINI.md"),
        "codex_rule": os.path.expanduser("~/.codex/AGENTS.md"),
        "claude_rule": os.path.expanduser("~/.claude/CLAUDE.md"),
        "workbuddy_rule": os.path.expanduser("~/.workbuddy/USER.md"),
        "antigravity_hooks": os.path.expanduser("~/.gemini/antigravity/hooks.json"),
        "codex_hooks": os.path.expanduser("~/.codex/hooks.json"),
        "claude_settings": os.path.expanduser("~/.claude/settings.json"),
    }
    status: dict[str, str] = {}
    for name, path in targets.items():
        if not os.path.exists(path):
            status[name] = "missing"
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
            if name.endswith("_rule"):
                status[name] = "synced" if "DAILY-SUMMARY START" in content else "unsynced"
            else:
                status[name] = "hooked" if "hook_bridge.py" in content else "no_hook"
        except OSError:
            status[name] = "unreadable"

    err_log = os.path.join(data_dir, "live", "hook_errors.log")
    recent_errors: list[str] = []
    if os.path.exists(err_log):
        try:
            with open(err_log, "r", encoding="utf-8", errors="ignore") as fh:
                recent_errors = [l.strip() for l in fh.readlines()[-5:] if l.strip()]
        except OSError:
            pass
    return {"targets": status, "recent_hook_errors": recent_errors}


def _check_json_files(data_dir: str) -> dict[str, Any]:
    issues: list[str] = []
    checked = 0
    candidates = [
        os.path.join(data_dir, "summary_all_history.json"),
        os.path.join(data_dir, "project_registry.json"),
        os.path.join(data_dir, "decisions.json"),
    ]
    candidates.extend(sorted(glob.glob(os.path.join(data_dir, "live", "live_*.json"))))
    candidates.extend(sorted(glob.glob(os.path.join(data_dir, "live", "machines", "*", "live_*.json"))))

    for fp in candidates:
        if not os.path.exists(fp):
            continue
        checked += 1
        try:
            with open(fp, "r", encoding="utf-8") as fh:
                json.load(fh)
        except Exception as exc:
            issues.append(f"JSON 损坏 {os.path.relpath(fp, data_dir)}: {exc}")

    # Validate decisions integrity (broken supersede links / cycles)
    issues.extend(dec_mod.validate_decisions_integrity(data_dir))

    # Check stale hook temp files (> 10 min old)
    stale_tmp: list[str] = []
    now_ts = time.time()
    for fp in glob.glob(os.path.join(data_dir, "live", "hook_*.json")):
        try:
            if now_ts - os.path.getmtime(fp) > 600:
                stale_tmp.append(fp)
        except OSError:
            continue

    # Check registry orphan projects (registered with no aliases/paths/history)
    days = query_memory.load_all_days(data_dir)
    reg = load_registry(data_dir)
    history_names = {
        str(it.get("project") or "").strip().lower()
        for d in days
        for it in (d.get("work_items") or [])
        if isinstance(it, dict)
    }
    orphans: list[str] = []
    for p in reg.get("projects") or []:
        if not isinstance(p, dict):
            continue
        names = [str(p.get("project") or "").strip().lower(), *(str(a).strip().lower() for a in (p.get("aliases") or []))]
        if not any(n in history_names for n in names) and not (p.get("paths") or []):
            orphans.append(str(p.get("project") or ""))

    return {
        "checked_files": checked,
        "issues": issues,
        "stale_hook_tmp": stale_tmp,
        "orphan_registry_projects": orphans,
    }


def run_doctor(
    data_dir: str,
    *,
    cfg: dict[str, Any] | None = None,
    remote_config_path: str | None = None,
    fix: bool = False,
    index_output: str | None = None,
) -> dict[str, Any]:
    cfg = cfg if cfg is not None else query_memory.load_config()
    env = _check_sqlite_fts()
    hooks = _check_global_rules_and_hooks(data_dir)
    data_check = _check_json_files(data_dir)
    fts = search_fts.index_stats(data_dir)

    idx_path = os.path.expanduser(index_output or query_memory.resolve_index_path(cfg))
    idx_bytes = os.path.getsize(idx_path) if os.path.exists(idx_path) else 0
    idx_health = {
        "path": idx_path,
        "exists": os.path.exists(idx_path),
        "bytes": idx_bytes,
        "bloated": idx_bytes > 65536,
    }

    rc = remote_client.load_remote_config(remote_config_path, project_cfg=cfg)
    remote_info: dict[str, Any] = {"configured": rc.configured, "endpoints": []}
    if rc.configured:
        remote_info["endpoints"] = remote_client.probe_endpoints(rc)

    repaired: list[str] = []
    if fix:
        for fp in data_check["stale_hook_tmp"]:
            try:
                os.remove(fp)
                repaired.append(f"清理过期临时文件: {os.path.basename(fp)}")
            except OSError:
                pass
        if fts.get("stale") or not fts.get("exists") or fts.get("error"):
            try:
                res = search_fts.rebuild_structured_index(data_dir)
                repaired.append(f"重建 FTS5 索引 ({res['structured_docs']} 条文档)")
                fts = search_fts.index_stats(data_dir)
            except Exception as exc:
                data_check["issues"].append(f"FTS5 重建失败: {exc}")
        if not idx_health["exists"] or fts.get("stale"):
            try:
                days = query_memory.load_all_days(data_dir)
                cat = ProjectCatalog(days, load_registry(data_dir))
                st = query_memory.generate_memory_index(
                    days,
                    output_path=idx_path,
                    tz_name=str(cfg.get("timezone") or "Asia/Shanghai"),
                    catalog=cat,
                    data_dir=data_dir,
                )
                idx_health["exists"] = True
                idx_health["bytes"] = st["bytes"]
                idx_health["bloated"] = st["bytes"] > 65536
                repaired.append(f"刷新 MEMORY_INDEX.md ({st['bytes']} bytes)")
            except Exception as exc:
                data_check["issues"].append(f"MEMORY_INDEX.md 刷新失败: {exc}")

    healthy = (
        env["ok"]
        and not data_check["issues"]
        and idx_health["exists"]
        and not fts.get("error")
    )
    return {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "healthy": healthy,
        "data_dir": data_dir,
        "environment": env,
        "memory_index": idx_health,
        "fts_index": fts,
        "data_integrity": data_check,
        "hooks_and_rules": hooks,
        "remote": remote_info,
        "repaired": repaired,
    }


def format_doctor_md(report: dict[str, Any]) -> str:
    badge = "HEALTHY" if report["healthy"] else "ATTENTION NEEDED"
    env = report["environment"]
    idx = report["memory_index"]
    fts = report["fts_index"]
    di = report["data_integrity"]
    lines = [
        f"## Agent Context Bus 体检报告 · `{badge}`",
        "",
        f"- **运行环境**：Python `{env['python']}` ｜ SQLite `{env['sqlite']}` ｜ FTS5 unicode61=`{env['fts5_unicode61']}` ｜ trigram=`{env['fts5_trigram']}`",
        f"- **数据目录**：`{report['data_dir']}`（已校验 {di['checked_files']} 个核心 JSON）",
        f"- **L1 快照**：`{idx['path']}`（{'存在' if idx['exists'] else '缺失'}，{idx['bytes']} bytes{'，体积偏大' if idx['bloated'] else ''}）",
        f"- **FTS5 派生索引**：`{fts.get('db_path', '-')}`（{'存在' if fts.get('exists') else '未构建'}，{fts.get('size_bytes', 0)} bytes，stale=`{fts.get('stale')}`，文档分布=`{fts.get('counts', {})}`）",
    ]
    if di["issues"]:
        lines.extend(["", "### 发现问题"])
        for iss in di["issues"]:
            lines.append(f"- [FAIL] {iss}")
    if di["stale_hook_tmp"]:
        lines.append(f"- [WARN] 存在 {len(di['stale_hook_tmp'])} 个过期 hook 临时文件（可加 `--fix` 清理）")
    if di["orphan_registry_projects"]:
        lines.append(f"- [INFO] 登记表中无历史记录且无路径的孤儿项目：{', '.join(di['orphan_registry_projects'][:8])}")

    lines.extend(["", "### 四端规则与 Hook 状态"])
    for k, v in report["hooks_and_rules"]["targets"].items():
        lines.append(f"- `{k}`: `{v}`")
    if report["hooks_and_rules"]["recent_hook_errors"]:
        lines.append("- 最近 Hook 错误：")
        for err in report["hooks_and_rules"]["recent_hook_errors"]:
            lines.append(f"  - `{err}`")

    if report["remote"]["configured"]:
        lines.extend(["", "### 远程 NAS 端点探测"])
        for ep in report["remote"]["endpoints"]:
            ok = "OK" if ep["ok"] else "FAIL"
            lines.append(f"- [{ok}] **{ep['name']}** `{ep['url']}` ({ep['latency_ms']} ms, HTTP {ep['status']})")

    if report["repaired"]:
        lines.extend(["", "### 本次 `--fix` 已修复"])
        for r in report["repaired"]:
            lines.append(f"- {r}")

    return "\n".join(lines)
