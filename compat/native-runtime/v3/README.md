# Native runtime canonical corpora v3

<!-- docs-language-switcher:start -->
[中文](../../../README.md) / [English](../../../README.en.md)
<!-- docs-language-switcher:end -->

This additive corpus freezes the public, deterministic verification surface of
`federated-replica-attestation-v1`. It contains public keys and signatures only;
no private key material is stored.

The vector binds the Fleet root identity, online signer certificate, Ed25519
domain-separated signatures, immutable transfer identity, pinned failure-domain
metadata, canonical Receipt v4 and Commit v4 bytes, and the attestation lifetime.

The v1 and v2 corpus files remain unchanged.
