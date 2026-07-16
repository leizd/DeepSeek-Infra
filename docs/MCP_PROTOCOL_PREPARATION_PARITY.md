# MCP Protocol Preparation Parity

DeepSeek Infra 3.6.0 adds an optional Rust delegate for deterministic MCP JSON-RPC protocol preparation. The stable development line remains 3.x; `v4.0.0-rc.1` is a historical architecture preview, not the active release target.

## Ownership boundary

With `DEEPSEEK_RUST_MCP=1`, Python first computes the local protocol result and then asks the Rust sidecar to prepare the same input. Rust may parse and validate the JSON-RPC envelope, normalize supported method parameters, and return a routing description whose owner is always `python`. Python adopts the Rust result only when it is contract-identical to the local result.

Python continues to own transports, authentication, sessions, capability decisions, registries, tool execution, resources, prompts, cancellation, scheduling, tracing, credentials, and all business state. Rust never executes tools, reads resource or prompt content, or receives caller credentials. The feature is disabled by default and the default Compose deployment remains Python-only.

Backend failures and malformed or divergent Rust responses use the already-computed Python result. User protocol errors remain errors with the same stable category; they are never disguised as backend fallback.

## Deterministic corpus

The shared fixture [`fixtures/mcp/protocol_preparation_cases.json`](../fixtures/mcp/protocol_preparation_cases.json) contains 105 deterministic cases. It covers envelopes, request IDs, requests, notifications, responses, initialize capabilities and protocol versions, tools, resources, prompts, Unicode, oversized payloads, and excessive nesting. The established single-message behavior is preserved; JSON-RPC batch is not added.

Run the parity gate against a local Rust sidecar:

```bash
python scripts/check_mcp_protocol_parity.py \
  --base-url http://127.0.0.1:8787 \
  --strict \
  --report docs/evidence/mcp-protocol-parity-v4.0.2.json
```

The comparison requires matching accept/reject decisions, message type, normalized method/ID/params, routing owner, and stable internal and JSON-RPC error codes. Natural-language error wording may differ. Reports contain only redacted fingerprints and summaries; full params and tool arguments are never written.

The `mcp-protocol-parity` CI job uploads `docs/evidence/mcp-protocol-parity-v4.0.2.json` even when the gate fails.
