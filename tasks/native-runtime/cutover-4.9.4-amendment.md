# The 4.9.4 cutover amendment — applied to five stores

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

**Status: applied.** All five `rust_data` domains carry `current_owner: "rust"`. This file
records what was signed, where each edit landed, what evidence backs it, and what is
still open. It replaces the earlier "prepared, not applied" version of this document,
which described a four-domain cutover that has since been superseded — the store set is
five, not four, because `frontend_mirror_store` joined at the same cutover.

The native runtime as a whole remains **未完成 / NOT_READY**. Five data-plane stores
changing owner is not the 4.9.4 milestone, and it is not a release claim.

## The five domains and where each landed

| Domain | Cutover | Landed in | `durable_store` |
| --- | --- | --- | --- |
| `memory_store` | 4.9.4 | `d88c757c` | `rust_data` |
| `reminders_store` | 4.9.4 | `57f0595b` | `rust_data` |
| `skills_store` | 4.9.4 | `f6777344` | `rust_data` |
| `project_metadata_store` | 4.9.4 | `f7561a22` | `rust_data` |
| `frontend_mirror_store` | 4.9.4 | `7fe490d6` | `rust_data` |

Measured over all 49 domains: `current_owner` is **python 36 / rust 12 / typescript 1**,
and `target_owner` is go 21 / rust 25 / python 2 / typescript 1. The other seven
`current_owner: "rust"` domains are the storage-free 4.9.2 set (`public_http_listener`,
`llm_gateway_sse`, `chat_completions_fast_path`, `mcp_jsonrpc`, `rag_hot_path`,
`tool_sandbox`, `url_path_capability_policy`). **All 21 control-plane domains are still
`python`**; zero have moved to `go`.

## What the validator enforces, and why the header still freezes 4.8.0

`scripts/native_runtime_contract.py::validate_ownership` is the machine gate. It now
admits `{python, typescript, rust, go}` for `current_owner`, and requires that a
cut-over domain's `current_owner` already equals its `target_owner`. Two header rules
are deliberately unchanged:

- `status == "accepted"` — this file is an accepted revision, not a live ledger.
- `source_commit == "a37735c68398fc8f795babaa269e2de6a5acd567"` — the **header** stays
  frozen at the 4.8.0 merge. The per-domain field is the part that moves.
- `current_production_authority` stays `"python"` on purpose: overall authority follows
  only when the default runtime stops being Python, and it also accepts `"rust_go"` while
  refusing that value while any production domain is still `python` (line 158-164).

So the earlier claim in this file — *"`current_owner: "rust"` is rejected by the
contract's own validator"* — is **no longer true**. `7fe490d6` relaxed it together with
the domain list.

## The two tests that had to change with it

Cutting a domain over is a **requirement change**, because one of these assertions is
what previously pinned "no cutover has happened":

1. `tests/test_native_runtime_ownership_contract.py:36-42` — was
   `== {"python"}`; now `target_owner ⊆ {rust, go}` and
   `current_owner ⊆ {python, rust, go}`, and **a domain whose `current_owner` is already
   `rust` or `go` must have `current_owner == target_owner`**. That last clause is what
   rejects a half-flip. The companion rule that overall authority cannot become
   `rust_go` while any production domain is still `python` lives at lines 41-42.
2. `tests/test_memory_failure_paths_332.py:192` — it **discovers** the `rust_data`
   domains by reading `current_owner in {"python", "rust"} and target_owner == "rust"
   and durable_store == "rust_data"`. A flipped domain would have dropped out of that set,
   so leaving the discovery on `== "python"` alone would have **silently shrunk
   coverage**. It now accepts both owners, so the set is unchanged by the flip.

## The mechanical denial that makes the handover real

A store is only safe to hand over if Python cannot still write it. Two conditions must
both hold, and neither alone is sufficient:

1. deployment-level `DEEPSEEK_RUNTIME_MODE=python_disabled` (ADR-0049 §4.9.4), and
2. the domain is declared `python -> rust` with a `rust_data` store **and** is cut over.

A store that is not declared is refused whatever the mode says. The gate is
`authority.py:129-134`, and the asymmetry is intentional: `rust_data` domains are denied
**only** under `PYTHON_DISABLED`, never under `GO_AUTHORITATIVE`, because that mode says
nothing about the data plane and during 4.9.3 the data plane can still be Python's.

`authority.RUST_DATA_DOMAINS` holds exactly the five domains above — it is a **subset**
of the contract's `rust_data` domains, and writing the two as equal fails. Each domain
has exactly **one** Python write choke point, which is what makes a per-store gate
mechanical rather than a convention each caller must remember:

| Domain | Write choke point | Landed in |
| --- | --- | --- |
| `memory_store` | `memory._save_memories_unlocked` | `d88c757c` |
| `reminders_store` | `reminders._write_reminders` | `57f0595b` |
| `skills_store` | `registry.skill_store_scope()` | `f6777344` |
| `project_metadata_store` | `projects.write_project` (+3 sibling call sites) | `f7561a22` |
| `frontend_mirror_store` | `backup_mirror.put_frontend_mirror` | `7fe490d6` |

`project_metadata_store` is the one with four `assert_python_writer_allowed` call sites
(`infra/data/projects.py:39,60,230,306`). Callers such as
`workspace/projects.py::rename_project` and `::upsert_project_conversation` are covered
**indirectly** — they reach the gate through `legacy_projects.write_project`, so the
refusal is asserted at the choke point, not at each of its callers.

## Evidence, and what each piece does and does not prove

| Claim | Evidence | Does it prove cutover? |
| --- | --- | --- |
| the five domains are `rust`-owned and gate-closed | `test_native_runtime_ownership_contract.py` + `test_native_runtime_mechanical_denial.py` (19 tests pass locally) | yes, for the contract and the gate |
| project metadata writes are denied after cutover | `test_project_metadata_ownership.py` — six real write paths refused under `python_disabled`, `project.json` bytes unchanged, no `files/` child created | yes, for that store |
| the mirror writer is denied, and still allowed while Python owns the store | `test_native_runtime_mechanical_denial.py:94-160` | yes, both sides |
| provider recovery around real worker death | exact-head CI `37412342800`, `native-s3-transport`, 3 providers × 2 fault variants, all PASS (63.01s) | no — provider qualification, not ownership |

The recovery evidence is real but it is **not** cutover evidence: it exercises the leased
storage path and stops at `VERIFYING`, and administrative promotion remains an offline
administrative fixture. Exact-head CI for that run passed **37/37** jobs, with Go
statement coverage **95.084647% (8,144/8,565)**, Rust line coverage **80.43%** against
the 80% floor, and Python **95.23%** against the 95.20% floor.

**One honesty gap, now closed:** commit `466a0d0b` stated the provider runner passes
"with Go race", but `scripts/run_native_s3_e2e.py` ran `command3` **without** `-race` — the
race-enabled run had only ever been the local one, so CI ran these six scenarios with no
detector. That is fixed in this slice: `command3` now carries `-race`, and because
`-race requires cgo` the runner re-enables `CGO_ENABLED` for that one command only. The
workflow keeps `CGO_ENABLED: "0"` at the job level, so the production image and every
other step stay statically linked. `-timeout` went `3m -> 15m` and the job's
`timeout-minutes` `20 -> 40`, because the detector adds an instrumented build before the
suite starts. `tests/test_native_s3_transport_gate.py` now asserts all of it, and every
assertion was mutation-checked: dropping `-race`, reverting the cgo setting and restoring
the 300s bound each fail the gate.

**It cannot be closed on this machine.** `CGO_ENABLED=1 go test -race` compiles
(1m55s, 42 MB binary, only `msvcrt.dll` + `kernel32.dll` imported), but running it on this
Windows host fails as `0xc0000139` / a silent exit-0 with empty output, and from the
workspace tree it is refused outright (exit 127). `wsl.exe` is on the program blacklist, so
there is no local route to a real detector run. That part is a property of this host, not
of the change.

**Exact-head CI has now closed it.** Run
[37447970451](https://github.com/leizd/DeepSeek-Infra/actions/runs/37447970451) at
`headSha` `f331ede8` passes **37/37** jobs, `native-s3-transport` included. Two independent
measurements show the detector actually ran rather than being silently dropped: the gap
between the last Rust suite finishing (`10:13:26.7`) and the Go suite starting
(`10:14:10.4`) is the ~44s instrumented build, and the suite went **63.01s -> 86.15s**
(1.37x). `DATA RACE` is absent because the detector found nothing, which is the intended
outcome and is **not** evidence the flag was set — the build gap and the slowdown are.
The six scenarios still pass: three providers x `lost-response-committed-true` /
`-false`. Go statement coverage stays **95.084647% (8,144/8,565)**, Rust line coverage
**80.43%**, Python **95.23%** — unchanged by the race build.

## What a rollback would be

Per ADR-0049, rolling back a data-owner cutover is a **controlled ownership transfer**,
not a revert: the reverse revision has to fence the store, prove the native writer has
stopped, and hand the pen back with the Python-side gate removed **in the same change**
that moves `current_owner` back. A revert of the field alone would re-open a dual writer.

## Still open

- **All 21 control-plane domains are `python`.** Zero of the 19 `4.9.3` domains and zero
  of the two `4.9.0` domains have moved. The per-domain cutover mechanism (authority
  claim → authorized cutover → signed apply → v12 handback) passes local tests, but **no
  production domain is flipped and the deployment gate is off**.
- Independent external authorization, cross-store atomicity for existing shadow history,
  and provider-backed kill/takeover evidence for the control plane.
- Leased proof/risk settlement; the recovery paths stop at `VERIFYING`.
- Fleet production custody and key rotation (the signer custody slice qualifies the
  mechanism, not the fleet).
- `release/native_runtime_5_0_evidence_v1.json` is still `NOT_READY` with `gates: {}` and
  `provenance.exact_head: null`. Nothing in this file changes that.
- Remaining native routes, desktop/Android zero-Python deployment, and the zero-Python
  workload measurement.
