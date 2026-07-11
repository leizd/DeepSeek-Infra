# ADR-0040: Python-first hybrid runtime architecture for 4.0

- Status: Approved
- Date: 2026-07-11
- Target: `4.0.0` / `4.0.0-rc.1`
- Approver: leizd
- Machine contract: [`release/4_0_runtime_decision.json`](../../release/4_0_runtime_decision.json)

## Context

The 3.2.5 readiness audit identified five architecture blockers: the Rust default-on set, default sidecar deployment, Python fallback lifecycle, Gateway streaming ownership, and MCP tool-execution ownership. The implemented Rust surfaces are useful opt-in delegates, but they do not yet replace every Python responsibility. Treating incomplete Rust paths as default solely to reach a major version would weaken compatibility and operational recovery.

The 4.0 readiness contract allows a blocker to exit either through completed implementation or through an explicit approved architecture decision. This ADR chooses a stable Python-first hybrid architecture for the 4.x series.

## Decision

1. **Rust default-on set:** empty. Gateway, MCP, Policy, and RAG Rust delegates remain explicit opt-ins.
2. **Default deployment:** Python-only. The Rust sidecar remains available through its optional Compose configuration and is not added to the default Compose deployment.
3. **Python fallback lifecycle:** Python fallbacks are supported throughout the complete 4.x series. Removal may not be considered before 5.0.0 and would require a separate compatibility and migration decision.
4. **Gateway streaming:** Python owns streaming chat in 4.0. Rust owns model listing and opt-in non-streaming chat delegation.
5. **MCP tool execution:** Python Tool Runtime owns real tool execution in 4.0. Rust owns JSON-RPC validation and protocol routing when the MCP delegate is enabled.

An empty `rust_default_on_components` array is an intentional, approved value. It is not an omitted decision.

## Runtime invariants

- `.env.example` keeps `DEEPSEEK_RUST_GATEWAY`, `DEEPSEEK_RUST_MCP`, `DEEPSEEK_RUST_POLICY`, and `DEEPSEEK_RUST_RAG` set to `0`.
- `docker-compose.yml` remains Python-only.
- Streaming requests continue to use the Python Gateway path.
- Real MCP tool calls continue to execute through Python Tool Runtime.
- Sidecar failure can fall back to Python for the entire 4.x series.
- The 4.0 RC measured Python coverage target remains 95.00%.

## Consequences

The five architecture blockers can be closed from a verifiable decision contract without claiming that Rust streaming or a real MCP tool bridge has been implemented. The 4.0 release line has a conservative compatibility baseline and a defined rollback path. Rust work can continue behind opt-in delegates, with default-on changes requiring a later ADR and matching evidence.

This approval does not make `4.0.0-rc.1` ready. Measured Python coverage remains below the 95.00% RC target, so strict readiness continues to fail.

## Rejected alternatives

- Enabling one or more Rust delegates by default before their full ownership boundaries and failure contracts are approved.
- Bundling the sidecar into the default deployment for 4.0.
- Removing Python fallbacks during 4.x.
- Describing Python-owned streaming or tool execution as completed Rust functionality.
