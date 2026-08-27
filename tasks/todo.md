# 4.7.3 Todo — Proof-Carrying Fleet Resilience

## Phase 1: Correctness contracts

- [x] Strict RiskSubject v1 exact matching
- [ ] Strong `dr-readiness-proof-v1` producer and verifier
- [ ] Durable repair/rebalance cancellation before `COMPENSATED`
- [ ] Checkpoint: focused tests, Ruff, and Mypy

## Phase 2: Fenced execution control plane

- [ ] Atomic `admit_and_claim_action()` transaction
- [ ] CAS action lease renewal with resource-lock renewal
- [ ] Lease takeover enters `RECONCILING`
- [ ] Reconciler drives resume/verify/recreate/compensate/unknown branches
- [ ] Long-running repair, rebalance, and DR operations heartbeat leases
- [ ] Checkpoint: concurrency, crash, and stale-worker tests

## Phase 3: Fleet coordination

- [ ] Observed copy/failure-domain blast-radius wave simulation
- [ ] Unsafe wave fail-closed state
- [ ] Risk debt model
- [ ] Weighted fair policy scheduling
- [ ] Safe-point-only preemption
- [ ] Repair/DR bandwidth reserves and rebalance opportunism
- [ ] Checkpoint: fairness, starvation, preemption, and reserve tests

## Phase 4: Evidence

- [ ] Three distinct production S3 endpoint repair scenario
- [ ] Separate production S3 rebalance scenario
- [ ] Production restore DR drill scenario
- [ ] Scenario-specific typed proof fields
- [ ] Autonomous claims added to `REQUIRED_PROOF_CHECKS`
- [ ] Exit-code-only evidence rejected

## Phase 5: Release closure

- [ ] 4.7.3 version surfaces
- [ ] Fleet coordination runbook and release notes
- [ ] Frozen wire contract assertions
- [ ] Frontend check, Ruff, Mypy, full pytest coverage gate
- [ ] Offline eval gates and release version check
- [ ] Five-axis code review

## Explicit non-goals

- No wire-format changes
- No LLM autonomous decisions
- No automatic primary promotion or copy deletion
- No automatic policy mutation
- No Raft or multi-primary Authority
