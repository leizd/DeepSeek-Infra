# DeepSeek Infra

<!-- docs-language-switcher:start -->
[中文](README.md) / [English](README.en.md)
<!-- docs-language-switcher:end -->


![Version](https://img.shields.io/badge/version-4.5.9-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-green)
![Coverage Gate](https://img.shields.io/badge/coverage%20gate-95%25-brightgreen)
![License](https://img.shields.io/badge/license-MIT-black)

DeepSeek Infra is a local-first Agentic AI infrastructure platform that combines an LLM gateway, persistent Agent DAG runtime, MCP-native tool hub, A2A-style agent mesh, local RAG, automation, workspace data, and end-to-end observability in one private runtime.

> **4.5.8 — Durable Storage Control Plane & Geo-Aware Lifecycle.** Converges Repair, Replication, Rebalance, Drain, Retirement, Capacity, and Transfer QoS into a cross-process, restart-safe control plane. Retirement preserves Formal Receipt/Commit history, Policy/Topology mutations use SQLite CAS, placement scopes diversity to one Logical Recovery Point, the Maintenance Supervisor advances durable cursors, and production transfers enforce global/source/destination P0-P6 budgets.

## 4.5.8 at a glance

- Chain-Preserving Retirement: Keeps Receipt v4 and Commit v4 byte-identical, authenticates target-local retirement markers, and GCs only payload ciphertext without live copy/job references.
- Durable Control Authority: Uses `.backup-control/control.sqlite3` and `BEGIN IMMEDIATE` CAS for Policy revisions, topology generations, maintenance cursors, and shared transfer budgets.
- Point-Scoped Geo Placement: Applies copy, Failure Domain, and Region objectives to one Logical Recovery Point; matching RecoveryClass RTO evidence and operator cost estimates remain subordinate to correctness.
- Confidence-Aware Capacity and Multipart Reconciliation: Unknown Force Full capacity fails closed; persisted multipart progress is reconciled against provider `ListParts` before resume.
- Autonomous Maintenance and Managed Transfers: A leased supervisor advances bounded repair/replication/rebalance/drain/retirement work, while actual publish/restore/repair traffic consumes global, source-read, and destination-write tokens.
- Exact-Merge Evidence Boundary: The real gate uses three independent MinIO servers, boto3, `S3TargetStore`, production workers, and real randomized Age; fake S3 and stub crypto cannot satisfy it.

See the [4.5.8 release notes](docs/releases/4.5.8.md) and [Evidence index](docs/EVIDENCE_INDEX.md).

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

Historical baseline: [4.3.6](docs/releases/4.3.6.md), [4.3.7](docs/releases/4.3.7.md), [4.4.0](docs/releases/4.4.0.md), [4.4.1](docs/releases/4.4.1.md), [4.4.2](docs/releases/4.4.2.md), [4.4.3](docs/releases/4.4.3.md), [4.4.4](docs/releases/4.4.4.md), [4.4.5](docs/releases/4.4.5.md), [4.4.6](docs/releases/4.4.6.md), [4.4.7](docs/releases/4.4.7.md), [4.4.8](docs/releases/4.4.8.md), [4.4.9](docs/releases/4.4.9.md), [4.4.10](docs/releases/4.4.10.md), [4.4.11](docs/releases/4.4.11.md), [4.4.12](docs/releases/4.4.12.md), [4.4.13](docs/releases/4.4.13.md), [4.5.0](docs/releases/4.5.0.md), [4.5.1](docs/releases/4.5.1.md), [4.5.2](docs/releases/4.5.2.md), [4.5.3](docs/releases/4.5.3.md), [4.5.4](docs/releases/4.5.4.md), [4.5.5](docs/releases/4.5.5.md), [4.5.6](docs/releases/4.5.6.md), [4.5.7](docs/releases/4.5.7.md), [4.5.8](docs/releases/4.5.8.md).

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
