# 4.4.13 Checklist

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

- [x] Version surfaces and ADR-0042
- [ ] Pure projection module (granularity / digest / dependency closure)
- [ ] Durable selection freeze in the remote restore session
- [ ] Retry rejects changed selection (409 restore-selection-mismatch)
- [ ] From-target preview (fetch + metadata extract + plan report)
- [ ] Metadata-only and selective ZIP extraction
- [ ] Lazy pack verification (parse without hashing; verify on first use)
- [ ] Projection-aware chain materializer (output vs support separation)
- [ ] Projected `prepare_restore` / commit / rollback
- [ ] `serverTransactionDigest` includes `selectionDigest`
- [ ] Frontend/external-MCP participation derived from selection
- [ ] Hold lifecycle (release on terminal; retain on recovery-required)
- [ ] Adaptive Full uses packed-container physical cost
- [ ] Index-maintenance migration (rebuild + atomic swap)
- [ ] Real Age + MinIO production restore E2E CI contract
- [ ] Release documentation and Evidence contract
- [ ] Full local gates
- [ ] Atomic commits, push, PR, and green CI
