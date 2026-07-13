# Gateway Request Preparation Parity

Applicable version: v3.5.0.

DeepSeek Infra 3.5.0 can delegate deterministic, credential-free preparation of a non-streaming Gateway request to the existing Rust sidecar. The stable release line remains 3.x. The published `v4.0.0-rc.1` is an architecture preview and release-flow rehearsal, not a commitment to ship stable 4.0.0 next.

## Runtime boundary

Enable the delegate explicitly:

```text
DEEPSEEK_RUST_GATEWAY=1
```

Python assembles the upstream body without credentials and calls:

```text
POST /gateway/request/prepare
```

Rust performs only pure input-to-output work: model and message validation, role/content checks, tool-definition filtering, `tool_choice` validation, bounded numeric normalization, JSON/depth/size checks, and deterministic assembly of the non-streaming body. A successful response contains `ok`, `request`, and Rust diagnostics. An invalid user request contains a stable code such as `invalid_message_role`; natural-language error wording is not a parity surface.

Python continues to own:

- API keys, local authorization, and other credentials;
- provider routing and real upstream HTTP;
- streaming and SSE;
- retries, backoff, circuit breaking, and scheduling;
- semantic-cache storage and policy;
- search, RAG, memory, and dynamic-context injection;
- real tool execution;
- tracing lifecycle and all database or filesystem writes.

The sidecar never receives an API key or `Authorization` header. `/v1/models` and streaming chat remain Python-owned.

## Defensive validation and fallback

Python computes the safe reference contract before delegation and lightly validates every successful Rust response. The returned request must be an object, contain a supported non-empty model and a message list, use only allowed upstream fields, serialize safely as JSON, and exactly match the Python normalized contract. Rust cannot inject credentials, local paths, internal fields, or fields absent from the normalized request.

Sidecar connection failures, timeouts, empty bodies, malformed JSON, non-object responses, missing fields, and defensive-validation failures use Python fallback when `DEEPSEEK_RUST_GATEWAY_FALLBACK=1`. Invalid user requests keep their deterministic input error instead of being mislabeled as backend outages. Rust failures never pass an untrusted body directly upstream.

Safe diagnostics are attached as `gatewayRequestPreparation`:

```json
{
  "runtime": "rust",
  "fallback": false,
  "latencyMs": 2
}
```

Fallback uses `runtime: "python"`, `fallback: true`, and a stable reason such as `rust_backend_unavailable`. Diagnostics do not record credentials, full sensitive prompts, full tool arguments, or local absolute paths.

## Shared corpus and CI

The fixture at `fixtures/gateway/request_preparation_cases.json` contains 68 deterministic valid and invalid cases, covering minimal and multi-turn chat, system/assistant/tool messages, CJK, mixed language, emoji, multipart content, tool choices, numeric boundaries, malformed structures, non-finite numbers, oversized requests, and excessive nesting.

Run live parity against a sidecar:

```bash
python scripts/check_gateway_request_parity.py \
  --base-url http://127.0.0.1:8787 \
  --strict \
  --report artifacts/gateway-request-parity-report.json
```

Successful cases require the complete normalized core request to match. Failed cases require only the stable error category to match. The `gateway-request-parity` CI job uploads the JSON report even on failure. The hybrid Compose E2E separately proves Python Gateway to Rust preparation to Python offline upstream execution, stops the sidecar, and proves the same request succeeds through Python fallback.

## Non-goals

3.5.0 does not implement Rust streaming, upstream HTTP, provider routing, retry/backoff, credential management, semantic-cache policy, RAG/search/memory injection, real MCP tool execution, default-on Rust flags, default sidecar deployment, removal of Python fallback, a new 4.0 RC, or stable 4.0.0.
