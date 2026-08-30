# Implementation Plan: 4.7.6 Production Predictive Control & Verifiable Simulation

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

## Overview

Close the production gap left by 4.7.5: predictive inputs must come from
authoritative production sources, durable waves must execute and reconcile real
Action Journal effects, fair service must settle from observed effect telemetry,
capacity forecasts must be sampled and backtested automatically, and What-If
must prove write denial through capability enforcement and pre/post digests.
Exact-merge Evidence gains a typed `predictive-planning-proof-v1` artifact while
all frozen storage, control, encryption, and Evidence envelope contracts remain
unchanged.

## Architecture Decisions

- `build_fresh_state_bundle()` is the only production admission source. It reads
  Authority, complete RiskSnapshot, capacity, active effects, action/transfer
  budgets, maintenance decisions, and blast simulation internally and fails
  closed if any source is unavailable.
- Wave execution owns schedule/wave/action leases and fenced epochs, but delegates
  remote effect idempotency and crash reconciliation to the existing production
  `resilience_action_journal.execute_autonomous_action()` lifecycle.
- Terminal service settlement reads a durable Action Journal telemetry record
  bound to action ID, execution epoch, and effect handle; callers cannot provide
  bytes or duration.
- The existing storage maintenance supervisor is the production sampler loop.
  It records normalized observations with target incarnation and provenance,
  persists 30/90-day forecast records, and evaluates due records on later probes.
- `/whatif` accepts hypothetical candidates only. Present truth is built from
  production registries and exposed to the optimizer through a read-only
  capability. Every denied mutation is audited and any attempt fails closed.
- `predictive-planning-proof-v1` is a typed payload inside the unchanged
  `evidence-proof-v2` envelope. Assembly requires report, autonomous proof, and
  predictive proof as distinct exact artifacts.
- Federation remains read-only. No cross-fleet mutation, consensus, remote
  placement, or remote remediation is introduced.

## Dependency Graph

```text
Fresh-state sources ──> Wave admission ──> Fenced Wave runner ──> Journal terminal telemetry
        │                                           │                         │
        └────────────> Optimizer input builder      └────────────> Fair settlement

Target probe ──> Capacity observation ──> Forecast registry ──> Automatic backtest
        │                    │                                  │
        └────────────────────┴──────────> Predictive input/proof bindings

Read-only capability ──> What-If pre/post digest proof ──> Predictive proof artifact
                                                              │
Autonomous proof + report ─────────────────────────────────────┴──> Evidence assembly
```

## Task List

### Phase 1: Release baseline and production fresh state

#### Task 1: Prepare the 4.7.6 version surface

**Acceptance criteria:** all executable release surfaces resolve to 4.7.6; no
frozen protocol identifier changes.

**Verification:** `python scripts/check_release_version.py`; focused wire-freeze
tests; `git diff --check`.

**Dependencies:** None.

#### Task 2: Build and enforce the fresh-state bundle

**Acceptance criteria:** every required source is source-backed and digest-bound;
missing Authority, risk, capacity, effects, budgets, maintenance, or blast state
returns `WAVE_NOT_ADMITTED`; API/callers cannot provide freshness flags.

**Verification:** focused `test_backup_476_fresh_state.py` RED/GREEN suite plus
existing 4.7.5 wave tests updated to the production boundary.

**Dependencies:** Task 1.

### Checkpoint: Fresh-state foundation

- [ ] Missing-source paths fail closed.
- [ ] No caller-supplied safety input reaches Wave admission.
- [ ] Ruff and Mypy pass for touched modules.

### Phase 2: Real wave execution, fencing, and service settlement

#### Task 3: Add the crash-recoverable production Wave runner

**Acceptance criteria:** `run_next_wave(schedule_id)` transitions through
ADMITTING/CLAIMING/EXECUTING/VERIFYING, invokes the production Action Journal,
advances successors only after `VERIFIED_SUCCESS`, and persists monotonic
schedule/wave/action epochs and fenced leases.

**Verification:** focused real-lifecycle tests, two-worker takeover test, and
existing action-journal reconciliation suites.

**Dependencies:** Task 2.

#### Task 4: Settle fair service from terminal effect telemetry

**Acceptance criteria:** actual bytes, duration, traffic class, outcome, effect
handle, and execution epoch come from durable source-of-truth telemetry; replay
is exactly once; failed effects release unused reservations.

**Verification:** focused fairness RED/GREEN tests plus restart/replay coverage.

**Dependencies:** Task 3.

### Checkpoint: Production wave closure

- [ ] Manual `verify_wave_action(actual_bytes=...)` is not a completion path.
- [ ] Takeover reconciles an existing remote effect and never recreates it.
- [ ] Reservation settlement is bound and exactly once.

### Phase 3: Production capacity and forecast lifecycle

#### Task 5: Add the production capacity sampler and incarnation isolation

**Acceptance criteria:** storage maintenance probes call the sampler; normalized
observations carry source, target incarnation, capacity revision, and observed
time; a new incarnation starts a new series; read APIs create no observations.

**Verification:** sampler tests and maintenance-loop integration tests.

**Dependencies:** Task 1.

#### Task 6: Persist Forecast Registry records and automatic backtests

**Acceptance criteria:** 30/90-day forecasts persist as ACTIVE records with due
times and observation-set bindings; later observations atomically transition
due records to BACKTESTED with MAE, MAPE, bias, and interval coverage; confidence
uses persisted calibration.

**Verification:** registry/due-evaluator RED/GREEN tests and restart tests.

**Dependencies:** Task 5.

### Checkpoint: Forecast production closure

- [ ] A control-loop probe produces a durable observation and forecast record.
- [ ] A later real observation backtests due forecasts exactly once.
- [ ] Incarnation changes cannot mix historical series.

### Phase 4: Authoritative optimizer and verifiable simulation

#### Task 7: Build optimizer inputs from production truth

**Acceptance criteria:** baseline, forecast, price catalog, Authority head,
running effects, maintenance windows, and observed fleet snapshot are internal
sources; clients supply only candidate/hypothetical deltas.

**Verification:** API and module tests reject present-truth overrides and prove
each source binding.

**Dependencies:** Tasks 2 and 6.

#### Task 8: Add write-deny simulation capability and mutation audit

**Acceptance criteria:** Storage, Authority, Action Journal, Policy, and Target
mutation operations are unavailable or denied; attempts are audited and fail
with `SIMULATION_VIOLATION`; pre/post digest maps are independently measured and
must match.

**Verification:** denied-write tests, real state digest tests, and tamper tests.

**Dependencies:** Task 7.

### Checkpoint: Verifiable simulation

- [ ] No constant zero counter is accepted as mutation Evidence.
- [ ] Every mutation domain has a measured pre/post digest.
- [ ] Attempted writes cause proof failure even when the write was blocked.

### Phase 5: Typed proof, exact artifacts, and real Three-MinIO

#### Task 9: Add `predictive-planning-proof-v1` semantic validation

**Acceptance criteria:** the proof binds capacity observation set, forecast
record/backtest, price catalog, candidate plan, fresh-state bundle, and measured
simulation state; the validator independently recomputes digests and durability
constraints and rejects self-reported zero mutation.

**Verification:** proof construction, tamper, missing-field, and recomputation
tests.

**Dependencies:** Tasks 4, 6, and 8.

#### Task 10: Require the predictive exact artifact in assembly

**Acceptance criteria:** the runner emits
`storage-control-plane-predictive-proof-v4.7.6.json`; CI uploads it; producer and
global assembly require report + autonomous proof + predictive proof and fail
closed on absence or semantic invalidity.

**Verification:** evidence inventory/producer/assembly workflow tests.

**Dependencies:** Task 9.

#### Task 11: Prove the pipeline against three real MinIO targets

**Acceptance criteria:** real backup/repair/rebalance capacity changes feed
observations and forecasts under injectable logical time; What-If preserves the
queried object inventory; the exact predictive artifact passes semantic
validation.

**Verification:** real Three-MinIO predictive E2E plus complete storage-control-
plane runner.

**Dependencies:** Task 10.

### Phase 6: Documentation, release, and final verification

#### Task 12: Document closure boundaries and verify the release

**Acceptance criteria:** runbook, ADR/release notes, architecture/version surfaces,
wire freeze, and explicit federation non-goals are current; all CI-equivalent
gates pass with coverage margin.

**Verification:** frontend check, Ruff, Mypy, full Pytest at 95.0%, retained JS
syntax, release-version gate, Evidence assembly, and clean worktree review.

**Dependencies:** Tasks 1-11.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Wave and Action Journal transactions are separate | duplicate or falsely completed effects | use fenced ownership and Journal idempotency; never infer terminal success from Wave state |
| Fresh probes partially fail | unsafe fail-open admission | typed missing-source reasons and no `None` defaults |
| Existing DBs lack new columns | startup/runtime failure | idempotent SQLite migrations and restart tests |
| Sampler mixes replaced targets | invalid trend and forecast | partition every query and forecast by target incarnation |
| Simulation audit is self-reported | false zero-mutation claim | capability denial plus measured pre/post digests and attempted-write audit |
| Scenario tests masquerade as runtime proof | invalid release Evidence | exact typed predictive artifact with independent validator and mandatory assembly binding |
| Large release reduces reviewability | hidden regressions | vertical slices, focused RED/GREEN tests, atomic commits, and checkpoints |

## Frozen Contracts and Non-goals

Frozen unchanged: `object-set-v1`, Receipt v4, Commit v4, FastCDC v3,
randomized Age, `control-authority-v1`, AuthorityCheckpoint v1,
`dr-readiness-proof-v1`, and the `evidence-proof-v2` envelope.

Not in 4.7.6: cross-fleet writes, remote autonomous repair or policy mutation,
multi-primary Authority, consensus/Raft, automated durability reduction,
automatic primary promotion, or any v2/v5 storage/control protocol.
