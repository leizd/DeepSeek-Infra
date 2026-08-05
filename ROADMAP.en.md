# DeepSeek Infra Roadmap

<!-- docs-language-switcher:start -->
[中文](ROADMAP.md) / [English](ROADMAP.en.md)
<!-- docs-language-switcher:end -->


This standalone roadmap summarizes the current direction. The detailed historical checklist is preserved in the [Chinese roadmap](ROADMAP.md), while completed release behavior remains governed by the [implementation status matrix](docs/IMPLEMENTATION_STATUS.md), release notes, tests, and versioned evidence.

## Current baseline: 4.4.2

- [x] Python-first hybrid runtime stabilized for 4.0.
- [x] Optional Rust delegates remain disabled by default with deterministic Python fallback.
- [x] React owns the default workspace; the legacy frontend was retired in 4.0.8.
- [x] Workspace restore is a crash-recoverable cross-tier transaction with browser epoch fencing and backend journals.
- [x] An optional official-TypeScript-SDK MCP plane keeps no client sessions in process, stores durable tasks/idempotency in Redis, runs two round-robin instances, recovers expired leases, and emits OpenTelemetry.
- [x] Standard age v1 passphrase/X25519 encryption protects complete backups without persisting secrets or exposing plaintext metadata.
- [x] Strict/best-effort coverage reports external durable state, and Stateless MCP Redis state travels as logical JSONL with no lease replay.

## Next frontend slices

- [ ] Move attachment selection, upload progress, cancellation, and file-reader flows into React.
- [ ] Move history management, search, rename, export, and destructive confirmation flows into React.
- [ ] Move Agent run/activity views and trace navigation into React.
- [ ] Move Projects, Skills, Memory, advanced settings, speech, diagnostics, and remaining PWA behavior into React.
- [x] Retire the legacy frontend after browser smoke coverage proved React parity (completed in 4.0.8).

## Runtime and release direction

- [ ] Keep Python authoritative for streaming, tool execution, persistence, document/media processing, and ecosystem-heavy integrations.
- [ ] Expand Rust only through small credential-free deterministic delegates with explicit fallback and parity evidence.
- [ ] Keep Python coverage at or above 95% and Rust coverage gated in CI.
- [ ] Keep docs, manifests, tests, preflight checks, and versioned evidence synchronized for every release.
- [ ] Continue browser, security, Docker, hybrid-runtime, parity, and release-readiness gates on every main-branch update.
- [ ] Keep the stateless MCP type/unit, Docker/Compose, and owner-failover gates green; add third-party GUI evidence only when actually reproduced.

## Completed milestone families

- [x] 2.2.x — visualization, verification, MCP bridge hardening, A2A streaming, eval baselines, and release readiness.
- [x] 2.3.x–2.4.x — protocol interoperability evidence, headless MCP/A2A compatibility, and security evaluation hard gates.
- [x] 2.5.x — backend web-route decomposition.
- [x] 2.6.x — skill system, workbench, packs, versioning, analytics, security review, and local catalog.
- [x] 2.7.x–2.8.x — media, edge-router, context-taint, and browser-control stabilization.
- [x] 3.x — personal runtime GA, quality uplift, semantic-cache work, Rust candidate audits, and 95% Python coverage.
- [x] 4.0.0–4.0.3 — Python-first hybrid GA, frontend security/offline reliability, React migration foundation, and React chat vertical slice.
- [x] 4.0.4–4.4.2 — React parity and legacy retirement, query/reload/checkpoint resilience, portable and encrypted backups, crash-safe restore, and portable stateless MCP task state.

For exact per-version changes, use the [Changelog](CHANGELOG.md) and [release notes](docs/releases/).
