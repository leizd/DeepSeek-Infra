# Implementation Plan: 4.7.3 Proof-Carrying Fleet Resilience

## Objective

Upgrade 4.7.2 coordinated autonomous remediation from structural claims to
proof-carrying production coordination. Completion requires real provider
effects, fenced renewable execution, atomic admission, transient durability
simulation, exact risk identity, strong DR proof, real compensation, and a fair
fleet scheduler.

## Frozen boundaries

- Preserve `object-set-v1`, Receipt v4, Commit v4, FastCDC v3, randomized Age,
  Projection semantics, `control-authority-v1`, and AuthorityCheckpoint v1.
- Do not add LLM decision making, automatic primary promotion, automatic copy
  deletion, policy mutation, Raft, or multi-primary Authority.
- A real-MinIO claim must use three distinct S3 endpoints through production
  TargetStore paths. Filesystem targets, mocked transfer/authentication, manual
  ledger commits, and scenario-exit-only evidence cannot satisfy it.

## Architecture decisions

1. `admit_and_claim_action()` owns one `BEGIN IMMEDIATE` transaction containing
   budget checks, resource locks, execution-epoch increment, token issue, and
   claim state transition.
2. A takeover records the prior state and enters `RECONCILING`; only a typed
   `RECREATE_EFFECT` result may create a new remote effect.
3. Action and resource-lock leases renew in the same transaction and are fenced
   by `(actionId, executionEpoch, claimToken)`.
4. Blast-radius simulation reads observed committed/recoverable copies and
   failure domains, then evaluates every coordination wave before materializing
   it. Unknown topology fails closed.
5. RiskSubject v1 compares every non-empty scoped field exactly: `type`,
   `policyId`, `backupId`, `targetId`, and `failureDomain`.
6. DR completion accepts only a fully bound `dr-readiness-proof-v1` whose
   digests and verification booleans are complete and true.
7. Fleet scheduling is deterministic and pure: severity, accrued risk debt,
   policy weight, safe preemption state, and transfer reserves produce an
   execution wave; journal admission remains the transactional authority.
8. Evidence claims are derived from scenario-specific typed proof fields, never
   from a pytest process exit code.

## Dependency graph

```text
Exact identity + proof validators + real cancellation
                    |
                    v
Atomic admission + renewable fencing + reconciliation
                    |
                    v
Observed blast-radius simulation
                    |
                    v
Fleet scheduler + safe preemption + transfer reserves
                    |
                    v
Three-S3 production E2E + typed Evidence assembly
                    |
                    v
Version/docs/full release gates
```

## Tasks

### Phase 1: Correctness contracts

- Task 1: Strict RiskSubject v1 matching
  - Acceptance: scoped fields require exact presence/equality; `backupId` and
    `failureDomain` participate; unscoped legacy actions remain explicit.
  - Verify: focused outcome-verifier tests.
  - Files: outcome verifier and its focused tests.
- Task 2: Strong DR proof validation
  - Acceptance: missing/mismatched schema, drill/action/backup identity,
    verification flags, cleanup, or workspace digests fail closed.
  - Verify: focused DR outcome tests.
  - Files: outcome verifier, DR readiness producer, focused tests.
- Task 3: Real effect cancellation
  - Acceptance: cancelable repair/rebalance effects reach a durable cancelled
    job state before `COMPENSATED`; uncertainty becomes `EFFECT_UNKNOWN`.
  - Verify: focused compensation and job lifecycle tests.
  - Files: replication subsystem, action journal, focused tests.

### Checkpoint 1

- Focused tests green; Ruff and Mypy green for touched modules.

### Phase 2: Fenced execution control plane

- Task 4: Atomic admission and claim
  - Acceptance: global/target/policy/failure-domain budgets, locks, and claim
    commit in one transaction; two workers cannot oversubscribe.
  - Verify: deterministic SQLite concurrency tests.
  - Files: action journal, resource locks, concurrency tests.
- Task 5: Renewable action and lock leases
  - Acceptance: allowed active states renew by CAS; action and locks share one
    lease deadline; stale epochs/tokens cannot renew or mutate.
  - Verify: lease/takeover tests.
  - Files: action journal, resource locks, crash/lease tests.
- Task 6: Production takeover reconciliation
  - Acceptance: expired active actions enter `RECONCILING`; typed outcomes
    resume, verify, recreate, compensate, or fail unknown; recreation is the
    only branch allowed to create.
  - Verify: crash recovery tests for repair, rebalance, drill, and uncertainty.
  - Files: action journal, effect reconciler, crash tests.

### Checkpoint 2

- Concurrency and crash suites green; stale-worker tests prove fencing.

### Phase 3: Coordination safety

- Task 7: Real blast-radius wave simulation
  - Acceptance: copies/domains before/during/after come from observed ledger and
    target topology; unsafe or unknown waves are blocked.
  - Verify: coordinator unit/integration tests.
  - Files: coordinator, optional focused simulator module, tests.
- Task 8: Fleet execution scheduler
  - Acceptance: deterministic waves use severity, risk debt, criticality,
    weighted fairness, safe-point preemption, byte/cost estimates, and repair/DR
    reserves without weakening durability.
  - Verify: fairness, starvation, preemption, and reserve tests.
  - Files: new scheduler module, policy/budget adapters, scheduler tests.

### Checkpoint 3

- Coordinator and scheduler suites green; every scheduled wave is admissible by
  the same safety-budget model used at claim time.

### Phase 4: Provider-backed Evidence

- Task 9: Genuine three-S3 autonomous scenarios
  - Acceptance: independent repair, rebalance, and DR scenarios read endpoints
    A/B/C and use production S3 targets, ciphertext transfer, authentication,
    Receipt v4, Commit v4, and durable effect handles without forbidden mocks.
  - Verify: provider-backed E2E when environment variables are available.
  - Files: E2E test(s), setup helper only if required, proof fixture schema.
- Task 10: Typed proof-backed claims
  - Acceptance: all 4.7.x autonomous claims are in `REQUIRED_PROOF_CHECKS` and
    require endpoint/bucket/object/action/epoch/effect/digest bindings.
  - Verify: evidence workflow tests reject exit-only and incomplete proof.
  - Files: runner, evidence proof validator, workflow tests.

### Phase 5: Release closure

- Task 11: Version and operations documentation
  - Acceptance: all version surfaces are 4.7.3; runbook/release notes describe
    actual behavior and never promote unexecuted evidence to PASS.
  - Verify: release version check and documentation searches.
- Task 12: Full verification and review
  - Acceptance: frontend check, Ruff, Mypy, pytest with 95% coverage, offline
    eval gates, wire-freeze assertions, and five-axis review all pass.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Long synchronous S3 calls outlive a lease | Duplicate remote I/O | Heartbeat callbacks at durable transfer checkpoints plus takeover reconciliation |
| SQLite claim races | Budget oversubscription | `BEGIN IMMEDIATE` and no nested connection during admission |
| Rebalance transiently removes durability | Data-loss window | Model add/commit/remove phases separately and block unknown topology |
| Missing MinIO environment locally | False release claim | Mark scenario not executed; only CI/provider proof can satisfy the claim |
| Scheduler fairness changes safety | Unsafe prioritization | Scheduling selects candidates; atomic admission remains authoritative |

## Verification commands

```powershell
pytest <focused-test-file> --no-cov
ruff check .
mypy .
npm run check --prefix frontend
pytest --cov --cov-fail-under=95.0
python scripts/check_release_version.py
```

## Open questions

None. The user-provided Gate A-N specification and final acceptance line are
authoritative for this implementation.
