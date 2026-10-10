# Native runtime canonical corpora v31

<!-- docs-language-switcher:start -->
[中文](../../../README.md) / [English](../../../README.en.md)
<!-- docs-language-switcher:end -->

This additive corpus freezes `control-storage-operation-grant-v1`, the canonical
JSON grant a Go control plane attaches as `canonical_authorization` on a storage
mutation RPC. It uses the public RFC 8032 test Ed25519 key; no production or
Federation root private key is stored.

The grant binds action/epoch, operation identity, placement/key, exact condition,
object digest/length, and lease claim revision. The signing input is sorted JSON
with domain separator `deepseek-infra:control-storage-operation-grant-v1`.
Deterministic Protobuf encoding is not a portable canonicalization rule and is
rejected by the verifier.

A valid grant does not authorize production mutation, advance a live epoch, or
move payload bytes. Epoch install remains a separate `control-authority-request-v1`
document. `control-mutation-request-v1` remains the frozen propose-mutation
envelope and is not an execution grant.

The v1 through v30 corpus files remain unchanged.
