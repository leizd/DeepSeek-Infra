# DeepSeek Infra

<!-- docs-language-switcher:start -->
[中文](README.md) / [English](README.en.md)
<!-- docs-language-switcher:end -->


![Version](https://img.shields.io/badge/version-4.4.4-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-green)
![Coverage Gate](https://img.shields.io/badge/coverage%20gate-95%25-brightgreen)
![License](https://img.shields.io/badge/license-MIT-black)

DeepSeek Infra is a local-first Agentic AI infrastructure platform that combines an LLM gateway, persistent Agent DAG runtime, MCP-native tool hub, A2A-style agent mesh, local RAG, automation, workspace data, and end-to-end observability in one private runtime.

## 4.4.4 at a glance

- Scheduled backups run under durable Backup Policies that persist only public `age1...` recipients; the Recovery Identity is never written to disk and unattended passphrase backups are refused.
- Browser sessions join background backups through a sealed Frontend Replica Mirror: the envelope is verified, age-encrypted at rest, epoch-fenced, and never stored as plaintext on the server.
- Every scheduled run performs an unattended round trip with an ephemeral verification recipient, proving the ciphertext unlocks and matches the manifest before publication.
- A durable SQLite scheduler with IANA timezones, DST semantics, leases and fencing tokens executes each schedule slot exactly once, even across crashes and multiple workers.
- Filesystem targets are recognized by marker files rather than drive letters; publication is atomic, and the Backup Catalog is an append-only hash chain of receipts that can be rebuilt from disk.
- Grandfather-father-son retention runs as a previewable two-phase delete with a trash grace period; pinned, restore-referenced, and minimum-healthy copies are never auto-deleted.

- Python remains the default and authoritative runtime.
- Every Rust delegate is opt-in and protected by Python fallback.
- DeepSeek and Tavily credentials stay in memory in the React application.

See the [4.4.4 release notes](docs/releases/4.4.4.md), [Evidence index](docs/EVIDENCE_INDEX.md), [frontend boundaries](docs/FRONTEND_MODULES.md), and [support policy](docs/4_0_SUPPORT_POLICY.md).

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
python scripts/preflight_release.py --version 4.4.4 --ga
```

Except for requests explicitly sent to configured providers such as DeepSeek or Tavily, project data remains local by default.
