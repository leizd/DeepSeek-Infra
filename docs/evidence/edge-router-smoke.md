# Edge Router Smoke Evidence

- Version: 2.5.8
- Generated: 2026-06-28T07:39:20Z
- Status: PASS
- Base URL: `http://127.0.0.1:8000`

## Environment

| Key | Value |
| --- | --- |
| os | `Windows` |
| python | `3.13.5` |
| ci | `False` |
| ollamaEnabled | `1` |
| ollamaBaseUrl | `http://127.0.0.1:59295` |
| ollamaProviderNote | `Ollama-compatible local smoke backend` |
| edgeInferenceEnabled | `0` |

| Check | Status |
| --- | --- |
| ollamaModelsListed | PASS |
| openaiCompatibleLocalCall | PASS |
| edgeStatusEndpoint | PASS |
| fallbackReady | PASS |

## Steps

| Step | Status | Detail |
| --- | --- | --- |
| healthz | pass | status=ok |
| edge.status | warn | enabled=False provider=llama_cpp available=False |
| openai.models | pass | models=3 ollama=1 |
| openai.chat | pass | model=ollama/llama3.2 choices=1 |
