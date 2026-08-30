# DeepSeek Infra

<!-- docs-language-switcher:start -->
[中文](README.md) / [English](README.en.md)
<!-- docs-language-switcher:end -->


![Version](https://img.shields.io/badge/version-4.7.6-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-green)
![Coverage Gate](https://img.shields.io/badge/coverage%20gate-95%25-brightgreen)
![License](https://img.shields.io/badge/license-MIT-black)

DeepSeek Infra is a local-first Agentic AI infrastructure platform that combines an LLM gateway, persistent Agent DAG runtime, MCP-native tool hub, A2A-style agent mesh, local RAG, automation, workspace data, and end-to-end observability in one private runtime.

> **4.7.6 — Production Predictive Control & Verifiable Simulation.** Production fresh-state admission, real crash-recoverable Wave execution, effect-telemetry fairness settlement, automatic capacity sampling/backtesting, write-deny What-If, and a separately validated typed predictive proof. Cross-fleet writes remain out of scope.

## 4.7.6 at a glance

- Scope-aware risk reconciliation so superseded backups and disabled policies
  cannot leave ghost OPEN debt, while incomplete coverage never silently clears.
- Scheduler reservations are not fair-share; only verified terminal bytes charge
  persistent virtual runtime, and preempted/stale plans release the hold.
- Durable wave execution waits for verified predecessor success and revalidates
  Authority, risk, budget, and blast radius before each wave.
- Windowed 1h/24h/7d/30d/lifetime SLO percentiles with INSUFFICIENT_DATA, plus
  30/90-day P50/P90 capacity forecasts from durable observations.
- Durability-constrained, side-effect-free What-If optimization proofs; federation
  readiness is credential-free and read-only.

See the [4.7.5 release notes](docs/releases/4.7.5.en.md),
[autonomous operations runbook](docs/runbooks/COORDINATED_AUTONOMOUS_REMEDIATION.md),
and [Evidence index](docs/EVIDENCE_INDEX.md).

- Python remains the default and authoritative runtime.
- Every Rust delegate is opt-in and protected by Python fallback.
- DeepSeek and Tavily credentials stay in memory in the React application.

Historical baseline: [4.3.6](docs/releases/4.3.6.md), [4.3.7](docs/releases/4.3.7.md), [4.4.0](docs/releases/4.4.0.md), [4.4.1](docs/releases/4.4.1.md), [4.4.2](docs/releases/4.4.2.md), [4.4.3](docs/releases/4.4.3.md), [4.4.4](docs/releases/4.4.4.md), [4.4.5](docs/releases/4.4.5.md), [4.4.6](docs/releases/4.4.6.md), [4.4.7](docs/releases/4.4.7.md), [4.4.8](docs/releases/4.4.8.md), [4.4.9](docs/releases/4.4.9.md), [4.4.10](docs/releases/4.4.10.md), [4.4.11](docs/releases/4.4.11.md), [4.4.12](docs/releases/4.4.12.md), [4.4.13](docs/releases/4.4.13.md), [4.5.0](docs/releases/4.5.0.md), [4.5.1](docs/releases/4.5.1.md), [4.5.2](docs/releases/4.5.2.md), [4.5.3](docs/releases/4.5.3.md), [4.5.4](docs/releases/4.5.4.md), [4.5.5](docs/releases/4.5.5.md), [4.5.6](docs/releases/4.5.6.md), [4.5.7](docs/releases/4.5.7.md), [4.5.8](docs/releases/4.5.8.md), [4.5.9](docs/releases/4.5.9.md), [4.6.0](docs/releases/4.6.0.md), [4.6.2](docs/releases/4.6.2.md), [4.6.3](docs/releases/4.6.3.md), [4.6.4](docs/releases/4.6.4.md), [4.6.5](docs/releases/4.6.5.md), [4.6.6](docs/releases/4.6.6.md), [4.6.7](docs/releases/4.6.7.md), [4.6.8](docs/releases/4.6.8.md), [4.6.9](docs/releases/4.6.9.md), [4.7.0](docs/releases/4.7.0.md), [4.7.1](docs/releases/4.7.1.md), [4.7.2](docs/releases/4.7.2.md), [4.7.3](docs/releases/4.7.3.md), [4.7.4](docs/releases/4.7.4.md), [4.7.5](docs/releases/4.7.5.md).

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
