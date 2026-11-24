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
- 写表脚本：`write_excel.py`（自动按 `scope: "work" | "personal"` 写入两份独立总表）
- 对外周报/日报导出：`export_report.py`（基于正式工作表导出脱敏 Markdown 周报/日报）
- 云端同步预处理：`prepare_gsheets.py`
- 单元测试：`test_daily_summary.py`

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json

# 运行单元测试（需 Python 3.14+ 内置 compression.zstd 与 openpyxl）
.venv/bin/python -m unittest -v test_daily_summary.py
```
