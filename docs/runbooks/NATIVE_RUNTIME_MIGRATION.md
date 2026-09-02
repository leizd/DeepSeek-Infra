# Native Runtime Migration Runbook

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

Applies to 4.8.1 contract freeze through 5.0.0. Production mutation authority
in 4.8.1 remains Python.

## Ownership at a glance

| Plane | 4.8.1 authority | 5.0 authority |
| --- | --- | --- |
| Public HTTP / data / crypto / transfer | Python | Rust |
| Scheduler / journal / federation control | Python | Go |
| Offline oracle / eval / release tooling | Python | Python (non-production) |

Machine contract: `release/native_runtime_ownership_v1.json`.

## Prove who owns state

1. Read `current_production_authority` in the ownership contract. For 4.8.1 it
   is `python`.
2. Confirm default Compose still starts only the Python service.
3. Confirm `deepseekd` is `DEEPSEEKD_MODE=shadow` and
   `DEEPSEEKD_PRODUCTION_STORE` is unset.
4. Confirm Rust workers are not scheduled against production targets.

## 4.8.2 control-plane shadow

Go evaluates scheduler, risk, wave, and federation decisions only. Compare

`pythonDecisionDigest == goDecisionDigest`

via `python scripts/control_plane_shadow.py --check` and `go test ./internal/shadow`.
Do not cut over mutation until that gate stays green.

## Shadow safety

- Go shadow evaluation writes only decision digests. `ExecuteRepair` and every
  other mutation entry return `MUTATION_DENIED`.
- Shadow mode rejects a configured production store path.
- Do not point Go or Rust at Python SQLite files.

## Unknown effect

If a Rust worker or remote provider result is missing, malformed, or
`EFFECT_STATE_UNSPECIFIED`, treat it as `EFFECT_UNKNOWN`. Never interpret that
as `NOT_APPLIED` and never retry a replacement side effect until the original
`actionId + executionEpoch` is reconciled.

## Stale fence

Rust admission rejects `execution_epoch == 0`, empty `action_id`, and any
command whose epoch is lower than the live epoch for that action. Lost Go
leases do not authorize a late Rust commit.

## Corpus correction

Canonical corpora are immutable after freeze. To correct a fixture:

1. Do not edit the hashed file in place to make a new implementation pass.
2. Add a new corpus version and record the compatibility reason.
3. Re-run `python scripts/native_runtime_contract.py --check`.

## Rollback

4.8.1 does not cut over production owners. Rollback is `git revert` of the
foundation commits. After a later data-owner cutover, rollback is a fenced
export/import, not dual-write and not automatic Python fallback.

## Commands

```text
python scripts/native_runtime_contract.py --check
cargo test --manifest-path rust/Cargo.toml -p deepseek-protocol -p deepseek-worker
go test ./...
go test -race ./...
```
