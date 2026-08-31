# ADR-0047: Production predictive control and verifiable simulation

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

- Status: Approved
- Date: 2026-08-30
- Target: `4.7.6`
- Approver: leizd

## Context

4.7.5 introduced durable risk, fairness, wave, forecast, optimizer, and
federation models, but production closure was incomplete. Wave state did not own
real Action Journal execution, fresh-state inputs could be absent, capacity history
was test-seeded, actual fair service was caller supplied, and What-If zero-mutation
claims were counters returned by the simulation itself. Predictive Evidence was
therefore scenario-test-derived rather than bound to one real runtime execution.

## Decision

1. Production admission has one fail-closed fresh-state builder. It reads and
   digest-binds Authority, complete RiskSnapshot, target capacity, running effects,
   action/transfer budgets, maintenance decisions, and blast simulation.
2. A fenced Wave runner owns claim, execution, reconciliation, outcome verification,
   and predecessor release through the production Action Journal. Takeover uses a
   higher epoch and reuses the existing remote effect.
3. Fair service settles exactly once from terminal effect telemetry bound to action,
   execution epoch, effect handle, and transfer/effect identity.
4. The maintenance loop records provider-backed capacity observations. S3/MinIO
   usage comes from the real paginated object inventory; unavailable inventory does
   not fall back to local estimates. Forecast records and due backtests are durable
   and isolated by target incarnation and capacity revision.
5. Clients control only hypothetical optimizer deltas. The service controls policy
   baseline, forecast, price catalog, Authority, running effects, maintenance, and
   all other present truth.
6. What-If runs inside a write-deny capability covering Storage, Authority, Action
   Journal, Policy, and Target mutation paths. Attempted writes and independently
   captured pre/post state maps are part of the result.
7. `predictive-planning-proof-v1` is a typed payload inside the unchanged
   `evidence-proof-v2` envelope. The exact-merge producer and Assembly require the
   report, autonomous proof, and predictive proof as three distinct artifacts.
8. Federation remains read-only. Snapshot integrity, freshness, distinct fleet ID,
   credentials, and frozen storage wire compatibility fail closed. Signing and all
   cross-fleet mutation remain deferred.

## Consequences

- A missing production source blocks admission instead of becoming an implicit safe
  value.
- A Wave state cannot claim completion without a verified production effect.
- Forecast confidence is calibrated by later real observations rather than a test
  calling a backtest helper.
- A self-reported zero counter cannot satisfy predictive Evidence; validator-side
  recomputation and state equality are mandatory.
- Provider inventory probing is exact but potentially more expensive than local
  estimates; inability to complete it is observable and fail-closed.
- The Evidence artifact is larger because each required claim carries the exact
  typed runtime payload, but no new envelope or storage/control wire protocol is
  introduced.

## Explicit non-goals

Cross-fleet writes, remote autonomous Repair, remote policy mutation, signed
federation, multi-primary Authority, Raft/consensus, automatic durability reduction,
automatic replica deletion, and primary promotion are not part of 4.7.6.

## Frozen contracts

object-set-v1, Receipt v4, Commit v4, FastCDC v3, randomized Age,
control-authority-v1, AuthorityCheckpoint v1, dr-readiness-proof-v1, and the
evidence-proof-v2 envelope remain unchanged.
