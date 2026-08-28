# Implementation Plan: 4.7.4 Durable Fleet SLO & Evidence-Closed Autonomous Operations

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

## Objective

Close the remaining correctness gap between real storage effects and durable,
forensically reproducible autonomous-operation claims. The implementation must
bind proofs to actual S3 object bytes and actual process/journal events, persist
risk and scheduler history across control loops, schedule every runnable action
into a typed execution wave, enforce transfer reservations and safe preemption,
and expose durable Fleet SLO/readiness state.

## Frozen boundaries

- Preserve `object-set-v1`, Receipt v4, Commit v4, FastCDC v3, randomized Age,
  Projection semantics, `control-authority-v1`, AuthorityCheckpoint v1, and
  `dr-readiness-proof-v1`.
- Do not add LLM decisions, primary promotion, deletion, replication weakening,
  Raft, multi-primary authority, Receipt v5, Commit v5, or object-set-v2.
- A correctness claim is satisfiable only by recorded production-path state or
  provider bytes. Booleans, placeholder keys, synthetic digests, and manually
  supplied epochs are not evidence.

## Architecture decisions

1. Exact autonomous proof records the resolved target identity and hashes raw
   Receipt/Commit objects read back from that exact target store. Validation
   independently binds `sha256(rawReceipt) == Commit.receiptDigest` and the
   Receipt/Commit `objectSetDigest` values.
2. The Storage Control Plane producer owns both its summary report and the exact
   autonomous proof. Its producer descriptor hashes both files; Evidence
   Assembly rejects a missing or altered proof and re-runs semantic validation.
3. Risk observations, scheduler service history, and SLO samples use durable
   SQLite tables beneath `.resilience-journal`; tests patch these paths through
   `tmp_settings` so no repository runtime state is written.
4. Risk debt uses a durable exact RiskSubject lifecycle. `firstSeenAt` is
   immutable; `openSinceAt` resets only on a real reopen so cleared time does not
   accrue as unresolved debt.
5. Scheduler fairness reads and updates durable per-policy virtual service
   state. It iteratively partitions the full DAG into waves; every candidate is
   either assigned exactly once or returned as `UNSCHEDULABLE` with a typed
   reason.
6. Repair/DR/rebalance traffic maps to existing `backup_transfer_budget`
   classes. Rebalance is denied whenever it would consume the active/pending
   repair reserve; enforcement exists in both scheduler selection and durable
   QoS token consumption.
7. Safe preemption is selected and committed inside the same `BEGIN IMMEDIATE`
   journal transaction that claims the critical repair. Only `PENDING` or
   `CLAIMED + NO_EFFECT` victims are eligible; the transaction releases their
   action budget and locks before the new claim.
8. Blast-radius simulation evaluates running effects plus the proposed wave.
   Healthy baselines must retain policy minima; already-degraded baselines must
   not lose another committed copy or failure domain.
9. Fleet readiness is an additive authenticated GET endpoint. It returns a
   stable camelCase snapshot assembled from durable risk, scheduling, SLO, burn
   rate, and proof-freshness state; no existing endpoint changes shape.

## Dependency graph

```text
Version surface + proof contract
        |
        +--> raw S3 Receipt/Commit proof --> exact proof artifact assembly
        |
        +--> durable risk observations --> durable fairness history
                                            |
                                            v
                                   true multi-wave scheduler
                                     |       |        |
                                     v       v        v
                              transfer   safe-point  monotonic
                              reserves   preemption  blast radius
                                     \       |        /
                                      durable SLO ledger
                                              |
                                              v
                                  Fleet readiness API + docs
                                              |
                                              v
                           real process/MinIO Evidence + full gates
```

## Tasks

### Phase 0: Release contract

- Task 1: Prepare the 4.7.4 version surface and ADR.
  - Acceptance: canonical version checks pass; ADR fixes persistence, proof
    ownership, scheduler, and compatibility decisions before implementation.
  - Verification: `python scripts/check_release_version.py`; architecture doc
    tests.
  - Files: version surfaces, `docs/adr/ADR-0045-*`, release skeleton.

### Phase 1: Evidence closure

- Task 2: Bind autonomous proof to actual target-store bytes.
  - Acceptance: repair and rebalance proof contains endpoint, bucket, targetId,
    real keys, raw Receipt SHA-256, Commit receipt digest, raw Commit SHA-256,
    object-set digest, backupId, and actionId; no digest fallback exists.
  - Verification: RED/GREEN semantic-validator tests plus real three-MinIO E2E.
  - Files: proof validators, provider E2E, focused tests.
- Task 3: Carry the exact proof through producer staging and assembly.
  - Acceptance: report embeds proof path/hash/scenario; producer descriptor and
    release manifest cover proof bytes; missing or tampered proof fails.
  - Verification: evidence workflow and assembly tests.
  - Files: evidence inventory/assembly, runner, CI upload, tests.
- Task 4: Produce real crash and multiprocess admission proof.
  - Acceptance: a killed worker and distinct takeover PID prove higher epoch,
    `RECONCILING`, existing effect discovery, and one underlying job; independent
    OS processes prove global/target/policy/failure-domain admission limits.
  - Verification: process tests locally; provider-backed crash scenario in CI.
  - Files: process helpers/E2E, action journal instrumentation, proof tests.

### Checkpoint 1

- Evidence validators reject all synthetic/self-reported variants.
- Exact proof staging/assembly tests, process concurrency tests, Ruff, and Mypy
  pass.

### Phase 2: Persistent risk and fairness

- Task 5: Add the Risk Observation Ledger.
  - Acceptance: exact subject digest persists first/last seen, open interval,
    count, severity, clear/reopen state, and scope; clear stops debt accrual.
  - Verification: restart and repeated-control-loop tests.
  - Files: new risk ledger, risk engine/planner integration, fixture, tests.
- Task 6: Derive risk debt from the durable lifecycle.
  - Acceptance: repeated planner runs preserve `riskFirstSeenAt`; debt age grows
    while open and resets to the reopened interval after clear/reopen.
  - Verification: focused scheduler/debt tests.
  - Files: planner, scheduler, risk ledger tests.
- Task 7: Persist weighted-fair service history.
  - Acceptance: production scheduling reads/updates policy virtual runtime,
    actions/bytes served, and last scheduled time without caller history.
  - Verification: restart and starvation-bound tests.
  - Files: new scheduler service, fleet scheduler, tests.

### Checkpoint 2

- Risk lifecycle and fairness state survive module/process restart.
- Focused suites, Ruff, and Mypy pass.

### Phase 3: Scheduler correctness

- Task 8: Partition the entire dependency DAG into execution waves.
  - Acceptance: every runnable action receives one wave; dependencies and
    conflicts are preserved; true impossibilities have typed reasons.
  - Verification: repair/rebalance/drill DAG and conflict tests.
  - Files: fleet scheduler and focused tests.
- Task 9: Enforce transfer-budget reservations.
  - Acceptance: pending/active repair reserve cannot be consumed by rebalance;
    the scheduler emits `DEFERRED_TRANSFER_BUDGET`; durable QoS enforces the same
    traffic-class rule.
  - Verification: scheduler and transfer-budget tests.
  - Files: fleet scheduler, transfer budget/control, tests.
- Task 10: Integrate transactional safe-point preemption.
  - Acceptance: a typed `PreemptionDecision` is persisted; safe victim state,
    locks, and budget change atomically with critical-repair claim; unsafe
    victims remain byte-identical.
  - Verification: journal transaction and race tests.
  - Files: action journal, fleet scheduler helper, tests.
- Task 11: Enforce monotonic degraded blast-radius safety.
  - Acceptance: healthy baselines retain minima; degraded baselines retain the
    current copy/domain counts; running effects participate.
  - Verification: focused coordinator tests.
  - Files: coordinator and tests.

### Checkpoint 3

- All candidates are waved or typed-unschedulable; transfer and preemption
  behavior is enforced rather than reported; coordinator suites are green.

### Phase 4: Durable Fleet SLO and operations API

- Task 12: Add the persistent SLO ledger and burn-rate evaluation.
  - Acceptance: queue delay, risk clear, repair/rebalance, DR freshness, lease
    takeover, starvation, and proof freshness samples persist; configurable 1h
    and 24h burn rates are computed.
  - Verification: deterministic-clock persistence and burn tests.
  - Files: new SLO ledger, journal/risk integrations, tests.
- Task 13: Enforce maintenance-window constraints.
  - Acceptance: critical repair overrides; warning rebalance waits; a DR drill
    waits unless DR staleness is critical; timezone/overnight windows work.
  - Verification: focused scheduler tests.
  - Files: fleet scheduler, policy tests.
- Task 14: Expose Fleet Readiness.
  - Acceptance: authenticated `GET /api/workspace/resilience/readiness` returns
    additive, typed readiness/SLO/riskDebt/scheduler/burnRate/evidence fields.
  - Verification: route authentication and response-contract tests.
  - Files: SLO/readiness module, backup governance route, route tests.

### Phase 5: Provider-backed release closure

- Task 15: Run the real MinIO/process scenarios and lock 4.7.4 check names.
  - Acceptance: exact raw-byte proof, real crash takeover, real multiprocess
    budgets, monotonic simulation, and wire-freeze checks are required by the
    Storage Control Plane producer.
  - Verification: dedicated three-MinIO runner in CI; proof artifact inspection.
- Task 16: Complete runbook, release notes, and full release gates.
  - Acceptance: docs describe only implemented behavior; frontend, Ruff, Mypy,
    Python 3.10/3.11/3.12 coverage margin, evals, wire contracts, Evidence
    Assembly, package, and RC readiness pass on exact merge.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| New durable tables accidentally use repo state in tests | Dirty checkout | Patch every new path in `tmp_settings`; assert clean status |
| Repeated risk assessment mutates evidence unpredictably | Non-reproducible plans | Canonical RiskSubject digest and injected clocks |
| Scheduler persistence changes dry-run semantics | Hidden side effects | Explicit `record_service` option for pure previews; production default persists |
| Proof path is valid on producer but absent in assembly | Forensic gap | Make proof a required owned EvidenceSpec with descriptor SHA-256 |
| Cross-database transfer release cannot be atomic | Partial preemption | Preempt only `NO_EFFECT`; no transfer token may exist at that safe point |
| Local machine lacks three-MinIO CI topology | False PASS claim | Unit/process tests locally; provider Gates remain unclaimed until exact CI |

## Verification commands

```powershell
pytest <focused-test-file> --no-cov
ruff check .
mypy .
npm run check --prefix frontend
pytest --cov --cov-fail-under=95.0
python scripts/check_release_version.py
python evals/runners/run_tool_eval.py
python evals/runners/run_injection_adversarial.py --strict --no-report
python evals/runners/run_security_corpus.py --strict
python evals/runners/run_agent_eval.py --strict
```

## Open questions

None. The user-provided Gate A-N specification, check-name inventory, wire freeze,
and final acceptance line are authoritative.
