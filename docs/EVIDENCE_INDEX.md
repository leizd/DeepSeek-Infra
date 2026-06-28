# Release Evidence Index

适用版本：v2.4.3。

本页汇总 DeepSeek Infra v2.3.x 以来的互操作证据、评测报告、v2.4 质量门禁证据与 release artifact，作为证据链的统一入口。所有标 ✅ 的项都有可复现的 smoke / evidence 路径；标 🟡 的项需要人工 GUI、本地模型或真实第三方生态实测。

## Evidence Matrix

| Evidence | File | Status | Reproduce |
| --- | --- | --- | --- |
| MCP official SDK interop | [docs/integrations/external-mcp-server.md](integrations/external-mcp-server.md) | ✅ Tested | `python scripts/smoke_mcp_compat.py --external-server-url <url>` |
| Headless MCP bridge | [docs/evidence/headless-mcp-bridge.json](evidence/headless-mcp-bridge.json) | ✅ Tested | `python scripts/smoke_mcp_headless_bridge.py --out docs/evidence/headless-mcp-bridge.json` |
| A2A external peer | [docs/evidence/a2a-external-peer.json](evidence/a2a-external-peer.json) | ✅ Tested | `python scripts/smoke_a2a_external_peer.py --out docs/evidence/a2a-external-peer.json` |
| Edge Router smoke | [docs/evidence/edge-router-smoke.json](evidence/edge-router-smoke.json) / [edge-router-smoke.md](evidence/edge-router-smoke.md) | ✅ Smoke evidence | `python examples/edge_router_smoke.py --require-ollama --out docs/evidence/edge-router-smoke.json --markdown docs/evidence/edge-router-smoke.md` |
| Claude Desktop GUI | [docs/integrations/claude-desktop.md](integrations/claude-desktop.md) | ✅ GUI tested | Claude Desktop 0.9.0, commit `54228c4`, Windows 11, 2026-06-28 |
| Cursor GUI | [docs/integrations/cursor.md](integrations/cursor.md) | ✅ GUI tested | Cursor 0.48.0, commit `54228c4`, Windows 11, 2026-06-28 |
| Third-party A2A ecosystem | [docs/integrations/a2a-third-party-plan.md](integrations/a2a-third-party-plan.md) | 🟡 Adapter path documented | 待 LangGraph / CrewAI 等真实第三方 A2A peer 实测 |

## Eval Reports

| Report | File | Status |
| --- | --- | --- |
| Offline eval suite | [evals/reports/latest.json](../evals/reports/latest.json) / [latest.md](../evals/reports/latest.md) | PASS |
| Agent eval | [evals/reports/agent-latest.json](../evals/reports/agent-latest.json) / [agent-latest.md](../evals/reports/agent-latest.md) | PASS |
| Baseline compare | [evals/reports/baseline-compare-latest.json](../evals/reports/baseline-compare-latest.json) | PASS |
| Security corpus | [evals/reports/security-latest.json](../evals/reports/security-latest.json) / [security-latest.md](../evals/reports/security-latest.md) | PASS |

## Quality Gate Evidence（v2.4.3）

| Gate | Evidence | Required |
| --- | --- | --- |
| Coverage | `pyproject.toml` + CI `pytest --cov --cov-fail-under=80` | >= 80% |
| Offline eval | `evals/reports/latest.json` | `status=PASS` |
| Agent Eval | `evals/reports/agent-latest.json` | `status=PASS` |
| Baseline compare | `evals/reports/baseline-compare-latest.json` | `status=PASS` |
| Injection strict | `latest.json.injection.status=PASS` + `gateMode=hard` | PASS |
| Security corpus | `evals/reports/security-latest.json` | `status=PASS` |
| Runtime doctor | `python scripts/doctor.py --offline` | exit 0 |
| Release preflight | `python scripts/preflight_release.py --version 2.4.3` | exit 0 |
| Smoke release | `python scripts/smoke_release.py --offline` | exit 0 |

## Release Artifacts

每次发布生成以下 evidence artifact：

| Artifact | Example | Purpose |
| --- | --- | --- |
| Release zip | `dist/deepseek-infra-2.4.3.zip` | 可分发源码包 |
| Checksum | `dist/deepseek-infra-2.4.3.zip.sha256` | 校验 zip 完整性 |
| Manifest | `dist/deepseek-infra-2.4.3.manifest.json` | 版本、commit、构建环境、evidence 清单与 `qualityGates` |

构建命令：

```bash
python scripts/release.py --clean-workspace --version 2.4.3
```

## Preflight Checks

发版前必须通过的 preflight 检查：

```bash
python scripts/preflight_release.py --version 2.4.3
```

关键检查项：

- `docs_encoding_sanity`：文档无 `???`、`锟斤拷`、\ufffd 等乱码。
- `headless_mcp_bridge_evidence`：`docs/evidence/headless-mcp-bridge.json` 存在、版本匹配、关键步骤 PASS。
- `a2a_external_peer_evidence`：`docs/evidence/a2a-external-peer.json` 存在、版本匹配、关键 checks PASS。
- `edge_router_smoke_evidence`：缺失时 WARNING；存在时必须版本匹配、`status=PASS` 且四类 checks 全 PASS。
- `gui_interop_evidence`：Claude Desktop / Cursor 已在 v2.4.2 完成 GUI 实测并改为 PASS。
- `a2a_third_party_evidence`：Third-party A2A ecosystem 为 WARNING，待真实实测。
- `baseline_compare_report` / `security_corpus_report` / `quality_gate_evidence`：v2.4 质量门禁证据齐全且 PASS。

## Refresh Before Release

在干净的 CI / 本地环境依次复现：

```bash
python scripts/smoke_mcp_headless_bridge.py --out docs/evidence/headless-mcp-bridge.json
python scripts/smoke_a2a_external_peer.py --out docs/evidence/a2a-external-peer.json
python examples/edge_router_smoke.py --require-ollama --out docs/evidence/edge-router-smoke.json --markdown docs/evidence/edge-router-smoke.md
python evals/runners/run_offline_eval_suite.py --include-agent --strict --out evals/reports/latest.json --markdown evals/reports/latest.md
python evals/runners/run_agent_eval.py --report-dir evals/reports --strict
python evals/runners/run_security_corpus.py --strict --out evals/reports/security-latest.json --markdown evals/reports/security-latest.md
python evals/runners/compare_eval_baseline.py --strict --baseline evals/baselines/v2.2.6.json --current evals/reports/latest.json --agent-baseline evals/baselines/agent-v2.2.8.json --out evals/reports/baseline-compare-latest.json
python scripts/preflight_release.py --version 2.4.3
```
