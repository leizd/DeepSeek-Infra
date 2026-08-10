# Getting Started

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


Applicable version: v4.4.13.

DeepSeek Infra 4.4.13 uses the React workspace as the default UI and keeps projects, memory, skills, media, browser snapshots, automations, saved items, artifacts and exports in the local runtime root unless you explicitly call an upstream API. Workspace Backup can create plaintext or standard age v1 encrypted Full + Delta chains using a passphrase or X25519 Recovery Identity. Index v3 stores only changed/deleted path operations for Incrementals, while `incremental-v5` writes small Whole payloads and every unmatched CDC payload into snapshot-local packs without publishing plaintext hashes. Remote restore can be frozen into a Contributor/Project projection whose `selectionDigest` is durable across retries; the full logical Merkle chain is always verified while only selected payload bytes are materialized, and unselected contributors are never mutated. Because the remote archive stays a single whole-age object, the API/UI report `networkSelective: false` and never claim network-level selective fetch. The packaged `deepseek-backup` helper uses a bounded persistent worker pool with Python per-file fallback; the optional general Rust sidecar remains disabled by default. A separate stateless MCP stack is available for horizontally scaled code search and durable test tasks, and its Redis-backed durable state can be included through the optional logical-snapshot Contributor.

## Local Run

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
npm ci --prefix frontend
npm run build --prefix frontend
python app.py
```

Open `http://127.0.0.1:8000` for the workspace.

To run the optional two-instance stateless MCP stack:

```powershell
$env:MCP_AUTH_TOKEN = '<replace-with-a-long-random-token>'
docker compose -f docker-compose.stateless-mcp.yml up -d --build
```

Connect the MCP client to `http://127.0.0.1:8010/mcp`. See [STATELESS_MCP.md](STATELESS_MCP.md) for the five-tool boundary and failover demo.

## Release Smoke

```bash
python scripts/doctor.py --offline
python scripts/smoke_ga.py --offline --out docs/evidence/ga-v4.4.13.json
npm ci --prefix stateless-mcp
npm run check --prefix stateless-mcp
python scripts/preflight_release.py --version 4.4.13 --ga
```

The GA smoke creates an isolated project chain: Project -> Skill -> Media -> Browser Snapshot -> Saved Item -> Artifact -> Automation -> Export.

## Data Location

Set `DEEPSEEK_INFRA_ROOT` to move all writable default-runtime data. Release archives exclude local runtime state such as `.projects`, `.memory`, `.media`, `.automation`, `.generated`, `.local-rag`, `.traces`, `.skills` and secret files. The optional stateless MCP Redis volume is separate and must be backed up or removed independently.
