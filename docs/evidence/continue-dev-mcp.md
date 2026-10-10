# Continue.dev MCP Evidence / Continue.dev MCP 证据

- 客户端: Continue.dev
- 版本: 2.6.0
- 提交: 2e2782e
- 状态: 通过
- 生成时间: 2026-06-28T10:00:00Z
- 操作系统: Windows
- Python: 3.13.5
- CI: false

## 检查项

| 检查项 | 结果 |
| --- | --- |
| configLoaded | 通过 |
| mcpInitialize | 通过 |
| toolsList | 通过 |
| lowRiskToolCall | 通过 |
| policyDenial | 通过 |
| promptInjectionClean | 通过 |

## 传输

- 类型: streamable-http
- MCP URL: http://127.0.0.1:8000/mcp
- 认证: 已禁用

## 步骤

1. **server.healthz**: 通过 — status=ok
2. **config.loaded**: 通过 — Continue.dev MCP 配置已加载，使用 streamable-http 传输
3. **mcp.initialize**: 通过 — protocol=2025-06-18 server=deepseek-infra
4. **mcp.tools_list**: 通过 — 暴露了 17 个工具
5. **mcp.tools_call**: 通过 — data_transform count=4
6. **mcp.policy_denial**: 通过 — fetch_url 本地探测被 Tool Policy 阻止

## 摘要

Continue.dev 成功连接到 DeepSeek Infra 的 MCP 端点，列出全部 17 个本地工具，执行了 `data_transform`（低风险工具调用），并通过 Tool Policy 门控正确拒绝了 `fetch_url` SSRF 探测。系统提示词未被工具结果污染。

完整配置指南和验证操作手册请参阅 [docs/integrations/continue-dev.md](../integrations/continue-dev.md)。
