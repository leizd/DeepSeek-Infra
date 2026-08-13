# 4.5.0 Production Recovery Orchestration Checklist

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

## Review gate

- [x] Audit `origin/main` after PR #128 and protect unrelated untracked files
- [x] Write proposed 4.5.0 specification and ADR
- [x] Break implementation into dependency-ordered, verifiable slices
- [x] User approves specification, ADR, and implementation plan

## Gate A — Transport

- [x] Freeze compatibility fixtures and prepare 4.5.0 development version
- [x] Enforce Scheduler priority and FD budgets
- [x] Parallelize remote object-set upload
- [ ] Persist digest-keyed per-Component restore states
- [ ] Defer Payload HEAD until verified Projection closure
- [ ] Parallelize required Component download
- [ ] Checkpoint A: focused tests, ruff, and mypy green

## Gate B — Cache

- [ ] Add verified encrypted Component cache
- [ ] Add active/recovery-required pins and 20 GiB LRU quota
- [ ] Checkpoint B: warm restore has zero remote Payload GET

## Gate C — Pipeline

- [ ] Persist strongly bound verified Projection Plans
- [ ] Reuse Control metadata without redecode
- [ ] Overlap network/crypto and scrub plaintext Component ZIPs immediately
- [ ] Checkpoint C: plan reuse and plaintext-lifetime tests green

## Gate D — Safety

- [ ] Replace fixed holds with renewable generationed leases
- [ ] Add durable pause/resume/phase-aware abort
- [ ] Add disk/dependency Recovery preflight
- [ ] Checkpoint D: restart, long-lease, and insufficient-disk tests green

## Gate E — Cost and integrity

- [ ] Introduce Prepared Object Sets and remove object-set double compression
- [ ] Probe authoritative provider full-object SHA-256
- [ ] Avoid readback only when strong checksum is proven
- [ ] Checkpoint E: fallback and multipart-ETag rejection tests green

## Gate F — DR readiness

- [ ] Persist bounded/redacted Recovery telemetry
- [ ] Expose actual RPO and explicitly estimated RTO readiness
- [ ] Add isolated manual Recovery Drill
- [ ] Checkpoint F: live Workspace remains byte-identical in every drill test

## Gate G — Evidence and release

- [ ] Real MinIO + real Age cold selective recovery
- [ ] Real MinIO warm-cache recovery with zero Payload GET
- [ ] Real subprocess per-Component and pause/resume recovery
- [ ] Fault injection for disk, lease, cache, remote mutation, and partial commit
- [ ] Frozen wire-format and legacy compatibility Evidence
- [ ] Full Python/frontend/eval/security/release gates
- [ ] Exact-merge CI Evidence only; no fabricated PASS values
- [ ] Release 4.5.0
