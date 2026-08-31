# Runbook: Production Predictive Control & Verifiable Simulation (DeepSeek Infra 4.7.6)

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

4.7.6 connects predictive planning to production observations and execution.
It does not authorize destructive optimizer decisions or cross-fleet writes.

## Production flow

```text
Authority + complete RiskSnapshot + budgets + running effects
                         + maintenance + blast simulation
                                      |
                                      v
                           Fresh-state bundle
                                      |
                   +------------------+------------------+
                   v                                     v
       Fenced production Wave runner         Authoritative optimizer input
                   |                                     |
                   v                                     v
       Action Journal effect telemetry          Write-deny What-If
                   |                                     |
                   v                                     v
       Exactly-once fair settlement        Pre/post state digest proof

Real target probe -> observation -> Forecast Registry -> due backtest
                                      |
                                      v
                         predictive-planning-proof-v1
```

## Operator endpoints

- `GET /api/workspace/resilience/readiness` returns source-backed risk and SLO state.
- `GET /api/workspace/resilience/waves` returns durable schedule/wave/action state.
- `POST /api/workspace/resilience/waves/run` advances one fenced production wave.
- `GET /api/workspace/resilience/capacity-forecast` reads the durable registry; it
  never creates an observation.
- `POST /api/workspace/resilience/whatif` accepts only `{"candidate": {...}}`.
  Present truth cannot be supplied or overridden by the caller.
- `GET /api/workspace/resilience/federation` returns a credential-free read-only
  snapshot. It has no remote mutation endpoint.

## Admission failure handling

Wave admission and What-If input construction fail closed when Authority head,
complete risk coverage, capacity, running effects, action/transfer budgets,
maintenance decisions, or blast simulation are unavailable. Do not bypass these
errors by supplying booleans or digests. Restore the unavailable production source
and retry the same durable schedule/action identity.

For worker crashes, a replacement worker must acquire a higher schedule/wave/action
execution epoch and reconcile the existing Action Journal effect handle. An unknown
remote effect remains `EFFECT_UNKNOWN`; creating a replacement Repair, Rebalance,
or Drill is forbidden until reconciliation establishes its outcome.

## Capacity and forecast lifecycle

The maintenance control loop probes each target. MinIO/S3 capacity is measured from
the provider's paginated object inventory; an unavailable inventory fails closed
instead of falling back to a caller or local-ledger estimate. Every observation is
bound to target incarnation, capacity revision, probe source, and observed time.

After at least three observations spanning time, 30/90-day Forecast Records become
`ACTIVE`. A later observation at or after `evaluationDueAt` transitions the record
through `DUE` to `BACKTESTED`, persists MAE/MAPE/bias/interval coverage, and feeds
the calibration of the next forecast. Replacing a target starts a new series.

## Verifiable What-If

The optimizer input builder reads the policy baseline, durable forecast record,
versioned price catalog, Authority head, risk snapshot, capacity, running effects,
budgets, maintenance decision, and blast simulation. The simulation capability
denies Storage, Authority, Action Journal, Policy, and Target mutations. A denied
attempt is still a proof failure. Success additionally requires independently
measured pre/post digests and exact storage inventories to match.

## Evidence verification

The exact-merge producer must emit all three artifacts:

- `storage-control-plane-minio-v4.7.6.json`
- `storage-control-plane-autonomous-proof-v4.7.6.json`
- `storage-control-plane-predictive-proof-v4.7.6.json`

The predictive artifact uses the unchanged `evidence-proof-v2` envelope and carries
`predictive-planning-proof-v1` typed payloads. Validate downloaded CI bytes with:

```powershell
python scripts/validate_evidence_proof.py `
  --proof docs/evidence/storage-control-plane-predictive-proof-v4.7.6.json `
  --scenario real-three-minio-predictive-planning
```

Local real-MinIO success is developer evidence only. Release PASS requires these
exact bytes from the final merge SHA and successful Evidence Assembly.

## Federation and wire boundary

Federation remains a read-only thought experiment. Snapshot digest, freshness,
distinct fleet ID, credentials, and required frozen storage wire identifiers are
validated before simulation. Snapshot signing, remote Repair/placement/policy
mutation, multi-primary Authority, and consensus remain 4.8.0-or-later work.

Frozen contracts: object-set-v1, Receipt v4, Commit v4, FastCDC v3, randomized
Age, control-authority-v1, AuthorityCheckpoint v1, dr-readiness-proof-v1, and
the evidence-proof-v2 envelope.
