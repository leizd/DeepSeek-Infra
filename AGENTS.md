# AGENTS.md

Repo-specific guidance for AI coding agents working in DeepSeek Infra.
Executable config (CI, `pyproject.toml`, requirements) is the source of truth; this file only captures what isn't obvious from those.

## Layout

- `app.py` and `launch.py` are thin shims. Real entry points:
  - HTTP server: `deepseek_infra/app.py:main` → `deepseek_infra/web/server.py:create_server`
  - `launch.py` flags: `--gui` (Tk launcher), `--mobile` (mobile launcher), `--server` (headless), `--app` (desktop WebView, default)
- All backend code is the single package `deepseek_infra/`. The 9 infra modules live under `deepseek_infra/infra/` (`gateway`, `agent_runtime`, `rag`, `tool_runtime`, `observability`, `mcp`, `evaluation`, `data`).
- Frontend is hand-written vanilla JS in `static/` — **no bundler, no build step, no `package.json`**. CI only syntax-checks specific JS files with `node --check` (see below). Do not introduce a JS build pipeline.
- `android/` is an Android Studio project wrapping the Python backend into an APK; `scripts/build_exe.py` builds a single-file PyInstaller exe.

## Dev verification (run in this order — matches CI)

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
ruff check .
mypy .
pytest --cov --cov-fail-under=75
# JS syntax (only these files are checked):
node --check static/vendor/katex/katex.min.js static/math_core.js static/seek_core.js static/app.js \
      static/modules/network.js static/modules/markdown.js static/modules/settings.js static/modules/panels.js \
      static/modules/chat.js static/modules/trace_waterfall.js static/modules/trace_viewer.js
```

- Python 3.10+ (CI matrix: 3.10 / 3.11 / 3.12). `mypy` targets `python_version="3.10"`.
- No API key or network needed for tests or evals — everything is offline.
- Single test: `pytest tests/test_mcp.py::test_name`. Run fast subset: `pytest -m "not integration and not slow"`.

### Tooling quirks

- **`ruff` config is intentionally minimal**: `line-length=140`, rules `E4,E7,E9,F` only (in `pyproject.toml`). Don't assume broader lint rules are enforced; don't add style rules without checking.
- **`mypy .`** runs on the whole repo; `ignore_missing_imports=true` is set, so third-party stub misses are not errors. `warn_unused_ignores=true` — don't leave stale `# type: ignore`.
- **Coverage gate is 75%** (raised from 70% in v2.2.6), `source = ["deepseek_infra"]`. `--cov-fail-under=75` fails the run; lower locally with `pytest --no-cov` when iterating.
- **`pytest` uses `--strict-markers`** (from `pyproject.toml`). Registered markers: `integration` (spins up a real HTTP server on an ephemeral `127.0.0.1` port) and `slow` (>1s). Both run in CI's default `pytest` invocation.

### Offline eval gates (no API key)

```bash
PYTHONHASHSEED=0 python evals/runners/run_rag_eval.py   # hash seed is REQUIRED for reproducible BM25 ties
python evals/runners/run_tool_eval.py                    # exits 1 on any policy misjudgment — hard CI gate
python evals/runners/run_injection_adversarial.py --no-report  # v2.2.6: soft gate (warns on unmet thresholds, exits 0; --strict exits 1)
```
- Scoring core is the pure, I/O-free `deepseek_infra/infra/evaluation/harness.py` (unit-tested in `tests/test_eval_harness.py`). Runners only orchestrate.
- `run_agent_eval.py` is offline but **not** a CI gate yet.
- **Injection soft gate (v2.2.6)**: `run_injection_adversarial.py` enforces versioned thresholds (`blockRate>=0.85`, `falsePositiveRate<=0.10`, `bypassRate<=0.15`) as a *soft* gate — unmet thresholds print `SOFT GATE: WARNING` but still exit 0 so CI keeps flowing. `--strict` turns it into a hard failure (the v2.3 graduation path). `run_tool_eval.py` remains the hard gate (exits 1 on any policy misjudgment).

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
