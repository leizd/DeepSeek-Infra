# DeepSeek Infra

<!-- docs-language-switcher:start -->
[中文](README.md) / [English](README.en.md)
<!-- docs-language-switcher:end -->


![Version](https://img.shields.io/badge/version-4.5.4-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-green)
![Coverage Gate](https://img.shields.io/badge/coverage%20gate-95%25-brightgreen)
![License](https://img.shields.io/badge/license-MIT-black)

DeepSeek Infra is a local-first Agentic AI infrastructure platform that combines an LLM gateway, persistent Agent DAG runtime, MCP-native tool hub, A2A-style agent mesh, local RAG, automation, workspace data, and end-to-end observability in one private runtime.

> **4.5.4 — Autonomous Replica Healing & Backup Write Failover.** Durable resumable repair jobs, bounded streaming ciphertext transfers, CAS remote corrupt replacement, target-side protection leases, in-place committed copy healing without control-plane mutation, two-phase remote DR audit with global commit chain validation, copy safety retention derived from replication objectives, deterministic write placement plans with forced full failovers, and failback stability governance.

## 4.5.4 at a glance

- Durable Autonomous Replica Healing: State machine with per-component checkpointing, crash resumption, bounded streaming memory ($O(\text{buffer}\times\text{workers})$), and CAS quarantine/replacement of corrupted remote objects.
- In-Place Committed Copy Healing: Missing/corrupt components are repaired in-place without generating new Receipt/Commit markers or advancing `targetGeneration` and `control/head.json`.
- Two-Phase Remote DR Audit: Paged scans stage candidates without premature promotion; global sort by `targetGeneration` validates complete commit chain and head before promoting to recoverable.
- Retention Copy Safety Single Source of Truth: Retention deletion safety is derived directly from `policy.replication.minCommittedCopies` and canonical target IDs, protecting active repair sources.
- Backup Write Failover & Continuity: Primary target unavailability triggers deterministic write failover to healthy required replicas, forcing Full snapshots, reusing the same encrypted spool package, and failing back safely after stability windows.

See the [4.5.4 release notes](docs/releases/4.5.4.md) and [Evidence index](docs/EVIDENCE_INDEX.md).

## 4.4.15 at a glance

- Full and Incremental snapshots share one projection pipeline, and project selection is validated against the fully verified target snapshot rather than only the Full baseline.
- Adaptive Full uses a bounded temporary delta archive and aborts oversized candidates before Age encryption, keeping memory O(buffer).
- `object-set-v1` stores one independently encrypted control object and independently randomized encrypted payload components addressed only by ciphertext SHA-256.
- Restore decrypts control metadata first, resolves the full Merkle-verified dependency closure, then downloads only required ciphertext components; unselected components receive zero GET requests.
- Receipt/Commit v4 exposes only ciphertext digests and sizes. Plaintext hashes, paths, projects, contributors and component roles remain inside encrypted control metadata.
- Real subprocess restart and MinIO Evidence gates cover download resumption, federated commit resumption, holds, orphan GC and permanent Whole-Age v2-v5 compatibility.

- Python remains the default and authoritative runtime.
- Every Rust delegate is opt-in and protected by Python fallback.
- DeepSeek and Tavily credentials stay in memory in the React application.

Historical baseline: [4.3.6](docs/releases/4.3.6.md), [4.3.7](docs/releases/4.3.7.md), [4.4.0](docs/releases/4.4.0.md), [4.4.1](docs/releases/4.4.1.md), [4.4.2](docs/releases/4.4.2.md), [4.4.3](docs/releases/4.4.3.md), [4.4.4](docs/releases/4.4.4.md), [4.4.5](docs/releases/4.4.5.md), [4.4.6](docs/releases/4.4.6.md), [4.4.7](docs/releases/4.4.7.md), [4.4.8](docs/releases/4.4.8.md), [4.4.9](docs/releases/4.4.9.md), [4.4.10](docs/releases/4.4.10.md), [4.4.11](docs/releases/4.4.11.md), [4.4.12](docs/releases/4.4.12.md), [4.4.13](docs/releases/4.4.13.md), [4.5.0](docs/releases/4.5.0.md), [4.5.1](docs/releases/4.5.1.md), [4.5.2](docs/releases/4.5.2.md), [4.5.3](docs/releases/4.5.3.md).

See the [4.4.15 release notes](docs/releases/4.4.15.md) (previous [4.4.13](docs/releases/4.4.13.md)), [Evidence index](docs/EVIDENCE_INDEX.md), [frontend boundaries](docs/FRONTEND_MODULES.md), and [support policy](docs/4_0_SUPPORT_POLICY.md).

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

Stateless MCP stack:

```powershell
$env:MCP_AUTH_TOKEN = '<replace-with-a-long-random-token>'
docker compose -f docker-compose.stateless-mcp.yml up -d --build
```

Use `http://127.0.0.1:8010/mcp` through the load balancer. Redis durable state is stored in a separate volume; see the deployment guide before backing up or exposing the service.

## Documentation

The language switcher at the top of every human-maintained Markdown document returns to either the Chinese or English documentation entry. Deep technical documents remain canonical even when a complete line-by-line translation is not yet available.

- [Standalone roadmap](ROADMAP.en.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Stateless MCP](docs/STATELESS_MCP.md)
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
npm ci --prefix stateless-mcp
npm run check --prefix stateless-mcp
ruff check .
mypy .
pytest --cov --cov-fail-under=95
python scripts/preflight_release.py --version 4.5.0 --ga
```

Except for requests explicitly sent to configured providers such as DeepSeek or Tavily, project data remains local by default.
