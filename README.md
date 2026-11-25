# 每日工作汇总 (Daily Summary)

自动采集本地 **Antigravity / Codex / WorkBuddy / T3 Code** 多端工作轨迹（其中 T3 Code 通过 `statev2.sqlite` 按实际 Provider 自动归口并与 Codex 去重），支持定时任务触发，并**严格分离为两份独立 Excel 总表**：

1. **正式工作表**：`~/Documents/DailyReport/每日工作汇总.xlsx`（3 个 Sheet：`每日概览`、`项目总览`、`项目流程明细`）
   - 收录正式业务工作（公文材料、海报排版、内容统计、视频制作等）。
2. **个人技术折腾表**：`~/Documents/DailyReport/个人技术与折腾汇总.xlsx`（4 个 Sheet：`每日概览`、`项目总览`、`项目流程明细`、`生活与随想记录`）
   - 收录工具开发、AI 工作流、系统运维、硬件调试等技术折腾，以及生活探索与闲聊。

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

## 快速开始

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
