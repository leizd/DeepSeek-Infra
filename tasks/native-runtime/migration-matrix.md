# Native runtime migration matrix

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

Generated from ownership JSON, route modules, launchers, Compose files, Go/Rust
packages, and CI jobs — not from README claims. Completeness is **wired native
behavior with evidence**, not file/crate counts.

Companion files: [`continuation.md`](continuation.md),
[`go-action-admission-plan.md`](go-action-admission-plan.md),
[`worker-execution-plan.md`](worker-execution-plan.md). Historical 4.8.1–4.9.x
plans remain; this matrix is the unified tracker.

Legend: `doc` documented target; `code` native code exists; `wire` production
or public entry uses it; `local` this workspace verified; `ci` exact-head CI;
`prod` production owner. `py` = Python still owns the live path.

## 1. Machine-readable ownership domains

Source: `release/native_runtime_ownership_v1.json` (43 domains). Current
production authority is Python. Target owners are 5.0 goals.

| Domain | Target | Current prod | Native code | Wired | Evidence | Blocker |
| --- | --- | --- | --- | --- | --- | --- |
| public_http_listener | rust | py | deepseek-gateway routes registered | chat non-stream + SSE wired; MCP/A2A fail-closed | local gateway tests only | MCP/A2A, catalog parity |
| llm_gateway_sse | rust | py | gateway request-prep + non-stream + SSE execution | **SSE wired, byte-parity verified locally** | `chat_stream.rs` (15 unit) + `tests/chat_stream.rs` (7 real-boundary) + byte-identical probe | exact-head CI; `/api/chat` NDJSON |
| chat_completions_fast_path | rust | py | route exists; non-stream loop runs tool rounds through `dispatch` | wired; boundary-verified | `tests/chat_execution.rs` (real scripted upstream: continuation, data branch, budget exhaustion) | streaming tool loop; exact-head CI |
| chat_streaming_openai_sse | rust | py | `chat_stream.rs` decoder + encoder + read loop | wired via `chat_completions` | byte-identical to oracle (`b9129475…`); 6 scripted upstream cases | tool-round refusal is in-band; no `/api/chat` |
| openai_facade_translation + composition | rust | py | `openai_facade::openai_to_internal_payload` — the six forwarded fields (`model`/`messages`/`stream`/`thinkingEnabled`/`localBaseUrl`/`temperature`), the seven dropped (`tools`, `tool_choice`, `max_tokens`, `top_p`, `reasoning_effort`, `thinking`, everything else), and the two refusals (`invalid_payload`, 400); plus `native_chat` — the `call_deepseek`/`prepare_deepseek_call` composition (validate → memory → build) with `PreparedOpenAiChat` | **ported and probe-verified; not yet invoked by the route** | translation byte-identical (`openai_facade_parity_probe`, **56 keys, 12 204 chars**); **composition byte-identical** (`native_chat_composition_parity_probe`, **15 cases, 298 431 chars** — body + diagnostics + tool names + api key); 10 unit tests | **the native route still builds its body from the OpenAI request directly** (`request_preparation::prepare_chat_request`), which **forwards six fields the oracle drops** and **omits `thinkingEnabled`/`localBaseUrl` that the oracle sets** — a visible divergence on a public route (§五.11). Remaining: `AssemblyEnv::from_env` + its settings env readers, `request_base_url`, the error envelope, a real-upstream test. **Measured and closed on the way:** the composed tool list has **no `web_search`** (`searchEnabled` is never forwarded) and `forced_search_mode` is **structurally unreachable** here, so no refusal is owed for it |
| chat_message_key_order (nested) | rust | py | `request_assembly::NESTED_ORDERS` — the message/tool-call key orders the body renderer reconstructs | **fixed** — `messages` now `role, tool_call_id, content, tool_calls` (a superset serving all three oracle message shapes) plus a `tool_calls` entry `id, type, function` | composition probe byte-identical; `request_assembly_parity_probe` unchanged at 1 946 077 chars | the old `["role","content"]` rendered a tool result as `role, content, tool_call_id` and a call entry as `function, id, type`; the assembly probe's corpus has no tool-role turn, so only the composition probe could see it |
| assembly_env | rust | py | `assembly_env::NativeAssembly` — the nine `AssemblyEnv` fields bound from the server environment (five settings `Default`s, `budget_store` + `LedgerDeps`, `FileStore` + `attachment_context` expander, `local_clock::system_local_now`), with the file-index guard returned as a flag | **landed; not yet invoked by the route** | 7 unit tests incl. both sides of the index guard (a 70 000-char attachment trips it, a short one does not) and the unset-root refusal | **env readers for the settings are not ported**, so only a default-configured deployment agrees; `DEEPSEEK_INFRA_ROOT` unset is an error rather than a degrade (unlike `chat_tool_loop`, the assembly has nowhere else to read the stores from) |
| chat_message_normalization | rust | py | both layers fail closed on the same codes | aligned | 101 passed (`df7dfa13`); `oracle_layering_probe.py` | none for preparation; SSE/tool rounds separate |
| tool_round_control (layer 1) | rust | py | `tool_rounds.rs`: accumulator, lenient normalize, round decision, message assembly, force-final | **implemented; wired into the non-streaming loop** (`chat_tool_loop.rs`) | byte-identical to oracle (`ca9b072a…`), 36 keys / 12 scripts; 24 unit tests; `docs/GATEWAY_TOOL_ROUND_PARITY.md` | streaming round continuation; layer 2's unported branches degrade, not block |
| tool_dispatch_seam (layer 2a) | rust | py | `deepseek-policy::tool_dispatch`: envelope, `parse_tool_arguments`/`safe_limit`/`tool_call_name`/`is_parallel_safe_tool`, branch inventory + routing, parse→gate→route order | **ported; wired — `chat_tool_loop`'s runner calls `dispatch` per call** | byte-identical to oracle (`9d491ef3…`), 49 keys; `docs/GATEWAY_TOOL_DISPATCH.md`; boundary test pins the follow-up request | none |
| tool_branch: generate_chart | rust | py | `tool_dispatch::generate_chart` + `chart_markdown_table` | **ported; wired via the loop** | in the same 49-key diff; boundary test asserts the markdown table reaches the model | none |
| tool_branch: data_transform | rust | py | `tool_transform`: `data_transform` + 4 operations + helpers (json-path, csv, stats) | **ported; wired via the loop** | in the 80-key batch diff (`3f088f27…`) | none |
| tool_batch (layer 2b) | rust | py | `tool_batch`: `execute_tool_calls` batching + cancellation + `role:"tool"` message assembly | **ported; wired — the loop runs one batch per round** | in the 80-key diff; concurrency not reproduced (sequential group = same index-ordered list) | thread pool is a mechanism; the 7 unported branches degrade to "Tool did not run", visibly |
| tool_branches: search family | rust | py | `tool_search`: `web_search` + `compare_search_results` (callback injected via `ExecutorContext`) + `search_result_key` | **ported; wired via the loop (no provider yet → "not enabled for this request")** | in the 104-key diff (`a6aa9b0e…`) | the web-search provider the gateway owns |
| data_layer_storage (memory/reminders/projects) | rust | py | `none equivalent` | **measured; reminders ported, memory/projects not** | `docs/DATA_LAYER_MEASUREMENT.md` (measurement), `docs/REMINDERS_STORE.md` (slice B) | memory (slice D) needs the scorer; projects (slice E) needs `rag/files.py` |
| data_branch: reminders | rust | py | `reminders`: store + `create_reminder` + `list_reminders` + `delete_reminder` + `due_reminders` + `parse_due_at` | **ported; wired via the loop's `WorkspaceContext`** | byte-identical to oracle (`a636cd45…`), 74 keys; 171 tests; file bytes incl. key order; boundary test writes through the fence | `due_reminders` ported but not yet in a compared corpus |
| data_branch: memory triple + memory index read path | rust | py | `memory`: store + `suggest_memory` + `recall_memory` + `forget_memory` + normalization/fingerprint/category/conflict helpers + **the turn-state half** (`prepare_memory_state`, `apply_explicit_memory_command`, `format_memory_context`, `memory_scope_candidates`/`_label`, `upsert_memory`, `clear_memories`, `delete_memory_by_id`) + **`memory_index`**: the `local_rag` memory-collection read path (`hash_text_embedding`/`normalize_vector`/`cosine_similarity` reused from `attachment_context`, `bm25_scores`, `_python_normalize_query`, `parse_embedding`, `load_candidate_rows`, `_search_db`, `search_memories_index`, read-only on `.local-rag/rag.sqlite3`) | **ported; wired via the loop's `WorkspaceContext`**; the turn-state half and the index provider are ported but unwired | byte-identical to oracle (`97187819…`), 194 keys; **index read path byte-identical (`memory_index_parity_probe`, 64 keys, `turn::differing = 7 of 8`)**; 400 tests; `docs/MEMORY_STORE.md` | **the bonus is not bounded — measured**, and the provider that reproduces it now exists, so the wiring must inject it rather than `None`. The `sqlite-vec` deployment still refuses (`VectorTableNotReadable`): `vec0` is a Python-loaded extension, absent from every shipped dependency set and from CI. **Wiring `chat_execution` is the remaining step** |
| workspace_file_lock (shared) | rust | py | `file_lock`: exclusive OS lock, `LockFileEx` retry / `flock` split | **ported** (used by `memory` and `mutation_gate`, both now reached from the loop) | in the same 206 tests, incl. cross-thread serialization | none |
| data_branch: projects read path (slice E1) | rust | py | `projects`: id validation, the `normalize_*` family, `read_project`, `public_project`, `list_projects` | **ported; wired via the loop's `WorkspaceContext`** | byte-identical to oracle (`787f519d…`), 76 keys; `docs/PROJECTS_STORE.md` | none |
| tool_catalog (28 definitions + schema index + ordered trees) | rust | py | `tool_catalog`: `available_tool_definitions` + `tool_parameter_schemas` + `schema_for_tool` + `agent_tool_definitions` + `ordered_tool_definitions` / `ordered_tool_definition` (the `OrderedJson` trees the request body renders from; `python_json::loads` reads the asset order-preservingly) | **ported; consumed by the assembly render; not wired** | byte-identical asset + 18-key parity probe (`a2fa62de…`); asset round-trip test (parse + indent renderer reproduce the committed bytes); 231 tests; generated from the oracle rather than transcribed | the `mcp__` / external-MCP arms are injected; `None` reproduces the oracle escape hatch |
| search layers 1+2 (planning/normalize/rank/cache) | rust | py | `search`: `normalize_search_query_text` + `simplified_retry_query` + `should_search_for_query` + `search_intent` + `search_reason_for_query` + `search_queries_for` + `tavily_options_for_query` + `search_domain_filters` + `normalize_search_response` + `aggregate_search_rounds` + `rerank_search_results` + `search_result_score` + `domain_from_url` + `normalize_search_url` + `compact_search_tool_result` + `search_round_status` + `rounds_in_order` + `search_round_from_cache` + `should_retry_tavily_error` + the cache | **ported; not wired** | byte-identical to oracle (`a4259543…`), 97 keys; 231 tests | the HTTP layer is next: `search_tavily` + `search_tavily_with_retry` + a shared clock for the cache. Live path measured ~5% available, so a stub upstream is the verification route |
| search HTTP layer | rust | py | `search`: `search_tavily` + `search_tavily_with_retry` + `format_upstream_error` + `tavily_request_body_json` | **ported; transport unimplemented** | byte-identical to oracle (`b5077e1e…`), 113 keys; the transport is a `dyn Fn` parameter, so the whole path runs offline | WIRED: `search_provider.rs` (`TavilyConfig` + `reqwest::blocking` transport + the `web_search` callback) bound in `chat_tool_loop`; `reqwest` gained the `blocking` feature. `search_budget` and `progress_callback` remain out, with owners noted in the module docs |
| search prefetch (slices 1-4) + per-turn context + request-shaping leaves | rust | py | `search`: the three payload predicates + the two `format_*` + `search_multiple` (parallel); `context_taint`: **the whole module** — guard, both tables, alternation, `scan_text`, the hardening trio, `classify_request_messages`, `build_taint_report`, `report_is_tainted`, `taint_status`; `dynamic_context`: the reader of `searchContext` + `append_context_to_latest_user` + the per-turn helpers; `request_shaping`: the parallel hint, `tools_for_payload`, `forced_artifact_tool_name`, `normalize_reasoning_effort`, `has_image_content`, `count_payload_attachments`, plus `empty_memory_state` / `memory_scope_from_payload`; `context_engine`: **complete** — the token half (heuristics, estimators, breakdown, window lookup, budget plan, `token_trim`) and the identity half (`base_context_id` with a new `sha1` dependency, `build_context_diff`, `build_engine_diagnostics`); `context_manager`: **complete** — tool ordering, the count window, the token-aware pass, the counters and the merge; `model_router`: **complete** — auto-routing, the explicit path with the vision override, `cascade_plan`, `quality_gate`, `router_status`; plus the **consumed surface** of `edge_inference` (three query-shape patterns + the two payload readers) and `normalize_model_name`; `request_messages`: fail-closed `normalize_chat_messages`, both validators, the tool-call helpers and `MESSAGE_HARD_LIMIT` (the content expander is **injected** — the attachment path reads the file index); `budget_manager`: the **pure half** — the pricing table and cost arithmetic, the policy with its payload override, the in-memory `ToolBudget`, the scope key and the cost diagnostic, plus a byte-level float fix in `python_json` (`serde_json` renders a fraction-of-a-cent cost as `0.000049` where Python writes `4.9e-05`) | **slices 1-4 + the assembly ported; `context_taint` complete; not wired** | byte-identical to oracle (`84b6f0f6…` 2461 lines; `49e390b4…` 176 keys; `e3a065e9…` 39 keys; `7c7860c9…` 121 keys; `08a48874…` 209 keys; `347c7fb2…` 69 keys; `0e22a712…` 75 keys; `65c1e23c…` 21 keys; `adfbfd90…` 406 rows — the whole assembly, byte-identical); lib-test harness **fixed** by a committed `rust/.cargo/config.toml` rustflag (374 tests green) — see `continuation.md` | remaining: **the assembly is ported now** (`de2bd60a`, probe closed by `02976415`): the probe pair is byte-identical over **406 rows** (md5 `adfbfd90…`, every branch the corpus can reach, including the forced `tool_choice` object and the taint block's builder-owned order) — **not wired**, since the production chat path builds its own thinner body today. Remaining: the file **vector index** (`local_rag`, ~1,200 lines; refused via `NATIVE_FILE_VECTOR_INDEX_NOT_READY`/501 until then), the OS local-timezone resolution, `search_if_needed`, and a production caller for the assembly. The `edge_inference` edge-routing half (~430) is **not** in this closure |
| data_branch: projects branches (slice E2) | rust | py | `projects` branches + `file_cache`: `list_project_files` + `read_file_chunk` + `load_cached_file` + `project_file_cache_dir` | **ported; wired via the loop's `WorkspaceContext`** | byte-identical to oracle (`5baaaba2…`), 29 keys; `docs/PROJECTS_STORE.md` | **data layer complete and wired**; the A1 gate test's rare failure led to a poisoning fidelity fix (see docs/PROJECTS_STORE.md) - narrowed, not closed |
| workspace_mutation_gate (slice A1) | rust | py | `mutation_gate`: fence read/assert, exclusive OS lock, durable generation counter, `mutation_scope` | **ported; wired — the loop's data writes pass through it** | byte-identical to oracle (`57e0ede2…`), 32 keys; 155 tests incl. a multi-thread serialisation case; `docs/WORKSPACE_MUTATION_GATE.md` | none |
| core_utils_scorer (slice C) | rust | py | `core_utils`: `query_tokens` + `score_chunk` + `utc_now_iso` + `latest_user_query` | **ported; not wired** | byte-identical to oracle (`2170fb90…`), 37 keys; 180 tests; `docs/RETRIEVAL_SCORER.md` | **oracle is non-deterministic here** (hash-seed dependent `[:80]` cap) — the port is deterministic by design; unblocks slice D and `search_files` |
| tool_branches: 14 remaining | rust | py | `none equivalent` | **unimplemented** — 7 remain after the loop wiring (`browser_*`, `python_eval`, `search_files`, `fetch_url`, `create_mindmap`, `create_pptx`, `create_document`); each resolves to the visible `Tool did not run` envelope | `Branch::blocker()` names each package; a test asserts none is silent | `browser_*`, `python_eval` (needs a real sandbox), `search_files` (rag), `fetch_url` (HTTP client), mindmap/pptx/document |
| tool_policy_pure_core (layer 3a) | rust | py | `deepseek-policy::tool_policy`: guards, sanitizers, schema validation, metadata + capability tables | **ported; wired — the loop's executor attaches a policy per request** | byte-identical to oracle (`26c7723c…`), 158 keys; 30 unit tests; `docs/GATEWAY_TOOL_POLICY_PARITY.md` | none |
| tool_policy_engine (layer 3b) | rust | py | `deepseek-policy::tool_policy`: `ToolPolicy.evaluate`, denial output, diagnostics, taint, `AuditSink` + JSONL writer, args hash, `tool_policy_status` + `ToolPolicySettings` + `ToolAuditPaths` | **ported; wired — `ToolRoundExecutor::from_env` builds the main-chat profile** | byte-identical to oracle (`bae3a9e5…`), 257 keys; 82 unit tests; `docs/GATEWAY_TOOL_POLICY_PARITY.md` | payload-driven narrowing (capability/allowedTools/approvedTools) and the context-taint firewall are not ported; config env reader not ported |
| policy_url_route_parity | rust | py | `url_guard::validate_url_access` delegates to `tool_policy::evaluate_url_safety` | **aligned (route now matches the oracle); flag off** | `guard::*` keys compare the route against the oracle over all 59 URL cases in the same 257-key diff | `path_guard` still a different (containment) operation — needs its own slice; do not enable `DEEPSEEK_RUST_POLICY` before that |
| mcp_jsonrpc | rust | py | deepseek-mcp crate | no | corpus replay only | public `/mcp` |
| rag_hot_path | rust | py | deepseek-rag | no | document-prep sidecar | query/index ownership |
| tool_sandbox | rust | py | policy crate | no | none | sandbox execution |
| url_path_capability_policy | rust | py | deepseek-policy | **URL aligned; path partial** | crate tests + 59-case route parity | `path_guard` root-containment vs oracle argument scan — separate slice |
| fastcdc_chunk_pipeline | rust | py | deepseek-backup / storage | no | library tests | worker dispatch |
| age_crypto | rust | py | backup-crypto | helper binary | helper tests | production key path |
| s3_minio_streaming | rust | py | deepseek-storage s3 | optional feature | local MinIO e2e (not this HEAD) | signed ops + kill/takeover |
| object_set_validation | rust | py | deepseek-storage | library | frozen corpus | production writes |
| receipt_commit_verification | rust | py | storage + proof | library | frozen corpus | provider-backed Receipt |
| backup_restore_data_movement | rust | py | storage/transfer | no | library / some MinIO | Go claim → Rust effect |
| federation_ciphertext_transfer | rust | py | deepseek-federation / transfer | no | journal corpora | Gate A leases |
| ed25519_sign_verify | rust | py | proof / federation | library | corpora | Rust-only key custody in prod |
| evidence_crypto_verifier | rust | py | deepseek-proof | library | corpora | not a production signer |
| federation_private_key_custody | rust | py | federation custody | library | crate tests | no Go private keys |
| transfer_jobs | rust | py | deepseek-transfer | no | frozen phase corpora | worker RPC dispatch |
| data_plane_effect_journal | rust | py | worker sqlite | qualification | local worker tests | TLS+signed ops |
| worker_checkpoint | rust | py | worker / transfer checkpoints | qualification | local | process-kill |
| config_runtime_lifecycle | go | py | deepseekd lifecycle | shadow | go tests | production mode |
| control_api | go | py | `/internal` + gateway proxy isolation | no public `/api` | gateway tests | authenticated proxy |
| policy_crud | go | py | control store domain | shadow | store tests | cutover |
| target_registry | go | py | control store domain | shadow | store tests | cutover |
| backup_scheduler | go | py | scheduler shadow | digest parity | shadow tests | mutation denied |
| agent_dag_scheduler | go | py | internal/agent | shadow | fuzz/parity | production DAG |
| job_controller | go | py | action coordinator | qualification | go tests | cutover + signed ops |
| lease_fencing | go | py | writer + action leases | shadow/qual | go tests | live Rust lease install |
| action_journal | go | py | schema v5–v7 | shadow/qual | this slice | v30 replay; verifiers |
| wave_scheduler | go | py | resilience/wave | shadow | digest parity | cutover |
| risk_lifecycle | go | py | resilience/risk | shadow | digest parity | cutover |
| capacity_forecast | go | py | store forecast domain | shadow | store tests | cutover |
| maintenance_controller | go | py | shadow | shadow | tests | cutover |
| resilience_coordinator | go | py | coordinator qualification | no | go tests | provider settlement |
| federation_peer_registry | go | py | store peer domain | shadow | tests | Gate A |
| federation_session_lifecycle | go | py | store session | shadow | tests | Gate A |
| ingress_grant_lifecycle | go | py | store grant | shadow | tests | Gate A |
| federated_transfer_journal | go | py | store transfer | shadow | tests | Rust data path |
| dr_orchestration | go | py | shadow | shadow | tests | cutover |
| health_readiness | go | py | deepseekd health | internal | process tests | public edge health |
| offline_eval_oracle | python | python | allowed | n/a | pytest/evals | must stay non-prod |
| migration_release_tooling | python | python | allowed | n/a | scripts | must stay out of images |
| browser_ui | ts | ts | frontend/ | `/` via Python today | frontend CI | serve from rust edge |

## 2. Python capability packages still on the production path

`deepseek_infra/infra/` is the production package. Native crates exist beside
several of these, but HTTP/CLI still import Python.

| Package | Production entry | Target owner | Native stand-in | Status |
| --- | --- | --- | --- | --- |
| gateway | `web/routes/chat.py`, `openai_api.py` | rust | deepseek-gateway | non-stream chat wired; MCP/A2A/SSE fail-closed |
| agent_runtime | A2A routes, agent runs | go+rust | proto/agent, go/agent | shadow DAG only |
| rag | `routes/rag.py`, local_rag | rust | deepseek-rag | library |
| tool_runtime | tools, OCR, documents, slides | rust | none equivalent | **unmigrated** |
| observability | traces, metrics, `/api` status | rust+go | gateway observability | partial |
| mcp | `POST /mcp`, registry, executor | rust | deepseek-mcp | codec/library |
| evaluation | evals/ | python oracle | n/a | allowed if offline |
| data | projects, reminders | go | control store subset | unwired |
| workspace | backup/DR/federation/resilience HTTP | go+rust | store + worker + proof | Python HTTP |
| automation | `routes/automation.py` | go | none | **unmigrated** |
| browser | browser controller | rust worker | none | **unmigrated** |
| media | `routes/media.py` | rust | none | **unmigrated** |
| memory | `routes/memory.py` | go/rust | none | **unmigrated** |
| skills | `routes/skills.py` | rust/go | none | **unmigrated** |
| diagnostics | evidence assembly | python tooling | n/a | allowed as release tool |
| native_runtime | mechanical denial | both | authority.py | enforcement helpers |
| rust_core | optional sidecar clients | rust | crates | not production owner |

## 3. Public and control HTTP (from route modules)

Python registers these today in `deepseek_infra/web/`. Rust gateway lists the
public inventory but does not implement behavior.

| Surface | Current entry | Target | Native | Retire when |
| --- | --- | --- | --- | --- |
| `/v1/chat/completions`, `/v1/models` | `routes/chat.py` | rust edge | non-stream + SSE wired (tool rounds refuse) | tool-round parity + browser parity |
| `/api/chat` (NDJSON), `/api/title`, search | `routes/chat.py` | rust or go `/api` | no | proxy + Go API |
| `/mcp`, `/api/mcp/*` | `routes/mcp.py` | rust | no | MCP corpus on edge |
| `/.well-known/agent-card.json`, `/a2a` | agent_runtime | rust | fail-closed | A2A corpus |
| `/api/workspace/*` backups/DR/resilience | workspace + backup_governance | go `/api` via edge | isolation only | Go control API |
| `/api/media`, `/api/memory`, `/api/skills` | media/memory/skills | rust/go | none | native impl |
| `/api/automation` | automation | go | none | native impl |
| `/api/rag/*`, `/api/file-*` | rag/files | rust | library | edge RAG |
| `/federation/v1/*` | `web/federation_app.py` | rust data + go control | crates | signed Federation Gate A |
| `/healthz`, `/metrics` | server + gateway | rust edge | registered | edge is public listener |
| traces | `observability/trace_api.py` | rust/go | partial | native export |

## 4. Launchers, images, platforms

| Entry | Current runtime | Target | Python in default path | Evidence needed |
| --- | --- | --- | --- | --- |
| `launch.py` default `--app` | Python desktop WebView | native host + rust edge | yes | no-Python desktop run |
| `launch.py --server` | `deepseek_infra.app` | rust edge | yes | no-Python server |
| `launch.py --gui` | Tk launcher | native GUI | yes | native launcher |
| `launch.py --mobile` | Python mobile launcher | platform UI | yes | Android/desktop evidence |
| `docker-compose.yml` | `deepseek-infra:4.0.3` Python | native compose | yes | default must change after gates |
| `docker-compose.native.yml` | edge/deepseekd/worker images | 5.0 topology | no Python service entry | runtime workload, not static YAML |
| `docker-compose.stateless-mcp.yml` | TS + Redis | rust MCP | TS server | migrate server, keep Redis if needed |
| Android `android/` | Python backend APK | native + edge | yes | APK without CPython |
| PyInstaller `scripts/build_exe.py` | Python exe | native installer | yes | no python3.dll |

## 5. Go control plane (qualification, not production)

| Module | Role | Schema / status | Tests | Remaining |
| --- | --- | --- | --- | --- |
| `internal/store` | isolated SQLite, unique writer | CurrentSchema v7 in this tree | store tests | v30 oracle replay |
| admission v5 | claim/lease/resources | implemented | local historically | policy identity |
| reconciliation v6 | RECONCILING takeover | implemented | local historically | coordinator wiring |
| verification v7 | VERIFYING / ASSESSING_EFFECT | this tree | this session | outcome/risk verifiers |
| `internal/action` | leased execute + recover | VERIFYING on APPLIED | this session | signed ops |
| `internal/worker` | gRPC client + TLS dial | this tree | this session | CI TLS + signed ops |
| `internal/shadow` | decision digest | implemented | go + script | not mutation |
| `cmd/deepseekd` | lifecycle, shadow HTTP | shadow | process tests | production auth |
| cutover | `ErrCutoverNotAuthorized` | fail-closed | tests | authorization protocol |

## 6. Rust data / security plane

| Crate | Role | Production | Remaining |
| --- | --- | --- | --- |
| deepseek-gateway | public HTTP | not authoritative | implement routes; proxy `/api` |
| deepseek-worker | gRPC worker | mutation denied | TLS (this slice), signed ops, provider kill |
| deepseek-storage / transfer / federation / proof | libraries | Python still writes | dispatch + evidence |
| deepseek-mcp / rag / policy | libraries | Python HTTP | edge wiring |
| backup-crypto, deepseek-backup | production *helpers* | not control plane | keep as helpers until worker owns bytes |

## 7. CI jobs that native changes must keep green

From `.github/workflows/ci.yml`: `test` (pytest 95%), `frontend`, `rust`
(fmt/clippy/test), `rust-coverage` (80%), `native-protocol`, `native-go`
(gofmt/vet/test/race/coverage 95% + real worker), `native-s3-transport`,
eval/security/docs/release-version/evidence. Do not lower thresholds.

## 8. Python that may remain (must be registered, not a dumping ground)

| Use | Entry | Production reachable? |
| --- | --- | --- |
| Offline eval | `evals/runners/*` | no if images exclude it |
| Frozen oracles | `scripts/native_action_lifecycle_oracle.py`, compat corpora | no |
| Codegen/contract check | `scripts/native_codegen.py`, `native_runtime_contract.py` | no |
| Evidence assembly | `deepseek_infra/infra/diagnostics/*` | no |
| Release version | `scripts/check_release_version.py` | no |

Unmigrated business modules above are **not** oracles.

## 9. Ordered remaining chain

1. TLS transport qualification — landed locally (worker server key, Go CA +
   server-name verify, `TestRustWorkerTLSRealBoundary`); exact-head CI pending.
2. Durable grant sqlite journal + Go coordinator attaching live grants (v31
   document and RPC admission are locally verified).
3. Provider-backed execute/reconcile with process kill.
4. Verification/compensation beyond journal primitives; v30 replay.
5. Edge behavior + Go `/api`. The assembly's last prerequisite is ported
   (`prepare_memory_state` and the command grammar); what remains before wiring
   `chat_execution` onto `build_deepseek_request` is a **Rust provider for the
   memory vector index read path or a narrow refusal** — the paired measurement
   proved `None` is not a bounded divergence — plus the two existing refusals
   (forced-search mode, the file vector index).
6. Remaining packages: media, OCR/docs, browser, skills, memory, automation, stateless-mcp, A2A.
7. Per-domain cutover shadow → dual-evaluate → Go-authoritative → Python-disabled.
8. Launchers/images/Android without Python.
9. Exact-head CI, Evidence Assembly, performance, zero-Python workload.
