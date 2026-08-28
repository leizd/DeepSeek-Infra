# 4.7.3 Todo — Proof-Carrying Fleet Resilience

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


## Phase 1: Correctness contracts

- [x] Strict RiskSubject v1 exact matching
- [x] Strong `dr-readiness-proof-v1` producer and verifier
- [x] Durable repair/rebalance cancellation before `COMPENSATED`
- [x] Checkpoint: focused tests, Ruff, and Mypy

## Phase 2: Fenced execution control plane

- [x] Atomic `admit_and_claim_action()` transaction
- [x] CAS action lease renewal with resource-lock renewal
- [x] Lease takeover enters `RECONCILING`
- [x] Reconciler drives resume/verify/recreate/compensate/unknown branches
- [x] Long-running repair, rebalance, and DR operations heartbeat leases
- [x] Checkpoint: concurrency, crash, and stale-worker tests

## Phase 3: Fleet coordination

- [x] Observed copy/failure-domain blast-radius wave simulation
- [x] Unsafe wave fail-closed state
- [x] Risk debt model
- [x] Weighted fair policy scheduling
- [x] Safe-point-only preemption
- [x] Repair/DR bandwidth reserves and rebalance opportunism
- [x] Checkpoint: fairness, starvation, preemption, and reserve tests

## Phase 4: Evidence

- [x] Three distinct production S3 endpoint repair scenario
- [x] Separate production S3 rebalance scenario
- [x] Production restore DR drill scenario
- [x] Scenario-specific typed proof fields
- [x] Autonomous claims added to `REQUIRED_PROOF_CHECKS`
- [x] Exit-code-only evidence rejected

## Phase 5: Release closure

- [x] 4.7.3 version surfaces
- [x] Fleet coordination runbook and release notes
- [x] Frozen wire contract assertions
- [x] Frontend check, Ruff, Mypy, full pytest coverage gate
- [x] Offline eval gates and release version check
- [x] Five-axis code review
