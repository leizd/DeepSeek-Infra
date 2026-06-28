# Edge Router Runbook

适用版本：v2.4.6。

Edge-Cloud Model Router 仍是 **Experimental**：CI 覆盖路由决策、配置面和云失败回退，但不下载模型、不安装本地推理后端，也不跑真实 GGUF/MLC 推理。v2.4.3 把“怎么在自己的机器上验收”推进为结构化 evidence，方便把本地 Ollama / Ollama-compatible provider 的结果带进 release preflight。

## Ollama Provider Smoke

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

## GGUF Edge Router Smoke

适合验证 `EDGE_INFERENCE_ENABLED=1` 后的端侧模型状态与路由准备度。

1. 安装可选依赖：

```powershell
python -m pip install -r requirements-edge.txt
```

2. 配置本地模型路径：

```powershell
$env:EDGE_INFERENCE_ENABLED="1"
$env:EDGE_INFERENCE_PROVIDER="llama_cpp"
$env:EDGE_MODEL_PATH="C:\models\your-model.Q4_K_M.gguf"
$env:EDGE_MODEL_NAME="edge-local"
$env:AUTH_DISABLED="1"
python app.py
```

3. 查看状态：

```powershell
curl http://127.0.0.1:8000/api/edge/status
python examples/edge_router_smoke.py --require-edge
python examples/edge_router_smoke.py --require-edge --out docs/evidence/edge-router-smoke.json --markdown docs/evidence/edge-router-smoke.md
```

通过标准：`edgeInference.enabled=true`、`dependencyAvailable=true`、`modelPathExists=true`、`available=true`。

## OpenAI-Compatible Local Call

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

## Failure Triage

| Symptom | Meaning | Fix |
| --- | --- | --- |
| `enabled=false` | Edge router 没打开 | 设置 `EDGE_INFERENCE_ENABLED=1` 并重启服务 |
| `dependencyAvailable=false` | 本地推理依赖未安装 | `llama_cpp` 用 `requirements-edge.txt`；MLC 需本地安装 `mlc-llm` |
| `modelPathConfigured=false` | 没有配置模型路径 | 设置 `EDGE_MODEL_PATH` |
| `modelPathExists=false` | 路径不存在或不是 `.gguf` | 检查文件路径、扩展名和权限 |
| `/v1/models` 没有 `ollama/` | Ollama provider 没启用或 Ollama 不可达 | 设置 `OLLAMA_ENABLED=1`，确认 `OLLAMA_BASE_URL` 和 `ollama list` |
| `401 / unauthorized` | 本地 token 鉴权开启 | 传 `Authorization: Bearer <local-token>`，或仅在可信开发机用 `AUTH_DISABLED=1` |

## Evidence Template

`examples/edge_router_smoke.py` 可直接生成两份可提交证据：

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
