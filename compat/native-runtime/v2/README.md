# Native runtime canonical corpora v2

<!-- docs-language-switcher:start -->
[中文](../../../README.md) / [English](../../../README.en.md)
<!-- docs-language-switcher:end -->

This additive corpus corrects the 4.9.1 storage rewrite baseline: v1 froze only
Receipt v4 and Commit v4 field names, while v2 freezes the semantic bytes and
digests produced by the Python 4.8.0 authority. The v1 files remain unchanged.

The storage vector is authoritative for:

- role-blind `object-set-v1` inventory ordering and commitment bytes;
- plain lowercase SHA-256 digest representation;
- canonical Receipt v4 document bytes and digest; and
- Commit v4 slot and commit-body digests.

```text
python scripts/native_runtime_contract.py --check
cargo test --locked --manifest-path rust/Cargo.toml -p deepseek-storage
```
