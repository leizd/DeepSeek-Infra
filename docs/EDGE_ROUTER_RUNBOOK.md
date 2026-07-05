# 边缘路由运维手册

适用版本：v2.9.0。

Edge-Cloud Model Router 在 v2.7.3 进入 **MVP stabilization**：CI 会离线覆盖 doctor、状态字段、route-preview、fake provider、路由策略、云不可用回退和 forced-local 409，不下载模型、不安装本地推理后端，也不跑真实 GGUF/MLC 推理。真实 Ollama / GGUF / MLC 仍作为可选实机 evidence 补充。

## 最小配置

```powershell
$env:EDGE_INFERENCE_ENABLED="1"
$env:EDGE_PROVIDER="llama_cpp"
$env:EDGE_MODEL_PATH="/models/qwen2.5-1.5b-instruct-q4_k_m.gguf"
$env:EDGE_MODE="auto"
```

兼容旧变量名 `EDGE_INFERENCE_PROVIDER`。`EDGE_MODE=auto` 时，简单总结/改写/翻译类请求可走端侧；联网、新闻、搜索、图片、多 Agent、代码/数学/产物生成等请求会走云端。

## 离线 dry-run 验证（CI 默认）

不需要真实模型：

```powershell
python scripts/doctor.py --offline
python scripts/smoke_edge_router.py --offline --out docs/evidence/edge-router-v2.9.0.json
```

启动服务后可直接解释一轮请求为什么会走端侧或云端：

```powershell
curl http://127.0.0.1:8000/api/edge/route-preview `
  -H "Content-Type: application/json" `
  -H "Authorization: Bearer <local-token>" `
  -d "{\"messages\":[{\"role\":\"user\",\"content\":\"Summarize this note.\"}],\"edgeMode\":\"auto\"}"
```

典型响应：

```json
{
  "useEdge": true,
  "reason": "simple_task_local",
  "mode": "auto",
  "provider": "llama_cpp",
  "status": {"available": true, "providerSupported": true, "suggestions": []}
}
```

## Ollama Provider 冒烟测试

适合先验证 `/v1/models` 的本地模型暴露链路，不需要 GGUF 文件。

1. 启动 Ollama，并确保至少有一个模型：

```powershell
ollama pull llama3.2
ollama list
```

2. 启动 DeepSeek Infra：

```powershell
$env:OLLAMA_ENABLED="1"
$env:AUTH_DISABLED="1"
python app.py
```

3. 验证模型目录：

```powershell
curl http://127.0.0.1:8000/v1/models
python examples/edge_router_smoke.py --require-ollama
python examples/edge_router_smoke.py --require-ollama --out docs/evidence/edge-router-smoke.json --markdown docs/evidence/edge-router-smoke.md
```

通过标准：`/v1/models` 里出现 `ollama/<tag>`，例如 `ollama/llama3.2`。

## GGUF 边缘路由冒烟测试

适合验证 `EDGE_INFERENCE_ENABLED=1` 后的端侧模型状态与路由准备度。

1. 安装可选依赖：

```powershell
python -m pip install -r requirements-edge.txt
```

2. 配置本地模型路径：

```powershell
$env:EDGE_INFERENCE_ENABLED="1"
$env:EDGE_PROVIDER="llama_cpp"
$env:EDGE_MODEL_PATH="C:\models\your-model.Q4_K_M.gguf"
$env:EDGE_MODEL_NAME="edge-local"
$env:EDGE_MODE="auto"
$env:AUTH_DISABLED="1"
python app.py
```

3. 查看状态：

```powershell
curl http://127.0.0.1:8000/api/edge/status
curl http://127.0.0.1:8000/api/edge/route-preview -H "Content-Type: application/json" -d "{\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}]}"
python examples/edge_router_smoke.py --require-edge
python examples/edge_router_smoke.py --require-edge --out docs/evidence/edge-router-smoke.json --markdown docs/evidence/edge-router-smoke.md
```

通过标准：`edgeInference.enabled=true`、`dependencyAvailable=true`、`modelPathExists=true`、`available=true`。

## OpenAI 兼容本地调用

当 Ollama 已启用并且 `/v1/models` 能看到 `ollama/<tag>` 后，可以用标准 OpenAI-compatible 请求验证本地 provider：

```powershell
curl http://127.0.0.1:8000/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d "{\"model\":\"ollama/llama3.2\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hello in one sentence.\"}]}"
```

如果本地鉴权开启，请加：

```powershell
-H "Authorization: Bearer <local-token>"
```

## 故障排查

| 症状 | 含义 | 修复方法 |
| --- | --- | --- |
| `enabled=false` | Edge router 没打开 | 设置 `EDGE_INFERENCE_ENABLED=1` 并重启服务 |
| `providerSupported=false` | 配置了不支持的 provider | 使用 `EDGE_PROVIDER=llama_cpp` / `mlc` / `fake` |
| `dependencyAvailable=false` | 本地推理依赖未安装 | `llama_cpp` 用 `requirements-edge.txt`；MLC 需本地安装 `mlc-llm` |
| `modelPathConfigured=false` | 没有配置模型路径 | 设置 `EDGE_MODEL_PATH` |
| `modelPathSuffixSupported=false` | llama_cpp 模型不是 `.gguf` | 换成 GGUF 文件或切 provider |
| `modelPathExists=false` | 路径不存在或不是 `.gguf` | 检查文件路径、扩展名和权限 |
| `route-preview` 返回 `reason=complex_task_cloud` | 请求需要联网/代码/数学/产物能力或超出简单任务范围 | 这是预期路由；如确要本地，传 `edgeMode=local` 并确认端侧 available |
| `route-preview` 返回 409 | 强制本地但端侧不可用 | 看 `suggestions`，修依赖、provider、模型路径或后缀 |
| `/v1/models` 没有 `ollama/` | Ollama provider 没启用或 Ollama 不可达 | 设置 `OLLAMA_ENABLED=1`，确认 `OLLAMA_BASE_URL` 和 `ollama list` |
| `401 / unauthorized` | 本地 token 鉴权开启 | 传 `Authorization: Bearer <local-token>`，或仅在可信开发机用 `AUTH_DISABLED=1` |

## 证据模板

v2.7.3 默认发版证据由 `scripts/smoke_edge_router.py` 生成：

- `docs/evidence/edge-router-v2.9.0.json`：release preflight 的硬门禁证据，覆盖 doctor、dry-run route-preview、fake provider、路由策略、fallback 和 forced-local 409。

`examples/edge_router_smoke.py` 可直接生成两份可选实机证据：

- `docs/evidence/edge-router-smoke.json`：release preflight 读取的结构化 evidence。
- `docs/evidence/edge-router-smoke.md`：方便在 PR / issue / release note 中人工审阅的摘要。

JSON 的关键 checks 是：

```json
{
  "version": "2.4.6",
  "status": "PASS",
  "checks": {
    "ollamaModelsListed": "PASS",
    "openaiCompatibleLocalCall": "PASS",
    "edgeStatusEndpoint": "PASS",
    "fallbackReady": "PASS"
  }
}
```

把 Edge Router 实机结果补回 issue / PR / compatibility matrix 时，建议带上：

- DeepSeek Infra commit：`git rev-parse --short HEAD`
- OS / Python：`python --version`
- Backend：Ollama tag 或 GGUF 文件名与量化等级
- 命令：`python examples/edge_router_smoke.py --require-ollama --out docs/evidence/edge-router-smoke.json --markdown docs/evidence/edge-router-smoke.md` 或 `--require-edge`
- 输出摘要：`edgeInference.available`、`dependencyAvailable`、`modelPathExists`、`ollamaModels`、`openaiCompatibleLocalCall`
