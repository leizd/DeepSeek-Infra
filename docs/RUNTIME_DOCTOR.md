# Runtime Doctor

适用版本：v2.5.6。

Runtime Doctor 回答一个最常见的问题：**到底为什么起不来？** 它把一个全新 DeepSeek Infra 安装需要的环境逐项体检，每项输出 `PASS` / `WARNING` / `FAIL`，让你快速区分到底是 Python 版本、依赖缺失、`.env` / API Key、数据目录权限、static 路径、端口占用还是服务没起来。

## 用法

离线模式（CI 安全，不访问网络、不要求 API Key、不需要服务在跑）：

```bash
python scripts/doctor.py --offline
```

对着一个已经启动的服务做体检（额外探活 `/healthz` `/readyz` `/metrics`）：

```bash
python scripts/doctor.py --with-server --base-url http://127.0.0.1:8000
```

机器可读输出：

```bash
python scripts/doctor.py --offline --json
```

## 退出码

- `0`：没有 `FAIL`（`WARNING` 不算失败）。
- `1`：存在 `FAIL`。

离线模式下 `DEEPSEEK_API_KEY` 未配置、`.env` 缺失、端口被占用、GUI 依赖未装都只是 `WARNING`，不会让 doctor 失败，因此 `doctor.py --offline` 适合放进 CI。

## 检查项

| 检查 | 状态含义 |
| --- | --- |
| `python` | 当前 Python 是否 ≥ 3.10。低于则 `FAIL`。 |
| `requirements` | 核心运行时依赖（fastapi / uvicorn / multipart / defusedxml / openpyxl / pypdf / PyMuPDF / python-pptx / python-docx / reportlab）是否可导入。缺失 `FAIL`。 |
| `optional_requirements` | GUI 依赖（customtkinter / pywebview）是否可导入。缺失只 `WARNING`（无头环境正常）。 |
| `env_file` | `.env` 是否存在。缺失 `WARNING`（并提示复制 `.env.example`）。 |
| `api_key` | `DEEPSEEK_API_KEY` 是否设置。未设置 `WARNING`；设置时只输出脱敏片段，绝不打印明文。 |
| `root_writable` | `DEEPSEEK_INFRA_ROOT`（或仓库根）是否可写、可创建。不可写 `FAIL`。 |
| `static_dir` | static 目录是否存在、是否含 `index.html`。缺失 `FAIL`；存在但无 `index.html` `WARNING`。 |
| `data_dirs` | `.traces` / `.agent-runs` / `.a2a` / `.local-rag` / `.semantic-cache` 是否可创建并写入。失败 `FAIL`。 |
| `port` | `host:port` 是否空闲。被占用 `WARNING`（服务可能无法 bind）。 |
| `auth_token` | `.auth-token` 是否存在。缺失 `WARNING`（首次启动会自动生成）；存在时只输出脱敏片段。 |
| `health:/healthz` `health:/readyz` `health:/metrics` | 仅 `--with-server` 模式：对运行中服务探活，非 200 `FAIL`。 |

## 实现位置

- 核心检查（纯函数，可单测）：[`deepseek_infra/infra/diagnostics/runtime_doctor.py`](../deepseek_infra/infra/diagnostics/runtime_doctor.py)。
- CLI：[`scripts/doctor.py`](../scripts/doctor.py)。
- 测试：[`tests/test_runtime_doctor.py`](../tests/test_runtime_doctor.py)。

## 排障映射

| Doctor 输出 | 可能原因 | 处理 |
| --- | --- | --- |
| `python` FAIL | Python < 3.10 | 升级 Python。 |
| `requirements` FAIL | 依赖没装全 | `python -m pip install -r requirements.txt`。注意 multipart 是 `multipart`，不是 `python-multipart`。 |
| `env_file` WARNING | 没有 `.env` | `cp .env.example .env` 并填写 `DEEPSEEK_API_KEY`。 |
| `api_key` WARNING | 没配 Key | 云端对话 / 多 Agent / A2A 任务会失败；在 `.env` 或页面设置里填。 |
| `root_writable` FAIL | 数据目录不可写 | 检查 `DEEPSEEK_INFRA_ROOT` 权限；Docker 下确认 `/data` 卷挂载且属主是运行用户。 |
| `static_dir` FAIL | static 路径不对 | 检查 `DEEPSEEK_INFRA_STATIC_DIR`；裸机应指向仓库 `static/`。 |
| `data_dirs` FAIL | 子目录不可创建 | 同 `root_writable`，通常是父目录权限问题。 |
| `port` WARNING | 端口被占用 | 换 `PORT`，或停掉占用进程。 |
| `auth_token` WARNING | 还没有 token | 首次启动会自动生成；或用 `AUTH_TOKEN` 固定一个。 |
| `health:*` FAIL（with-server） | 服务没起来 / 探针路径被挡 | 先 `curl /healthz`；若经反向代理，确认 `/healthz` `/readyz` `/metrics` 没被挡掉。 |

## CI

`release-readiness` job 会跑 `python scripts/doctor.py --offline`，确保环境体检在干净 Ubuntu runner 上通过。它不要求 API Key，也不访问公网。
