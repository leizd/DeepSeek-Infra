# 部署指南（Docker / Compose / 裸机）

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


适用版本：v4.4.2。

DeepSeek Infra 默认服务形态是一个单进程 FastAPI / ASGI 运行时：`/v1` OpenAI 兼容网关、`/mcp`、`/a2a`、`/api/*` 业务端点，加 `/healthz`·`/readyz`·`/metrics` 运维三件套。它的可写状态集中在 `DEEPSEEK_INFRA_ROOT`（或兼容变量 `DEEPSEEK_MOBILE_ROOT`）指定的数据目录。另有可选的无状态 MCP 双实例栈；它把持久任务状态放在独立 Redis AOF 卷中，因此不属于默认单卷备份边界。

## 1. Docker Compose（推荐）

```bash
cp .env.example .env        # 填写 DEEPSEEK_API_KEY 等
docker compose up -d
docker compose logs -f deepseek-infra
```

验证：

```bash
curl http://127.0.0.1:8000/healthz
# {"status":"ok","version":"4.4.2",...}
curl http://127.0.0.1:8000/readyz
curl http://127.0.0.1:8000/metrics | head
```

默认 [docker-compose.yml](../docker-compose.yml) 把端口发布在 `127.0.0.1:8000`（因为运维端点不鉴权），数据持久化在命名卷 `deepseek-data`（容器内 `/data`）。

### 取得本地访问 token

`AUTH_DISABLED=0`（默认）时 `/api/*`、`/mcp`、`/a2a` 需要本地 token。两种方式：

- 在 `.env` 里固定 `AUTH_TOKEN=<你自己的随机串>`（推荐，客户端直接用它做 Bearer）；
- 或留空让服务端自动生成，再读出来：`docker compose exec deepseek-infra cat /data/.auth-token`。

浏览器访问用 `http://127.0.0.1:8000/?token=<token>`，API 客户端用 `Authorization: Bearer <token>`。

## 2. 无状态 MCP 双实例 Compose

代码搜索、测试运行、日志查询需要横向扩展与实例故障恢复时，使用独立 Compose：

```powershell
$env:MCP_AUTH_TOKEN = '<replace-with-a-long-random-token>'
docker compose -f docker-compose.stateless-mcp.yml up -d --build
curl http://127.0.0.1:8010/healthz
curl http://127.0.0.1:8010/readyz
curl http://127.0.0.1:8010/instance
```

拓扑包括 Redis（AOF）、OpenTelemetry Collector、`mcp-instance-1`、`mcp-instance-2` 和 NGINX round-robin。客户端只连接 `http://127.0.0.1:8010/mcp`；`8011` / `8012` 只用于本机实例诊断，`9464` 暴露 Collector 的 Prometheus 指标。该服务不需要粘性会话，任务通过 Redis lease/fencing 接管。

生产环境必须覆盖 `MCP_AUTH_TOKEN`，为入口配置 TLS，保持 Redis 不对宿主公网开放，并收紧 `MCP_ALLOWED_HOSTS`。`MCP_WORKSPACE_ROOT` 是代码搜索和 pytest 目标的安全根；服务容器对该目录拥有的权限就是测试代码能获得的权限，不应把不可信仓库交给高权限实例。

Redis 命名卷保存任务参数、日志、结果摘要、租约和幂等索引。它不在 `deepseek-data:/data` 内：备份或迁移时应单独保存 Redis AOF/RDB，并保证与服务版本兼容；删除卷会丢失任务恢复历史。故障恢复演练：

```powershell
npm ci --prefix stateless-mcp
npm run smoke:failover --prefix stateless-mcp
```

演练会启动完整栈、确认轮询分流、创建测试任务、终止 lease owner、验证客户端重试与另一实例接管，并确认相同幂等键不会重复执行。详细说明见 [STATELESS_MCP.md](STATELESS_MCP.md)。

## 3. 纯 Docker

```bash
docker build -t deepseek-infra:4.4.2 .
docker run -d --name deepseek-infra \
  -p 127.0.0.1:8000:8000 \
  --env-file .env \
  -v deepseek-data:/data \
  deepseek-infra:4.4.2
```

镜像要点（见 [Dockerfile](../Dockerfile)）：`python:3.12-slim`、`pip --no-cache-dir`、非 root 用户运行、`HEALTHCHECK` 打 `/healthz`、数据卷 `/data`、静态资源固定在镜像内（`DEEPSEEK_INFRA_STATIC_DIR`，旧变量 `DEEPSEEK_MOBILE_STATIC_DIR` 继续兼容），并在构建后清理 `__pycache__`。CI 的 docker job 会验证默认镜像/Compose，也会构建 [stateless-mcp/Dockerfile](../stateless-mcp/Dockerfile) 并校验 [docker-compose.stateless-mcp.yml](../docker-compose.stateless-mcp.yml)。

## 4. 裸机 / systemd

```bash
python -m pip install -r requirements.txt
cp .env.example .env && $EDITOR .env
set -a && . ./.env && set +a     # 或用你自己的进程管理器注入
python app.py
```

systemd 单元示意：

```ini
[Unit]
Description=DeepSeek Infra
After=network-online.target

[Service]
WorkingDirectory=/opt/deepseek-infra
EnvironmentFile=/opt/deepseek-infra/.env
Environment=DEEPSEEK_INFRA_ROOT=/var/lib/deepseek-infra
ExecStart=/usr/bin/python3 app.py
Restart=on-failure
User=deepseek

[Install]
WantedBy=multi-user.target
```

安全加固可在 `[Service]` 段按需追加 `ProtectSystem=strict`、`ReadWritePaths=/var/lib/deepseek-infra` 等。

## 5. 配置参考

- 模板：[.env.example](../.env.example)（核心变量带注释）；完整清单见 README「环境变量」。
- 数据目录：`DEEPSEEK_INFRA_ROOT`（优先，`DEEPSEEK_MOBILE_ROOT` 向后兼容；容器内默认 `/data`；裸机默认仓库根目录）。各子目录含义见 README「本地数据与隐私」。
- 外接 MCP server（v2.2.1）：默认关闭。启用时设置 `MCP_CLIENT_ENABLED=1` 与 `MCP_CLIENT_SERVERS='[{"name":"docs","url":"http://127.0.0.1:9001/mcp"}]'`，只连接可信地址；上线前用 `GET /api/mcp/external/tools` 核对 bridged tools、风险等级和审批要求。v2.2.2 起，Agent 和 `/mcp tools/call` 两条入口都会在外部 MCP executor 内部再次执行 ToolPolicy。
- 升级：换新镜像 tag 重新 `up -d` 即可；数据目录内的 SQLite schema 由各模块幂等迁移，跨小版本升级无需手工步骤。升级前推荐用“备份与恢复”界面或 `python scripts/backup_workspace.py --out workspace.dsibackup` 生成并校验可移植备份。

### 4.4.2 加密备份、外部覆盖与崩溃恢复策略

- **在线备份（推荐）**：优先使用 age 保护的 `.dsibackup.age`。运行时在跨进程 Mutation Gate 下记录 Contributor generation，流式复制后再次核对；ZIP 直接进入 Rust helper 的加密流，不发布最终明文包。SQLite 使用 backup API，逐文件校验，并排除 `.env`、`.auth-token`、API Key、缓存、PID 与锁。
- **恢复**：先运行 `python scripts/restore_workspace.py workspace.dsibackup --inspect`。浏览器参与的包必须通过 UI 的协调式 Prepare / Frontend Prepared / Commit / Complete 流程；服务端不会再用旧单调用 API 提前提交浏览器恢复。无浏览器状态的 CLI 包仍可 `--apply --mode merge`。
- **崩溃恢复**：`.restore-staging/<restoreId>/transaction.json` 记录 Contributor 准备、目录交换和 rollback 路径，`.workspace-restore-fence.json` 阻止普通写入，`.workspace-mutation.lock` 在 Web、CLI 和后台 Worker 间互斥。服务启动时会扫描未完成 Journal；`recovery-required` 记录和对应 Safety Backup 不得自动删除。
- **冷备**：必须先完全停止服务，再复制整个 `/data` 卷。直接复制运行中的卷可能得到跨 SQLite / JSON 时间点不一致的内容。
- **外部覆盖**：配置 `STATELESS_MCP_BACKUP_URL=http://127.0.0.1:8011`、`STATELESS_MCP_BACKUP_TOKEN`，并在 Stateless MCP 实例设置相同的 `MCP_INTERNAL_BACKUP_TOKEN`。`strict` 在服务不可达时失败；`best-effort` 会在 Manifest 明确记录遗漏。
- **Helper**：源码环境运行 `python scripts/build_backup_crypto.py`；发布 ZIP 与 PyInstaller 构建会自动携带 `bin/backup-crypto[.exe]`。缺失 helper 时 API 明确禁用加密，不会回退为明文。
- 旧 `.dsibackup` 仍可恢复，但应按完整工作区敏感数据保管。分享用 Export 已脱敏或裁剪，不能用于 Restore。

## 6. 暴露到局域网 / 公网前必读

- `/metrics`、`/healthz`、`/readyz` **不鉴权**：保持只绑回环，或在反向代理上挡掉这三个路径再对外。
- 反向代理（Caddy 示例）：

  ```
  ai.example.internal {
      reverse_proxy 127.0.0.1:8000
      @ops path /metrics /healthz /readyz
      respond @ops 403
  }
  ```

  使用自定义域名时把它加进 `AUTH_ALLOWED_HOSTS`（Host 头白名单）。
- PWA 安装、剪贴板等浏览器能力需要 HTTPS；局域网 HTTP 适合开发与试用。
- 不要把 `.env`、`/data`（含 `.auth-token`、向量索引、trace、记忆等隐私数据）打进镜像或提交进 git；`.dockerignore` / `.gitignore` / `scripts/release.py` 三处都已排除。
- 安全边界与威胁模型见 [docs/SECURITY.md](SECURITY.md) 与 [docs/THREAT_MODEL.md](THREAT_MODEL.md)。

## 7. Production Readiness

DeepSeek Infra is designed for **local-first personal / lab / internal use**. Before exposing it to the public Internet, you should add:

- **TLS termination** — reverse proxy (Caddy / nginx) with Let's Encrypt
- **Reverse proxy authentication** — basic auth, OAuth2 proxy, or mTLS in front of `/api/*`, `/mcp`, `/a2a`
- **Rate limiting** — IP-based or token-based rate limits on the reverse proxy layer
- **Request body size limit** — enforce `UPLOAD_MAX_BYTES` and match the reverse proxy's `client_max_body_size`
- **Audit log rotation** — `.tool-audit/audit.jsonl` grows unbounded; add logrotate or periodic pruning
- **Backup policy** — prefer a verified `.dsibackup` created by the UI or `scripts/backup_workspace.py`; copy `/data` only as a cold backup after the service is fully stopped
- **External secret manager** — prefer injecting `DEEPSEEK_API_KEY` from a vault/secret store rather than `.env` on disk

These are not built into the runtime itself — they belong at the infrastructure layer around it. Being explicit about this boundary makes the project safer: it doesn't pretend to solve what it doesn't.

## 8. 常见启动失败排查

服务起不来时，先跑运行时体检，它会用 PASS / WARNING / FAIL 把环境问题逐项指出来：

```bash
python scripts/doctor.py --offline
```

对照 Doctor 输出的常见根因：

| 现象 | 根因 | 处理 |
| --- | --- | --- |
| `port` WARNING：端口被占用 | 8000 已被别的进程 / 实例占用 | 换 `PORT`，或停掉占用进程；Docker 下确认没有两个容器抢同一宿主端口。 |
| `root_writable` / `data_dirs` FAIL | `DEEPSEEK_INFRA_ROOT` 不可写 | 裸机检查目录属主；Docker 下确认 `/data` 卷挂载且属主是运行用户（`chown -R 10001:10001 /data`）。 |
| `api_key` WARNING | 没配 `DEEPSEEK_API_KEY` | 云端对话 / 多 Agent / A2A 任务会失败；在 `.env` 或页面设置里填。注意这只是 WARNING，本地纯离线能力不受影响。 |
| `static_dir` FAIL | static 路径不对 | 裸机应指向仓库 `static/`；Docker 镜像内固定 `/app/static`（`DEEPSEEK_INFRA_STATIC_DIR`）。 |
| `auth_token` WARNING | 还没有本地 token | 首次启动会自动生成 `.auth-token`；或用 `AUTH_TOKEN` 固定一个便于客户端复用。 |
| `requirements` FAIL | 依赖没装全 | `python -m pip install -r requirements.txt`；注意 multipart 是 `multipart`，不是 `python-multipart`。 |
| Docker volume 权限 | 非 root 用户写不进 `/data` | 构建后 `RUN mkdir -p /data && chown -R appuser:appuser /data` 已处理；自建镜像时确保卷属主是 `10001`。 |

发版前还要跑 `python scripts/preflight_release.py --version 2.3.2` 确认版本徽章 / CHANGELOG / Docker tag / eval 报告版本同步，详见 [docs/RELEASE_READINESS.md](RELEASE_READINESS.md) 与 [docs/RUNTIME_DOCTOR.md](RUNTIME_DOCTOR.md)。
