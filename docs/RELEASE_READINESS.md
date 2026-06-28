# Release Readiness

适用版本：v2.4.6。

v2.4.6 的发布主题是 **OpenAI-Compatible SDK Evidence Patch / OpenAI-Compatible SDK 兼容性证据补丁**：不新增核心运行时能力，而是把 OpenAI API Compatibility 中仍处于 🔲 的 Other OpenAI-compatible SDKs 从 Not tested 推进为结构化 SDK smoke evidence，验证 LangChain (ChatOpenAI)、LiteLLM、LlamaIndex (OpenAILike) 能通过 DeepSeek Infra 的 `/v1` OpenAI-compatible endpoint 完成 models list、chat completion 与 streaming 调用。

## 1. Release Preflight — 版本一致性体检

发版前确认版本号在所有该出现的地方都同步，eval 报告是当前版本，且发布脚本仍排除本地缓存 / 日志 / 密钥：

```bash
python scripts/preflight_release.py --version 2.4.6
```

检查项：

- README 版本徽章是 `2.4.6`。
- `CHANGELOG.md` 顶部有 `## [2.4.6]` 条目。
- `Dockerfile` 示例 tag 是 `deepseek-infra:2.4.6`。
- `docs/IMPLEMENTATION_STATUS.md` 与 `evals/README.md` 的「适用版本」是 `v2.4.6`。
- `docs/EVIDENCE_INDEX.md` 存在且包含 Headless MCP bridge / A2A external peer / A2A third-party peer / Edge Router / Continue.dev MCP / OpenAI-compatible SDK / eval reports 索引。
- `evals/reports/latest.json`、`agent-latest.json`、`baseline-compare-latest.json` 与 `security-latest.json` 的 `version` 是 `2.4.6`，且包含统一 metadata。
- `docs/evidence/headless-mcp-bridge.json` 可解析、版本为 `2.4.6`，且关键 MCP bridge 步骤全为 PASS。
- `docs/evidence/a2a-external-peer.json` 可解析、版本为 `2.4.6`，且关键 A2A external peer checks 全为 PASS。
- `docs/evidence/a2a-third-party-peer.json` 缺失时为 WARNING；存在时必须版本为 `2.4.6`、`peerType=third-party`、`status=PASS` 且八类 A2A checks 全 PASS。
- `docs/evidence/edge-router-smoke.json` 缺失时为 WARNING；存在时必须版本为 `2.4.6`、`status=PASS` 且四类 Edge checks 全 PASS。
- `docs/evidence/continue-dev-mcp.json` 缺失时为 WARNING；存在时必须版本为 `2.4.6`、`status=PASS` 且六类 MCP checks 全 PASS。
- `docs/evidence/openai-compatible-sdks.json` 缺失时为 WARNING；存在时必须版本为 `2.4.6`、`status=PASS` 且 LangChain/LiteLLM/LlamaIndex 关键 SDK checks 全 PASS。
- `quality_gate_evidence` 确认 coverage 80%、offline eval、Agent Eval、baseline compare、injection strict 与 security corpus 全部 PASS。
- CHANGELOG / README / COMPATIBILITY / IMPLEMENTATION_STATUS / RELEASE_READINESS / EVIDENCE_INDEX / `docs/integrations/*.md` 不出现 `???`、`锟斤拷`、`\ufffd` 等乱码。
- `scripts/release.py` 仍排除 `.traces` / `.local-rag` / `.auth-token` / `.env` / `server*.log`。

退出码：`1` 表示有 `FAIL`；GUI、本地模型、第三方生态这类 `WARNING` 不阻断 CI。`--json` 输出机器可读摘要。

实现：[`scripts/preflight_release.py`](../scripts/preflight_release.py)；测试：[`tests/test_preflight_release.py`](../tests/test_preflight_release.py)。

## 2. Release Smoke Suite — 一键编排

把 doctor、strict 离线评测、security corpus、Agent 评测、baseline compare、可选 MCP / A2A smoke 串成一个命令：

```bash
# 离线，CI 安全：doctor + strict eval suite + security corpus + Agent eval + baseline compare
python scripts/smoke_release.py --offline

# 带服务：额外跑 MCP / A2A 兼容 smoke
python scripts/smoke_release.py --with-server --base-url http://127.0.0.1:8000 --token <token>
```

`smoke_release.py` 只编排，不持有新逻辑。任意阶段非零退出则整体退出 `1`。可用 `--skip-doctor` / `--skip-evals` / `--skip-security` / `--skip-agent` / `--skip-compare` / `--skip-mcp` / `--skip-a2a` 裁剪。

实现：[`scripts/smoke_release.py`](../scripts/smoke_release.py)；测试：[`tests/test_smoke_release.py`](../tests/test_smoke_release.py)。

## 3. Release Manifest & Checksum — 发布产物证明

每次跑 [`scripts/release.py`](../scripts/release.py) 不再只产出一个 zip，还会在 `dist/` 下产出三件套：

```text
dist/deepseek-infra-2.4.5.zip
dist/deepseek-infra-2.4.5.zip.sha256
dist/deepseek-infra-2.4.5.manifest.json
```

`manifest.json` 记录发布的关键事实，可独立校验：

```json
{
  "schemaVersion": "release-manifest.v1",
  "version": "2.4.6",
  "commit": "abc1234",
  "builtAt": "2026-06-28T00:00:00Z",
  "python": "3.12",
  "coverageGate": "80%",
  "qualityGates": {
    "coverage": "80%",
    "offlineEval": "PASS",
    "agentEval": "PASS",
    "injectionStrict": "PASS",
    "baselineCompare": "PASS",
    "securityCorpus": "PASS"
  },
  "evalReport": "evals/reports/latest.json",
  "agentReport": "evals/reports/agent-latest.json",
  "evidence": [
    "docs/evidence/headless-mcp-bridge.json",
    "docs/evidence/a2a-external-peer.json",
    "docs/evidence/a2a-third-party-peer.json",
    "docs/evidence/edge-router-smoke.json",
    "docs/evidence/continue-dev-mcp.json",
    "docs/evidence/openai-compatible-sdks.json",
    "evals/reports/latest.json",
    "evals/reports/agent-latest.json",
    "evals/reports/baseline-compare-latest.json",
    "evals/reports/security-latest.json",
    "docs/EVIDENCE_INDEX.md"
  ],
  "artifact": "deepseek-infra-2.4.6.zip",
  "sha256": "...",
  "bytes": 1234567
}
```

`.sha256` 是标准 `<hex>  <filename>` 格式，可用 `sha256sum -c` 校验。`--no-manifest` 可跳过这两个伴生产物；`--dry-run` 只枚举将要打包的文件数，不写 zip / checksum / manifest。

实现：[`deepseek_infra/infra/diagnostics/release_manifest.py`](../deepseek_infra/infra/diagnostics/release_manifest.py)；测试：[`tests/test_release_manifest.py`](../tests/test_release_manifest.py)。

## 4. CI release-readiness job

`.github/workflows/ci.yml` 的 `release-readiness` job 在干净 Ubuntu runner 中跑：

```yaml
- run: python scripts/smoke_mcp_headless_bridge.py --out docs/evidence/headless-mcp-bridge.json
- run: python scripts/smoke_a2a_external_peer.py --out docs/evidence/a2a-external-peer.json
- run: python scripts/preflight_release.py --version 2.4.6
- run: python scripts/doctor.py --offline
- run: python scripts/release.py --clean-workspace --dry-run
```

CI 不强制安装真实第三方 A2A server、Ollama 或 GGUF 模型；这些 evidence 缺失时是 WARNING。一旦提交对应 evidence，preflight 会按严格 schema 和 PASS checks 验证。

## 5. Headless MCP Evidence（v2.3.2）

`preflight_release.py` 自 v2.3.2 起增加 `headless_mcp_bridge_evidence` 硬检查。它读取 `docs/evidence/headless-mcp-bridge.json`，确认无 GUI 的 MCP stdio bridge 路径已经自动跑通：

- `bridge.start`
- `mcp.initialize`
- `mcp.tools_list`
- `mcp.tools_call`
- `mcp.policy_denial`

本项是最低交付标准，缺失或失败会让 preflight 返回 `FAIL`。刷新命令：

```bash
python scripts/smoke_mcp_headless_bridge.py --out docs/evidence/headless-mcp-bridge.json
```

## 6. A2A External Peer Evidence（v2.3.3）

`preflight_release.py` 自 v2.3.3 起增加 `a2a_external_peer_evidence` 硬检查。它读取 `docs/evidence/a2a-external-peer.json`，确认一个无 GUI、无 API key 的外部 A2A peer 路径已经自动跑通：

- `agentCard`
- `messageSend`
- `messageStream`
- `tasksGet`
- `tasksCancel`
- `tasksList`
- `artifactChunks`
- `sseFinalEvent`

本项是最低交付标准，缺失或失败会让 preflight 返回 `FAIL`。刷新命令：

```bash
python scripts/smoke_a2a_external_peer.py --out docs/evidence/a2a-external-peer.json
```

## 7. A2A Third-Party Peer Evidence（v2.4.4）

`preflight_release.py` 自 v2.4.4 起增加 `a2a_third_party_peer_evidence` 可选检查。它读取 `docs/evidence/a2a-third-party-peer.json`，确认 DeepSeek Infra 的 `A2AClient` 路径已经连接到第三方或第三方风格 A2A-compatible peer，并完成完整互操作 smoke：

- `agentCard`
- `messageSend`
- `messageStream`
- `tasksGet`
- `tasksCancel`
- `tasksList`
- `artifactChunks`
- `sseFinalEvent`

本项缺失时返回 `WARNING`，避免没有第三方生态环境的 CI runner 被阻断；一旦 evidence 文件存在，则 `version`、统一 metadata、`peerType=third-party`、`status=PASS` 与八类 checks 都必须通过，否则 preflight 返回 `FAIL`。刷新命令：

```bash
python scripts/smoke_a2a_external_peer.py --peer-url http://<third-party-host>:<port> --peer-type third-party --out docs/evidence/a2a-third-party-peer.json --markdown docs/evidence/a2a-third-party-peer.md
```

当前 evidence 记录的是 A2A-compatible third-party-style smoke peer；LangGraph / CrewAI / Google A2A reference 等具体实现仍保留在 [a2a-third-party-plan.md](integrations/a2a-third-party-plan.md) 中作为后续候选。

## 8. Edge Router Smoke Evidence（v2.4.3）

`preflight_release.py` 自 v2.4.3 起增加 `edge_router_smoke_evidence` 可选检查。它读取 `docs/evidence/edge-router-smoke.json`，确认 Edge / Ollama / 本地 OpenAI-compatible provider 路径已经记录结构化 evidence：

- `ollamaModelsListed`
- `openaiCompatibleLocalCall`
- `edgeStatusEndpoint`
- `fallbackReady`

本项缺失时返回 `WARNING`，避免没有 Ollama / GGUF 模型的 CI runner 被强制阻断；一旦 evidence 文件存在，则 `version`、`status=PASS` 与四类 checks 都必须通过，否则 preflight 返回 `FAIL`。刷新命令：

```bash
python examples/edge_router_smoke.py --require-ollama --out docs/evidence/edge-router-smoke.json --markdown docs/evidence/edge-router-smoke.md
```

真实 GGUF / MLC 推理仍依赖本地模型文件与可选依赖；本检查只把可复现的本地 provider 路径纳入 release evidence，不把 Edge-Cloud Model Router 升级为 Working。

## 9. Continue.dev MCP Evidence（v2.4.5）

`preflight_release.py` 自 v2.4.5 起增加 `continue_dev_mcp_evidence` 可选检查。它读取 `docs/evidence/continue-dev-mcp.json`，确认 Continue.dev MCP client 路径已经记录结构化 evidence：

- `configLoaded`
- `mcpInitialize`
- `toolsList`
- `lowRiskToolCall`
- `policyDenial`
- `promptInjectionClean`

本项缺失时返回 `WARNING`，避免没有 Continue.dev GUI 环境的 CI runner 被强制阻断；一旦 evidence 文件存在，则 `version`、统一 metadata、`status=PASS` 与六类 checks 都必须通过，否则 preflight 返回 `FAIL`。Continue.dev 配置指南与验证 runbook 见 [docs/integrations/continue-dev.md](integrations/continue-dev.md)。

## 10. OpenAI-Compatible SDK Evidence（v2.4.6）

`preflight_release.py` 自 v2.4.6 起增加 `openai_compatible_sdk_evidence` 可选检查。它读取 `docs/evidence/openai-compatible-sdks.json`，确认 LangChain (ChatOpenAI)、LiteLLM、LlamaIndex (OpenAILike) 等 OpenAI-compatible SDK 路径已经记录结构化 evidence：

- `sdks.langchain.modelsList`
- `sdks.langchain.chatCompletion`
- `sdks.langchain.streaming`
- `sdks.litellm.modelsList`
- `sdks.litellm.chatCompletion`
- `sdks.litellm.streaming`
- `sdks.llamaindex.chatCompletion`

本项缺失时返回 `WARNING`，避免没有安装 LangChain / LiteLLM / LlamaIndex 等可选依赖的 CI runner 被强制阻断；一旦 evidence 文件存在，则 `version`、统一 metadata、`status=PASS` 与七类 SDK checks 都必须通过，否则 preflight 返回 `FAIL`。SDK smoke 依赖放在 `requirements-sdk-smoke.txt` 中，与默认运行时依赖解耦。

```bash
python scripts/smoke_openai_compatible_sdks.py --base-url http://127.0.0.1:8000/v1 --model deepseek-v4-pro --out docs/evidence/openai-compatible-sdks.json --markdown docs/evidence/openai-compatible-sdks.md
```

## 11. Evidence Index & Metadata（v2.3.4）

v2.3.4 新增 [`docs/EVIDENCE_INDEX.md`](../docs/EVIDENCE_INDEX.md) 作为所有互操作证据的统一入口，并在 preflight 中检查：

- `docs/EVIDENCE_INDEX.md` 存在。
- 关键证据 JSON（headless MCP bridge、A2A external peer、A2A third-party peer、Edge Router、Continue.dev MCP、OpenAI-compatible SDK、latest eval、agent eval）包含统一 metadata：`version`、`commit`、`generatedAt`、`environment`（含 `os` / `python` / `ci`）、`status`。
- release manifest 包含 `evidence` 列表。

刷新命令：

```bash
python scripts/smoke_mcp_headless_bridge.py --out docs/evidence/headless-mcp-bridge.json
python scripts/smoke_a2a_external_peer.py --out docs/evidence/a2a-external-peer.json
python scripts/smoke_a2a_external_peer.py --peer-url http://<third-party-host>:<port> --peer-type third-party --out docs/evidence/a2a-third-party-peer.json --markdown docs/evidence/a2a-third-party-peer.md
python examples/edge_router_smoke.py --require-ollama --out docs/evidence/edge-router-smoke.json --markdown docs/evidence/edge-router-smoke.md
python scripts/smoke_openai_compatible_sdks.py --base-url http://127.0.0.1:8000/v1 --model deepseek-v4-pro --out docs/evidence/openai-compatible-sdks.json --markdown docs/evidence/openai-compatible-sdks.md
python evals/runners/run_offline_eval_suite.py --include-agent --strict --out evals/reports/latest.json --markdown evals/reports/latest.md
python evals/runners/run_security_corpus.py --strict --out evals/reports/security-latest.json --markdown evals/reports/security-latest.md
python evals/runners/run_agent_eval.py --report-dir evals/reports --strict
python evals/runners/compare_eval_baseline.py --strict --baseline evals/baselines/v2.2.6.json --current evals/reports/latest.json --agent-baseline evals/baselines/agent-v2.2.8.json --out evals/reports/baseline-compare-latest.json
```

## 12. Docs Encoding Sanity（v2.3.4）

`preflight_release.py` 自 v2.3.4 起新增 `docs_encoding_sanity` 硬检查，扫描以下文档是否包含编码乱码：

- `CHANGELOG.md`
- `README.md`
- `docs/COMPATIBILITY.md`
- `docs/IMPLEMENTATION_STATUS.md`
- `docs/RELEASE_READINESS.md`
- `docs/EVIDENCE_INDEX.md`
- `docs/integrations/*.md`

识别模式：连续 `???`、`锟斤拷`、Unicode replacement character `\ufffd`。发现即 FAIL，防止 v2.3.3 的 CHANGELOG 乱码问题再次出现。

## 13. Quality Gate Evidence（v2.4.6）

`preflight_release.py` 自 v2.4.2 起增加 `quality_gate_evidence` 硬检查。它聚合以下证据：

- coverage gate：`pyproject.toml` 与 CI 均为 80%。
- offline eval：`evals/reports/latest.json` `status=PASS`。
- Agent Eval：`evals/reports/agent-latest.json` `status=PASS`。
- baseline compare：`evals/reports/baseline-compare-latest.json` `status=PASS`。
- injection strict：`latest.json` 的 `injection.status=PASS` 且 `gateMode=hard`。
- security corpus：`evals/reports/security-latest.json` `status=PASS`。

刷新命令：

```bash
python scripts/update_eval_report.py
python scripts/preflight_release.py --version 2.4.6
```

## 14. GUI Interop Evidence Checklist（v2.3.1）

`preflight_release.py` 自 v2.3.1 起增加 `gui_interop_evidence` 检查，扫描 `docs/COMPATIBILITY.md` 中 Claude Desktop / Cursor 行的状态标记：

- **🟡 状态**：GUI 实机证据尚未填入，检查结果为 `WARNING`。
- **✅ GUI tested 状态**：人工完成 GUI 验证 runbook 并更新矩阵后，检查结果为 `PASS`。

详见 [docs/integrations/claude-desktop.md](integrations/claude-desktop.md) 和 [docs/integrations/cursor.md](integrations/cursor.md)。

## 发版前最小流程

```bash
# 1. 刷新 eval / agent 报告到当前版本
python scripts/update_eval_report.py

# 2. 刷新 headless MCP bridge evidence
python scripts/smoke_mcp_headless_bridge.py --out docs/evidence/headless-mcp-bridge.json

# 3. 刷新 A2A external peer evidence
python scripts/smoke_a2a_external_peer.py --out docs/evidence/a2a-external-peer.json

# 4. 刷新 A2A third-party evidence（需要第三方或第三方风格 A2A-compatible peer）
python scripts/smoke_a2a_external_peer.py --peer-url http://<third-party-host>:<port> --peer-type third-party --out docs/evidence/a2a-third-party-peer.json --markdown docs/evidence/a2a-third-party-peer.md

# 5. 刷新 Edge Router smoke evidence（需要本地 Ollama / Ollama-compatible provider）
python examples/edge_router_smoke.py --require-ollama --out docs/evidence/edge-router-smoke.json --markdown docs/evidence/edge-router-smoke.md

# 6. 版本一致性与质量证据体检
python scripts/preflight_release.py --version 2.4.6

# 7. 运行时体检
python scripts/doctor.py --offline

# 8. 一键 smoke（离线）
python scripts/smoke_release.py --offline

# 9. 打包并生成 manifest + checksum + qualityGates
python scripts/release.py --clean-workspace --version 2.4.6
```

也可以直接用 `python scripts/smoke_release.py --offline` 刷新离线质量证据；本地模型和第三方生态 evidence 需要在具备对应环境时单独补齐。
