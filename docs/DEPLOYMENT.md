# 部署指南（Docker / Compose / 裸机）

适用版本：v2.6.5。

DeepSeek Infra 的服务形态是一个单进程 FastAPI / ASGI 运行时：`/v1` OpenAI 兼容网关、`/mcp`、`/a2a`、`/api/*` 业务端点，加 `/healthz`·`/readyz`·`/metrics` 运维三件套。所有可写状态（鉴权 token、文件缓存、向量索引、trace、语义缓存、记忆、任务快照）都集中在**一个数据目录**下，由 `DEEPSEEK_INFRA_ROOT`（优先）或 `DEEPSEEK_MOBILE_ROOT`（向后兼容）指定——这也是容器化只需要一个卷的原因。

## 1. Docker Compose（推荐）

```bash
cp .env.example .env        # 填写 DEEPSEEK_API_KEY 等
docker compose up -d
docker compose logs -f deepseek-infra
```

验证：

```bash
curl http://127.0.0.1:8000/healthz
# {"status":"ok","version":"2.3.2",...}
curl http://127.0.0.1:8000/readyz
curl http://127.0.0.1:8000/metrics | head
```

默认 [docker-compose.yml](../docker-compose.yml) 把端口发布在 `127.0.0.1:8000`（因为运维端点不鉴权），数据持久化在命名卷 `deepseek-data`（容器内 `/data`）。

### 取得本地访问 token

`AUTH_DISABLED=0`（默认）时 `/api/*`、`/mcp`、`/a2a` 需要本地 token。两种方式：

- 在 `.env` 里固定 `AUTH_TOKEN=<你自己的随机串>`（推荐，客户端直接用它做 Bearer）；
- 或留空让服务端自动生成，再读出来：`docker compose exec deepseek-infra cat /data/.auth-token`。

浏览器访问用 `http://127.0.0.1:8000/?token=<token>`，API 客户端用 `Authorization: Bearer <token>`。

## 2. 纯 Docker

```bash
docker build -t deepseek-infra:2.3.2 .
docker run -d --name deepseek-infra \
  -p 127.0.0.1:8000:8000 \
  --env-file .env \
  -v deepseek-data:/data \
  deepseek-infra:2.3.2
```

镜像要点（见 [Dockerfile](../Dockerfile)）：`python:3.12-slim`、`pip --no-cache-dir`、非 root 用户运行、`HEALTHCHECK` 打 `/healthz`、数据卷 `/data`、静态资源固定在镜像内（`DEEPSEEK_INFRA_STATIC_DIR`，旧变量 `DEEPSEEK_MOBILE_STATIC_DIR` 继续兼容），并在构建后清理 `__pycache__`。CI 的 docker job 会同时跑 `docker build -t deepseek-infra:test .` 和 `docker compose config`，确保镜像可构建、Compose 语法有效。

## 3. 裸机 / systemd

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

## 4. 配置参考

- 模板：[.env.example](../.env.example)（核心变量带注释）；完整清单见 README「环境变量」。
- 数据目录：`DEEPSEEK_INFRA_ROOT`（优先，`DEEPSEEK_MOBILE_ROOT` 向后兼容；容器内默认 `/data`；裸机默认仓库根目录）。各子目录含义见 README「本地数据与隐私」。
- 外接 MCP server（v2.2.1）：默认关闭。启用时设置 `MCP_CLIENT_ENABLED=1` 与 `MCP_CLIENT_SERVERS='[{"name":"docs","url":"http://127.0.0.1:9001/mcp"}]'`，只连接可信地址；上线前用 `GET /api/mcp/external/tools` 核对 bridged tools、风险等级和审批要求。v2.2.2 起，Agent 和 `/mcp tools/call` 两条入口都会在外部 MCP executor 内部再次执行 ToolPolicy。
- 升级：换新镜像 tag 重新 `up -d` 即可；数据目录内的 SQLite schema 由各模块幂等迁移，跨小版本升级无需手工步骤。备份 = 备份 `/data` 卷。

## 5. 暴露到局域网 / 公网前必读

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

## 6. Production Readiness

DeepSeek Infra is designed for **local-first personal / lab / internal use**. Before exposing it to the public Internet, you should add:

- **TLS termination** — reverse proxy (Caddy / nginx) with Let's Encrypt
- **Reverse proxy authentication** — basic auth, OAuth2 proxy, or mTLS in front of `/api/*`, `/mcp`, `/a2a`
- **Rate limiting** — IP-based or token-based rate limits on the reverse proxy layer
- **Request body size limit** — enforce `UPLOAD_MAX_BYTES` and match the reverse proxy's `client_max_body_size`
- **Audit log rotation** — `.tool-audit/audit.jsonl` grows unbounded; add logrotate or periodic pruning
- **Backup policy** — the `/data` volume (or `DEEPSEEK_INFRA_ROOT`, with `DEEPSEEK_MOBILE_ROOT` kept for compatibility) contains all user state; back it up regularly
- **External secret manager** — prefer injecting `DEEPSEEK_API_KEY` from a vault/secret store rather than `.env` on disk

These are not built into the runtime itself — they belong at the infrastructure layer around it. Being explicit about this boundary makes the project safer: it doesn't pretend to solve what it doesn't.

## 7. 常见启动失败排查

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
