# DeepSeek Infra

<!-- docs-language-switcher:start -->
[中文](README.md) / [English](README.en.md)
<!-- docs-language-switcher:end -->


![Version](https://img.shields.io/badge/version-4.3.7-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-green)
![Coverage Gate](https://img.shields.io/badge/coverage%20gate-95%25-brightgreen)
![License](https://img.shields.io/badge/license-MIT-black)

DeepSeek Infra is a local-first Agentic AI infrastructure platform that combines an LLM gateway, persistent Agent DAG runtime, MCP-native tool hub, A2A-style agent mesh, local RAG, automation, workspace data, and end-to-end observability in one private runtime.

## 4.3.7 at a glance

- Conflicted conversations enter an isolated branch: continued edits advance only the branch chain and can never advance the shared Head. An indexed durable ledger retains every concurrent loser until the user explicitly resolves it.
- “Keep copy” commits and digest-verifies a stable independent copy before releasing the selected branch; crash retries converge without duplicate copies. “View latest” discards only the selected conflict.
- Per-document UUID writer identity is separate from tab-selection continuity. BroadcastChannel writer claims rotate duplicates so revisions, leases, capsules and peer filtering cannot collide.
- Without Web Locks, immutable proposals preserve all sibling snapshots and choose one deterministic canonical Head. A lock callback that has started is never rerun after an error.
- Degraded Heads self-heal to a verified parent under exclusive arbitration, corrupt data is quarantined, and a known base can never resurrect an id after its Head and tombstone disappear.
- Recovery Capsule V2 adds per-entry and whole-capsule digests, write-back verification, deterministic pressure compaction, quarantine and resolved markers while preserving every message body byte.
- Page exits write a per-tab recovery capsule that startup reconciles exactly once inside the write lock; crashed tabs' capsules are reclaimed once their lease dies.
- The vendor runtime (react/react-dom/react-router/tanstack) splits into a cacheable core chunk via Vite `manualChunks`, cutting the entry asset from 390KB to 157KB while bundle budgets stay unchanged.
- Compatibility: 4.3.5 durable checkpoints and recovery integrity, 4.3.4 activation-transaction/page-lifecycle persistence, 4.3.3 discovery/quiescent reload, 4.3.2 immutable identity and Client Build Leases, and 4.3.1 lazy continuity remain unchanged; the 4.2.8 exact-merge Evidence assembly remains the release-trust foundation.
- Python remains the default and authoritative runtime.
- Every Rust delegate is opt-in and protected by Python fallback.
- DeepSeek and Tavily credentials stay in memory in the React application.

See the [4.3.7 release notes](docs/releases/4.3.7.md), [Evidence index](docs/EVIDENCE_INDEX.md), [frontend boundaries](docs/FRONTEND_MODULES.md), and [support policy](docs/4_0_SUPPORT_POLICY.md).

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
python scripts/preflight_release.py --version 4.3.7 --ga
```

Except for requests explicitly sent to configured providers such as DeepSeek or Tavily, project data remains local by default.
