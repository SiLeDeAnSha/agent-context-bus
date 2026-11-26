#!/usr/bin/env python3
"""Comprehensive unit & integration tests for Agent Context Bus v0.5.0 upgrades."""

from __future__ import annotations

from datetime import datetime
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from urllib import error as urlerror
from urllib import request as urlrequest
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SERVER_DIR = os.path.join(ROOT, "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import adapters
import decisions
import doctor
import live_memory
import multi_machine
import query_memory
import redact
import remote_client
import search_fts
import app as hub_app


class V05UpgradeTests(unittest.TestCase):
    def test_redaction_rules_and_no_false_positives(self) -> None:
        sample = (
            "key=sk-proj-1234567890abcdefghijklmnop "
            "gh=ghp_1234567890abcdefghijklmno "
            "aws=AKIAIOSFODNN7EXAMPLE "
            "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U "
            "https://admin:SuperSecretPass123@nas.local:8765/api "
            "password=MyRealPassword999 "
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAtest\n-----END RSA PRIVATE KEY-----"
        )
        cleaned = redact.redact_text(sample)
        self.assertNotIn("sk-proj-1234567890", cleaned)
        self.assertNotIn("ghp_1234567890", cleaned)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", cleaned)
        self.assertNotIn("SuperSecretPass123", cleaned)
        self.assertNotIn("MyRealPassword999", cleaned)
        self.assertNotIn("MIIEowIBAAKCAQEAtest", cleaned)
        self.assertIn("[REDACTED:API_KEY]", cleaned)
        self.assertIn("[REDACTED:PASS]", cleaned)
        self.assertIn("[REDACTED:PRIVATE_KEY]", cleaned)

        # Ensure normal git hashes, file paths, placeholders, and env vars are untouched
        safe = (
            "commit 77883a2b1c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f at ~/Desktop/project/agent-context-bus "
            "date 2026-11-26 token=$DAILY_MEMORY_TOKEN password=placeholder api_key=settings.api_key"
        )
        self.assertEqual(redact.redact_text(safe), safe)

    def test_live_tail_extraction_and_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tpath = os.path.join(tmp, "transcript.jsonl")
            events = [
                {"type": "USER_INPUT", "content": "<USER_REQUEST>帮我排查 macOS 无法打开网页的问题</USER_REQUEST>"},
                {
                    "type": "PLANNER_RESPONSE",
                    "content": "已定位到 Clash TUN 与 Tailscale CorpDNS 冲突，关闭 CorpDNS 后恢复正常。",
                    "tool_calls": [
                        {"name": "edit_file", "args": {"Path": "/Users/test/project/clash/script.js"}},
                        {"name": "read_file", "args": {"file_path": "/Users/test/project/clash/config.yaml"}},
                    ],
                },
            ]
            with open(tpath, "w", encoding="utf-8") as fh:
                for ev in events:
                    fh.write(json.dumps(ev, ensure_ascii=False) + "\n")

            tail = live_memory._extract_transcript_tail("antigravity", tpath)
            self.assertIn("Clash TUN", tail["last_assistant_summary"])
            self.assertIn("无法打开网页", tail["last_user_prompt"])
            self.assertEqual(len(tail["touched_files"]), 2)

            summary = live_memory._choose_live_summary("Mac所有网站无法打开排查", "Mac所有网站无法打开排查", tail)
            self.assertNotEqual(summary, "Mac所有网站无法打开排查")
            self.assertIn("Clash TUN", summary)
            self.assertIn("script.js", summary)

            # Verify add_note redacts credentials before saving
            data_dir = os.path.join(tmp, "data")
            os.makedirs(data_dir, exist_ok=True)
            saved = live_memory.add_note(
                data_dir,
                [],
                "代理分流",
                "临时测试 token=sk-proj-abcdefghijklmnopqrstuvwxyz1234",
                cfg={"timezone": "Asia/Shanghai", "day_start_hour": 5},
            )
            self.assertNotIn("sk-proj-abcdefgh", saved["note"]["text"])
            self.assertIn("[REDACTED:API_KEY]", saved["note"]["text"])

    def test_decisions_lifecycle_and_supersede_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            days = [
                {
                    "workday": "2026-11-25",
                    "weekday": "周三",
                    "work_items": [{"project": "四端代理规则优化", "scope": "personal", "summary": "更新分流"}],
                }
            ]
            d1 = decisions.add_decision(
                tmp,
                days,
                text="旧方案：直接把全部域名塞进 TUN 规则",
                scope="project",
                project="四端代理规则优化",
                kind="decision",
                tags="clash,tun",
            )
            g1 = decisions.add_decision(
                tmp,
                days,
                text="macOS 互联网共享无法转发 utun 流量，车机热点抓包需切代理模式",
                scope="global",
                kind="constraint",
                tags=["macos", "hotspot"],
            )
            d2 = decisions.supersede_decision(
                tmp,
                days,
                d1["id"],
                "新方案：仅高优先级域名走住宅代理，避免污染全局 TUN",
            )
            self.assertEqual(d2["supersedes"], [d1["id"]])

            active = decisions.query_decisions(tmp, project="四端代理规则优化")
            self.assertEqual([d["id"] for d in active], [d2["id"]])
            all_decs = decisions.query_decisions(tmp, project="四端代理规则优化", include_inactive=True)
            statuses = {d["id"]: d["status"] for d in all_decs}
            self.assertEqual(statuses[d1["id"]], "superseded")
            self.assertEqual(statuses[d2["id"]], "active")

            # Candidate auto-ingestion deduplicates and keeps confirmed decisions intact
            ingested = decisions.ingest_candidate_decisions(
                tmp,
                days,
                {
                    "workday": "2026-11-25",
                    "candidate_decisions": [
                        {"scope": "global", "kind": "pitfall", "text": "Python 3.14 触发 PEP 668 需使用独立 venv", "tags": ["python"]},
                    ],
                },
            )
            self.assertEqual(len(ingested), 1)
            self.assertEqual(ingested[0]["confidence"], "candidate")

            decisions.forget_decision(tmp, g1["id"], reason="测试软删")
            self.assertEqual(decisions.validate_decisions_integrity(tmp), [])

    def test_fts5_search_chinese_fuzzy_and_auto_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            days = [
                {
                    "workday": "2026-11-25",
                    "weekday": "周三",
                    "headline": "RTX 3080 Ti 算力解锁与瑞士风看板上线",
                    "core_outputs": "推送 gpu-broker 探针",
                    "work_items": [
                        {
                            "project": "Windows RTX 3080 Ti 算力解锁",
                            "scope": "personal",
                            "category": "硬件调试",
                            "tool": "Antigravity",
                            "machine": "macbook-pro",
                            "session_name": "FP16 算力解锁验证",
                            "summary": "修复驱动回退并新增 FP16/BF16 算力解锁探针，接入企业微信告警",
                            "outputs": "gpu-broker/watchdog",
                            "status": "已完成",
                            "next_step": "-",
                        },
                        {
                            "project": "荣县两会政务长图",
                            "scope": "work",
                            "category": "设计排版",
                            "tool": "Codex",
                            "machine": "macbook-pro",
                            "session_name": "两会报告排版",
                            "summary": "对齐七个委员会标题续行并导出可编辑 PSD",
                            "outputs": "两会报告.psd",
                            "status": "已完成",
                            "next_step": "-",
                        },
                    ],
                    "life_items": [],
                }
            ]
            multi_machine.atomic_write_json(os.path.join(tmp, "summary_all_history.json"), {"days": days})
            decisions.add_decision(
                tmp,
                days,
                text="RTX 3080 Ti 驱动升级前必须备份 b12498 稳定版本",
                scope="project",
                project="Windows RTX 3080 Ti 算力解锁",
                kind="pitfall",
            )

            # 2-char Chinese term + 3-char substring fuzzy match + decision recall
            res_2char = query_memory.query_keyword(days, "算力 告警", data_dir=tmp, use_fts=True)
            self.assertEqual(res_2char["search_mode"], "fts5")
            self.assertEqual(len(res_2char["hits"]), 1)
            self.assertIn("3080 Ti", res_2char["hits"][0]["project"])

            res_dec = query_memory.query_keyword(days, "b12498", data_dir=tmp, use_fts=True)
            self.assertEqual(len(res_dec["decisions"]), 1)

            # Exact mode fallback
            res_exact = query_memory.query_keyword(days, "委员会 PSD", data_dir=tmp, use_fts=False)
            self.assertEqual(res_exact["search_mode"], "exact")
            self.assertEqual(len(res_exact["hits"]), 1)

            # Deleting search_index.db auto-rebuilds transparently
            db_path = search_fts.index_db_path(tmp)
            self.assertTrue(os.path.exists(db_path))
            os.remove(db_path)
            res_after = query_memory.query_keyword(days, "瑞士风 算力", data_dir=tmp, use_fts=True)
            self.assertTrue(os.path.exists(db_path))
            self.assertGreaterEqual(len(res_after["hits"]), 1)

    def test_multi_machine_live_mcp_write_auth_and_web_ui(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = hub_app.HubState(
                storage_root=tmp,
                read_token="read-secret-tok",
                write_token="write-secret-tok",
                enable_auto_summarize=False,
            )
            server = hub_app.create_server("127.0.0.1", 0, state)
            port = server.server_address[1]
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            base_url = f"http://127.0.0.1:{port}"
            try:
                rc_write = remote_client.RemoteConfig(
                    endpoints=[remote_client.Endpoint("local", base_url, timeout=3.0)],
                    read_token="read-secret-tok",
                    write_token="write-secret-tok",
                    machine_id="macbook-pro",
                    cache_dir=os.path.join(tmp, "cache"),
                )
                # 1. Push same-day live WAL from two machines (with secret to verify server-side redaction)
                wd = live_memory.resolve_workday({"timezone": "Asia/Shanghai", "day_start_hour": 5})
                remote_client.push_live(
                    rc_write,
                    {
                        "workday": wd,
                        "notes": [{"time": "10:00", "created_at": f"{wd}T10:00:00+08:00", "project": "GPU调度", "text": "Mac端验证完成 key=sk-proj-1234567890abcdefghijklmnop"}],
                        "sessions": [{"key": "antigravity:c1", "tool": "Antigravity", "id": "c1", "title": "Mac调试", "summary": "完成 FTS5 压测", "time": "10:05", "updated_at": f"{wd}T10:05:00+08:00"}],
                        "git": [],
                    },
                    machine_id="macbook-pro",
                )
                remote_client.push_live(
                    rc_write,
                    {
                        "workday": wd,
                        "notes": [],
                        "sessions": [{"key": "codex:c2", "tool": "Codex", "id": "c2", "title": "Win编译", "summary": "Windows端 llama.cpp 编译通过", "time": "10:15", "updated_at": f"{wd}T10:15:00+08:00"}],
                        "git": [],
                    },
                    machine_id="win-desktop",
                )

                live_resp, _ = remote_client.request_json(rc_write, "GET", "/api/v1/live")
                self.assertEqual(len(live_resp["live_days"]), 1)
                merged_day = live_resp["live_days"][0]
                self.assertEqual(sorted(merged_day["machines"]), ["macbook-pro", "win-desktop"])
                self.assertIn("[REDACTED:API_KEY]", merged_day["notes"][0]["text"])

                idx_md, _ = remote_client.request_text(rc_write, "GET", "/api/v1/index")
                self.assertIn("完成 FTS5 压测", idx_md)
                self.assertIn("Windows端 llama.cpp 编译通过", idx_md)

                # 2. MCP write tool requires write_token; read_token must be rejected with -32001
                def call_mcp(tok: str, body: dict) -> dict:
                    req = urlrequest.Request(
                        f"{base_url}/mcp",
                        data=json.dumps(body).encode("utf-8"),
                        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                        method="POST",
                    )
                    with urlrequest.urlopen(req, timeout=3.0) as resp:
                        return json.loads(resp.read().decode("utf-8"))

                tools_list = call_mcp("read-secret-tok", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                tool_names = [x["name"] for x in tools_list["result"]["tools"]]
                self.assertEqual(len(tool_names), 8)
                self.assertIn("memory_add_decision", tool_names)

                denied = call_mcp(
                    "read-secret-tok",
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "memory_add_note", "arguments": {"project": "测试", "text": "应被拒绝"}},
                    },
                )
                self.assertEqual(denied["error"]["code"], -32001)

                allowed = call_mcp(
                    "write-secret-tok",
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "memory_add_decision",
                            "arguments": {"scope": "global", "kind": "constraint", "text": "所有写接口强制校验 write_token"},
                        },
                    },
                )
                self.assertFalse(allowed["result"]["isError"])

                # 3. Sync offline cache includes decisions and live_days
                cache_res = remote_client.sync_local_cache(rc_write)
                self.assertTrue(os.path.exists(os.path.join(rc_write.cache_dir, "data", "decisions.json")))
                self.assertEqual(cache_res["endpoint_name"], "local")

                # 4. Web UI serves HTML with security headers; unauthenticated API calls get 401
                with urlrequest.urlopen(f"{base_url}/ui", timeout=3.0) as resp:
                    self.assertEqual(resp.getcode(), 200)
                    self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
                    html = resp.read().decode("utf-8")
                    self.assertIn("AGENT CONTEXT BUS", html)

                with self.assertRaises(urlerror.HTTPError) as ctx:
                    urlrequest.urlopen(f"{base_url}/api/v1/live", timeout=3.0)
                self.assertEqual(ctx.exception.code, 401)
                ctx.exception.close()
            finally:
                server.shutdown()
                server.server_close()

    def test_adapters_registry_and_doctor_self_repair(self) -> None:
        self.assertEqual(sorted(adapters.list_adapters()), ["antigravity", "claude", "codex", "workbuddy"])
        for name in adapters.list_adapters():
            adp = adapters.get_adapter(name)
            self.assertIsNotNone(adp)
            self.assertEqual(adp.name, name)

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            live_dir = os.path.join(data_dir, "live")
            os.makedirs(live_dir, exist_ok=True)
            multi_machine.atomic_write_json(os.path.join(data_dir, "summary_all_history.json"), {"days": []})
            # Create stale hook temp file (>10 min old)
            stale_fp = os.path.join(live_dir, "hook_stale.json")
            with open(stale_fp, "w", encoding="utf-8") as fh:
                fh.write("{}")
            old_ts = time.time() - 1200
            os.utime(stale_fp, (old_ts, old_ts))

            idx_out = os.path.join(tmp, "MEMORY_INDEX.md")
            rep1 = doctor.run_doctor(data_dir, cfg={"timezone": "Asia/Shanghai"}, remote_config_path=os.path.join(tmp, "none.json"), fix=False, index_output=idx_out)
            self.assertFalse(rep1["healthy"])
            self.assertEqual(len(rep1["data_integrity"]["stale_hook_tmp"]), 1)

            rep2 = doctor.run_doctor(data_dir, cfg={"timezone": "Asia/Shanghai"}, remote_config_path=os.path.join(tmp, "none.json"), fix=True, index_output=idx_out)
            self.assertTrue(rep2["healthy"])
            self.assertFalse(os.path.exists(stale_fp))
            self.assertTrue(os.path.exists(idx_out))
            self.assertTrue(os.path.exists(search_fts.index_db_path(data_dir)))


if __name__ == "__main__":
    unittest.main()
