# Native Runtime Migration Roadmap

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

This roadmap implements the approved direction in the
[5.0 Native Rust/Go Runtime specification](specs/5.0-native-rust-go-runtime.md).
It does not replace the historical [Rust Core Migration Roadmap](RUST_MIGRATION_ROADMAP.md),
which records the Python-first 3.x/4.0 hybrid path. This document starts the
ownership-inversion line.

## Governing invariants

- Go decides what should happen; Rust decides how bytes and security-sensitive
  effects happen.
- `actionId + executionEpoch` is exact and end-to-end.
- Unknown remote effects fail closed as `EFFECT_UNKNOWN`.
- One durable table has one authoritative runtime writer.
- Cross-process contracts are versioned Protobuf/gRPC, not new ad-hoc JSON or FFI.
- Existing public and frozen storage/federation/evidence wire semantics do not
  change merely because the implementation language changes.
- A release gate needs exact-head, real-execution evidence. Unit tests and mock
  parity are development evidence only.

## 4.8.1: Native Runtime Contract Freeze and Ownership Foundation

### Objective

Turn the complete Python 4.8.0 production behavior into a versioned compatibility
oracle and establish enforceable native ownership boundaries without moving any
production mutation authority.

### Deliverables

- Accepted ADR-0049 and machine-readable ownership matrix
  (`release/native_runtime_ownership_v1.json`).
- Operator runbook: [Native Runtime Migration](runbooks/NATIVE_RUNTIME_MIGRATION.md).
- `proto/{common,control,action,storage,federation,evidence,agent}/v1`.
- Pinned, reproducible Go/protoc/plugin toolchain and generated Rust/Go bindings.
- Immutable canonical corpora for REST, SSE, MCP, A2A, state transitions,
  object-set/Receipt/Commit, Federation, and Evidence.
- Rust and Go replay harnesses with semantic/byte parity diagnostics.
- `deepseekd` config/lifecycle/health foundation in read-only shadow mode.
- Rust worker command/admission/result foundation with stale-epoch rejection.
- Native CI: format/lint/test/race/coverage, protocol drift, compatibility replay.
- Rollback and contract-correction runbooks.

### Exit gate

- All canonical corpora replay identically in Python, Rust, and Go where the
  target language owns the eventual behavior.
- Go cannot mutate production state and Rust workers cannot execute an unfenced
  command.
- Generated code is reproducible from the pinned toolchain.
- No 4.8.0 behavior, frozen contract, or production owner changes.

## 4.8.2: Go Control Plane Shadow Foundation

Python remains production-authoritative. Go `deepseekd` evaluates scheduler,
risk, wave, and federation decisions in an isolated shadow plane and CI requires
`pythonDecisionDigest == goDecisionDigest`. Mutation RPCs stay denied. Package
layout:

`go/cmd/deepseekd`, `internal/{api,config,store,scheduler,action,resilience,federation,observability}`,
`pkg/protocol`.

## 4.9.0: Go Control Plane Foundation

### Objective

Build the Go control model in isolated shadow state and prove deterministic
decision parity before allowing any production mutation.

### Scope

- Config/runtime lifecycle and internal health/readiness.
- Stores and state machines for scheduler, actions, risk, waves, federation, and
  Agent DAG, introduced one bounded domain at a time.
- Python event export -> Go shadow evaluation -> immutable decision-digest report.
- State-machine fuzzing, race detection, lease/fencing process tests, and 95%
  Go coverage with meaningful branch margin.

### Exit gate

- Python and Go decision digests match over canonical and live shadow corpora.
- Shadow state is isolated, has no production credentials, and produces zero
  external effects.
- Every planned Go-owned table has a migration/rollback and unique writer plan.

## 4.9.1: Rust Storage and Transfer Plane

### Objective

Remove Python from all production payload-byte paths while preserving exact
storage, encryption, federation, and proof semantics.

### Native crates

- `deepseek-storage`
- `deepseek-transfer`
- `deepseek-federation`
- `deepseek-proof`
- `deepseek-worker`

### Exit gate

- Backup, restore, repair, rebalance, and federated-transfer payload bytes never
  enter Python.
- Rust produces exact Receipt v4, Commit v4, object-set-v1, and proof bindings.
- Real Three-MinIO and two-Fleet/four-MinIO tests prove resume, unknown-effect
  reconciliation, stale-epoch rejection, and one underlying effect after kill.

## 4.9.2: Rust Edge Authority

### Objective

Make Rust the only public listener and owner of all fast/data/security endpoints.

### Scope

- `/v1/models`, `/v1/chat/completions`, SSE, MCP, RAG, tool sandbox, federation
  data, and backup data.
- Authentication verification, body limits, rate limiting, backpressure,
  timeouts, cancellation, tracing, and stable error mapping.
- Reverse proxy of `/api/*` to Go through the internal contract.
- Native replacement of retained production document/OCR/media and stateless MCP
  behavior, or completed approved deprecations.

### Exit gate

- Public canonical corpus and SDK/browser smokes match the 4.8.0 oracle.
- Bounded SSE memory, gateway p50 <1 ms and p99 <5 ms added latency under the
  declared benchmark profile, and zero Python production HTTP requests.

## 4.9.3: Go Full Control Authority

### Objective

Move every control domain to Go through fenced, reversible ownership cutovers.

### Cutover sequence

```text
shadow -> dual-evaluate -> Go-authoritative -> Python-shadow -> Python-disabled
```

Only decisions are dual-evaluated; durable authoritative writes are never
dual-written. Each domain has an export/import checkpoint, ownership fence,
rollback window, zero-old-writer telemetry, and provider-backed effect evidence.

### Exit gate

- Go owns scheduler, actions, Agent DAG, resilience, risk/waves, capacity/
  forecast, maintenance, federation control, and DR orchestration.
- Old Python writers are mechanically denied, not merely unused by convention.
- Go controller kill/takeover preserves exactly-once effect identity and fencing.

## 4.9.4: Python De-authoritization

### Objective

Make the default production topology fully native while retaining an explicit,
bounded 4.x rollback package.

```text
deepseek-edge    Rust
deepseekd        Go
deepseek-worker  Rust
frontend         static assets
```

### Exit gate

- Default Compose/server/desktop/Android distributions do not start Python.
- Python request/mutation/scheduler/storage/sign counters are all zero in real
  native scenarios.
- `DEEPSEEK_LEGACY_PYTHON=1` is explicit rollback only, cannot share stores, and
  requires a fenced ownership handback procedure.

## 5.0.0: Native Rust/Go Runtime

### Release definition

Production ownership has completely migrated. Production artifacts contain no
Python interpreter, Python service, or server-side TypeScript service. Offline
oracles, evals, migrations, and release tooling may remain in the source tree.

### Final evidence

- Exact public and frozen-contract parity.
- Native two-Fleet/four-MinIO recovery and federation.
- Rust receiver/worker and Go controller SIGKILL recovery.
- Zero duplicate/late effects across epoch takeover.
- Production image/distributable inspection proves no Python runtime.
- Rust/Go security, coverage, fuzz, dependency, performance, and release gates.
- Zero active legacy runtime usage, followed by removal of fallback code,
  configuration, production tests, and deployment artifacts.

## Performance contract

- Cold server-mode startup: <500 ms under the declared reference runner.
- Combined Go controller + Rust edge/worker idle RSS: below the Python 4.8
  baseline under the same topology.
- Gateway added latency: p50 <1 ms, p99 <5 ms.
- SSE memory is bounded by configured connection/window limits.
- Backup/restore memory is O(chunk), never O(object or backup).
- A 1 GiB transfer performs no full-buffer copy.
- 10,000 durable actions retain bounded reconciliation latency.

Benchmarks record toolchain, target, CPU, OS, commit, topology, warmup, samples,
concurrency, and raw results. Public-runner noise cannot be used to claim a pass
without the declared comparison contract.

## CI lanes

1. **Python reference/eval:** canonical oracle, offline eval, migration/release
   tooling. It cannot be treated as production runtime evidence.
2. **Go control:** format, vet, staticcheck, vulnerability scan, test, race,
   coverage, fuzz, and state-machine compatibility.
3. **Rust data/security:** fmt, clippy, test, coverage, deny, fuzz, and canonical
   wire/security compatibility.
4. **Native integration:** generated-protocol drift, public replay, native process
   topology, provider storage, crash recovery, federation, zero-Python artifacts,
   and exact-head Evidence Assembly.

## Stop rules

- Stop a cutover on any unexplained parity divergence.
- Stop mutations on lease loss, stale epoch, proof mismatch, or unknown remote
  effect; reconcile the existing effect before retry.
- Stop release qualification if provider-backed topology, image inspection, or
  exact-head Evidence is missing.
- Do not compensate for a failing gate with mocks, manual ledgers, skipped tests,
  or constant telemetry.
