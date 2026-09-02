# DeepSeek Infra

<!-- docs-language-switcher:start -->
[中文](README.md) / [English](README.en.md)
<!-- docs-language-switcher:end -->


![Version](https://img.shields.io/badge/version-4.8.0-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-green)
![Coverage Gate](https://img.shields.io/badge/coverage%20gate-95%25-brightgreen)
![License](https://img.shields.io/badge/license-MIT-black)

DeepSeek Infra is a local-first Agentic AI infrastructure platform that combines an LLM gateway, persistent Agent DAG runtime, MCP-native tool hub, A2A-style agent mesh, local RAG, automation, workspace data, and end-to-end observability in one private runtime.

> **4.8.0 — Signed Federation & Cross-Fleet Disaster Recovery (candidate implementation complete).** Gate A now closes immutable Wave Schedule identity, renewable runner leases, and real process takeover. Signed Federation adds dedicated Fleet signing identity, operator-pinned peer trust, receiver-controlled ingress, signed offsite replica proofs, and production federated DR while Fleets retain independent Authority.

## 4.8.0 at a glance

- Gate A was completed before Federation writes: schedule identity and long-running
  runner ownership are proven under a real process kill.
- Fleet signing keys are separate from Age and Control Authority identities.
- Peer trust is operator-pinned and explicitly rejects TOFU.
- Receiver-issued, transfer-bound grants authorize ciphertext custody without
  exposing long-lived Receiver storage credentials.
- Federated copies never reduce local copies or local failure-domain requirements.
- Existing `object-set-v1`, Receipt v4, Commit v4, FastCDC v3, randomized Age,
  Control Authority, and Evidence envelope contracts stay frozen.

See the [4.8.0 release notes](docs/releases/4.8.0.en.md), the
[operations runbook](docs/runbooks/SIGNED_FEDERATION_DR.md), and the
[Evidence index](docs/EVIDENCE_INDEX.md). The local real-MinIO candidate is
complete; formal release readiness still requires final PR-head/merge CI, all
three exact Federation artifacts, and successful Evidence Assembly. The current
stable release remains [4.7.6](docs/releases/4.7.6.en.md).

- Python remains the default and authoritative runtime.
- Every Rust delegate is opt-in and protected by Python fallback.
- DeepSeek and Tavily credentials stay in memory in the React application.

Historical baseline: [4.3.6](docs/releases/4.3.6.md), [4.3.7](docs/releases/4.3.7.md), [4.4.0](docs/releases/4.4.0.md), [4.4.1](docs/releases/4.4.1.md), [4.4.2](docs/releases/4.4.2.md), [4.4.3](docs/releases/4.4.3.md), [4.4.4](docs/releases/4.4.4.md), [4.4.5](docs/releases/4.4.5.md), [4.4.6](docs/releases/4.4.6.md), [4.4.7](docs/releases/4.4.7.md), [4.4.8](docs/releases/4.4.8.md), [4.4.9](docs/releases/4.4.9.md), [4.4.10](docs/releases/4.4.10.md), [4.4.11](docs/releases/4.4.11.md), [4.4.12](docs/releases/4.4.12.md), [4.4.13](docs/releases/4.4.13.md), [4.5.0](docs/releases/4.5.0.md), [4.5.1](docs/releases/4.5.1.md), [4.5.2](docs/releases/4.5.2.md), [4.5.3](docs/releases/4.5.3.md), [4.5.4](docs/releases/4.5.4.md), [4.5.5](docs/releases/4.5.5.md), [4.5.6](docs/releases/4.5.6.md), [4.5.7](docs/releases/4.5.7.md), [4.5.8](docs/releases/4.5.8.md), [4.5.9](docs/releases/4.5.9.md), [4.6.0](docs/releases/4.6.0.md), [4.6.2](docs/releases/4.6.2.md), [4.6.3](docs/releases/4.6.3.md), [4.6.4](docs/releases/4.6.4.md), [4.6.5](docs/releases/4.6.5.md), [4.6.6](docs/releases/4.6.6.md), [4.6.7](docs/releases/4.6.7.md), [4.6.8](docs/releases/4.6.8.md), [4.6.9](docs/releases/4.6.9.md), [4.7.0](docs/releases/4.7.0.md), [4.7.1](docs/releases/4.7.1.md), [4.7.2](docs/releases/4.7.2.md), [4.7.3](docs/releases/4.7.3.md), [4.7.4](docs/releases/4.7.4.md), [4.7.5](docs/releases/4.7.5.md), [4.7.6](docs/releases/4.7.6.en.md).

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

```bash
docker compose -f docker-compose.stateless-mcp.yml up -d
curl http://127.0.0.1:8080/health
```

Run tests:

```bash
pytest
```
