#!/usr/bin/env python3
"""Unit tests for daily-summary collector, 4-sheet Excel writer, catch-up, and external export."""

from compression import zstd
from datetime import datetime
import json
import os
import tempfile
import unittest
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

import collect
import export_report
import query_memory
import write_excel
from provenance import verify_day_sources
from project_identity import ProjectCatalog, edit_registry, load_registry, project_id


class DailySummaryTests(unittest.TestCase):
    def test_project_decisions_update_old_and_future_progress(self) -> None:
        from summarizer import normalize_summary_schema

        main = "ComfyUI 人像与视频管线"
        alias = "comfyui-mcp"
        face = "保脸工作流"
        main_id = project_id(main)
        face_id = project_id(face)
        old = {
            "workday": "2026-11-21",
            "work_items": [{"project": main}, {"project": alias}],
            "project_briefs": {main: {"ag": "人像处理"}, alias: {"codex": "MCP 集成"}},
        }
        new = normalize_summary_schema(
            {"work_items": [
                {"project": face, "suggested_project_id": main_id, "project_suggestion_reason": "人像工作流"},
                {"project": "其他", "suggested_project_id": "invented-id"},
            ]},
            {"workday": "2026-11-22"},
            valid_project_ids={main_id},
        )
        days = [old, new]
        with tempfile.TemporaryDirectory() as data_dir:
            before = ProjectCatalog(days)
            self.assertEqual(before.for_item(new["work_items"][0])["project_id"], face_id)
            self.assertEqual([p["label"] for p in before.suggestions(days)], [face])
            self.assertNotIn("suggested_project_id", new["work_items"][1])
            merged = edit_registry(data_dir, days, "merge", main, [alias, face])
            self.assertEqual(merged["project_id"], main_id)
            self.assertEqual(edit_registry(data_dir, days, "merge", main_id, [alias, face]), merged)
            for term in (main, main_id, alias, "ＣＯＭＦＹＵＩ－ＭＣＰ", face, face_id):
                result = query_memory.query_by_project(days, term, data_dir=data_dir)
                self.assertEqual(result["total_matches"], 3)
                self.assertEqual([p["project_id"] for p in result["matched_identities"]], [main_id])
                self.assertEqual(result["entries"][0]["project_label"], face)
            catalog = ProjectCatalog(days, load_registry(data_dir))
            self.assertEqual(catalog.suggestions(days), [])
            self.assertEqual(catalog.briefs(old["project_briefs"]), {main: {"ag": "人像处理", "codex": "MCP 集成"}})

            split = edit_registry(data_dir, days, "split", face, [])
            self.assertEqual(split["project_id"], face_id)
            self.assertEqual(edit_registry(data_dir, days, "split", face, []), split)
            future = {"workday": "2026-11-23", "work_items": [{"project": face, "suggested_project_id": main_id}]}
            write_excel.sync_json_indexes(days + [future], data_dir=data_dir)
            history = query_memory.load_all_days(data_dir)
            catalog = ProjectCatalog(history, load_registry(data_dir))
            self.assertEqual(catalog.suggestions(history), [])
            self.assertEqual(query_memory.query_by_project(history, face_id, data_dir=data_dir)["total_matches"], 2)
            self.assertEqual(query_memory.query_by_project(history, main_id, data_dir=data_dir)["total_matches"], 2)
            self.assertEqual(query_memory.query_keyword(history, alias, data_dir=data_dir)["total_matches"], 2)
            self.assertEqual(query_memory.query_exact_date(history, old["workday"], data_dir=data_dir)
                             ["day"]["work_items"][1]["project"], main)
            self.assertEqual(history[0]["work_items"][1]["project"], alias)
            self.assertEqual(query_memory.project_candidates(data_dir)[0]["project_id"], face_id)

    def test_workday_boundary_5am(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        start, end = collect.workday_bounds("2026-11-21", "Asia/Shanghai", 5)
        late_night = datetime(2026, 11, 22, 4, 30, tzinfo=tz)
        next_morning = datetime(2026, 11, 22, 5, 0, tzinfo=tz)
        self.assertTrue(start <= late_night < end)
        self.assertFalse(start <= next_morning < end)

    def test_estimate_active_hours_merges_clusters(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        ts = [
            datetime(2026, 11, 21, 10, 0, tzinfo=tz),
            datetime(2026, 11, 21, 10, 20, tzinfo=tz),
            datetime(2026, 11, 21, 10, 40, tzinfo=tz),
            datetime(2026, 11, 21, 15, 0, tzinfo=tz),
        ]
        self.assertAlmostEqual(collect.estimate_active_hours(ts, 30), 0.85, places=2)

    def test_codex_uuid_extraction_and_zstd(self) -> None:
        fname = "rollout-2026-11-19T23-58-19-01a1f487-bf3e-7500-a644-9a9a5f0d726e.jsonl.zst"
        self.assertEqual(collect.extract_codex_uuid(fname), "01a1f487-bf3e-7500-a644-9a9a5f0d726e")
        with tempfile.TemporaryDirectory() as tmpdir:
            zst_path = os.path.join(tmpdir, fname)
            with zstd.open(zst_path, "wt", encoding="utf-8") as f:
                f.write(json.dumps({"type": "response_item"}) + "\n")
            with zstd.open(zst_path, "rt", encoding="utf-8") as f:
                self.assertEqual(json.loads(f.readline())["type"], "response_item")

    def test_split_work_and_personal_workbooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_xlsx = os.path.join(tmpdir, "work.xlsx")
            personal_xlsx = os.path.join(tmpdir, "personal.xlsx")
            day1 = {
                "workday": "2026-11-19",
                "weekday": "周四",
                "work_items": [
                    {
                        "scope": "work",
                        "project": "小雪节气海报",
                        "category": "设计与排版",
                        "tool": "Antigravity",
                        "session_name": "Poster Chat",
                        "summary": "在 ~/demo 优化海报",
                        "outputs": "海报.psd",
                        "status": "已完成",
                        "next_step": "-",
                        "active_span": "10:00-11:00",
                        "estimated_hours": 1.0,
                    },
                    {
                        "scope": "personal",
                        "project": "ComfyUI 管线",
                        "category": "AI工作流",
                        "tool": "Codex",
                        "session_name": "评审工作流",
                        "summary": "盲测评审",
                        "outputs": "eval.md",
                        "status": "进行中",
                        "next_step": "待真机验收",
                        "active_span": "22:30-23:00",
                        "estimated_hours": 0.5,
                    },
                ],
                "life_items": [
                    {
                        "tool": "Antigravity",
                        "session_name": "Chat A",
                        "topic": "闲聊A",
                        "summary": "聊了午饭",
                        "active_span": "12:10",
                    }
                ],
            }
            day2 = {
                "workday": "2026-11-21",
                "weekday": "周六",
                "work_items": [
                    {
                        "scope": "personal",
                        "project": "VPN代理规则",
                        "category": "网络与客户端配置",
                        "tool": "Antigravity",
                        "session_name": "VPN配置",
                        "summary": "调整 clash 代理规则",
                        "outputs": "conf",
                        "status": "已完成",
                        "next_step": "-",
                        "active_span": "10:40-11:00",
                        "estimated_hours": 0.3,
                    }
                ],
                "life_items": [],
            }

            res = write_excel.update_split_workbooks([day1, day2], work_xlsx, personal_xlsx)
            self.assertEqual(res["work"], {"overview_rows": 2, "project_rows": 1, "detail_rows": 1})
            self.assertEqual(res["personal"], {"overview_rows": 2, "project_rows": 2, "detail_rows": 2, "life_rows": 1})

            missing = collect.find_missing_workdays({"excel_path": work_xlsx}, "2026-11-21", 4)
            self.assertEqual(missing, ["2026-11-20"])

            wb_w = load_workbook(work_xlsx, data_only=True)
            self.assertEqual(wb_w.sheetnames, ["每日概览", "项目总览", "项目流程明细"])
            r0 = write_excel.DATA_START_ROW
            self.assertEqual(wb_w[write_excel.SHEET_OVERVIEW].cell(row=r0, column=3).value, "无正式工作记录")
            self.assertEqual(wb_w[write_excel.SHEET_OVERVIEW].cell(row=r0 + 1, column=3).value, "小雪节气海报")
            wb_w.close()

            wb_p = load_workbook(personal_xlsx, data_only=True)
            self.assertEqual(wb_p.sheetnames, ["每日概览", "项目总览", "项目流程明细", "生活与随想记录"])
            self.assertEqual(wb_p[write_excel.SHEET_PROJECTS].cell(row=r0, column=1).value, "VPN代理规则")
            wb_p.close()

            cfg = collect.load_config()
            md = export_report.generate_report(work_xlsx, "2026-11-19", "2026-11-21", cfg)
            self.assertIn("小雪节气海报", md)
            self.assertNotIn("ComfyUI", md)
            self.assertNotIn("VPN", md)
            self.assertNotIn("/Users/youruser", md)

    def test_monthly_folder_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_xlsx = os.path.join(tmpdir, "每日工作汇总.xlsx")
            personal_xlsx = os.path.join(tmpdir, "个人技术与折腾汇总.xlsx")
            days = [
                {
                    "workday": "2026-06-21",
                    "weekday": "周日",
                    "work_items": [
                        {
                            "scope": "personal",
                            "project": "PRL 监控",
                            "category": "自动化",
                            "tool": "Codex",
                            "session_name": "PRL晨报",
                            "summary": "配置监控",
                            "outputs": "-",
                            "status": "已完成",
                            "next_step": "-",
                            "active_span": "14:00",
                            "estimated_hours": 0.3,
                        }
                    ],
                    "life_items": [],
                },
                {
                    "workday": "2026-10-18",
                    "weekday": "周日",
                    "work_items": [
                        {
                            "scope": "work",
                            "project": "重阳海报",
                            "category": "设计",
                            "tool": "Codex",
                            "session_name": "重阳海报调整",
                            "summary": "定稿海报",
                            "outputs": "重阳.psd",
                            "status": "已完成",
                            "next_step": "-",
                            "active_span": "10:00",
                            "estimated_hours": 1.0,
                        }
                    ],
                    "life_items": [],
                },
                {
                    "workday": "2026-11-18",
                    "weekday": "周三",
                    "work_items": [
                        {
                            "scope": "work",
                            "project": "小雪海报",
                            "category": "设计",
                            "tool": "Codex",
                            "session_name": "海报调整",
                            "summary": "定稿海报",
                            "outputs": "海报.psd",
                            "status": "已完成",
                            "next_step": "-",
                            "active_span": "10:00",
                            "estimated_hours": 1.0,
                        }
                    ],
                    "life_items": [
                        {
                            "tool": "Antigravity",
                            "session_name": "闲聊",
                            "topic": "休息",
                            "summary": "聊了天气",
                            "active_span": "12:00",
                        }
                    ],
                },
            ]
            res = write_excel.update_split_workbooks(days, work_xlsx, personal_xlsx, split_months=True)
            self.assertIn("months", res)
            self.assertFalse(os.path.exists(os.path.join(tmpdir, "2026-06", "每日工作汇总_2026-06.xlsx")))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "2026-06", "个人技术与折腾汇总_2026-06.xlsx")))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "2026-10", "每日工作汇总_2026-10.xlsx")))
            self.assertFalse(os.path.exists(os.path.join(tmpdir, "2026-10", "个人技术与折腾汇总_2026-10.xlsx")))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "2026-11", "每日工作汇总_2026-11.xlsx")))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "2026-11", "个人技术与折腾汇总_2026-11.xlsx")))


    def test_compress_turn_history_and_side_span(self) -> None:
        prompts = [f"第{i}轮任务需求说明" for i in range(1, 31)] + ["继续", "好的"]
        compressed = collect.compress_turn_history(prompts)
        self.assertIn("第28轮任务需求说明", compressed)
        self.assertNotIn("继续", compressed)
        self.assertTrue(write_excel.span_sort_key("14:00-15:00") < write_excel.span_sort_key("含于主会话"))

    def test_codex_sessions_precede_archived_and_json_index_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            s_dir = os.path.join(tmpdir, "sessions", "2026", "11")
            a_dir = os.path.join(tmpdir, "archived_sessions")
            os.makedirs(s_dir, exist_ok=True)
            os.makedirs(a_dir, exist_ok=True)
            s_file = os.path.join(s_dir, "rollout-1.jsonl")
            a_file = os.path.join(a_dir, "rollout-1.jsonl")
            open(s_file, "w").close()
            open(a_file, "w").close()
            ordered = collect.list_codex_rollout_paths([os.path.join(tmpdir, "sessions"), a_dir])
            self.assertEqual(ordered, [s_file, a_file])

            data_dir = os.path.join(tmpdir, "data")
            res1 = write_excel.sync_json_indexes([{"workday": "2026-11-21", "work_items": [], "life_items": []}], data_dir=data_dir)
            self.assertEqual(res1, {"total_days": 1, "updated_months": 1})
            res2 = write_excel.sync_json_indexes([{"workday": "2026-11-22", "work_items": [], "life_items": []}], data_dir=data_dir)
            self.assertEqual(res2, {"total_days": 2, "updated_months": 1})
            with open(os.path.join(data_dir, "summary_all_history.json"), "r", encoding="utf-8") as f:
                self.assertEqual([d["workday"] for d in json.load(f)["days"]], ["2026-11-21", "2026-11-22"])

    def test_workbuddy_column_and_parser(self) -> None:
        self.assertEqual(collect.clean_workbuddy_user_text('<system-reminder data-role="x">secret</system-reminder> 你好帮我看下 '), "你好帮我看下")
        with tempfile.TemporaryDirectory() as tmpdir:
            work_xlsx = os.path.join(tmpdir, "work.xlsx")
            personal_xlsx = os.path.join(tmpdir, "personal.xlsx")
            day = {
                "workday": "2026-11-23",
                "weekday": "周一",
                "work_items": [
                    {
                        "scope": "personal",
                        "project": "KPL抢票",
                        "category": "自动化",
                        "tool": "WorkBuddy",
                        "session_name": "编写KPL抢票脚本 [目录: project]",
                        "summary": "编写脚本",
                        "outputs": "kpl.py",
                        "status": "已完成",
                        "next_step": "-",
                        "active_span": "12:22-12:40",
                        "estimated_hours": 0.4,
                    }
                ],
                "life_items": [],
            }
            res = write_excel.update_split_workbooks([day], work_xlsx, personal_xlsx)
            self.assertEqual(res["personal"]["project_rows"], 1)
            wb = load_workbook(personal_xlsx, data_only=True)
            ws_ov = wb[write_excel.SHEET_OVERVIEW]
            self.assertEqual(ws_ov.cell(row=write_excel.HEADER_ROW, column=6).value, "WorkBuddy 干了啥")
            self.assertEqual(ws_ov.cell(row=write_excel.DATA_START_ROW, column=6).value, "编写脚本")
            ws_p = wb[write_excel.SHEET_PROJECTS]
            self.assertEqual(ws_p.cell(row=write_excel.HEADER_ROW, column=14).value, "WB 跨度(h)")
            self.assertAlmostEqual(float(ws_p.cell(row=write_excel.DATA_START_ROW, column=14).value), 0.4, places=2)
            wb.close()

    def test_t3_code_collector_filtering_and_dedup(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "statev2.sqlite")
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE projection_projects (
                    project_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    workspace_root TEXT NOT NULL
                );
                CREATE TABLE orchestration_v2_projection_threads (
                    thread_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    default_provider TEXT NOT NULL,
                    provider_instance_id TEXT,
                    archived_at TEXT,
                    deleted_at TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE orchestration_v2_projection_provider_threads (
                    provider_thread_id TEXT PRIMARY KEY,
                    thread_id TEXT,
                    provider TEXT NOT NULL,
                    provider_session_id TEXT
                );
                CREATE TABLE orchestration_v2_projection_messages (
                    message_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                """
            )
            conn.execute("INSERT INTO projection_projects VALUES ('p1', 't3code', '~/Desktop/t3code')")
            conn.executemany(
                "INSERT INTO orchestration_v2_projection_threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("t_ag", "p1", "日报自动化", "antigravity", "antigravity", None, None, json.dumps({"lineage": {}})),
                    ("t_cb", "p1", "打招呼", "acpRegistry_codebuddy_code", "acpRegistry_codebuddy_code", None, None, json.dumps({"lineage": {}})),
                    ("t_cl", "p1", "Claude调研", "claudeAgent", "claudeAgent", None, None, json.dumps({"lineage": {}})),
                    ("t_cx_dup", "p1", "Codex已采集", "codex", "codex", None, None, json.dumps({"lineage": {}})),
                    ("thread:subagent:1", "p1", "Sub Worker", "antigravity", "antigravity", None, None, json.dumps({"lineage": {"relationshipToParent": "subagent"}})),
                    ("t_handoff_only", "p1", "仅交接未发言", "antigravity", "antigravity", None, None, json.dumps({"lineage": {}})),
                    ("t_auto", "p1", "每日工作汇总", "antigravity", "antigravity", None, None, json.dumps({"lineage": {}})),
                ],
            )
            conn.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES ('pt1', 't_cx_dup', 'codex', 'cx-uuid-1')")
            conn.executemany(
                "INSERT INTO orchestration_v2_projection_messages VALUES (?, ?, ?, ?, ?)",
                [
                    ("m1", "t_ag", "user", "2026-11-24T03:07:26Z", json.dumps({"createdBy": "agent", "creationSource": "mcp", "text": "交接上下文"})),
                    ("m2", "t_ag", "user", "2026-11-24T03:12:00Z", json.dumps({"createdBy": "user", "creationSource": "web", "text": "把自动化搬到 T3 Code 上跑"})),
                    ("m3", "t_ag", "assistant", "2026-11-24T03:13:00Z", json.dumps({"createdBy": "agent", "creationSource": "provider", "text": "已完成 T3 适配和定时任务"})),
                    ("m4", "t_cb", "user", "2026-11-24T02:14:00Z", json.dumps({"createdBy": "user", "creationSource": "web", "text": "哈喽"})),
                    ("m5", "t_cl", "user", "2026-11-24T04:00:00Z", json.dumps({"createdBy": "user", "creationSource": "web", "text": "分析代码结构"})),
                    ("m6", "t_cx_dup", "user", "2026-11-24T04:10:00Z", json.dumps({"createdBy": "user", "creationSource": "web", "text": "修个bug"})),
                    ("m7", "thread:subagent:1", "user", "2026-11-24T04:15:00Z", json.dumps({"createdBy": "user", "creationSource": "web", "text": "子任务"})),
                    ("m8", "t_handoff_only", "user", "2026-11-24T03:07:30Z", json.dumps({"createdBy": "agent", "creationSource": "mcp", "text": "纯迁移交接"})),
                    ("m9", "t_auto", "user", "2026-11-24T01:00:00Z", json.dumps({"createdBy": "user", "creationSource": "scheduled", "text": "[DAILY-SUMMARY-AUTO] 执行汇总"})),
                ],
            )
            conn.commit()
            conn.close()

            tz = ZoneInfo("Asia/Shanghai")
            start_dt, end_dt = collect.workday_bounds("2026-11-24", "Asia/Shanghai", 5)
            cfg = {"merge_gap_minutes": 30, "self_filter_markers": ["[DAILY-SUMMARY-AUTO]"]}
            ag, _, cx, _, wb, _, other, _, hints = collect.collect_t3_code(
                start_dt, end_dt, tz, "2026-11-24", cfg, existing_codex_ids={"cx-uuid-1"}, db_path=db_path
            )
            self.assertEqual(len(ag), 1)
            self.assertEqual(ag[0]["display_title"], "日报自动化 [T3·t3code]")
            self.assertEqual(ag[0]["user_prompts"], ["把自动化搬到 T3 Code 上跑"])
            self.assertEqual(len(wb), 1)
            self.assertEqual(wb[0]["display_title"], "打招呼 [T3·Codebuddy/t3code]")
            self.assertEqual(len(other), 1)
            self.assertEqual(other[0]["source"], "Claude")
            self.assertEqual(len(cx), 0)
            self.assertEqual(hints.get("cx-uuid-1"), "Codex已采集 [T3·t3code]")

    def test_memory_index_generation_and_queries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, "data")
            raw_dir = os.path.join(data_dir, "raw", "2026-11")
            os.makedirs(raw_dir, exist_ok=True)
            days = [
                {
                    "workday": "2026-11-20",
                    "weekday": "周五",
                    "active_span": "10:00-12:00",
                    "headline": "旧方案尝试",
                    "core_outputs": "初版脚本",
                    "work_items": [
                        {
                            "scope": "personal",
                            "project": "记忆库改造",
                            "category": "自动化",
                            "tool": "Antigravity",
                            "session_name": "方案设计",
                            "summary": "使用旧方案 A",
                            "outputs": "old.py",
                            "status": "进行中",
                            "next_step": "待重构",
                            "active_span": "10:00-11:00",
                            "estimated_hours": 1.0,
                        },
                        {
                            "scope": "work",
                            "project": "政务海报",
                            "category": "设计",
                            "tool": "Codex",
                            "session_name": "海报定稿",
                            "summary": "输出最终海报",
                            "outputs": "poster.psd",
                            "status": "已完成",
                            "next_step": "-",
                            "active_span": "11:00-12:00",
                            "estimated_hours": 1.0,
                        },
                    ],
                    "life_items": [],
                },
                {
                    "workday": "2026-11-24",
                    "weekday": "周二",
                    "active_span": "14:00-16:00",
                    "headline": "记忆库落地",
                    "core_outputs": "query_memory.py",
                    "project_briefs": {"记忆库改造": {"ag": "切换到最新方案 B 并上线检索 CLI"}},
                    "work_items": [
                        {
                            "scope": "personal",
                            "project": "记忆库改造",
                            "category": "自动化",
                            "tool": "Antigravity",
                            "session_name": "检索CLI开发",
                            "summary": "落地新方案 B",
                            "outputs": "query_memory.py",
                            "sources": [
                                {"kind": "session", "id": "uuid-mem-123", "bucket": "antigravity",
                                 "machine": "", "workspace": "~/demo"},
                                {"kind": "commit", "id": "abc1234", "repo": "~/demo",
                                 "machine": "", "files": ["query_memory.py"]},
                            ],
                            "status": "待验证",
                            "next_step": "跑完单测",
                            "active_span": "14:00-16:00",
                            "estimated_hours": 2.0,
                        }
                    ],
                    "life_items": [
                        {
                            "tool": "Antigravity",
                            "session_name": "模型对比",
                            "topic": "Gemini 4 档位",
                            "summary": "讨论 Neon 与 Hydrogen 区别",
                            "active_span": "15:00",
                        }
                    ],
                },
            ]
            with open(os.path.join(data_dir, "summary_all_history.json"), "w", encoding="utf-8") as f:
                json.dump({"days": days}, f, ensure_ascii=False)
            with open(os.path.join(raw_dir, "raw_2026-11-24.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "workday": "2026-11-24",
                        "antigravity": [
                            {
                                "source": "Antigravity",
                                "id": "uuid-mem-123",
                                "display_title": "检索CLI开发",
                                "workspace": "~/demo",
                                "user_prompts": ["实现 query_memory.py 并支持 raw 下钻"],
                                "assistant_milestones": ["完成索引与检索逻辑"],
                            },
                            {
                                "source": "Antigravity",
                                "id": "unrelated-id",
                                "display_title": "记忆库改造与 query_memory.py",
                                "workspace": "~/demo",
                            }
                        ],
                        "git": [
                            {
                                "repo": "~/demo",
                                "repo_name": "demo",
                                "commits": [
                                    {
                                        "hash": "abc1234",
                                        "time": "15:30",
                                        "subject": "feat: add query_memory",
                                        "files": ["query_memory.py"],
                                    }
                                ],
                            }
                        ],
                        "files": [],
                    },
                    f,
                    ensure_ascii=False,
                )

            idx_path = os.path.join(tmpdir, "MEMORY_INDEX.md")
            stats = query_memory.generate_memory_index(days, output_path=idx_path)
            self.assertEqual(stats["days"], 2)
            self.assertEqual(stats["work_projects"], 1)
            self.assertEqual(stats["personal_projects"], 1)
            with open(idx_path, "r", encoding="utf-8") as f:
                md = f.read()
            self.assertIn("活跃与待跟进项目（置顶）", md)
            self.assertIn("切换到最新方案 B 并上线检索 CLI", md)
            self.assertIn("政务海报", md)
            self.assertIn("跑完单测", md)

            tl = query_memory.query_project_timeline(days, "记忆库", include_raw=True, data_dir=data_dir)
            self.assertEqual(tl["total_matches"], 2)
            self.assertEqual(tl["entries"][0]["workday"], "2026-11-24")
            self.assertEqual([s["id"] for s in tl["raw"][0]["sessions"]], ["uuid-mem-123"])
            self.assertEqual(tl["entries"][1]["source_status"], "来源待核对")
            self.assertIn("来源待核对", query_memory.format_project_query_md(tl))

            old = query_memory.query_exact_date(days, "2026-11-20", include_raw=True, data_dir=data_dir)
            self.assertIsNone(old["raw"])
            self.assertIn("来源待核对", query_memory.format_exact_date_md(old))
            offline = query_memory.query_project_timeline(days, "记忆库", include_raw=True, data_dir=tmpdir)
            self.assertEqual(offline["entries"][0]["raw_sources_unavailable"], 2)
            self.assertIn("uuid-mem-123", query_memory.format_project_query_md(offline))

            kw = query_memory.query_keyword(days, "query_memory", include_raw=True, data_dir=data_dir)
            self.assertEqual(kw["total_matches"], 1)
            self.assertEqual(kw["raw"][0]["git"][0]["commits"][0]["hash"], "abc1234")

            life_kw = query_memory.query_keyword(days, "Hydrogen", scope="life", data_dir=data_dir)
            self.assertEqual(life_kw["total_matches"], 1)
            self.assertEqual(life_kw["hits"][0]["kind"], "life_item")

    def test_provenance_verifies_sources_and_preserves_them_in_workbook(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "work.xlsx")
            raw = {
                "codex": [
                    {"id": "shared-id", "machine": "mac", "cwd": "/verified/work"},
                    {"id": "shared-id", "machine": "win", "workspace": "D:\\other"},
                ],
                "git": [{
                    "machine": "mac", "repo": "/verified/work",
                    "commits": [{"hash": "abc1234", "files": ["main.py"]}],
                }],
                "files": [{"machine": "mac", "paths": ["/verified/work/out.md"], "sample_files": ["out.md"]}],
            }
            item = {"project": "多端测试", "scope": "work", "summary": "发布报告", "sources": [
                {"kind": "session", "id": "shared-id"},
                {"kind": "session", "id": "shared-id", "machine": "mac", "bucket": "codex", "workspace": "/invented"},
                {"kind": "commit", "id": "abc1234", "repo": "/verified/work", "files": ["invented.py"]},
                {"kind": "commit", "id": "abc1234", "repo": "/other/repo"},
                {"kind": "file", "id": "/verified/work/out.md"},
                {"kind": "file", "id": "out.md"},
            ]}
            prior = {"workday": "2026-11-20", "work_items": [{"project": "旧项目", "manual_note": "保留人工备注"}]}
            write_excel.update_excel([prior], path, include_life=False)

            day = verify_day_sources({"workday": "2026-11-21", "work_items": [item]}, raw)
            sources = day["work_items"][0]["sources"]
            self.assertEqual([ref["kind"] for ref in sources], ["session", "commit", "file"])
            self.assertEqual(sources[0]["workspace"], "/verified/work")
            self.assertEqual(sources[1]["files"], ["main.py"])

            raw_dir = os.path.join(tmpdir, "raw", "2026-11")
            os.makedirs(raw_dir)
            raw["files"][0]["paths"].append("/verified/work/unrelated.md")
            with open(os.path.join(raw_dir, "raw_2026-11-21.json"), "w", encoding="utf-8") as f:
                json.dump(raw, f)
            result = query_memory.query_exact_date([day], "2026-11-21", include_raw=True, data_dir=tmpdir)
            self.assertEqual(result["raw"]["files"][0]["sample_files"], ["/verified/work/out.md"])
            self.assertEqual([s["machine"] for s in result["raw"]["sessions"]], ["mac"])

            write_excel.update_excel([day], path, include_life=False)
            wb = load_workbook(path)
            sheet = wb["项目流程明细"]
            self.assertEqual(sheet["N4"].value, "出处")
            self.assertIn("/verified/work/out.md", sheet["N5"].value)
            self.assertEqual(sheet["M6"].value, "保留人工备注")
            self.assertEqual(sheet["N6"].value, "来源待核对")
            wb.close()

    def test_sync_global_rules_idempotency_and_preservation(self) -> None:
        import importlib.util

        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills", "daily-memory", "scripts", "sync_global_rules.py")
        spec = importlib.util.spec_from_file_location("sync_global_rules", script_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Empty file -> appended, second run -> unchanged
            empty_fp = os.path.join(tmpdir, "AGENTS.md")
            open(empty_fp, "w", encoding="utf-8").close()
            self.assertEqual(mod.check_rule_file(empty_fp), "missing_block")
            r1 = mod.sync_rule_file(empty_fp)
            self.assertEqual(r1["action"], "appended")
            self.assertEqual(mod.check_rule_file(empty_fp), "synced")
            r2 = mod.sync_rule_file(empty_fp)
            self.assertEqual(r2["action"], "unchanged")

            # 2. Existing non-empty content + outdated block -> preserves surrounding content
            mixed_fp = os.path.join(tmpdir, "GEMINI.md")
            with open(mixed_fp, "w", encoding="utf-8") as f:
                f.write("<!-- FLUE START -->\nkeep me\n<!-- FLUE END -->\n\n")
                f.write(f"{mod.START_MARKER}\nold stale block\n{mod.END_MARKER}\n\n# Footer\n")
            self.assertEqual(mod.check_rule_file(mixed_fp), "outdated")
            r3 = mod.sync_rule_file(mixed_fp)
            self.assertEqual(r3["action"], "updated")
            with open(mixed_fp, "r", encoding="utf-8") as f:
                final_text = f.read()
            self.assertIn("keep me", final_text)
            self.assertIn("# Footer", final_text)
            self.assertIn("MEMORY_INDEX.md", final_text)
            self.assertNotIn("old stale block", final_text)
            self.assertEqual(mod.sync_rule_file(mixed_fp)["action"], "unchanged")

            # 3. Skill symlink idempotency
            skills_dir = os.path.join(tmpdir, "skills")
            src_dir = os.path.join(tmpdir, "src_skill")
            os.makedirs(skills_dir)
            os.makedirs(src_dir)
            s1 = mod.sync_skill_symlink(skills_dir, source_dir=src_dir, skill_name="daily-memory")
            self.assertEqual(s1["action"], "linked")
            s2 = mod.sync_skill_symlink(skills_dir, source_dir=src_dir, skill_name="daily-memory")
            self.assertEqual(s2["action"], "unchanged")


class NasHubAndMultiMachineTests(unittest.TestCase):
    def _sample_raw(self, workday: str, machine: str, session_id: str, title: str, workspace: str, span: str) -> dict:
        return {
            "workday": workday,
            "weekday": "周三",
            "machine": machine,
            "window_local": f"{workday} 05:00 ~ next 05:00 (Asia/Shanghai)",
            "overall_active_span": span,
            "overall_estimated_hours": 1.25,
            "antigravity_hours": 1.25,
            "codex_hours": 0.0,
            "workbuddy_hours": 0.0,
            "t3_other_hours": 0.0,
            "existing_projects": [],
            "counts": {
                "antigravity_sessions": 1,
                "codex_sessions": 0,
                "workbuddy_sessions": 0,
                "t3_other_sessions": 0,
                "git_repos": 1,
                "file_groups": 0,
            },
            "antigravity": [
                {
                    "id": session_id,
                    "source": "Antigravity",
                    "title": title,
                    "workspace": workspace,
                    "active_span": span,
                    "estimated_hours": 1.25,
                    "user_prompts": [f"继续推进 {title}"],
                    "assistant_milestones": [f"在 {machine} 完成 {title} 验证"],
                }
            ],
            "codex": [],
            "workbuddy": [],
            "t3_other": [],
            "git": [
                {
                    "repo": workspace,
                    "repo_name": os.path.basename(workspace.replace("\\", "/")),
                    "commits": [{"hash": "abc1234", "time": "10:30", "subject": f"{machine} feat", "files": ["main.py"]}],
                }
            ],
            "files": [],
        }

    def test_multi_machine_sharded_raw_merge_without_conflict(self) -> None:
        import multi_machine

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, "data")
            mac_raw = self._sample_raw("2026-11-25", "macbook", "ag-mac-1", "gpu-broker 服务端", "/Users/dev/gpu-broker", "09:10-10:40")
            win_raw = self._sample_raw("2026-11-25", "win-pc", "ag-win-1", "gpu-broker Windows Agent", "D:\\projects\\gpu-broker", "14:00-15:30")

            r1 = multi_machine.save_machine_raw(data_dir, "macbook", mac_raw)
            self.assertEqual(r1["machines"], ["macbook"])

            r2 = multi_machine.save_machine_raw(data_dir, "win-pc", win_raw)
            self.assertEqual(r2["machines"], ["macbook", "win-pc"])
            self.assertEqual(r2["counts"]["antigravity_sessions"], 2)
            self.assertEqual(r2["counts"]["git_repos"], 2)

            merged = query_memory.load_raw_day("2026-11-25", data_dir=data_dir)
            assert merged is not None
            self.assertEqual(merged["overall_active_span"], "09:10-15:30")
            self.assertEqual(merged["overall_estimated_hours"], 2.5)
            self.assertEqual({s["machine"] for s in merged["antigravity"]}, {"macbook", "win-pc"})

            # Re-uploading win-pc updates win-pc shard without wiping macbook
            r3 = multi_machine.save_machine_raw(data_dir, "win-pc", win_raw)
            self.assertEqual(r3["counts"]["antigravity_sessions"], 2)

    def test_nas_server_auth_mcp_failover_and_offline_cache(self) -> None:
        import subprocess
        import sys
        import threading
        from urllib import request as urlrequest
        import remote_client
        from server import app as hub_app

        os.environ["DAILY_MEMORY_QUIET_LOG"] = "1"
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = os.path.join(tmpdir, "nas_storage")
            cache_dir = os.path.join(tmpdir, "client_cache")
            state = hub_app.HubState(
                storage_root=storage_root,
                read_token="read-secret-token",
                write_token="write-secret-token",
                allow_fallback_summarize=True,
            )
            srv = hub_app.create_server("127.0.0.1", 0, state)
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            base_url = f"http://127.0.0.1:{port}"

            try:
                # 1. Healthz works without token; /api/v1/status rejects missing token
                with urlrequest.urlopen(f"{base_url}/healthz", timeout=3) as resp:
                    self.assertEqual(resp.status, 200)

                bad_rc = remote_client.RemoteConfig(
                    endpoints=[remote_client.Endpoint("local", base_url, 3.0)],
                    read_token="wrong",
                    write_token="wrong",
                    machine_id="macbook",
                    cache_dir=cache_dir,
                )
                with self.assertRaises(remote_client.RemoteAuthError):
                    remote_client.request_json(bad_rc, "GET", "/api/v1/status")

                # 2. Multi-endpoint failover: dead LAN endpoint -> working fallback endpoint
                ro_rc = remote_client.RemoteConfig(
                    endpoints=[
                        remote_client.Endpoint("dead-lan", "http://127.0.0.1:1", 0.5),
                        remote_client.Endpoint("ipv4-https", base_url, 3.0),
                    ],
                    read_token="read-secret-token",
                    write_token="read-secret-token",  # read-only token must fail write endpoints
                    machine_id="company-laptop",
                    cache_dir=cache_dir,
                )
                rw_rc = remote_client.RemoteConfig(
                    endpoints=[
                        remote_client.Endpoint("dead-lan", "http://127.0.0.1:1", 0.5),
                        remote_client.Endpoint("ipv4-https", base_url, 3.0),
                    ],
                    read_token="read-secret-token",
                    write_token="write-secret-token",
                    machine_id="macbook",
                    cache_dir=cache_dir,
                )

                mac_raw = self._sample_raw("2026-11-25", "macbook", "ag-mac-1", "gpu-broker Mac联调", "/Users/dev/gpu-broker", "09:00-10:00")
                win_raw = self._sample_raw("2026-11-25", "win-pc", "ag-win-1", "gpu-broker Win联调", "D:\\code\\gpu-broker", "14:00-15:00")

                # Read-only token must be blocked on ingest/raw
                with self.assertRaises(remote_client.RemoteAuthError):
                    remote_client.push_raw(ro_rc, mac_raw, machine_id="macbook")

                # Write token succeeds via failover endpoint
                res1, ep1 = remote_client.push_raw(rw_rc, mac_raw, machine_id="macbook")
                self.assertTrue(res1["ok"])
                self.assertEqual(ep1.name, "ipv4-https")
                res2, _ = remote_client.push_raw(rw_rc, win_raw, machine_id="win-pc")
                self.assertEqual(res2["machines"], ["macbook", "win-pc"])

                # 3. Trigger NAS-side fusion summarize (fallback rule mode) -> builds Excel + JSON + MEMORY_INDEX.md
                sum_res, _ = remote_client.request_json(
                    rw_rc,
                    "POST",
                    "/api/v1/summarize",
                    json_body={"workday": "2026-11-25", "allow_fallback": True},
                    write=True,
                )
                self.assertEqual(sum_res["status"], "ok")
                self.assertEqual(sum_res["workdays"], ["2026-11-25"])
                self.assertTrue(os.path.exists(state.memory_index_path))
                self.assertTrue(os.path.exists(state.personal_excel_path))

                # 4. Read-only client queries via REST & syncs local offline cache
                idx_md, _ = remote_client.request_text(ro_rc, "GET", "/api/v1/index")
                self.assertIn("gpu-broker", idx_md)
                win_only_md, _ = remote_client.request_text(
                    ro_rc, "GET", "/api/v1/query", params={"project": "gpu-broker", "machine": "win-pc", "raw": "1"}
                )
                self.assertIn("ag-win-1", win_only_md)
                self.assertNotIn("ag-mac-1", win_only_md)

                project = {"action": "merge", "target": "gpu-broker", "labels": ["推理调度器"]}
                with self.assertRaises(remote_client.RemoteAuthError):
                    remote_client.request_json(
                        ro_rc, "POST", "/api/v1/project-review", json_body=project, write=True,
                    )
                edited, _ = remote_client.request_json(
                    rw_rc, "POST", "/api/v1/project-review", json_body=project, write=True,
                )
                self.assertEqual(edited["project"]["aliases"], ["推理调度器"])
                review, _ = remote_client.request_json(ro_rc, "GET", "/api/v1/project-review")
                self.assertEqual(review["suggestions"], [])

                sync_info = remote_client.sync_local_cache(ro_rc)
                self.assertEqual(sync_info["total_days"], 1)
                self.assertTrue(os.path.exists(sync_info["index_path"]))

                # 5. Remote MCP (Streamable HTTP JSON-RPC 2.0) with read-only token
                init_resp, _ = remote_client.request_json(
                    ro_rc,
                    "POST",
                    "/mcp",
                    json_body={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
                    write=False,
                )
                self.assertEqual(init_resp["result"]["serverInfo"]["name"], "daily-memory-hub")

                tools_resp, _ = remote_client.request_json(
                    ro_rc,
                    "POST",
                    "/mcp",
                    json_body={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                    write=False,
                )
                tool_names = [tool["name"] for tool in tools_resp["result"]["tools"]]
                self.assertIn("memory_get_index", tool_names)
                self.assertIn("memory_query", tool_names)

                call_resp, _ = remote_client.request_json(
                    ro_rc,
                    "POST",
                    "/mcp",
                    json_body={
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "memory_query", "arguments": {"project": "推理调度器", "machine": "macbook", "raw": True}},
                    },
                    write=False,
                )
                call_text = call_resp["result"]["content"][0]["text"]
                self.assertIn("ag-mac-1", call_text)
                self.assertNotIn("ag-win-1", call_text)
            finally:
                srv.shutdown()
                srv.server_close()

            # 6. Offline fallback: server is now down; query_memory.py must transparently fall back to local cache
            remote_cfg_path = os.path.join(tmpdir, "remote.json")
            with open(remote_cfg_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "endpoints": [{"name": "down-nas", "url": base_url, "timeout": 0.5}],
                        "read_token": "read-secret-token",
                        "cache_dir": cache_dir,
                    },
                    fh,
                )
            proc = subprocess.run(
                [
                    sys.executable,
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), "query_memory.py"),
                    "--remote",
                    "--remote-config",
                    remote_cfg_path,
                    "--project",
                    "推理调度器",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("gpu-broker", proc.stdout)
            self.assertIn("offline cache", proc.stderr)

    def test_summarizer_openai_compatible_mock(self) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading
        import multi_machine
        import summarizer

        mock_reply = {
            "workday": "2026-11-25",
            "weekday": "周三",
            "headline": "双端联调 gpu-broker 并部署飞牛 NAS 记忆中心",
            "core_outputs": "完成双端联调与合并测试",
            "work_items": [
                {
                    "project": "gpu-broker 多机联调",
                    "category": "工具开发",
                    "tool": "Antigravity",
                    "machine": "win-pc",
                    "session_name": "gpu-broker Win联调",
                    "summary": "在 Windows 台式机完成推理节点验证。",
                    "outputs": "D:\\code\\gpu-broker",
                    "sources": [
                        {"kind": "session", "id": "ag-win-1", "bucket": "antigravity", "machine": "win-pc"},
                        {"kind": "session", "id": "invented-id"},
                    ],
                    "active_span": "14:00-15:00",
                    "estimated_hours": 1.0,
                    "status": "进行中",
                    "next_step": "接入飞牛定时汇总",
                    "scope": "personal",
                }
            ],
            "life_items": [],
            "project_briefs": {"gpu-broker 多机联调": {"ag": "完成 Windows 节点联调", "codex": "-", "wb": "-"}},
        }

        class MockLLMHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                self.server.request_body = json.loads(self.rfile.read(length))
                body = json.dumps(
                    {"choices": [{"message": {"content": json.dumps(mock_reply, ensure_ascii=False)}}]},
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        llm_srv = HTTPServer(("127.0.0.1", 0), MockLLMHandler)
        llm_port = llm_srv.server_address[1]
        threading.Thread(target=llm_srv.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                data_dir = os.path.join(tmpdir, "data")
                raw = self._sample_raw("2026-11-25", "win-pc", "ag-win-1", "gpu-broker Win联调", "D:\\code\\gpu-broker", "14:00-15:00")
                multi_machine.save_machine_raw(data_dir, "win-pc", raw)
                result = summarizer.summarize_workday(
                    "2026-11-25",
                    data_dir,
                    llm_base_url=f"http://127.0.0.1:{llm_port}/v1",
                    llm_api_key="test-key",
                    llm_model="mock-model",
                )
                self.assertEqual(result["workday"], "2026-11-25")
                self.assertEqual(result["machines"], ["win-pc"])
                self.assertEqual(result["personal_estimated_hours"], 1.0)
                self.assertEqual(result["work_items"][0]["machine"], "win-pc")
                sources = result["work_items"][0]["sources"]
                self.assertEqual([ref["id"] for ref in sources], ["ag-win-1"])
                self.assertEqual(sources[0]["workspace"], "D:\\code\\gpu-broker")
                self.assertIn("填写支持本条描述的 sources", llm_srv.request_body["messages"][0]["content"])
        finally:
            llm_srv.shutdown()
            llm_srv.server_close()


if __name__ == "__main__":
    unittest.main()
