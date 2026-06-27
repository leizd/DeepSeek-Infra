# Release Readiness

适用版本：v2.3.3。

v2.2.9 是 v2.2.x 的收官版，主题是**发布前体检、运行时诊断、版本一致性与产物可验证**——不再扩大协议面或评测面，而是为 v2.3 的真实互操作验证提供一个稳定、可自证交付的底座。本页把三件事串起来：发版前体检（preflight）、一键 smoke 编排、发布产物证明（manifest + checksum）。

## 1. Release Preflight — 版本一致性体检

发版前确认版本号在所有该出现的地方都同步，eval 报告是当前版本，且发布脚本仍排除本地缓存 / 日志 / 密钥：

```bash
python scripts/preflight_release.py --version 2.3.3
```

检查项：

- README 版本徽章是 `2.3.3`。
- `CHANGELOG.md` 顶部有 `## [2.3.3]` 条目。
- `Dockerfile` 示例 tag 是 `deepseek-infra:2.3.3`。
- `docs/IMPLEMENTATION_STATUS.md` 与 `evals/README.md` 的「适用版本」是 `v2.3.3`。
- `docs/AGENT_EVAL.md` / `docs/EVAL_REPORTS.md` / `docs/SECURITY_SMOKE.md` / `docs/integrations/headless-mcp-client.md` / `docs/integrations/a2a-external-peer.md` 存在。
- `evals/reports/latest.json` 的 `version` 是 `2.3.3`。
- `evals/reports/agent-latest.json` 可解析且 `version` 是 `2.3.3`。
- `docs/evidence/headless-mcp-bridge.json` 可解析、版本为 `2.3.3`，且关键 MCP bridge 步骤全为 PASS。
- `docs/evidence/a2a-external-peer.json` 可解析、版本为 `2.3.3`，且关键 A2A external peer checks 全为 PASS。
- `scripts/release.py` 仍排除 `.traces` / `.local-rag` / `.auth-token` / `.env` / `server*.log`。

退出码：`1` 表示有 `FAIL`；`WARNING`（如 eval 报告缺失）不失败。`--json` 输出机器可读摘要。

实现：[`scripts/preflight_release.py`](../scripts/preflight_release.py)；测试 [`tests/test_preflight_release.py`](../tests/test_preflight_release.py)。

## 2. Release Smoke Suite — 一键编排

把 doctor、离线评测、Agent 评测、（可选）MCP / A2A smoke 串成一个命令：

```bash
# 离线（CI 安全）：doctor + offline eval suite + Agent eval
python scripts/smoke_release.py --offline

# 带服务：额外跑 MCP / A2A 兼容 smoke
python scripts/smoke_release.py --with-server --base-url http://127.0.0.1:8000 --token <token>
```

`smoke_release.py` 只编排，不持有新逻辑。它按顺序跑：

1. `scripts/doctor.py`（离线模式带 `--offline`，带服务模式带 `--with-server --base-url`）。
2. `evals/runners/run_offline_eval_suite.py --out evals/reports/latest.json --markdown evals/reports/latest.md`。
3. `evals/runners/run_agent_eval.py --report-dir evals/reports --report-only`。
4. （`--with-server`）`scripts/smoke_mcp_compat.py`。
5. （`--with-server`）`scripts/smoke_a2a_compat.py`。

任意阶段非零退出则整体退出 `1`。可用 `--skip-doctor` / `--skip-evals` / `--skip-agent` / `--skip-mcp` / `--skip-a2a` 裁剪。`--json` 只打印计划不执行。

实现：[`scripts/smoke_release.py`](../scripts/smoke_release.py)；测试 [`tests/test_smoke_release.py`](../tests/test_smoke_release.py)。

## 3. Release Manifest & Checksum — 发布产物证明

每次跑 [`scripts/release.py`](../scripts/release.py) 不再只产出一个 zip，还会在 `dist/` 下产出三件套：

```
dist/deepseek-infra-2.3.3.zip
dist/deepseek-infra-2.3.3.zip.sha256
dist/deepseek-infra-2.3.3.manifest.json
```

`manifest.json` 记录发布的关键事实，可独立校验：

```json
{
  "schemaVersion": "release-manifest.v1",
  "version": "2.3.3",
  "commit": "abc1234",
  "builtAt": "2026-06-27T00:00:00Z",
  "python": "3.12",
  "coverageGate": "75%",
  "evalReport": "evals/reports/latest.json",
  "agentReport": "evals/reports/agent-latest.json",
  "artifact": "deepseek-infra-2.3.3.zip",
  "sha256": "...",
  "bytes": 1234567
}
```

`.sha256` 是标准 `<hex>  <filename>` 格式，可用 `sha256sum -c` 校验。`--no-manifest` 可跳过这两个伴生产物；`--dry-run` 只枚举将要打包的文件数，不写 zip / checksum / manifest（CI 用它确认发布脚本可执行）。

这是 v2.2.7 / v2.2.8 把 **eval evidence**（`latest.json` / `agent-latest.json`）做起来之后，v2.2.9 补齐的 **release evidence**：一个发布既是自描述的（manifest），又是可校验的（sha256）。

实现：[`deepseek_infra/infra/diagnostics/release_manifest.py`](../deepseek_infra/infra/diagnostics/release_manifest.py)；测试 [`tests/test_release_manifest.py`](../tests/test_release_manifest.py)。

## 4. CI release-readiness job

`.github/workflows/ci.yml` 新增 `release-readiness` job，在干净 Ubuntu runner 上跑：

```yaml
- run: python scripts/smoke_mcp_headless_bridge.py --out docs/evidence/headless-mcp-bridge.json
- run: python scripts/smoke_a2a_external_peer.py --out docs/evidence/a2a-external-peer.json
- run: python scripts/preflight_release.py --version 2.3.3
- run: python scripts/doctor.py --offline
- run: python scripts/release.py --clean-workspace --dry-run
```

它不要求 API Key，也不访问公网，确保每次 PR 都能确认版本同步、环境体检通过、发布脚本可执行。

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

本项是最低交付标准，缺失或失败会让 preflight 返回 `FAIL`。真实第三方生态 evidence 使用 `docs/evidence/a2a-third-party-peer.json`，缺失时只返回 `WARNING`。刷新命令：

```bash
python scripts/smoke_a2a_external_peer.py --out docs/evidence/a2a-external-peer.json
```

## 7. GUI Interop Evidence Checklist（v2.3.1）

`preflight_release.py` 自 v2.3.1 起增加 `gui_interop_evidence` 检查，扫描 `docs/COMPATIBILITY.md` 中 Claude Desktop / Cursor 行的状态标记：

- **🟡 状态**：GUI 实机证据尚未填入 → 检查结果为 `WARNING`（不阻断 CI，但发版摘要里会提醒）。
- **✅ GUI tested 状态**：人工完成 GUI 验证 runbook 并更新矩阵后 → 检查结果为 `PASS`。

### 人工完成 GUI 验证的步骤

1. 按 `docs/integrations/claude-desktop.md` 的 §5 runbook 在装有 Claude Desktop 的机器上跑通：连接 `/mcp` → `tools/list` → 低风险工具调用 → Tool Policy 拦截 → 系统提示无污染。
2. 按 `docs/integrations/cursor.md` 的 §5 runbook 在装有 Cursor 的机器上跑通同样验收项。
3. 把实测版本、日期、commit、OS 和通过项填入两份文档的 Evidence Template。
4. 更新 `docs/COMPATIBILITY.md` 的 MCP Client Compatibility 表，把 Claude Desktop / Cursor 行从 `🟡` 改为 `✅ GUI tested`，并补上实测版本与 commit。
5. 重跑 `python scripts/preflight_release.py --version <current>`，确认 `gui_interop_evidence` 变为 `PASS`。

详见 [docs/integrations/claude-desktop.md](integrations/claude-desktop.md) 和 [docs/integrations/cursor.md](integrations/cursor.md)。

## 发版前最小流程

```bash
# 1. 刷新 eval / agent 报告到当前版本
python scripts/update_eval_report.py

# 2. 刷新 headless MCP bridge evidence
python scripts/smoke_mcp_headless_bridge.py --out docs/evidence/headless-mcp-bridge.json

# 3. 刷新 A2A external peer evidence
python scripts/smoke_a2a_external_peer.py --out docs/evidence/a2a-external-peer.json

# 4. 版本一致性体检
python scripts/preflight_release.py --version 2.3.3

# 5. 运行时体检
python scripts/doctor.py --offline

# 6. 一键 smoke（离线）
python scripts/smoke_release.py --offline

# 7. 打包并生成 manifest + checksum
python scripts/release.py --clean-workspace --version 2.3.3
```

或直接用 `python scripts/smoke_release.py --offline`（已包含 doctor + evals + agent）。
