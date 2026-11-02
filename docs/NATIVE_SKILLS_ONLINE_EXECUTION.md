# Retained online Skill execution

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

The retained contract is all 52 Skill actions and the path-based run endpoint.
An authenticated, legal online run with an available provider credential must
actually execute, preserve provider content/model/usage/diagnostics, apply its
artifact/project policy, and record its real terminal state. Offline content is
not an online result. Python remains an offline oracle only.

The current parent `6ff4b8ab` still returns `NATIVE_SKILLS_ACTION_NOT_READY` for
every valid-key online run. This test-only baseline adds an actual production
router request with a real disposable loopback provider socket. It first checks
authentication and Python-owned-store denial without provider calls or partial
trace/artifact writes, then requires an actual successful online call. Later
assertions cover original output bytes and usage, project/artifact/trace/run
records, restart, the path entry with environment credentials and no persistence,
restore fencing, and conclusive provider rejection without another request.
No SQL state is seeded; the trace query is read-only. No real provider key or
production data is used. The baseline changes no production implementation.

The intended repair reuses the same native non-streaming chat composition as the
public chat route: validation, model selection, context, memory, search and tool
execution must not be replaced by a second simplified LLM exchange. All existing
Skill pre-execution ownership checks and terminal artifact/analytics/trace paths
remain mandatory. The actual baseline failure must be retained before a repair
is claimed. Native compilation and runtime verification remain in Actions.

This case is not all-Skill parity or full-product readiness. Full original
ownership/recovery/cutover, every retained capability and platform, zero production
Python/server TypeScript, original MCP test engine, complete exact-source
CI/Evidence, coverage, security and performance remain required. **NOT_READY**.
