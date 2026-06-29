# OpenAI-Compatible SDKs Smoke Evidence

- Version: 2.5.6
- Commit: 8a44088
- Status: PASS
- Generated: 2026-06-28T10:00:00Z
- OS: Windows
- Python: 3.13.5
- CI: false

## Target

- Base URL: http://127.0.0.1:8000/v1
- Model: deepseek-v4-pro

## SDK Checks

### langchain

| Check | Result |
| --- | --- |
| modelsList | PASS |
| chatCompletion | PASS |
| streaming | PASS |

### litellm

| Check | Result |
| --- | --- |
| modelsList | PASS |
| chatCompletion | PASS |
| streaming | PASS |

### llamaindex

| Check | Result |
| --- | --- |
| chatCompletion | PASS |

## Steps

1. **openai.healthz**: pass — starting SDK smoke
2. **sdk.langchain.models**: pass — 3 models
3. **sdk.langchain.chat**: pass — response=Hello
4. **sdk.langchain.stream**: pass — stream chunks=5
5. **sdk.litellm.models**: pass — 3 models
6. **sdk.litellm.chat**: pass — response=Hello
7. **sdk.litellm.stream**: pass — stream chunks=5
8. **sdk.llamaindex.chat**: pass — response=Hello

## Summary

LangChain (ChatOpenAI), LiteLLM, and LlamaIndex (OpenAILike) are verified to consume DeepSeek Infra's `/v1` OpenAI-compatible endpoint for model listing, chat completion, and (where applicable) streaming. Each SDK reuses the same base URL and auth token, confirming the endpoint follows standard OpenAI API conventions.
