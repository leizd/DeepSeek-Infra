# Native runtime canonical corpora v32

<!-- docs-language-switcher:start -->
[中文](../../../README.md) / [English](../../../README.en.md)
<!-- docs-language-switcher:end -->

This additive corpus freezes `control-mutation-request-v2`, the versioned
revision of the authenticated control mutation contract that carries a **record
body** and the `apply-mutation` operation. It contains a public RFC 8032 test
Ed25519 key and signatures only; no production or Federation root private key is
stored.

## Why a second revision instead of editing v1

`control-mutation-request-v1` (frozen in
[`v17`](../v17/control/mutation_request_vector.json)) allows exactly one
operation, `propose-mutation`, with one intent, `shadow-compare`, whose payload
is a comparison expectation (`intent`/`recordId`/`revision`/`state`) and carries
**no record body**. It therefore cannot authorize a production mutation, and
reinterpreting it as one would rewrite a frozen intent. v2 was approved as a
versioned addition so that a Go-authoritative control domain can apply a
production mutation.

## What v2 differs by, and nothing else

| Aspect | v1 | v2 |
| --- | --- | --- |
| `schema` / `schemaVersion` | `control-mutation-request-v1` / `1` | `control-mutation-request-v2` / `2` |
| `operation` | `propose-mutation` | `apply-mutation` |
| payload fields | `intent`, `recordId`, `revision`, `state` | `intent`, `recordId`, **`recordPayload`**, `revision`, `state` |
| signature domain | `deepseek-infra:control-mutation-request-v1\0` | `deepseek-infra:control-mutation-request-v2\0` |

The envelope field set, the canonical-JSON rules, the fencing/epoch/identity
checks and every error code are shared, so v1 behavior is unchanged. The
signature domain is deliberately different: a v2 document signed under the v1
domain must fail with `MUTATION_REQUEST_SIGNATURE_INVALID` (case
`v1-signature-domain`), which is what prevents cross-revision signature replay.

`recordPayload` must be a JSON object. It is covered by `payloadDigest`, so a v2
apply can only write bytes the signer committed to, and the canonical encoder
accepts only `null`/string/bool/int/list/object-with-string-keys — **no floats**
— which is what keeps those bytes identical across Python, Go and Rust.

## Verification

`tests/test_native_runtime_mutation_request_v2.py` replays the vector: the
positive request must verify and match the frozen digest, and each of the 33
negative cases must fail with its frozen code. The vector was produced by the
Python oracle (`deepseek_infra/infra/native_runtime/mutation_request.py`), not by
hand, and every case was executed against that oracle before being committed.

The v1 through v31 corpus files remain unchanged.
