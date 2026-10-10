# 边缘路由冒烟测试证据

- 版本: 2.6.0
- 生成时间: 2026-06-28T07:39:20Z
- 状态: 通过
- 基础 URL: `http://127.0.0.1:8000`

## 环境

| 键 | 值 |
| --- | --- |
| os | `Windows` |
| python | `3.13.5` |
| ci | `False` |
| ollamaEnabled | `1` |
| ollamaBaseUrl | `http://127.0.0.1:59295` |
| ollamaProviderNote | `Ollama 兼容的本地冒烟后端` |
| edgeInferenceEnabled | `0` |

| 检查项 | 状态 |
| --- | --- |
| ollamaModelsListed | 通过 |
| openaiCompatibleLocalCall | 通过 |
| edgeStatusEndpoint | 通过 |
| fallbackReady | 通过 |

## 步骤

| 步骤 | 状态 | 详情 |
| --- | --- | --- |
| healthz | 通过 | status=ok |
| edge.status | 警告 | enabled=False provider=llama_cpp available=False |
| openai.models | 通过 | models=3 ollama=1 |
| openai.chat | 通过 | model=ollama/llama3.2 choices=1 |
