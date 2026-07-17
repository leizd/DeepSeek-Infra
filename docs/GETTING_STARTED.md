# Getting Started

Applicable version: v4.0.3.

DeepSeek Infra 4.0.3 starts the React migration without replacing the stable workspace. The default `/` screen remains the complete legacy UI; `/ui/` exposes the isolated React migration preview. Projects, memory, skills, media, browser snapshots, automations, saved items, artifacts and exports stay in the local runtime root unless you explicitly call an upstream API. The optional Rust sidecar is supported but not required, and the frozen 4.0 runtime contract is unchanged.

## Local Run

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
npm ci --prefix frontend
npm run build --prefix frontend
python app.py
```

Open `http://127.0.0.1:8000` for the stable workspace. Open `http://127.0.0.1:8000/ui/` only to inspect the isolated migration preview.

## Release Smoke

```bash
python scripts/doctor.py --offline
python scripts/smoke_ga.py --offline --out docs/evidence/ga-v4.0.3.json
python scripts/preflight_release.py --version 4.0.3 --ga
```

The GA smoke creates an isolated project chain: Project -> Skill -> Media -> Browser Snapshot -> Saved Item -> Artifact -> Automation -> Export.

## Data Location

Set `DEEPSEEK_INFRA_ROOT` to move all writable runtime data. Release archives exclude local runtime state such as `.projects`, `.memory`, `.media`, `.automation`, `.generated`, `.local-rag`, `.traces`, `.skills` and secret files.
