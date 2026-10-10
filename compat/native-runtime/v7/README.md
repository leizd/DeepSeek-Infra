# Native runtime canonical corpora v7

<!-- docs-language-switcher:start -->
[中文](../../../README.md) / [English](../../../README.en.md)
<!-- docs-language-switcher:end -->

This additive corpus freezes `control-authority-request-v1`, the canonical
Go-to-Rust authority request document. It contains a public RFC 8032 test
Ed25519 key and signatures only; no production or Federation root private key
is stored.

The vector binds schema, domain, operation, fencing token, execution epoch,
request identity, expiry, replay/nonce, payload digest, and domain-separated
Ed25519 authentication. Verification is fail-closed. A valid request does not
authorize production mutation or switch the Go control store out of shadow mode.

The v1 through v6 corpus files remain unchanged.
