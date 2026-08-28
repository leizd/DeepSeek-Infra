# 4.7.4 Todo — Durable Fleet SLO & Evidence-Closed Autonomous Operations

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

## Phase 0: Release contract

- [x] Prepare 4.7.4 version surfaces
- [x] Record ADR-0045 architecture and frozen boundaries

## Phase 1: Evidence closure

- [x] Read actual Receipt and Commit bytes from the expected S3 target
- [x] Remove every synthetic digest and placeholder object key
- [x] Strengthen semantic proof validators for raw-byte bindings
- [x] Upload report, exact proof, and proof SHA-256 together
- [x] Make Evidence Assembly reject missing/tampered autonomous proof
- [x] Prove live remote-repair crash takeover with distinct PIDs and one job
- [x] Prove global/target/policy/failure-domain admission with OS processes
- [x] Checkpoint: focused Evidence/process tests, Ruff, Mypy

## Phase 2: Persistent risk and fairness

- [x] Add durable Risk Observation Ledger
- [x] Persist open/clear/reopen lifecycle and exact RiskSubject digest
- [x] Carry durable first-seen/open-since state through planner actions
- [x] Derive risk debt from unresolved risk age
- [x] Add durable scheduler virtual service history
- [x] Use persistent fairness history in production scheduling
- [x] Checkpoint: restart/debt/fairness tests, Ruff, Mypy

## Phase 3: Scheduler correctness

- [x] Partition all runnable actions into true DAG waves
- [x] Emit typed `UNSCHEDULABLE` reasons
- [x] Enforce repair reserve through `backup_transfer_budget`
- [x] Integrate atomic safe-point preemption and typed decision proof
- [x] Make degraded blast-radius safety monotonic
- [x] Include running effects in blast-radius simulation
- [x] Checkpoint: waves/budget/preemption/blast tests, Ruff, Mypy

## Phase 4: Fleet SLO and API

- [x] Persist Fleet SLO samples across restart
- [x] Measure queue, clear, remediation, DR, takeover, starvation, proof freshness
- [x] Compute configurable 1h/24h error-budget burn rates
- [x] Enforce timezone-aware maintenance windows and critical overrides
- [x] Add authenticated Fleet Readiness API
- [x] Checkpoint: SLO/burn/window/API tests, Ruff, Mypy

## Phase 5: Release closure

- [x] Lock all requested 4.7.4 Evidence names
- [ ] Run real three-MinIO proof and crash scenarios
- [x] Prove frozen wire semantics unchanged
- [ ] Update runbook, release notes, README, architecture, and Evidence index
- [ ] Run frontend, Ruff, Mypy, full 95% coverage, offline eval, release gates
- [ ] Inspect exact proof artifact from final CI assembly
- [ ] Perform final multi-axis code review
