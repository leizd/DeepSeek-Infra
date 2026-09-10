# AGENTS.md

Repo-specific guidance for AI coding agents working in DeepSeek Infra.
Executable config (CI, `pyproject.toml`, requirements) is the source of truth; this file only captures what isn't obvious from those.

## Layout

- `app.py` and `launch.py` are thin shims. Real entry points:
  - HTTP server: `deepseek_infra/app.py:main` → `deepseek_infra/web/server.py:create_server`
  - `launch.py` flags: `--gui` (Tk launcher), `--mobile` (mobile launcher), `--server` (headless), `--app` (desktop WebView, default)
- All backend code is the single package `deepseek_infra/`. Supporting layers are `core/` (`config`, `errors`, `utils`), `web/` (`server.py:create_app` plus `routes/`), `launcher/`, and `desktop_app.py` / `android_entry.py` / `federation_app.py`. The **17** capability subpackages live under `deepseek_infra/infra/`: `gateway`, `agent_runtime`, `rag`, `tool_runtime`, `observability`, `mcp`, `evaluation`, `data`, `workspace`, `automation`, `browser`, `media`, `memory`, `skills`, `diagnostics`, `native_runtime`, `rust_core`.
- **Python is the default and only production-authoritative runtime.** Three non-authoritative surfaces exist; do not assume any of them is wired in or owns production state:
  - `rust/` — cargo workspace (edition 2024, rust 1.85). Only `backup-crypto` (streaming age) and `deepseek-backup` (FastCDC scan) are built as production helpers; the rest (`deepseek-gateway`, `-mcp`, `-policy`, `-rag`, plus the 4.9.x targets `-storage`, `-transfer`, `-federation`, `-proof`, `-worker`) are opt-in delegates or migration foundations.
  - `go/` — module `github.com/leizd/DeepSeek-Infra/go` (Go 1.27). `cmd/deepseekd` is the control-plane runtime and currently runs in read-only **shadow** mode; `go/internal/shadow` reconciles `pythonDecisionDigest == goDecisionDigest`.
  - `stateless-mcp/` — standalone TypeScript + Redis MCP task plane on its own Compose stack; it does not replace the Python `POST /mcp` hub.
- Native **ownership rules are machine-enforced, not conventions**. `deepseek_infra/infra/native_runtime/authority.py` defines `GO_CONTROL_DOMAINS`; a Python write into a Go-owned domain raises `PythonWriterMechanicallyDeniedError`, and Rust worker mutations fail closed with `MUTATION_DENIED` (`release/native_runtime_command_codes_v1.json`).
- Cross-process contracts are versioned Protobuf in `proto/{common,control,action,storage,federation,evidence,agent}/v1`; versioned replay/parity corpora live in `compat/native-runtime/v1` … `v30`. The toolchain is checksum-pinned — read `release/native_runtime_toolchain_v1.json` before changing Go/protoc versions.
- `release/native_runtime_5_0_evidence_v1.json` is a **fail-closed readiness assessment, currently `NOT_READY`**. It is not proof that the 5.0 native topology is delivered; do not cite it as a pass.
- `/` serves the React + TypeScript + Vite build from `frontend/` exclusively. Generated assets are emitted into the gitignored `static/ui/`; build with `npm run build --prefix frontend` and never hand-edit that output. Startup and packaging require `static/ui/index.html`.
- `android/` is an Android Studio project wrapping the Python backend into an APK; `scripts/build_exe.py` builds a single-file PyInstaller exe.

## Dev verification (run in this order — matches CI)

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
npm ci --prefix frontend
npm run check --prefix frontend   # = typecheck + vitest + vite build + bundle check
ruff check .
mypy .
pytest --cov --cov-fail-under=95.0
# Retained vendor JavaScript syntax:
node --check static/vendor/katex/katex.min.js
```

- Python 3.10+ (CI matrix: 3.10 / 3.11 / 3.12). `mypy` targets `python_version="3.10"`.
- Node 22.12+ is required for the Vite frontend; CI uses Node 24 and the committed `frontend/package-lock.json`.
- No API key is needed. Evals and the fast non-integration subset are offline; the complete `pytest` gate builds the native backup helpers and provisions **five** real MinIO instances from a local binary or the pinned Docker image. Endpoints are named `DEEPSEEK_TEST_S3_ENDPOINT_{A..E}`; suffixes `D`/`E` form the second fleet and use distinct `FEDERATION_MINIO_ROOT_USER` / `FEDERATION_MINIO_ROOT_PASSWORD` credentials (the fixture asserts source and receiver credentials differ). See `tests/real_storage_environment.py`.
- Single test: `pytest tests/test_mcp.py::test_name`. Run fast subset: `pytest -m "not integration and not slow"`.
- `VERSION` at repo root is the canonical release version; `python scripts/check_release_version.py` enforces cross-surface consistency (CI gate job `release-version`).
- Native changes have dedicated CI lanes that a Python-only edit does not exercise — run the matching one locally before touching `rust/`, `go/`, `proto/`, or `compat/native-runtime/`: `rust`, `rust-coverage`, `rust-docker`, `native-protocol` (generated-code drift + `scripts/control_plane_shadow.py --check`), `native-go` (`gofmt` / `go vet` / `go test` / `-race` + Go coverage ≥ 95% + a real Go→Rust worker boundary run), and `native-s3-transport`.

### Tooling quirks

- **`ruff` config is intentionally minimal**: `line-length=140`, rules `E4,E7,E9,F` only (in `pyproject.toml`). Don't assume broader lint rules are enforced; don't add style rules without checking.
- **`mypy .`** runs on the whole repo; `ignore_missing_imports=true` is set, so third-party stub misses are not errors. `warn_unused_ignores=true` — don't leave stale `# type: ignore`.
- **Coverage gate is 95.0%** (raised from 90% in v3.3.2; restored in 4.6.2 after a temporary 4.6.1 94.9 floor), `source = ["deepseek_infra"]`, with branch measurement enabled. `--cov-fail-under=95.0` fails the run; branch coverage is reported but has no separate threshold yet. Lower locally with `pytest --no-cov` when iterating.
- **`pytest` uses `--strict-markers`** (from `pyproject.toml`). Registered markers: `integration` (spins up a real HTTP server on an ephemeral `127.0.0.1` port) and `slow` (>1s). Both run in CI's default `pytest` invocation.

### Offline eval gates (no API key)

```bash
PYTHONHASHSEED=0 python evals/runners/run_rag_eval.py   # hash seed is REQUIRED for reproducible BM25 ties
python evals/runners/run_tool_eval.py                    # exits 1 on any policy misjudgment — hard CI gate
python evals/runners/run_injection_adversarial.py --strict --no-report  # v2.3.0: hard CI gate (exits 1 on unmet thresholds)
python evals/runners/run_security_corpus.py --strict      # v2.4.0: versioned security corpus hard CI gate
python evals/runners/run_agent_eval.py --strict           # v2.4.0: Agent Eval hard CI gate
python evals/runners/compare_eval_baseline.py --strict --baseline evals/baselines/v2.2.6.json --current evals/reports/latest.json --agent-baseline evals/baselines/agent-v2.2.8.json
```
- Scoring core is the pure, I/O-free `deepseek_infra/infra/evaluation/harness.py` (unit-tested in `tests/test_eval_harness.py`). Runners only orchestrate.
- **Injection / Agent / security hard gates (v2.4.0)**: `run_injection_adversarial.py --strict` enforces versioned thresholds (`blockRate>=0.85`, `falsePositiveRate<=0.10`, `bypassRate<=0.15`); `run_agent_eval.py --strict` enforces Tool Call Accuracy >= 0.90, Agent Success Rate >= 0.85 and Prompt Regression Pass Rate >= 0.90; `run_security_corpus.py --strict` enforces versioned attack / benign corpus metrics; `compare_eval_baseline.py --strict` blocks RAG / Tool / Injection / Agent regressions. Without `--strict`, compatible runners still warn for local iteration.

### Security scan (CI `security` job)

```bash
npm audit --prefix frontend --audit-level=high --json > artifacts/npm-audit.json
python scripts/check_npm_audit.py artifacts/npm-audit.json  # high/critical gate; documented GHSA exceptions only
pip-audit -r requirements.txt -r requirements-dev.txt
bandit -r deepseek_infra --severity-level high -q          # only HIGH; medium is reviewed (docs/THREAT_MODEL.md)
detect-secrets scan --baseline .secrets.baseline           # ALWAYS pass --baseline; test fixtures contain deliberate fake keys
```

## Test-writing gotchas

- The `tmp_settings` fixture in `tests/conftest.py` is **the** mechanism for isolating local state: it monkeypatches module-level path constants (`config`, `files`, `memory`, `local_rag`, `observability`, `scheduler`, `a2a`, `tools`, …) onto a `tmp_path`. Use it; do not let tests touch the real repo-root dot-dirs. Because paths are module attributes, a new module reading a data dir must also be patched in `conftest.py` or tests will write to real locations.
- `fake_deepseek` / `mock_urlopen` fixtures stub the upstream DeepSeek API and `urllib` — prefer these over hitting the network.

## Dependency gotcha

- The multipart parser dependency is **`multipart`** (`>=1.3,<2`), **not** `python-multipart`. If both are installed, uploads break with an explicit error — reinstall per `requirements.txt`.

## Runtime data dirs (never commit)

These repo-root paths are gitignored runtime state — do not stage, package, or assume they exist on a fresh clone. `.gitignore` is the authoritative list; this is a grouped summary:

- **Chat / retrieval:** `.file-cache/` `.projects/` `.local-rag/` `.memory/` `.search-cache/` `.media/` `.skills/` `.reminders/`
- **Gateway / runtime / output:** `.traces/` `.semantic-cache/` `.request-queue/` `.budget/` `.tool-audit/` `.scheduler/` `.agent-runs/` `.a2a/` `.tools/` `.automation/` `.generated/`
- **Browser:** `.browser-audit/` `.browser-downloads/` `.browser-profiles/`
- **Backup / DR:** `.backups/` `.restore-staging/` `.backup-policies/` `.backup-mirror/` `.backup-scheduler/` `.backup-targets/` `.backup-catalog/` `.backup-retention/` `.backup-spool/` `.backup-run-plans/` `.backup-index/` `.backup-continuity/` `.backup-rebalance/` `.backup-component-cache/` `.backup-dr/` `.backup-drains/` `.backup-retirements/` `.backup-control/` `.backup-authority-retention/` `.backup-replication/`
- **Resilience / federation:** `.resilience-risk/` `.resilience-scheduler/` `.resilience-slo/` `.resilience-waves/` `.resilience-capacity/` `.resilience-cost/` `.resilience-optimizer/` `.federation/`
- **Locks / secrets / state files:** `.auth-token` `.launcher-config.json` (and `.tmp`) `.workspace-mutation.lock` `.workspace-generation`
- **Native build output:** `rust/target/`, `go/**/*.exe`, `/go/bin/`, `go/*.out`, `go/coverage*`, `go/cov-*`, `/bin/`
- For a clean distributable archive use `python scripts/release.py --clean-workspace` (emits `dist/deepseek-infra-<version>.zip`).
- `.env` holds secrets and is gitignored; only `.env.example` is tracked.

## Windows 约束

当前环境是 Windows 11 / pwsh 7

- 默认禁止使用 Bash 语法，除非确定此 shell 处在 Linux 环境
- 不要使用 Bash 引号/转义习惯，在 PowerShell 命令里，复杂正则优先用单引号包裹。
- 如果正则本身同时包含单引号和双引号，优先拆成多个简单 rg 命令。
- 执行多行 Python 禁止使用 Bash heredoc；改用 PowerShell here-string 通过管道传给 `python -`。
- pwsh 中，语句块表达式（如 `foreach`、`if`）不能直接作为管道输入。需要先使用 `$()` / `@()` 包裹，或先赋值给变量。普通命令输出可直接进入管道，无需额外包裹。
- PowerShell 使用 `rg` 时，通配目录必须先用 `Get-ChildItem -Filter` 展开为真实路径，禁止直接把含 `*` 的搜索路径传给 `rg`。
