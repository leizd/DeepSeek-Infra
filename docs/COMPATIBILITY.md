# Compatibility Matrix（兼容性矩阵）

适用版本：v2.1.7。

DeepSeek Infra 对外暴露标准协议端点（OpenAI `/v1`、MCP JSON-RPC 2.0、A2A Agent Card + 任务生命周期），理论上与遵循这些协议的客户端互操作。但诚实地讲，我们尚未对所有外部客户端逐一跑兼容性矩阵。这个页面就是 **"测了什么、没测什么"的唯一真实记录**。

## MCP Client Compatibility

| Client | Status | Notes |
| --- | --- | --- |
| `examples/mcp_tool_demo.py` | ✅ Tested | 本地 Python 客户端，覆盖 initialize / tools/list / tools/call / resources / prompts 全流程 |
| MCP test suite (`tests/test_mcp.py`) | ✅ Tested | CI 门禁：11 项（握手 / 目录 / 能力切片 / 真实执行 / 错误码族 / 回环 client） |
| `curl` JSON-RPC | ✅ Tested | 手动 daily driver：`curl -X POST http://127.0.0.1:8000/mcp -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'` |
| Claude Desktop | 🔲 Not tested | 协议上兼容（Streamable HTTP），有 `MCP_CAPABILITY` 能力切片，但未在 Claude Desktop 上跑过 |
| Cursor | 🔲 Not tested | 同上，协议路径是通的 |
| Continue.dev | 🔲 Not tested | 待验证 |
| Any MCP-compatible client | 🔲 Planned | v2.3 Roadmap：逐客户端跑兼容矩阵 |

### 能力切片说明

MCP 客户端经 `MCP_CAPABILITY` 配置获得不同工具面：
- `full`（默认）：全部 17 个工具
- `researcher`：只暴露搜索 / 检索 / 抓取面
- `coder`：暴露计算 / 文件 / 搜索面
- `reasoner`：无工具

`tools/list` 响应中每个工具带 `annotations`（`read-only` / `destructive` / `open-world`），供客户端自行做安全决策。

## A2A Interop Compatibility

| Peer | Status | Notes |
| --- | --- | --- |
| Local A2A test suite (`tests/test_a2a.py`) | ✅ Tested | CI 门禁：11 项 |
| Local Agent Card discovery | ✅ Tested | `curl /.well-known/agent-card.json` daily driver |
| `A2AClient` → external peer | 🔲 Not tested | 协议层与 A2A 规范对齐，但未与第三方 A2A 实现互测（Roadmap v2.3） |
| Google A2A | 🔲 Not tested | 草案阶段兼容，未互测 |

## OpenAI API Compatibility

| Client | Status | Notes |
| --- | --- | --- |
| OpenAI Python SDK (`openai>=1.0`) | ✅ Tested | `examples/openai_compatible_client.py` 覆盖 `/v1/chat/completions`（stream + non-stream）+ `/v1/models` |
| `curl` | ✅ Tested | README 文档中的示例 |
| Ollama (as provider) | ✅ Tested | `OLLAMA_ENABLED=1` 时 `/v1/models` 额外列出 `ollama/<tag>`，请求可经 `/v1` 网关路由 |
| Other OpenAI-compatible SDKs | 🔲 Not tested | 理论上兼容标准 `chat/completions` 与 `models` |

## 想帮忙补兼容性矩阵？

1. 挑一个上面标注 🔲 的客户端
2. 按 [docs/DEMO.md](DEMO.md) 起服务
3. 跑通你自己的客户端
4. 开 Issue 或 PR 汇报结果，我们更新这个页面

这是开源项目最实在的贡献之一。
