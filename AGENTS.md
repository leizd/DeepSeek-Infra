# AGENTS.md

Repo-specific guidance for AI coding agents working in DeepSeek Infra.
Executable config (CI, `pyproject.toml`, requirements) is the source of truth; this file captures architectural invariants, native boundaries, and gotchas that aren't obvious from those.

## Layout

- `app.py` and `launch.py` are thin shims. Real entry points:
  - HTTP server: `deepseek_infra/app.py:main` → `deepseek_infra/web/server.py:create_server`
  - `launch.py` flags: `--gui` (Tk launcher), `--mobile` (mobile launcher), `--server` (headless), `--app` (desktop WebView, default)
- All backend code is the single package `deepseek_infra/`. Supporting layers are `core/` (`config`, `errors`, `utils`), `web/` (`server.py:create_app` plus `routes/`), `launcher/`, and `desktop_app.py` / `android_entry.py` / `federation_app.py`. The **17** capability subpackages live under `deepseek_infra/infra/`: `gateway`, `agent_runtime`, `rag`, `tool_runtime`, `observability`, `mcp`, `evaluation`, `data`, `workspace`, `automation`, `browser`, `media`, `memory`, `skills`, `diagnostics`, `native_runtime`, `rust_core`.
- **Python is the default and only production-authoritative runtime.** Three non-authoritative surfaces exist; do not assume any of them is wired in or owns production state:
  - `rust/` — cargo workspace (edition 2024, rust 1.85, **14 crates**):
    - *Production helpers* (built and active in production): `backup-crypto` (streaming age cryptography) and `deepseek-backup` (FastCDC v3 scan).
    - *Worker execution plane*: `deepseek-worker` (loopback gRPC service on `:50052`, enforces `actionId + executionEpoch` admission fencing).
    - *Storage & transfer plane*: `deepseek-storage` (direct S3/MinIO streaming, Receipt/Commit v4), `deepseek-transfer` (zero-copy pipelines, repair/rebalance).
    - *Federation & proof plane*: `deepseek-federation` (Ed25519 identity, private keys stay in Rust custody), `deepseek-proof` (cryptographic proof verification).
    - *Edge, protocol & sidecar delegates*: `deepseek-gateway` (Axum HTTP/SSE gateway, reverse-proxies `/api/*` to Go), `deepseek-mcp` (JSON-RPC), `deepseek-policy` (path/SSRF/capability checks), `deepseek-rag` (BM25 + vector ranking & compact binary `f64le`), `deepseek-browser` (WebSocket CDP browser engine), `deepseek-protocol` (Protobuf codegen), `deepseek-core`.
  - `go/` — module `github.com/leizd/DeepSeek-Infra/go` (Go 1.27, `CGO_ENABLED=0`). `cmd/deepseekd` is the control-plane runtime and currently runs in read-only **shadow** mode on `:8090`; `go/internal/shadow` reconciles `pythonDecisionDigest == goDecisionDigest`.
  - `stateless-mcp/` — standalone TypeScript + Redis MCP task plane on its own Compose stack; it does not replace the Python `POST /mcp` hub.
- `/` serves the React + TypeScript + Vite build from `frontend/` exclusively. Generated assets are emitted into the gitignored `static/ui/`; build with `npm run build --prefix frontend` and never hand-edit that output. Startup and packaging require `static/ui/index.html`.
- `android/` is an Android Studio project wrapping the Python backend into an APK; `scripts/build_exe.py` builds a single-file PyInstaller exe.

## Key Specifications & Machine Contracts

Before proposing or modifying cross-language architecture, read the corresponding machine contract:

| Domain | Authoritative Spec / Contract | Purpose |
| :--- | :--- | :--- |
| **Native Ownership Matrix** | `release/native_runtime_ownership_v1.json` | Machine-enforced ownership, domains, and staged cutover schedule |
| **5.0 Native Readiness Gate** | `release/native_runtime_5_0_evidence_v1.json` | Fail-closed readiness assessment (currently `NOT_READY`) |
| **Ownership Inversion Spec** | `docs/specs/5.0-native-rust-go-runtime.md` · `docs/adr/ADR-0049-native-runtime-ownership-inversion.md` | Target native Rust data plane & Go control plane design |
| **Signed Federation & DR** | `docs/adr/ADR-0048-signed-federation-cross-fleet-dr.md` · `docs/runbooks/SIGNED_FEDERATION_DR.md` | Mutual Ed25519 trust, receiver-controlled ingress, offsite custody |
| **Cross-Process Contracts** | `proto/{common,control,action,storage,federation,evidence,agent,browser}/v1` | Versioned Protobuf/gRPC message definitions |
| **Toolchain Pins** | `release/native_runtime_toolchain_v1.json` · `release/native_runtime_command_codes_v1.json` | Checksum-pinned protoc, Go plugins, and error codes |

## Architectural Invariants & Forbidden Practices

These rules are machine-enforced and fail-closed:

1. **One Table, One Authoritative Writer (`one_table_one_authoritative_writer`)**:
   - Python alone writes `.dot-dirs` (`.local-rag/`, `.memory/`, `.projects/`, `.agent-runs/`, `.backups/`, `.traces/`, etc.).
   - Go alone writes `go-control/` (SQLite control database).
   - Rust alone writes data plane effect journals and transfer checkpoints.
   - **Never share SQLite writes** between Python, Go, and Rust.
2. **Mechanical Writer Denial**:
   - `deepseek_infra/infra/native_runtime/authority.py` defines `GO_CONTROL_DOMAINS`. A Python write into a Go-owned domain raises `PythonWriterMechanicallyDeniedError`.
   - Go shadow mode hardcodes `/internal/action/execute` to return HTTP 403 `ErrMutationDenied`.
   - Rust worker mutations fail closed with `MUTATION_DENIED` without valid authority.
3. **`actionId + executionEpoch` Bound End-to-End**:
   - All state mutations require valid action ID and execution epoch.
   - Stale epochs return `STALE_EXECUTION_EPOCH`; mismatched fences return `FENCE_MISMATCH`.
4. **Unknown Remote Effects Fail Closed**:
   - An unconfirmed or lost remote effect settles as `EFFECT_UNKNOWN`. It is never treated as unapplied, and retry must never generate duplicate side effects.
5. **Zero Cgo / No Ad-hoc JSON Bridges**:
   - New Go/Rust boundaries must use versioned Protobuf messages over gRPC (`proto/*/v1`). Do not introduce ad-hoc JSON-RPC or cgo bindings.
6. **Private Key Isolation**:
   - Federation Ed25519 private keys stay in Rust custody only; private keys must never enter the Go heap or wire protocol.

## Dev Verification & Quality Gates

### Fast Inner Loop (Daily development, <15s, offline, no MinIO required)

Use this fast path for normal Python-side iterations:

```bash
ruff check .
mypy .
pytest -m "not integration and not slow" -p no:cacheprovider
# Run a single test file:
pytest tests/test_mcp.py -v -p no:cacheprovider
```

### Native Changes Local Verification

Run the matching local command before touching native directories:

```bash
# Rust workspace lint & tests (when touching rust/):
cargo check --workspace --manifest-path rust/Cargo.toml
cargo test --workspace --manifest-path rust/Cargo.toml

# Go control-plane lint, race & tests (when touching go/):
cd go && go test -race ./... && cd ..

# Protocol drift & shadow decision parity (when touching proto/ or compat/):
python scripts/control_plane_shadow.py --check

# Release version consistency across surfaces:
python scripts/check_release_version.py

# Frontend typecheck & bundle (ONLY when touching frontend/):
npm run check --prefix frontend   # = typecheck + vitest + vite build + bundle check
```

### Full Release Gate (Preflight & CI, provisions 5 MinIO instances, >=95.0% coverage)

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
npm ci --prefix frontend
npm run check --prefix frontend
ruff check .
mypy .
pytest --cov --cov-fail-under=95.0 -p no:cacheprovider
node --check static/vendor/katex/katex.min.js
```

- Python 3.10+ (CI matrix: 3.10 / 3.11 / 3.12). `mypy` targets `python_version="3.10"`.
- Node 22.12+ is required for the Vite frontend; CI uses Node 24 and the committed `frontend/package-lock.json`.
- The complete `pytest` gate provisions **five** real MinIO instances from a local binary or the pinned Docker image (`DEEPSEEK_TEST_S3_ENDPOINT_{A..E}`; suffixes `D`/`E` use distinct `FEDERATION_MINIO_ROOT_USER`/`PASSWORD` credentials). See `tests/real_storage_environment.py`.
- `VERSION` at repo root is the canonical release version; `python scripts/check_release_version.py` enforces consistency (CI gate `release-version`).

### Tooling Quirks

- **`ruff` config is intentionally minimal**: `line-length=140`, rules `E4,E7,E9,F` only (in `pyproject.toml`). Don't assume broader lint rules are enforced; don't add style rules without checking.
- **`mypy .`** runs on the whole repo; `ignore_missing_imports=true` is set, so third-party stub misses are not errors. `warn_unused_ignores=true` — don't leave stale `# type: ignore`.
- **Coverage gate is 95.0%**, `source = ["deepseek_infra"]`, with branch measurement enabled. `--cov-fail-under=95.0` fails the run. Lower locally with `pytest --no-cov` when iterating.
- **`pytest` uses `--strict-markers`** (from `pyproject.toml`). Registered markers: `integration` (spins up a real HTTP server on an ephemeral `127.0.0.1` port) and `slow` (>1s).
- **Windows pytest cache warning**: On Windows, pytest may report `PytestCacheWarning: could not create cache path ... [WinError 5]`. Pass `-p no:cacheprovider` to suppress it; the exit code remains 0.

### Offline Eval Gates (no API key)

```bash
PYTHONHASHSEED=0 python evals/runners/run_rag_eval.py   # hash seed is REQUIRED for reproducible BM25 ties
python evals/runners/run_tool_eval.py                    # exits 1 on any policy misjudgment — hard CI gate
python evals/runners/run_injection_adversarial.py --strict --no-report  # hard CI gate (exits 1 on unmet thresholds)
python evals/runners/run_security_corpus.py --strict      # versioned security corpus hard CI gate
python evals/runners/run_agent_eval.py --strict           # Agent Eval hard CI gate
python evals/runners/compare_eval_baseline.py --strict --baseline evals/baselines/v2.2.6.json --current evals/reports/latest.json --agent-baseline evals/baselines/agent-v2.2.8.json
```
- Scoring core is the pure, I/O-free `deepseek_infra/infra/evaluation/harness.py`. Runners only orchestrate.
- **Thresholds**: `blockRate>=0.85`, `falsePositiveRate<=0.10`, `bypassRate<=0.15`; Agent Eval: Tool Call Accuracy >= 0.90, Agent Success Rate >= 0.85, Prompt Regression Pass Rate >= 0.90.

### Security Scan (CI `security` job)

```bash
npm audit --prefix frontend --audit-level=high --json > artifacts/npm-audit.json
python scripts/check_npm_audit.py artifacts/npm-audit.json  # high/critical gate; documented GHSA exceptions only
pip-audit -r requirements.txt -r requirements-dev.txt
bandit -r deepseek_infra --severity-level high -q          # only HIGH; medium is reviewed (docs/THREAT_MODEL.md)
detect-secrets scan --baseline .secrets.baseline           # ALWAYS pass --baseline; test fixtures contain deliberate fake keys
```

## Test-Writing Gotchas

- The `tmp_settings` fixture in `tests/conftest.py` is **the** mechanism for isolating local state: it monkeypatches module-level path constants (`config`, `files`, `memory`, `local_rag`, `observability`, `scheduler`, `a2a`, `tools`, …) onto a `tmp_path`. Use it; do not let tests touch the real repo-root dot-dirs. Because paths are module attributes, a new module reading a data dir must also be patched in `conftest.py` or tests will write to real locations.
- `fake_deepseek` / `mock_urlopen` fixtures stub the upstream DeepSeek API and `urllib` — prefer these over hitting the network.

## Dependency Gotcha

- The multipart parser dependency is **`multipart`** (`>=1.3,<2`), **not** `python-multipart`. If both are installed, uploads break with an explicit error — reinstall per `requirements.txt`.

## Runtime Data Dirs (never commit)

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

- 默认禁止使用 Bash 语法，除非确定此 shell 处在 Linux 环境。
- 跨语言文件路径在命令行与代码中**一律使用正斜杠 `/`**（pwsh、Python 与 Node 均原生支持）；避免在 PowerShell 字符串中使用 `\` 导致转义分歧。
- 不要使用 Bash 引号/转义习惯，在 PowerShell 命令里，复杂正则优先用单引号包裹。
- 如果正则本身同时包含单引号和双引号，优先拆成多个简单 `rg` 命令。
- 执行多行 Python 禁止使用 Bash heredoc；改用 PowerShell here-string 通过管道传给 `python -`。
- pwsh 中，语句块表达式（如 `foreach`、`if`）不能直接作为管道输入。需要先使用 `$()` / `@()` 包裹，或先赋值给变量。普通命令输出可直接进入管道，无需额外包裹。
- PowerShell 使用 `rg` 时，通配目录必须先用 `Get-ChildItem -Filter` 展开为真实路径，禁止直接把含 `*` 的搜索路径传给 `rg`。
- **后台异步任务与轮询禁令**：长时间执行命令（如构建、测试、`git push`）进入后台 Task 后，**严禁使用循环频繁轮询 `manage_task(status)`**，必须依靠系统的异步事件通知被动唤醒或使用 `schedule`。
- **Git 换行符行为**：修改 Markdown 或 SVG 时出现的 `warning: LF will be replaced by CRLF` 是 Windows Git 的常规提示，无需特意重写换行符。
