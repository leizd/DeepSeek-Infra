# DeepSeek Infra

<!-- docs-language-switcher:start -->
[中文](README.md) / [English](README.en.md)
<!-- docs-language-switcher:end -->


![Version](https://img.shields.io/badge/version-4.4.10-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-green)
![Coverage Gate](https://img.shields.io/badge/coverage%20gate-95%25-brightgreen)
![License](https://img.shields.io/badge/license-MIT-black)

DeepSeek Infra is a local-first Agentic AI infrastructure platform that combines an LLM gateway, persistent Agent DAG runtime, MCP-native tool hub, A2A-style agent mesh, local RAG, automation, workspace data, and end-to-end observability in one private runtime.

## 4.4.10 at a glance

- Remote target recovery now closes the public loop from durable chain fetch through bounded decryption/materialization into the existing federated restore transaction.
- Parent ranges and payload chunks are reconstructed with 1 MiB bounded streaming; no whole parent or delta payload is materialized in process memory.
- New writes use `fastcdc-gear-v3`; the v2 decoder remains supported and a protocol transition forces a new full baseline.
- Python and packaged Rust chunk engines implement one boundary contract, compute file and chunk digests in one pass, and fall back safely when the native helper is unavailable.
- Actual physical delta ratio freezes the adaptive run plan before publication; bounded file scanning and resumable four-worker S3 multipart uploads expose performance telemetry without chunk hashes.

- Python remains the default and authoritative runtime.
- Every Rust delegate is opt-in and protected by Python fallback.
- DeepSeek and Tavily credentials stay in memory in the React application.

See the [4.4.10 release notes](docs/releases/4.4.10.md) (previous [4.4.9](docs/releases/4.4.9.md)), [Evidence index](docs/EVIDENCE_INDEX.md), [frontend boundaries](docs/FRONTEND_MODULES.md), and [support policy](docs/4_0_SUPPORT_POLICY.md).

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
python scripts/preflight_release.py --version 4.4.10 --ga
```

Except for requests explicitly sent to configured providers such as DeepSeek or Tavily, project data remains local by default.
