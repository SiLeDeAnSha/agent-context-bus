---
name: daily-memory
description: 跨端 AI 记忆中枢（Agent Context Bus）检索与维护 skill（Antigravity / Codex / WorkBuddy / Claude Code）：秒查项目最新进度与物理路径、按关键词/时间线/原始会话 UUID 与 Git 提交溯源、跨工具接力续做、登记项目目录与配置文件路径、实时记笔记、刷新或补跑自动化日报、导出脱敏对外周报/日报，以及一键幂等同步各 AI 客户端全局记忆规则与 Stop 钩子。触发词：找一下之前弄的、接着之前的继续、上次做到哪了、查历史记录、刷新日报、补跑日报、导出周报、同步记忆规则、跨端 AI 记忆中枢、Agent Context Bus、daily-memory。
---

# 跨端 AI 记忆中枢 · Agent Context Bus（Daily Memory）

基于本地优先的跨端 AI 记忆中枢（`~/.gemini/antigravity/scratch/daily-summary/`）与输出目录（`~/Documents/DailyReport/`），为 Antigravity、Codex、Claude Code、WorkBuddy 提供统一的情景记忆、项目关键路径与现行状态索引。

## 核心铁律

1. **默认只读**：日常查历史、找文件、续接项目时，不得修改 `DailyReport/`、`data/` 或任何全局规则文件。
2. **禁止优先解析 `.xlsx`**：Excel（`每日工作汇总.xlsx` / `个人技术与折腾汇总.xlsx`）仅作人工查阅视图；除非用户明确要求核对或编辑表格，一律读 `MEMORY_INDEX.md`、CLI 输出或底层 JSON。
3. **禁止用 `对外报告/` 作事实源**：`对外报告/` 经过脱敏与过滤，缺失个人项与技术细节。
4. **时间优先 + 现行代码复核**：日报是按天追加的流水账，同一项目若前后方案冲突，以**最近日期记录**为准，并在动手改代码前读取目标项目当前文件二次确认。

---

## 工作流 A：查历史记忆与跨端接力（默认，只读）

按以下顺序按需下钻，控制上下文体积：

1. **L2 现行状态总览（首选，~18 KB）**：
   直接读取 `~/Documents/DailyReport/MEMORY_INDEX.md`，即可看到全部置顶进行中项目、正式工作与个人技术项目最新状态、下一步/卡点及近 7 天主线。
2. **精确检索（零依赖 `python3`，无需 `.venv`）**：
   ```bash
   # 查某项目完整演进时间线（最新在前；加 --asc 按时间正序）
   python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --project "<项目关键词>" --limit 20

   # 跨项目/随想搜关键词（空格分隔多词为 AND 匹配）
   python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --keyword "<词1 词2>" --limit 15

   # 按维度与时间窗口筛选项目列表（scope: all|work|personal|life）
   python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --projects --scope personal --days 14 --status "进行中"

   # 查看指定日期全天记录
   python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --date YYYY-MM-DD
   ```
3. **进展出处与原会话定位（v0.2.0）**：
   - 项目、关键词与日期查询结果会直接显示已校验的出处引用（`session` / `commit` / `file`；缺少可核验引用显示「来源待核对」）。加 `--raw` 只展开命中进展所引用的原始记录。
   - 需要找日报摘要和 `--raw` 省略的命令、报错原文或对话细节时，用出处里的会话 ID 搜索本机原会话：
   ```bash
   python3 ~/.gemini/antigravity/scratch/daily-summary/search_sessions.py --keyword "<命令或报错的一段原文>" --session-id "<会话 ID>"
   ```
   会话 ID 未知时可用 `--client antigravity|codex|workbuddy`、`--date YYYY-MM-DD`、`--from`/`--to` 缩小范围，或加 `--json` 输出结构化结果。默认返回最近 5 个命中片段及其出处；只读命中片段即可，普通日报检索仍走前两步。

---

## 工作流 B：刷新 / 补跑日报与重建索引

仅当用户明确要求刷新今日、补跑漏跑日期或重建索引时执行：

```bash
# 1. 采集昨日及漏跑日期原始轨迹（支持 --date YYYY-MM-DD / --days N）
~/.gemini/antigravity/scratch/daily-summary/.venv/bin/python \
  ~/.gemini/antigravity/scratch/daily-summary/collect.py --yesterday --missing

# 2. 将生成的汇总 JSON 写入双 Excel、同步按月/全量 JSON，并自动覆盖刷新 MEMORY_INDEX.md
~/.gemini/antigravity/scratch/daily-summary/.venv/bin/python \
  ~/.gemini/antigravity/scratch/daily-summary/write_excel.py --input <summary_json_path>

# 仅需基于现有 summary_all_history.json 重新生成 MEMORY_INDEX.md 时：
python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --rebuild-index
```

---

## 工作流 C：导出脱敏对外日报 / 周报

基于正式工作表自动过滤敏感与个人项并脱敏本机路径：

```bash
# 导出最近 7 天周报（也支持 --date YYYY-MM-DD 或 --start YYYY-MM-DD --end YYYY-MM-DD）
~/.gemini/antigravity/scratch/daily-summary/.venv/bin/python \
  ~/.gemini/antigravity/scratch/daily-summary/export_report.py --days 7
```

---

## 工作流 D：一键幂等装配 / 同步多端全局规则（Bootstrap）

**注意**：日常查记忆时不要触发本流程。仅在用户要求“把记忆库接到各端 / 同步全局规则 / 换机初始化”时执行。

同步脚本通过 `<!-- DAILY-SUMMARY START -->` … `<!-- DAILY-SUMMARY END -->` 锚点幂等更新，绝不改动锚点外的其他规则（如 `FLUE` 区块或个人档案内容）：

```bash
# 1. 先只读检查四端（Antigravity / Codex / Claude / WorkBuddy）规则与 Skill 软链状态
python3 ~/.cc-switch/skills/daily-memory/scripts/sync_global_rules.py --check

# 2. 幂等同步四端全局规则、补齐 Skill 软链并装配 Antigravity/Codex Stop 钩子（0 注入，仅异步写入实时层）
python3 ~/.cc-switch/skills/daily-memory/scripts/sync_global_rules.py --sync-skills --sync-hooks

# 如需关闭 Stop 钩子，一键回退：
python3 ~/.cc-switch/skills/daily-memory/scripts/sync_global_rules.py --disable-hooks
```

- 规则目标文件：
  - Antigravity: `~/.gemini/GEMINI.md`
  - Codex: `~/.codex/AGENTS.md`
  - Claude Code: `~/.claude/CLAUDE.md`
  - WorkBuddy: `~/.workbuddy/USER.md`
- 若需仅同步指定端，可加 `--targets codex,claude`；先预览变更可加 `--dry-run`。

---

## 工作流 E：飞牛 NAS 跨电脑中心模式（多端点自动切换 + Remote MCP）

当配置了 `~/.daily-memory/remote.json` 或 `DAILY_MEMORY_URLS` 时，`query_memory.py` 自动按 **LAN → Tailscale/组网 → IPv6 HTTPS → IPv4 公网 HTTPS** 优先级切换访问飞牛 NAS，全部离线时自动降级读取本地缓存（`~/.daily-memory/cache/`）：

```bash
# 探测全部网络线路延迟与连通性
python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --probe

# 同步 NAS 最新 MEMORY_INDEX.md 与全量历史到本地离线缓存
python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --sync-cache

# 按来源主机筛选（如只看 Mac 或 Windows 台式机记录）
python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --project "gpu-broker" --machine win-desktop --raw

# 本机采集并推送 raw 分片到飞牛 NAS（由 NAS 定时 LLM 融合汇总，无多机写入冲突）
python3 ~/.gemini/antigravity/scratch/daily-summary/collect.py --yesterday --missing --machine macbook-pro --push
```

- 部署脚本与配置模板位于 `~/.gemini/antigravity/scratch/daily-summary/deploy/`（含 `nas_deploy.sh`、`NAS_DEPLOYMENT.md`、`mcp_client_examples.json`）。

---

## 工作流 F：项目身份、别名、物理路径登记与首次导入向导

旧日报与新日报统一按稳定 `project_id` 聚合，查询接受主名称、别名、ID 或已登记路径。仅当用户明确要求整理重名项目、登记项目关键路径、审核 AI 合并建议或重跑首次导入时执行写操作：

```bash
# 1. 查看 AI 建议合并的重名/候选项目（只读）
python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --project-review

# 2. 合并项目别名（保留第一个项目的 ID 并自动合并去重双方 paths；写操作，配置 NAS 时自动走 NAS 并需写权限）
python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --merge-projects "<主项目名>" "<别名1>" "<别名2>"

# 3. 拆分误合并别名，或将候选名称确认为独立项目
python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --split-project "<别名>"
python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --split-project "<新项目主名称>" "<已合并别名A>" "<已合并别名B>"

# 4. 登记、移除或覆盖项目的物理目录 / 关键配置文件路径（会直接呈现在 MEMORY_INDEX.md 与 --project 顶部）
python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --add-path "<项目名>" "<路径1>" "<路径2>"
python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --remove-path "<项目名>" "<路径>"
python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --set-paths "<项目名>" "<路径1>" "<路径2>"

# 5. 当日 0 Token 实时动态记一笔 / 强制刷新实时层（跨端交接立即生效）
python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --note "<项目名>" "<刚完成的关键结论或改动>"
python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --live-sync

# 如需仅操作本机登记表，加 --local 或 --data-dir；更新后按需 --rebuild-index / --sync-cache

# 5. 首次导入终端向导（检查本机 4 端可读历史并导入近 7 天到 data/first-import/，不影响正式日报）
python3 ~/.gemini/antigravity/scratch/daily-summary/onboarding.py
```
