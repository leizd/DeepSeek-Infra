# AGENTS.md

Repo-specific guidance for AI coding agents working in DeepSeek Infra.
Executable config (CI, `pyproject.toml`, requirements) is the source of truth; this file only captures what isn't obvious from those.

## Layout

- `app.py` and `launch.py` are thin shims. Real entry points:
  - HTTP server: `deepseek_infra/app.py:main` → `deepseek_infra/web/server.py:create_server`
  - `launch.py` flags: `--gui` (Tk launcher), `--mobile` (mobile launcher), `--server` (headless), `--app` (desktop WebView, default)
- All backend code is the single package `deepseek_infra/`. The 9 infra modules live under `deepseek_infra/infra/` (`gateway`, `agent_runtime`, `rag`, `tool_runtime`, `observability`, `mcp`, `evaluation`, `data`).
- Frontend migration is past the dual-track phase: `/` serves the React + TypeScript + Vite build from `frontend/` by default (generated assets live in the gitignored `static/ui/`; build with `npm run build --prefix frontend`). The legacy vanilla JS workspace remains reachable at `/legacy`, and `DEEPSEEK_FRONTEND=legacy` restores it as the default. `static/` legacy files are still present pending the scheduled retirement commit; never hand-edit `static/ui/` output.
- `android/` is an Android Studio project wrapping the Python backend into an APK; `scripts/build_exe.py` builds a single-file PyInstaller exe.

## Dev verification (run in this order — matches CI)

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
npm ci --prefix frontend
npm run check --prefix frontend
ruff check .
mypy .
pytest --cov --cov-fail-under=95
# Legacy JS syntax (only these files are checked):
node --check static/vendor/katex/katex.min.js static/math_core.js static/seek_core.js static/app.js \
      static/modules/network.js static/modules/markdown.js static/modules/settings.js static/modules/panels.js \
      static/modules/chat.js static/modules/trace_waterfall.js static/modules/trace_viewer.js
```

- Python 3.10+ (CI matrix: 3.10 / 3.11 / 3.12). `mypy` targets `python_version="3.10"`.
- Node 22.12+ is required for the Vite frontend; CI uses Node 24 and the committed `frontend/package-lock.json`.
- No API key or network needed for tests or evals — everything is offline.
- Single test: `pytest tests/test_mcp.py::test_name`. Run fast subset: `pytest -m "not integration and not slow"`.

### Tooling quirks

- **`ruff` config is intentionally minimal**: `line-length=140`, rules `E4,E7,E9,F` only (in `pyproject.toml`). Don't assume broader lint rules are enforced; don't add style rules without checking.
- **`mypy .`** runs on the whole repo; `ignore_missing_imports=true` is set, so third-party stub misses are not errors. `warn_unused_ignores=true` — don't leave stale `# type: ignore`.
- **Coverage gate is 95%** (raised from 90% in v3.3.2), `source = ["deepseek_infra"]`, with branch measurement enabled. `--cov-fail-under=95` fails the run; branch coverage is reported but has no separate threshold yet. Lower locally with `pytest --no-cov` when iterating.
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

These repo-root dirs are gitignored runtime state — do not stage, package, or assume they exist on a fresh clone:
`.file-cache .projects .local-rag .traces .semantic-cache .request-queue .generated .tool-audit .scheduler .a2a .budget .memory .reminders .agent-runs .search-cache .auth-token`
- For a clean distributable archive use `python scripts/release.py --clean-workspace` (emits `dist/deepseek-infra-<version>.zip`).
- `.env` holds secrets and is gitignored; only `.env.example` is tracked.
