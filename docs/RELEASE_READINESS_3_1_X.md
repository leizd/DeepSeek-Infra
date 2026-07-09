# Release Readiness Checklist — 3.1.x

This checklist is the go/no-go gate for the 3.1.x hybrid Rust runtime line. It is intentionally conservative: it verifies that the Rust integration is stable, documented, and can be safely disabled, but it does not require Rust components to be enabled by default.

> **Non-goals for 3.1.x**: enable Rust by default, Dockerize the Rust sidecar, raise the Python coverage gate above 82%, or introduce 4.0.0 breaking changes. Those are tracked in [RUST_MIGRATION_ROADMAP.md](RUST_MIGRATION_ROADMAP.md).

---

## CI gates

The following jobs must pass on every PR and on `main`:

| Gate | Command / Job | Owner |
| --- | --- | --- |
| Python lint | `ruff check .` | ci / test |
| Python type check | `mypy .` | ci / test |
| Python test coverage | `pytest --cov --cov-fail-under=82` | ci / test |
| Rust formatting | `cargo fmt --manifest-path rust/Cargo.toml --all -- --check` | ci / rust |
| Rust lint | `cargo clippy --manifest-path rust/Cargo.toml --all-targets --all-features -- -D warnings` | ci / rust |
| Rust tests | `cargo test --manifest-path rust/Cargo.toml --all` | ci / rust |
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

Before declaring a 3.1.x release ready, verify the following runtime configurations manually or through the release-readiness CI job:

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

### 4. Sidecar unavailable fallback

With any flag enabled, stop the sidecar and repeat the relevant request:

```bash
# With Gateway enabled
DEEPSEEK_RUST_GATEWAY=1 python -m deepseek_infra.app
# Then kill the sidecar process
curl http://127.0.0.1:8000/v1/models -H "Authorization: Bearer ..."
```

Expected: request falls back to Python because `DEEPSEEK_RUST_GATEWAY_FALLBACK=1` by default.

### 5. Policy deny blocks unsafe tool call

With `DEEPSEEK_RUST_POLICY=1`, attempt a tool call that the Rust Policy sidecar should deny (e.g., a private IP URL or a path traversal). The call must be blocked and the deny reason must be present in the response or trace.

### 6. RAG CJK query preserved

With `DEEPSEEK_RUST_RAG=1`, run a query containing CJK characters through the RAG path. The normalized query and returned citations must preserve the CJK content; the evaluation harness verifies this with the `run_rag_eval.py` CJK cases.

---

## Release evidence

The `ci / release-readiness` job produces the following artifacts:

| Evidence | Script | Output |
| --- | --- | --- |
| MCP headless bridge | `scripts/smoke_mcp_headless_bridge.py` | `docs/evidence/headless-mcp-bridge.json` |
| A2A external peer | `scripts/smoke_a2a_external_peer.py` | `docs/evidence/a2a-external-peer.json` |
| General availability | `scripts/smoke_ga.py` | `docs/evidence/ga-v3.0.1.json` |
| Workspace | `scripts/smoke_workspace.py` | `docs/evidence/workspace-v3.0.1.json` |
| Edge router | `scripts/smoke_edge_router.py` | `docs/evidence/edge-router-v3.0.1.json` |
| Media | `scripts/smoke_media.py` | `docs/evidence/media-v3.0.1.json` |
| Browser | `scripts/smoke_browser.py` | `docs/evidence/browser-v3.0.1.json` |
| Automation | `scripts/smoke_automation.py` | `docs/evidence/automation-v3.0.1.json` |
| Skills | `scripts/smoke_skills.py` | `docs/evidence/skills-v3.0.1.json` |
| Skills UI | `scripts/smoke_skills_ui.py` | `docs/evidence/skills-ui-v3.0.1.json` |
| Skill builder | `scripts/smoke_skill_builder.py` | `docs/evidence/skill-builder-v3.0.1.json` |
| Skill packs | `scripts/smoke_skill_packs.py` | `docs/evidence/skill-packs-v3.0.1.json` |
| Skill eval dashboard | `scripts/smoke_skill_eval_dashboard.py` | `docs/evidence/skill-eval-dashboard-v3.0.1.json` |
| Skill versioning | `scripts/smoke_skill_versioning.py` | `docs/evidence/skill-versioning-v3.0.1.json` |
| Skill analytics | `scripts/smoke_skill_analytics.py` | `docs/evidence/skill-analytics-v3.0.1.json` |
| Skill security | `scripts/smoke_skill_security.py` | `docs/evidence/skill-security-v3.0.1.json` |
| Skill catalog | `scripts/smoke_skill_catalog.py` | `docs/evidence/skill-catalog-v3.0.1.json` |
| Context taint | `scripts/smoke_context_taint.py` | `docs/evidence/context-taint-v3.0.1.json` |

The release preflight also runs:

```bash
python scripts/preflight_release.py --version 3.0.1 --ga
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

## Sign-off

Before tagging a 3.1.x release, confirm:

- [ ] All CI gates above are green on the release commit.
- [ ] All offline eval gates above pass with `--strict`.
- [ ] Runtime gates 1–6 above have been executed and recorded.
- [ ] The hybrid runtime runbook is up to date: [RUST_HYBRID_RUNTIME_RUNBOOK.md](RUST_HYBRID_RUNTIME_RUNBOOK.md).
- [ ] `docs/IMPLEMENTATION_STATUS.md` and `docs/RUST_MIGRATION_ROADMAP.md` reflect the 3.1.x state.
- [ ] `CHANGELOG.md` has a release entry for the new 3.1.x version.
- [ ] No Docker, coverage gate, or 4.0.0 breaking changes are included in the release.

---

## Related documents

- [Hybrid Rust Runtime Runbook](RUST_HYBRID_RUNTIME_RUNBOOK.md)
- [Rust Migration Roadmap](RUST_MIGRATION_ROADMAP.md)
- [Implementation Status](IMPLEMENTATION_STATUS.md)
- [CHANGELOG.md](../CHANGELOG.md)
