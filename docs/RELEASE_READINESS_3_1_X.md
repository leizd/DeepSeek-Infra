# Release Readiness Checklist — 3.1.x / 3.2.5 Quality Track

This checklist is the go/no-go gate for the hybrid Rust runtime line through the 3.2.5 RC-readiness audit. It verifies that Rust integration is stable and safely disabled by default, then feeds those facts into the separate 4.0 RC blocker matrix.

> **Current verdict**: the 3.2.x hybrid baseline is green, but the repository is **NOT READY FOR 4.0.0-rc.1**. See [4_0_RC_READINESS.md](4_0_RC_READINESS.md). This milestone does not enable Rust components, raise the 85% current gate, or create an RC.

---

## CI gates

The following jobs must pass on every PR and on `main`:

| Gate | Command / Job | Owner |
| --- | --- | --- |
| Python lint | `ruff check .` | ci / test |
| Python type check | `mypy .` | ci / test |
| Python test coverage | `pytest --cov --cov-fail-under=85` | ci / test |
| Rust formatting | `cargo fmt --manifest-path rust/Cargo.toml --all -- --check` | ci / rust |
| Rust lint | `cargo clippy --manifest-path rust/Cargo.toml --all-targets --all-features -- -D warnings` | ci / rust |
| Rust tests | `cargo test --manifest-path rust/Cargo.toml --all` | ci / rust |
| Rust sidecar image | `ci / rust-docker` | ci / rust-docker |
| Hybrid runtime E2E | `ci / hybrid-runtime-e2e` | ci / hybrid-runtime-e2e |
| Rust/Python RAG parity | `ci / rag-parity` | ci / rag-parity |
| 4.0 RC readiness report | `ci / rc-readiness` | Release Engineering |
| JS syntax | `node --check static/vendor/katex/katex.min.js static/math_core.js static/seek_core.js static/app.js static/modules/network.js static/modules/markdown.js static/modules/settings.js static/modules/panels.js static/modules/chat.js static/modules/trace_waterfall.js static/modules/trace_viewer.js` | ci / test |
| Docs link check | `python scripts/check_doc_links.py` | ci / docs |
| Dependency audit | `pip-audit -r requirements.txt -r requirements-dev.txt` | ci / security |
| Static analysis | `bandit -r deepseek_infra --severity-level high -q` | ci / security |
| Secret scan | `detect-secrets scan --baseline .secrets.baseline` | ci / security |

---

## Offline eval gates

Run from the repo root with no API keys:

```bash
PYTHONHASHSEED=0 python evals/runners/run_rag_eval.py
python evals/runners/run_tool_eval.py
python evals/runners/run_injection_adversarial.py --strict --no-report
python evals/runners/run_security_corpus.py --strict
python evals/runners/run_agent_eval.py --strict
```

These enforce the v2.4.0 thresholds:

- Injection: `blockRate >= 0.85`, `falsePositiveRate <= 0.10`, `bypassRate <= 0.15`
- Agent: Tool Call Accuracy `>= 0.90`, Agent Success Rate `>= 0.85`, Prompt Regression Pass Rate `>= 0.90`
- Security corpus: versioned attack/benign corpus metrics
- Baseline compare: `python evals/runners/compare_eval_baseline.py --strict --baseline evals/baselines/v2.2.6.json --current evals/reports/latest.json --agent-baseline evals/baselines/agent-v2.2.8.json`

---

## Runtime gates

Before declaring the current hybrid release ready, verify the following runtime configurations manually or through the release-readiness CI job:

### 1. All Rust flags disabled (default)

```bash
python -m deepseek_infra.app
```

- `GET /api/rust/status` returns all flags disabled.
- `/v1/models`, `/mcp`, tool calls, and RAG queries work through Python only.
- Coverage gate still passes.

### 2. Each Rust flag enabled individually

For each component, start the sidecar and enable only that flag:

```bash
cd rust
cargo run -p deepseek-gateway &

# Gateway only
DEEPSEEK_RUST_GATEWAY=1 python -m deepseek_infra.app

# MCP only
DEEPSEEK_RUST_MCP=1 python -m deepseek_infra.app

# Policy only
DEEPSEEK_RUST_POLICY=1 python -m deepseek_infra.app

# RAG only
DEEPSEEK_RUST_RAG=1 python -m deepseek_infra.app
```

Verify that the enabled path hits Rust and the disabled paths still use Python. See [RUST_HYBRID_RUNTIME_RUNBOOK.md](RUST_HYBRID_RUNTIME_RUNBOOK.md) for verification commands.

### 3. All Rust flags enabled together

```bash
DEEPSEEK_RUST_GATEWAY=1 \
DEEPSEEK_RUST_MCP=1 \
DEEPSEEK_RUST_POLICY=1 \
DEEPSEEK_RUST_RAG=1 \
python -m deepseek_infra.app
```

- `GET /api/rust/status` shows all flags enabled and the gateway healthy.
- `/v1/models` and non-streaming `/v1/chat/completions` route through Rust Gateway.
- `/mcp` routes through Rust MCP.
- Tool URL/path/capability checks route through Rust Policy.
- RAG query normalization, chunk scoring, and citation formatting route through Rust RAG.

3.2.2 automates this configuration through the test-only Compose overlay:

```bash
cp .env.example .env
docker compose -f docker-compose.yml -f docker-compose.hybrid-test.yml up --detach --build
python scripts/smoke_hybrid_runtime.py
docker compose -f docker-compose.yml -f docker-compose.hybrid-test.yml down --volumes
```

The smoke uses only the Rust deterministic stub and local Python runtime. It does not require an API key, external model call, or external network service.

### 4. Sidecar unavailable fallback

With any flag enabled, stop the sidecar and repeat the relevant request:

```bash
# With Gateway enabled
DEEPSEEK_RUST_GATEWAY=1 python -m deepseek_infra.app
# Then kill the sidecar process
curl http://127.0.0.1:8000/v1/models -H "Authorization: Bearer ..."
```

Expected: request falls back to Python because `DEEPSEEK_RUST_GATEWAY_FALLBACK=1` by default.

The 3.2.2 smoke stops `rust-gateway` after the healthy-path checks and proves Gateway, MCP, Policy, and RAG fallback through the still-running Python container.

### 5. Policy deny blocks unsafe tool call and is auditable

With `DEEPSEEK_RUST_POLICY=1`, attempt a tool call that the Rust Policy sidecar should deny (e.g., a private IP URL or a path traversal). The call must be blocked before its implementation runs. The response and structured audit event must preserve the same stable `code`, `decision_id`, and `trace_id`; credentials, authorization values, complete sensitive arguments, and workspace roots must not appear in logs.

Repeat a safe tool call with the sidecar unavailable under each `DEEPSEEK_RUST_POLICY_FAILURE_MODE`:

- `fallback`: Python Tool Policy evaluates the call.
- `deny`: execution is blocked with `policy_backend_unavailable`.
- `error`: execution is blocked with a structured `status: 503` response.

### 6. Deterministic RAG parity corpus

Run the shared corpus against a live Rust sidecar:

```bash
python scripts/check_rag_parity.py \
  --base-url http://127.0.0.1:8787 \
  --strict \
  --report artifacts/rag-parity-report.json
```

All 38 cases must pass. Query normalization and citation output must match exactly; Top-K IDs and tie-break order must match exactly; scores must be within `1e-6`; validation outcomes must use the same stable category. See [RAG_PARITY_BASELINE.md](RAG_PARITY_BASELINE.md).

---

## Release evidence

The `ci / release-readiness` job produces the following artifacts:

| Evidence | Script | Output |
| --- | --- | --- |
| MCP headless bridge | `scripts/smoke_mcp_headless_bridge.py` | `docs/evidence/headless-mcp-bridge.json` |
| A2A external peer | `scripts/smoke_a2a_external_peer.py` | `docs/evidence/a2a-external-peer.json` |
| General availability | `scripts/smoke_ga.py` | `docs/evidence/ga-v3.8.0.json` |
| Workspace | `scripts/smoke_workspace.py` | `docs/evidence/workspace-v3.8.0.json` |
| Edge router | `scripts/smoke_edge_router.py` | `docs/evidence/edge-router-v3.8.0.json` |
| Media | `scripts/smoke_media.py` | `docs/evidence/media-v3.8.0.json` |
| Browser | `scripts/smoke_browser.py` | `docs/evidence/browser-v3.8.0.json` |
| Automation | `scripts/smoke_automation.py` | `docs/evidence/automation-v3.8.0.json` |
| Skills | `scripts/smoke_skills.py` | `docs/evidence/skills-v3.8.0.json` |
| Skills UI | `scripts/smoke_skills_ui.py` | `docs/evidence/skills-ui-v3.8.0.json` |
| Skill builder | `scripts/smoke_skill_builder.py` | `docs/evidence/skill-builder-v3.8.0.json` |
| Skill packs | `scripts/smoke_skill_packs.py` | `docs/evidence/skill-packs-v3.8.0.json` |
| Skill eval dashboard | `scripts/smoke_skill_eval_dashboard.py` | `docs/evidence/skill-eval-dashboard-v3.8.0.json` |
| Skill versioning | `scripts/smoke_skill_versioning.py` | `docs/evidence/skill-versioning-v3.8.0.json` |
| Skill analytics | `scripts/smoke_skill_analytics.py` | `docs/evidence/skill-analytics-v3.8.0.json` |
| Skill security | `scripts/smoke_skill_security.py` | `docs/evidence/skill-security-v3.8.0.json` |
| Skill catalog | `scripts/smoke_skill_catalog.py` | `docs/evidence/skill-catalog-v3.8.0.json` |
| Context taint | `scripts/smoke_context_taint.py` | `docs/evidence/context-taint-v3.8.0.json` |
| Hybrid Python/Rust runtime | `scripts/smoke_hybrid_runtime.py` | `ci / hybrid-runtime-e2e` log |
| Rust/Python RAG parity | `scripts/check_rag_parity.py` | `artifacts/rag-parity-report.json` |
| 4.0 RC readiness | `scripts/check_4_0_rc_readiness.py` | `artifacts/4-0-rc-readiness.json` |

The release preflight also runs:

```bash
python scripts/preflight_release.py --version 3.8.0 --ga
python scripts/doctor.py --offline
python scripts/release.py --clean-workspace --dry-run
```

---

## Rollback checklist

If a release is bad, roll back to the pure Python runtime:

1. Disable all Rust flags:

   ```bash
   export DEEPSEEK_RUST_GATEWAY=0
   export DEEPSEEK_RUST_MCP=0
   export DEEPSEEK_RUST_POLICY=0
   export DEEPSEEK_RUST_RAG=0
   ```

2. Restart the Python process.
3. Confirm `GET /api/rust/status` reports all flags disabled.
4. Verify `/v1/models`, `/mcp`, tool calls, and RAG work normally.

No state migration is needed because Rust components are stateless delegates.

---

## 4.0 RC readiness mode

Normal PRs and `main` run the checker with `--report-only`, upload its JSON artifact, and remain usable while future RC targets are incomplete. Pushes to `release/*` and `rc/*` run `--strict`; any unresolved blocker then fails `ci / rc-readiness`.

The current blockers are measured Python coverage below 95.00%, three unapproved runtime lifecycle/default decisions, Gateway streaming ownership, and the MCP real tool bridge. The 85% current CI gate is a passing baseline requirement, not a substitute for the 95% RC target.

## Sign-off

Before accepting the 3.2.5 readiness audit, confirm:

- [ ] All CI gates above are green on the release commit.
- [ ] All offline eval gates above pass with `--strict`.
- [ ] Runtime gates 1–6 above have been executed; gates 3–5 are covered by `ci / hybrid-runtime-e2e` and gate 6 by `ci / rag-parity`.
- [ ] The hybrid runtime runbook is up to date: [RUST_HYBRID_RUNTIME_RUNBOOK.md](RUST_HYBRID_RUNTIME_RUNBOOK.md).
- [ ] `docs/RUST_MIGRATION_ROADMAP.md` reflects the 3.2.5 readiness state.
- [ ] `CHANGELOG.md` has a 3.2.5 entry.
- [ ] `python scripts/check_4_0_rc_readiness.py --report-only` emits the current blocker matrix and JSON report.
- [ ] The default Compose deployment and Rust default-disabled behavior are unchanged.
- [ ] No coverage-gate increase or 4.0.0 breaking change is included in the release.

---

## Related documents

- [Hybrid Rust Runtime Runbook](RUST_HYBRID_RUNTIME_RUNBOOK.md)
- [4.0 RC Readiness](4_0_RC_READINESS.md)
- [RAG Parity Baseline](RAG_PARITY_BASELINE.md)
- [Rust Migration Roadmap](RUST_MIGRATION_ROADMAP.md)
- [Implementation Status](IMPLEMENTATION_STATUS.md)
- [CHANGELOG.md](../CHANGELOG.md)
