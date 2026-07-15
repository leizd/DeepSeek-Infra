# Getting Started

Applicable version: v4.0.0.

DeepSeek Infra 4.0.0 is the stable, local-first, Python-first hybrid Personal AI Runtime promoted from the validated rc.2 candidate. The first screen is the Workspace: projects, memory, skills, media, browser snapshots, automations, saved items, artifacts and exports all stay in the local runtime root unless you explicitly call an upstream API. The optional Rust sidecar is supported but not required; `v4.0.0-rc.1` is superseded historical preview material.

## Local Run

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python app.py
```

Open `http://127.0.0.1:8000`, create a project, then save useful chat snippets, media snapshots or generated artifacts into that project.

## Release Smoke

```bash
python scripts/doctor.py --offline
python scripts/smoke_ga.py --offline --out docs/evidence/ga-v4.0.0.json
python scripts/preflight_release.py --version 4.0.0 --ga
```

The GA smoke creates an isolated project chain: Project -> Skill -> Media -> Browser Snapshot -> Saved Item -> Artifact -> Automation -> Export.

## Data Location

Set `DEEPSEEK_INFRA_ROOT` to move all writable runtime data. Release archives exclude local runtime state such as `.projects`, `.memory`, `.media`, `.automation`, `.generated`, `.local-rag`, `.traces`, `.skills` and secret files.
