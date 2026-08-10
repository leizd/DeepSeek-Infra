# 4.4.12 Checklist

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

- [x] Version surfaces and ADR
- [x] Persistent Rust JSONL helper
- [x] Bounded Rust worker pool
- [x] Working-set scan budget
- [x] Immutable `file_versions`
- [x] Delta `snapshot_file_ops`
- [x] `current_effective_files` plus single-row head
- [x] Legacy materialized-index migration
- [x] Snapshot-local 64 MiB PackWriter
- [x] Typed pack-range and standalone payload references
- [x] `incremental-v5` builder integration
- [x] Bounded verified pack-range restore
- [x] v2-v5 compatibility contracts
- [x] File-version/chunk-map GC
- [x] Privacy-safe index/packing metrics
- [x] Thresholded incremental compaction
- [x] 100k-file/index-growth scale contracts
- [x] Real HTTP MinIO S3 E2E CI contract
- [x] Release documentation and Evidence contract
- [ ] Full local gates
- [ ] Atomic commits, push, PR, and green CI
