# DailyReport · 跨端 AI 记忆中枢（Agent Context Bus）入口

本目录是本地优先的跨端 AI 记忆中枢（Agent Context Bus，每日采集 Antigravity / Codex / WorkBuddy 多端轨迹）的输出目录，为所有客户端提供统一的**情景记忆库、项目关键路径与现行状态索引**。

## 1. AI 读取优先级（重要）

> **不要优先解析 `.xlsx` 文件**（Excel 仅作人工查阅视图）。AI 检索历史记录请严格按以下顺序：

1. **先看现行状态快照（首选入口，~15 KB，零成本总览全项目）**：
   `~/Documents/DailyReport/MEMORY_INDEX.md`
   （由日报流水线每次运行自动刷新，置顶进行中/待跟进项目，并汇总全部正式工作与个人技术项目的最新状态、下一步/卡点与近 7 天主线）
2. **按需精准检索与原始溯源（推荐，避免整文件撑爆上下文）**：
   使用零依赖只读 CLI `~/.gemini/antigravity/scratch/daily-summary/query_memory.py`（系统 `python3` 与 `.venv/bin/python` 均可直接运行）：
   - 查某项目完整时间线：
     `python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --project "<项目关键词>"`
   - 跨项目/随想搜关键词（空格分隔多词 AND 匹配，加 `--raw` 可下钻查会话 UUID、工作区路径、用户指令、Git 提交与改动文件）：
     `python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --keyword "<关键词>" --raw`
   - 按维度/时间窗口过滤：
     `python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --projects --scope personal --days 14`
     `python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --date YYYY-MM-DD --raw`
   - 审核/合并/拆分重名项目别名与登记项目物理路径（配置 NAS 时自动走 NAS 登记表）：
     `python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --project-review`
     `python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --merge-projects "<主项目名>" "<别名>"`
     `python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --split-project "<别名>"`
     `python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --add-path "<项目名>" "<物理目录或配置文件路径>"`
     `python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --remove-path "<项目名>" "<路径>"`
     `python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --set-paths "<项目名>" "<路径1>" "<路径2>"`
   - 0 Token 当日实时记一笔与刷新（跨端秒级同步，次日压实后自动退出）：
     `python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --note "<项目名>" "<关键结论或改动>"`
     `python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --live-sync`
   - 检索本机原会话原文片段（v0.2.0，当摘要和 `--raw` 省略具体命令/报错原文时按出处会话 ID 下钻）：
     `python3 ~/.gemini/antigravity/scratch/daily-summary/search_sessions.py --keyword "<原文关键词>" --session-id "<会话 ID>"`
3. **直接读结构化底层数据（需自定义统计或二次分析时）**：
   - 全量汇总：`~/.gemini/antigravity/scratch/daily-summary/data/summary_all_history.json`
   - 按月汇总：`~/.gemini/antigravity/scratch/daily-summary/data/months/summary_<YYYY-MM>.json`
   - 项目身份登记表：`~/.gemini/antigravity/scratch/daily-summary/data/project_registry.json`
   - 原始采集（含会话列表、Git 提交、本地改动文件）：`~/.gemini/antigravity/scratch/daily-summary/data/raw/<YYYY-MM>/raw_<YYYY-MM-DD>.json`

## 2. 目录边界与禁区

| 路径 | 用途 | AI 使用说明 |
|---|---|---|
| `MEMORY_INDEX.md` | 自动生成的全项目现行状态快照 | **首选入口**，只读，勿手工编辑（会被流水线覆盖） |
| `每日工作汇总.xlsx` | 正式业务台账 | 人工查阅视图 |
| `个人技术与折腾汇总.xlsx` | 个人技术折腾与随想台账 | 人工查阅视图 |
| `YYYY-MM/` | 按月分卷的 Excel 归档 | 人工翻阅与云端增量同步 |
| `对外报告/` | 脱敏导出的对外日报/周报 Markdown | **已过滤敏感与个人项，禁止作为完整事实源** |

## 3. 核心 JSON 字段速查

`summary_all_history.json` 顶层为 `{"days": [...]}`，每天记录包含：
- `workday` / `weekday` / `active_span`：工作日期（以凌晨 `05:00` 切日）、星期、活跃时段
- `headline` / `core_outputs`：当日核心主线与主要产出
- `work_items`：项目级明细（`scope`: `work` / `personal`、`project`、`project_id`、`category`、`tool`、`session_name`、`summary`、`outputs`、`sources`（出处校验引用）、`status`、`next_step`、`active_span`、`estimated_hours`）
- `life_items`：零碎问答、生活探索与随想（`tool`、`session_name`、`topic`、`summary`、`active_span`）
- `project_briefs`：按项目聚合的各工具（`ag` / `codex` / `wb`）当日精炼进展

## 4. 使用与冲突裁决规则

- **时间优先**：底层日报是按天追加的流水账，同一项目若出现前后方案或状态不一致，**以最近日期记录为准**，并结合对应项目目录当前代码/文件二次核实。
- **职责边界**：本记忆库负责“做过什么、在哪次会话、做到哪一步”（情景记忆与项目进度），不替代各工具的全局 Rules / Skills（长期偏好与硬约束）。
- **只读原则**：除用户明确要求刷新、补跑或重建索引外，AI 对本目录及 `data/` 下文件保持只读。
