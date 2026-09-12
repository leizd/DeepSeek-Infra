# Native runtime canonical corpora v17

<!-- docs-language-switcher:start -->
[中文](../../../README.md) / [English](../../../README.en.md)
<!-- docs-language-switcher:end -->

This additive corpus freezes `control-mutation-request-v1`, the canonical
authenticated control-domain mutation proposal document. It contains a public
RFC 8032 test Ed25519 key and signatures only; no production or Federation root
private key is stored.

The vector binds schema, domain, operation, operation identity, fencing token,
execution epoch, request identity, expiry, replay/nonce, payload digest, and
domain-separated Ed25519 authentication. The payload is a `shadow-compare`
intent for the `policy` domain. Verification is fail-closed. A valid request
does not authorize `MutateProduction` or switch the Go control store out of
shadow mode.

The v1 through v16 corpus files remain unchanged.
