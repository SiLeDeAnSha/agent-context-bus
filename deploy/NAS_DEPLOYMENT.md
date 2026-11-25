# 飞牛 NAS（fnOS）跨电脑 AI 记忆库部署与接入指南

## 架构总览（1A + 2C + IPv4 公网 + 多端接入）

- **唯一中心（飞牛 NAS Docker：`daily-memory-hub`）**：
  - 数据卷挂载到飞牛共享目录（如 `/vol1/1000/DailyMemory`），SMB / 飞牛 App 可直接查看双 Excel 与 `MEMORY_INDEX.md`。
  - **多机防冲突**：每台设备上传 `raw` 到 `data/machines/<machine_id>/raw/`，NAS 加锁自动合并生成全局 `data/raw/<YYYY-MM>/raw_<day>.json`。
  - **定时 LLM 融合汇总（1A）**：每天 `09:05`（可配置）自动调用 OpenAI 兼容 LLM API 将全部设备昨天的 `raw` 融合生成日报、更新双 Excel、JSON 索引与 `MEMORY_INDEX.md`。
  - **读写分离鉴权**：
    - `DAILY_MEMORY_READ_TOKEN`：仅允许读取与 Remote MCP（给公司/临时电脑用）。
    - `DAILY_MEMORY_WRITE_TOKEN`：允许上报 `raw`/`summary` 与触发汇总（给主力 Mac / Windows 用）。

---

## 1. 飞牛 NAS 一键 SSH 部署与存量迁移

待准备好飞牛 SSH 账号/IP 后，在当前 Mac 运行：

```bash
bash ~/.gemini/antigravity/scratch/daily-summary/deploy/nas_deploy.sh \
  --host <user>@<nas-ip> \
  --port 22 \
  --data-dir /vol1/1000/DailyMemory \
  --migrate-data
```

首次部署会自动在远端生成强随机 `READ_TOKEN` 与 `WRITE_TOKEN` 并打印到终端，随后在远端 `server/.env` 补填 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` 并重启容器即可开启每天自动融合总结：

```bash
ssh <user>@<nas-ip> "cd /vol1/1000/docker/daily-memory-hub/server && docker compose up -d"
```

---

## 2. 三网并存访问配置（局域网 / Tailscale / IPv6 HTTPS / IPv4 公网 HTTPS）

客户端 `~/.daily-memory/remote.json` 支持按优先级自动探测与故障切换：

| 优先级 | 线路 | 示例 URL | 适用场景 | 搭建方式 |
| :--- | :--- | :--- | :--- | :--- |
| 1 | `lan` | `http://192.168.x.x:8765` | 在家局域网（<5ms） | Docker 默认端口映射 |
| 2 | `tailscale` | `http://100.x.y.z:8765` | 组网加密直连（零公网暴露） | 飞牛 & 电脑装 Tailscale / EasyTier |
| 3 | `ipv6-https` | `https://nas-v6.yourdomain.com:8443` | 手机热点/支持 IPv6 的外网 | 复用飞牛已有 IPv6 域名 + 内置反代 HTTPS |
| 4 | `ipv4-https` | `https://mem.yourdomain.com` | **纯 IPv4 公司网/任意外网** | 下述任选一种 IPv4 方案 |

### IPv4 公网访问两种内置方案（任选其一）

#### 方案 A：Cloudflare Tunnel（免公网 IPv4、免配证书、推荐）
1. 在 Cloudflare Zero Trust → Networks → Tunnels 创建 Tunnel，拿到 Token 写入飞牛 `server/.env` 的 `CLOUDFLARE_TUNNEL_TOKEN`。
2. 在 Tunnel 的 Public Hostname 添加一条路由：`mem.yourdomain.com` → `HTTP` → `daily-memory-hub:8765`。
3. 飞牛上启用 profile：
   ```bash
   docker compose --profile cloudflared up -d
   ```

#### 方案 B：自有 VPS + FRP（国内低延迟）
1. 复制 `deploy/frpc.example.toml` 为 `deploy/frpc.toml` 并填好 VPS 地址与 Token。
2. 飞牛上启用 profile：`docker compose --profile frpc up -d`。
3. 在 VPS 上用 Caddy/Nginx 将 `https://mem.yourdomain.com` 反代到 `http://127.0.0.1:18765`。

配置完成后在任意电脑一键探测全部链路延迟：
```bash
python3 ~/.gemini/antigravity/scratch/daily-summary/query_memory.py --probe
```

---

## 3. 各终端接入方式

### 主力 Mac / Windows（双向采集 + 查询 + 本地离线缓存）
- **Mac**：
  ```bash
  bash ~/.gemini/antigravity/scratch/daily-summary/deploy/setup_mac_collector.sh --machine macbook-pro
  ```
- **Windows（PowerShell）**：
  ```powershell
  .\deploy\setup_windows_collector.ps1 -MachineId "win-desktop"
  ```
- 断网自动降级：`query_memory.py` 在全部远程线路不可达时会自动回退到 `~/.daily-memory/cache/` 本地离线缓存。

### 公司 / 临时电脑（30 秒只读接入，零数据残留）
- 仅配 `READ_TOKEN`，直接把 `deploy/mcp_client_examples.json` 加入 Antigravity / Claude Code / Codex / WorkBuddy 的 MCP 配置：
  - URL：`https://mem.yourdomain.com/mcp`
  - Header：`Authorization: Bearer <DAILY_MEMORY_READ_TOKEN>`
- 暴露 5 个只读工具：`memory_get_index`、`memory_list_projects`、`memory_query`、`memory_get_raw_day`、`memory_server_status`。
