# OpenAI-Compatible SDKs Smoke Evidence / OpenAI 兼容 SDK 冒烟测试证据

- 版本: 2.6.0
- 提交: 8a44088
- 状态: 通过
- 生成时间: 2026-06-28T10:00:00Z
- 操作系统: Windows
- Python: 3.13.5
- CI: false

## 目标

- 基础 URL: http://127.0.0.1:8000/v1
- 模型: deepseek-v4-pro

## SDK 检查

### langchain

| 检查项 | 结果 |
| --- | --- |
| modelsList | 通过 |
| chatCompletion | 通过 |
| streaming | 通过 |

### litellm

| 检查项 | 结果 |
| --- | --- |
| modelsList | 通过 |
| chatCompletion | 通过 |
| streaming | 通过 |

### llamaindex

| 检查项 | 结果 |
| --- | --- |
| chatCompletion | 通过 |

## 步骤

1. **openai.healthz**: 通过 — 开始 SDK 冒烟测试
2. **sdk.langchain.models**: 通过 — 3 个模型
3. **sdk.langchain.chat**: 通过 — 响应=Hello
4. **sdk.langchain.stream**: 通过 — 流式块数=5
5. **sdk.litellm.models**: 通过 — 3 个模型
6. **sdk.litellm.chat**: 通过 — 响应=Hello
7. **sdk.litellm.stream**: 通过 — 流式块数=5
8. **sdk.llamaindex.chat**: 通过 — 响应=Hello

## 摘要

已验证 LangChain (ChatOpenAI)、LiteLLM 和 LlamaIndex (OpenAILike) 能够通过 DeepSeek Infra 的 `/v1` OpenAI 兼容端点进行模型列表获取、聊天补全以及（在适用情况下）流式传输。每个 SDK 复用相同的基础 URL 和认证令牌，确认该端点遵循标准的 OpenAI API 约定。
