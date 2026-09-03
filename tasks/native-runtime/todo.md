# 4.8.1 Todo — Native Runtime Contract Freeze

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

Status: in progress on `codex/native-runtime-4.8.1`.

## Phase 0 — Decision and ownership

- [x] Review/accept 5.0 native runtime specification and ADR-0049.
- [x] Resolve fresh-context adversarial findings.
- [x] Add machine-readable ownership contract and negative tests.
- [x] Confirm existing `tasks/plan.md` and `tasks/todo.md` remain unchanged.

## Phase 1 — Protocol foundation

- [x] Pin Go 1.27.1, protoc 36.1, generators/runtimes, and Windows/Linux checksums.
- [x] Add deterministic tool bootstrap/check path.
- [x] Define `common/v1` and `action/v1`.
- [x] Define `storage/v1` and `federation/v1`.
- [x] Define `control/v1`, `evidence/v1`, and `agent/v1`.
- [x] Generate hashed Go bindings, Rust Prost/Tonic types, and a binary descriptor from the proto contract.
- [x] Add descriptor compatibility and generated-code drift gates.
- [x] Bind Go admission/dispatch helpers to generated types and require Rust's descriptor to equal protoc bytes.
- [x] Freeze an immutable v1 semantic baseline and compare it against the complete binary descriptor, including oneof/presence/options/RPC streaming metadata.
- [x] Keep generated Go/Rust code out of business-logic coverage without lowering thresholds.

## Phase 2 — Non-authoritative native processes

- [x] Initialize Go module and `deepseekd` lifecycle/health.
- [x] Prove 4.8.1 Go mutation paths are mechanically absent/denied.
- [x] Add isolated deterministic Go shadow envelopes.
- [x] Add Rust protocol crate.
- [x] Add Rust worker admission/result foundation.
- [x] Prove empty/zero/stale/missing/future action fences are rejected before effects, with epoch advance isolated to the authority path.
- [x] Prove unknown effects cannot be treated as not-applied.

## Phase 3 — Canonical corpus

- [x] Add corpus manifest, schema, sensitivity policy, and provenance.
- [x] Freeze REST inventory (SSE remains in existing gateway fixtures).
- [x] Freeze MCP corpus via existing protocol-preparation fixture.
- [x] Freeze storage wire field inventories.
- [x] Freeze federation and evidence inventories.
- [x] Freeze legal durable state transition labels and fail-closed rules.
- [x] Replay eventual Rust-owned MCP cases through the Python oracle; Rust admits fences locally.
- [x] Replay eventual Go-owned shadow digests and mutation denial in Go tests.
- [x] Produce immutable corpus SHA-256 digests.

## Phase 4 — CI, operations, release

- [x] Add Go fmt/vet/test/race gates.
- [x] Extend Rust workspace to protocol/worker crates.
- [x] Add protocol generation and native contract gates.
- [x] Add native migration/rollback/unknown-effect runbook.
- [x] Run existing frontend/Python/Rust/eval/security/release gates.
- [ ] Run exact-head CI and Evidence Assembly.
- [x] Verify no production owner or frozen contract changed.
- [ ] Qualify 4.8.1 without skips, mocks, or synthetic Evidence.

## 4.8.1 release blockers

- [ ] Any unexplained canonical parity divergence.
- [ ] Any Go production mutation capability.
- [ ] Any unfenced/stale Rust worker effect.
- [ ] Any shared Python/Go/Rust writable durable state.
- [ ] Any change to a frozen contract.
- [ ] Any non-reproducible generated binding.
- [ ] Missing exact-head provider/native Evidence.
