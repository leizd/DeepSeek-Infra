# 2 分钟 Demo 路径

适用版本：v2.4.3。

这一页的目标：**不读任何源码，2 分钟内亲眼看到 README 里的核心能力在跑**。四个脚本都在 [examples/](../examples/)，按「零门槛 → 需要服务 → 需要 Key」排序：

| Demo | 命令 | 需要本地服务 | 需要 DeepSeek Key |
| --- | --- | --- | --- |
| 本地 RAG（lineage + 引用校验） | `python examples/local_rag_demo.py` | ❌ 完全离线 | ❌ |
| MCP Tool Hub 协议回环 | `python examples/mcp_tool_demo.py` | ✅ | ❌（本地工具不走上游） |
| OpenAI 兼容网关 | `python examples/openai_compatible_client.py` | ✅ | ✅ |
| 多 Agent DAG 流式 | `python examples/run_agent_dag_demo.py` | ✅ | ✅（真实消耗 token） |

启动本地服务（开发模式，免 token）：

```bash
AUTH_DISABLED=1 python app.py
# 正式模式：python app.py，token 见终端打印的 ?token=... 或同目录 .auth-token 文件
```

---

## 1. 本地 RAG：离线检索 + 引用回链（无需任何服务 / Key）

```bash
python examples/local_rag_demo.py
```

把仓库自身 `docs/` 的参考文档索引进**临时**本地 RAG 索引（hash embedding + BM25 hybrid，不碰你真实的 `.local-rag`），检索一条安全问题。实际输出：

```
indexed 5 docs / 95 chunks in 126 ms（临时索引：rag-demo-myxfqlp9）

query: "fetch_url 的 SSRF 防护会拦截哪些内网或元数据地址？"  (19.8 ms, hybrid = vector*100 + bm25*10)

#1 docs/SECURITY.md · score=98 (vector=0.4205, bm25=6)
   ...

[chunk lineage] 检索结果可回溯：
   chunkId: file:_:docs/SECURITY.md:4
   docId: docs/SECURITY.md
   hash: 81a9b34dfceb02b4c8fb02d2
   docVersion: 8c5cb88d7cfd13891607fa25

[verify_citation] 片段 "…" → grounded=True coverage=1.0
[verify_citation] 编造片段 → grounded=False（引用造假会被拒绝）
```

要点：每条检索结果都能**回溯到文档 / chunk / 内容哈希**；`verify_citation` 把「编造的引用」直接判 `grounded=False`。这两件事是 RAG 引用可信的基础，更系统的打分见 `python evals/runners/run_rag_eval.py`（Recall@5 / Citation Accuracy）。

## 2. MCP Tool Hub：标准协议调用本地工具（需服务，免 Key）

```bash
python examples/mcp_tool_demo.py
```

用仓库内置的 `MCPClient` 对本机 `POST /mcp` 做 `initialize → tools/list → tools/call` 回环：

```
[initialize] protocol=2025-06-18 server=deepseek-infra v2.3.2

[tools/list] 17 tools:
   - web_search  [read-only, open-world]
   - python_eval  [read-only]
   - forget_memory  [destructive]
   - create_pptx
   ...
[tools/call] python_eval expression='(23 * 89 + 7) ** 0.5'
structuredContent: {"ok": true, "tool": "python_eval", "result": {"expression": "(23 * 89 + 7) ** 0.5", "result": "45.32107677449864"}}
```

每个 `tools/call` 都经过 Tool Policy 闸门（能力切片 / schema / SSRF / 路径 / 密钥外泄防护）。Claude Desktop、Cursor 等任意 MCP 客户端把 Streamable HTTP 地址指向 `http://127.0.0.1:8000/mcp` 即可获得同一工具面。

## 3. OpenAI 兼容网关：任意 OpenAI SDK 直连（需服务 + Key）

```bash
python examples/openai_compatible_client.py --prompt "总结这个项目的架构"
```

等价于手写：

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="<本地访问 token>")
resp = client.chat.completions.create(
    model="deepseek-v4-pro",
    messages=[{"role": "user", "content": "总结这个项目的架构"}],
)
print(resp.choices[0].message.content)
```

`api_key` 是**本地访问 token**；上游 DeepSeek Key 由服务端 `DEEPSEEK_API_KEY` 提供。没装 `openai` 包脚本会自动退到 stdlib HTTP，打的是同一个端点。

## 4. 多 Agent DAG：planner / workers / synthesizer 实时事件（需服务 + Key）

```bash
python examples/run_agent_dag_demo.py
```

流式打印 DAG 全过程（`agent` 卡片状态 → `agent_output` worker 产出 → 综合回答流式正文 → `done` 摘要）：

```
[agent] Leader → running
[agent] Researcher → running · 检索 BM25 与稠密检索的对比要点
[output] researcher 产出 1843 字符：…
[agent] Reasoner → done
--- 综合回答（流式） ---
…
=== done ===
usage: prompt=18234 completion=3920 total=22154
agent durations:
  - researcher: 21.4s
  - reasoner: 18.2s
traceId: tr-…
```

跑完拿着 `traceId` 在应用里点开 Trace 瀑布图，或者直接打开独立只读页面：

```bash
# 在浏览器打开：http://127.0.0.1:8000/trace/<traceId>
curl -OJ http://127.0.0.1:8000/api/traces/<traceId>/export.json
```

页面能看到 `run → agent.<id> → {context.build, deepseek}` 的 span 树、瀑布图、Agent / Tool / RAG / LLM 耗时、token 用量、cache hit 和错误信息；导出的 `trace-<traceId>.json` 会保留排障字段，但会脱敏 API Key、auth token、敏感 URL query，并截断大段私有文本。延迟基准见 `python benchmarks/bench_agent_dag.py`。

---

## 顺手能看的运维 / 协议端点

```bash
curl http://127.0.0.1:8000/healthz          # liveness（不鉴权）
curl http://127.0.0.1:8000/metrics          # Prometheus 指标（不鉴权，默认只绑 127.0.0.1）
curl http://127.0.0.1:8000/.well-known/agent-card.json   # A2A Agent Card 发现（不鉴权）
```

外部 MCP server 工具桥接（v2.2.1）启用后，可先核对工具面再让 Agent 使用。v2.2.2 已加固为 Agent 和 `/mcp tools/call` 两条入口共享 executor 内部 ToolPolicy：

```bash
curl http://127.0.0.1:8000/api/mcp/external/tools \
  -H "Authorization: Bearer <本地 token>"
```

A2A 任务委派（`message/send` 提交即返回 Task，后台执行）：

```bash
curl -X POST http://127.0.0.1:8000/a2a/agents/reasoner \
  -H "Authorization: Bearer <本地 token>" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"message/send","params":{"message":{"role":"user","parts":[{"kind":"text","text":"42 的质因数分解"}]}}}'
```


A2A artifact streaming / resubscribe loopback（本机另起一个 8001 peer 后执行）：

```bash
python examples/a2a_peer_demo.py --peer http://127.0.0.1:8001/a2a/agents/reasoner --token <local-token> --message "Stream an answer as artifact chunks"
```

更多端点字段见 [docs/API.md](API.md)；各模块落地程度见 [docs/IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)。
