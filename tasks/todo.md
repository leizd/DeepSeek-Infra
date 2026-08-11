# 4.4.13 Checklist

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

- [x] Version surfaces and ADR-0042
- [x] Pure projection module (granularity / digest / dependency closure)
- [x] Durable selection freeze in the remote restore session
- [x] Retry rejects changed selection (409 restore-selection-mismatch)
- [x] From-target preview (fetch + metadata extract + plan report)
- [x] Metadata-only and selective ZIP extraction
- [x] Lazy pack verification (parse without hashing; verify on first use)
- [x] Projection-aware chain materializer (output vs support separation)
- [x] Projected `prepare_restore` / commit / rollback
- [x] `serverTransactionDigest` includes `selectionDigest`
- [x] Frontend/external-MCP participation derived from selection
- [x] Hold lifecycle (release on terminal; retain on recovery-required)
- [x] Adaptive Full uses packed-container physical cost
- [x] Index-maintenance migration (rebuild + atomic swap)
- [x] Real Age + MinIO production restore E2E CI contract
- [x] Release documentation and Evidence contract
- [x] Full local gates
- [ ] Atomic commits, push, PR, and green CI
