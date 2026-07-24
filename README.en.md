# DeepSeek Infra

<!-- docs-language-switcher:start -->
[中文](README.md) / [English](README.en.md)
<!-- docs-language-switcher:end -->


![Version](https://img.shields.io/badge/version-4.3.3-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-green)
![Coverage Gate](https://img.shields.io/badge/coverage%20gate-95%25-brightgreen)
![License](https://img.shields.io/badge/license-MIT-black)

DeepSeek Infra is a local-first Agentic AI infrastructure platform that combines an LLM gateway, persistent Agent DAG runtime, MCP-native tool hub, A2A-style agent mesh, local RAG, automation, workspace data, and end-to-end observability in one private runtime.

## 4.3.3 at a glance

- The client discovers deployments through the stable no-store Workspace pointer on startup, visibility and online recovery, a visible five-minute interval, and manual checks.
- Upgrade workers install and verify their Core Cache but remain waiting. Exact `buildId` plus `assetSetDigest` consent is required before activation; first installation remains automatic.
- Reload waits for chat/Agent output, all React Query writes, uploads, unsent attachments, quotes, Composer drafts, and Workspace form drafts to become quiescent.
- Composer drafts are isolated per conversation in `sessionStorage`, and conversation plus draft persistence is flushed synchronously before activation.
- Reload occurs only after the new controller confirms the target identity, digest, and cache readiness. A superseded B target cannot overwrite or activate after C is discovered.
- Tabs coordinate availability and activation through `BroadcastChannel`, but only the initiating tab reloads itself.
- `index.html` and the stable pointer are no-store; build-scoped manifests, workers, and hashed assets are one-year immutable.
- Existing 4.3.2 immutable identity and Client Build Leases, 4.3.1 continuity behavior, and 4.3.0 bundle budgets remain unchanged.
- The 4.2.8 exact-merge Evidence assembly remains the release-trust foundation.
- Python remains the default and authoritative runtime.
- Every Rust delegate is opt-in and protected by Python fallback.
- DeepSeek and Tavily credentials stay in memory in the React application.

See the [4.3.3 release notes](docs/releases/4.3.3.md), [Evidence index](docs/EVIDENCE_INDEX.md), [frontend boundaries](docs/FRONTEND_MODULES.md), and [support policy](docs/4_0_SUPPORT_POLICY.md).

## Architecture

<details>
<summary><strong>中文架构图</strong></summary>

![DeepSeek Infra Chinese architecture](docs/assets/architecture.zh-CN.svg)

</details>

<details open>
<summary><strong>English architecture</strong></summary>

![DeepSeek Infra architecture overview](docs/assets/architecture.svg)

</details>

## Quick start

```bash
python -m pip install -r requirements.txt
cp .env.example .env
python launch.py --server
```

Open `http://127.0.0.1:8000/` for the stable workspace or `http://127.0.0.1:8000/ui/` for the React chat slice.

Docker:

```bash
cp .env.example .env
docker compose up -d
curl http://127.0.0.1:8000/healthz
```

## Documentation

The language switcher at the top of every human-maintained Markdown document returns to either the Chinese or English documentation entry. Deep technical documents remain canonical even when a complete line-by-line translation is not yet available.

- [Standalone roadmap](ROADMAP.en.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Getting started](docs/GETTING_STARTED.md)
- [API reference](docs/API.md)
- [Deployment](docs/DEPLOYMENT.md)
- [Security](docs/SECURITY.md)
- [Threat model](docs/THREAT_MODEL.md)
- [Implementation status](docs/IMPLEMENTATION_STATUS.md)
- [Evidence index](docs/EVIDENCE_INDEX.md)
- [Release checklist](docs/RELEASE_CHECKLIST.md)
- [Changelog](CHANGELOG.md)

## Validation

```bash
npm ci --prefix frontend
npm run check --prefix frontend
ruff check .
mypy .
pytest --cov --cov-fail-under=95
python scripts/preflight_release.py --version 4.3.3 --ga
```

Except for requests explicitly sent to configured providers such as DeepSeek or Tavily, project data remains local by default.
