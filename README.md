# 每日工作汇总 (Daily Summary)

自动采集本地 **Antigravity / Codex / WorkBuddy / T3 Code** 多端工作轨迹（其中 T3 Code 通过 `statev2.sqlite` 按实际 Provider 自动归口并与 Codex 去重），支持定时任务触发，并**严格分离为两份独立 Excel 总表**：

1. **正式工作表**：`~/Documents/DailyReport/每日工作汇总.xlsx`（3 个 Sheet：`每日概览`、`项目总览`、`项目流程明细`）
   - 收录正式业务工作（公文材料、海报排版、内容统计、视频制作等）。
2. **个人技术折腾表**：`~/Documents/DailyReport/个人技术与折腾汇总.xlsx`（4 个 Sheet：`每日概览`、`项目总览`、`项目流程明细`、`生活与随想记录`）
   - 收录工具开发、AI 工作流、系统运维、硬件调试等技术折腾，以及生活探索与闲聊。

## 首次导入并查项目

在仓库目录运行 `python3 onboarding.py`（Windows PowerShell 用 `python onboarding.py`）。向导会检查本机的 Antigravity、Codex、WorkBuddy 和 T3 Code 会话来源。按回车导入全部可读客户端，或输入编号选择。导入范围是最近 7 天，含当前工作日，日期按配置的时区和工作日切换时间计算。

导入后，终端会展示真实项目的一条会话记录及出处，并给出可直接复制的 `query_memory.py --project` 命令。项目名优先取客户端目录或工作区，缺少这些信息时使用会话标题。同一次导入碰到同名的不同工作区时，会附上完整路径和机器标识。把命令中的 `--project` 和其值换成 `--projects`，即可浏览这次导入的全部项目。

首次导入在本机运行，使用 Python 3.10+，无需 NAS、Excel 或模型 API。结果按次保存在被 Git 忽略的 `data/first-import/` 下，可用 `--output-dir` 指定其他上级目录。Codex 的 `.zst` 归档需要 Python 3.14+ 或 `zstandard`，Windows 如缺时区数据库可安装 `tzdata`。需要定时报表或跨机同步时，再继续下方的配置。

## 文件夹分月归档结构

为避免单表随时间变得臃肿、同时保留跨月项目汇总和全局检索能力，输出目录默认采用 **「根目录全量总表 + 按月子文件夹分卷」** 双层结构：

- **根目录全量总表**：`每日工作汇总.xlsx`、`个人技术与折腾汇总.xlsx`（涵盖全部历史月份）
- **按月子文件夹**（`YYYY-MM/`）：仅在当月存在记录时生成对应分月表，便于增量同步与按月翻阅
- **对外报告目录**（`对外报告/`）：存放脱敏导出的对外日报 / 周报 Markdown

## 表格结构说明

- **每日概览**：按 **`日期 × 大项目`** 拆成独立短行（同日多项目自动交替底色归组），精简展示各端工作内容、对话名称与跨度小时数。
- **项目总览**：一个项目一行（按最近更新倒序），含 `当前状态`、`下一步 / 卡点`、各端总结与分工具跨度。
- **项目流程明细**：按项目聚拢（项目内按时间正序），**1 个真实侧边栏对话占 1 独立行**，并标注所属目录/空间标签。
- **生活与随想记录**（仅个人表）：独立收录闲聊、生活探索与零碎问答。

## 进展出处

汇总 JSON 的每条进展可提交 `sources`，写入时根据该工作日的原始记录校验，并从原始记录补齐工作目录和提交涉及的文件。「项目流程明细」的「出处」列和项目、关键词、日期查询都会显示已保存的引用。缺少可核验引用的记录显示「来源待核对」。

- 会话引用需要 `{"kind":"session","id":"原会话 ID","bucket":"codex","machine":"机器 ID"}`。`bucket` 对应原始记录的数组名（`antigravity`、`codex`、`workbuddy` 或 `t3_other`）。
- Git 引用需要 `{"kind":"commit","id":"原始记录的 hash","repo":"仓库路径","machine":"机器 ID"}`。
- 文件引用需要 `{"kind":"file","id":"原始记录 paths 中的完整路径","machine":"机器 ID"}`。

同一条进展可以保存多个出处。`--raw` 只展开结果中各条进展引用的原始记录。离线缓存仍显示已保存的出处。重导入摘要时若缺少当天原始档案，保留出处并标为「原始档案暂不可用」。原档可用时会校验引用。

## 查原会话片段

日常先查 `MEMORY_INDEX.md` 或 `query_memory.py`。需要找摘要和 `--raw` 省略的命令、报错原文或对话时，用出处里的会话 ID 缩小范围，再搜索本机原会话。

```bash
python3 search_sessions.py --keyword '报错中的一段原文' --session-id '<会话 ID>'
python3 search_sessions.py --keyword 'ffprobe' --client codex --date 2026-11-24 --limit 3
```

按字面匹配关键词，忽略大小写。默认只返回最近 5 个命中片段，附会话 ID、时间和原文件位置。可用 `--from`、`--to` 限定工作日范围，或用 `--json` 输出结构化结果。检索覆盖本机 Antigravity 完整转录、Codex 活跃及归档会话、WorkBuddy 项目日志和 T3 Code 会话数据库中的消息、命令与工具输出。Codex 的 `.zst` 归档需要 Python 3.14+ 或 Python 的 `zstandard` 包。

## 项目身份、别名和纠错

旧日报会按项目名取得稳定的 `project_id`。查询接受主名称、别名或 ID，并按同一个 ID 汇总历史进展。每条进展同时保留入库时的名称，供纠错使用。AI 可以填写 `suggested_project_id` 和 `project_suggestion_reason`，候选 ID 来自采集结果的 `existing_projects`，由你确认是否合并。

```bash
python3 query_memory.py --project-review
python3 query_memory.py --merge-projects 'ComfyUI 人像与视频管线' 'comfyui-mcp' '保脸工作流'
python3 query_memory.py --project '保脸工作流'
python3 query_memory.py --split-project '保脸工作流'
```

也可以用合并前的别名 ID 拆分，拆出后保留原有名称和 ID。

- 合并保留第一个项目的 ID。被合并的名称和原 ID 都能查到合并后的项目。
- 拆分可把一个别名恢复为独立项目，也可用 `--split-project '新项目主名称' '已合并的别名 A' '已合并的别名 B'` 将多个别名转到指定项目。拆出的名称会独立归档，包括之前和以后以该名称记录的进展。对尚未合并的候选名称执行拆分，会将其确认为独立项目。
- 人工决定保存在 `data/project_registry.json`。配置 NAS 时，查询和纠错使用 NAS 上的登记表，纠错需要写权限。本地模式用 `--local` 或 `--data-dir`。用 `--sync-cache` 更新离线副本。
- 查询立即应用新决定。本地已生成的 `MEMORY_INDEX.md` 随下次日报写入刷新，也可运行 `--rebuild-index` 刷新。Excel 继续展示每条进展的原始项目名称。

## 目录与脚本

- 配置模板：`config.example.json`（复制为 `config.json` 按需修改；未创建 `config.json` 时自动回退读取模板）
- 采集脚本：`collect.py`（支持 `--yesterday --missing` 自动补齐漏跑日期）
- 写表脚本：`write_excel.py`（自动按 `scope: "work" | "personal"` 写入两份独立总表，并在同步 JSON 索引后自动原子刷新 `MEMORY_INDEX.md`）
- AI 记忆索引与检索 CLI：`query_memory.py`（零外部依赖，支持生成 `~/Documents/DailyReport/MEMORY_INDEX.md` 现状快照、按项目/关键词/日期/主机/`--raw` 原始会话与 Git 溯源检索，以及远程多端点自动故障切换 + 断网本地缓存降级）
- 跨电脑 NAS 中心服务（fnOS / Docker）：`server/`（`app.py`、`Dockerfile`、`docker-compose.yml`、`.env.example`）与 `multi_machine.py`、`remote_client.py`、`summarizer.py`：
  - 按 `machine_id` 分片上报防冲突合并（支持 Mac + Windows 同日协同）；
  - REST API + Remote MCP（Streamable HTTP `POST /mcp`）+ 读写分离 Bearer Token + IP 限流 + 双栈 HTTP/HTTPS；
  - NAS 端定时调用 OpenAI 兼容 LLM 接口融合多机原始轨迹并自动更新双 Excel、`summary_all_history.json` 与 `MEMORY_INDEX.md`。
- 部署与多端接入脚本：`deploy/`（`nas_deploy.sh` 一键 SSH 部署与存量迁移、`setup_mac_collector.sh`、`setup_windows_collector.ps1`、`remote.example.json`、`mcp_client_examples.json`、`NAS_DEPLOYMENT.md`）
- 跨端 AI Skill 与规则装配：`skills/daily-memory/`（内置 `SKILL.md` 与 `scripts/sync_global_rules.py`，可一键幂等同步 Antigravity / Codex / Claude Code / WorkBuddy 四端全局记忆规则与 Skill 软链）
- 输出目录入口模板：`templates/DailyReport_README.md`（复制到 `~/Documents/DailyReport/README.md` 作为跨端 AI 读取契约）
- 对外周报/日报导出：`export_report.py`（基于正式工作表导出脱敏 Markdown 周报/日报）
- 云端同步预处理：`prepare_gsheets.py`
- 单元测试：`test_daily_summary.py`

## 配置完整日报流程

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json
cp templates/DailyReport_README.md ~/Documents/DailyReport/README.md

# 重建全项目现行状态快照（零依赖，系统 python3 即可）
python3 query_memory.py --rebuild-index

# 一键幂等同步多端全局记忆规则与 daily-memory Skill 软链
python3 skills/daily-memory/scripts/sync_global_rules.py --sync-skills

# 运行单元测试（需 Python 3.14+ 内置 compression.zstd 与 openpyxl）
.venv/bin/python -m unittest -v test_daily_summary.py
```
