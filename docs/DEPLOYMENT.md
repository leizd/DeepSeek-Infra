# 部署指南（Docker / Compose / 裸机）

适用版本：v2.1.7。

DeepSeek Infra 的服务形态是一个单进程 FastAPI / ASGI 运行时：`/v1` OpenAI 兼容网关、`/mcp`、`/a2a`、`/api/*` 业务端点，加 `/healthz`·`/readyz`·`/metrics` 运维三件套。所有可写状态（鉴权 token、文件缓存、向量索引、trace、语义缓存、记忆、任务快照）都集中在**一个数据目录**下，由 `DEEPSEEK_MOBILE_ROOT` 指定——这也是容器化只需要一个卷的原因。

## 1. Docker Compose（推荐）

```bash
cp .env.example .env        # 填写 DEEPSEEK_API_KEY 等
docker compose up -d
docker compose logs -f deepseek-infra
```

验证：

```bash
curl http://127.0.0.1:8000/healthz
# {"status":"ok","version":"2.1.6",...}
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
docker build -t deepseek-infra:2.1.7 .
docker run -d --name deepseek-infra \
  -p 127.0.0.1:8000:8000 \
  --env-file .env \
  -v deepseek-data:/data \
  deepseek-infra:2.1.7
```

镜像要点（见 [Dockerfile](../Dockerfile)）：`python:3.12-slim` **多阶段构建**（builder 装依赖进独立 venv，运行层不带 pip 缓存与编译垃圾）、非 root 用户运行、`HEALTHCHECK` 打 `/healthz`、数据卷 `/data`、静态资源固定在镜像内（`DEEPSEEK_MOBILE_STATIC_DIR`）。

多架构构建（amd64 + arm64，需要 Docker buildx）：

```bash
docker buildx create --use --name deepseek-builder   # 一次性
docker buildx build --platform linux/amd64,linux/arm64 \
  -t <registry>/deepseek-infra:2.1.7 --push .
# 只构建本机架构验证时：docker buildx build --platform linux/arm64 -t deepseek-infra:2.1.7-arm64 --load .
```

依赖均为纯 Python 或带 arm64 wheel（PyMuPDF / reportlab 等在 PyPI 提供 manylinux aarch64 轮子），无需交叉编译工具链。

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
Environment=DEEPSEEK_MOBILE_ROOT=/var/lib/deepseek-infra
ExecStart=/usr/bin/python3 app.py
Restart=on-failure
User=deepseek

[Install]
WantedBy=multi-user.target
```

安全加固可在 `[Service]` 段按需追加 `ProtectSystem=strict`、`ReadWritePaths=/var/lib/deepseek-infra` 等。

## 4. 配置参考

- 模板：[.env.example](../.env.example)（核心变量带注释）；完整清单见 README「环境变量」。
- 数据目录：`DEEPSEEK_MOBILE_ROOT`（容器内默认 `/data`；裸机默认仓库根目录）。各子目录含义见 README「本地数据与隐私」。
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
