# 4.6.1 Todo — Recovery Control Plane Stabilization & Release Hardening

## Scope
- [x] Branch `release/4.6.1-stability` from main (includes #141 post-4.6.0 fixes)
- [x] Restore coverage gate to 95% (pyproject / CI / preflight / readiness / AGENTS)
- [x] Release checklist derives from `VERSION` (no hard-coded 4.4.13 / 4.5.0)
- [x] Bump all release surfaces to 4.6.1 (+ Android versionCode 400057)
- [x] `docs/releases/4.6.1.md` + CHANGELOG entry
- [ ] CI full green on PR (do not merge early)
- [ ] Tag after merge

## Explicit non-goals
- No new Storage Control Plane features
- No wire-format changes
