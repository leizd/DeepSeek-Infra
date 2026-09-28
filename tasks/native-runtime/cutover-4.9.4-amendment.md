# The 4.9.4 cutover amendment — prepared, not applied

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

**Status: prepared. Not applied, and it cannot be applied from this tree.** The maintainer
asked for it to be signed on 2026-09-26; this file records why signing is a **new accepted
revision of the contract** and not an edit, with everything such a revision has to touch.

## What the contract says today

`release/native_runtime_ownership_v1.json` is a **frozen record of the 4.8.0 snapshot**, not a
live ledger. Its own validator, `scripts/native_runtime_contract.py::validate_ownership`,
enforces that:

| rule in the validator | effect |
| --- | --- |
| `status == "accepted"` | the file is an accepted revision |
| `current_production_authority == "python"` | "4.8.1 production authority must remain python" |
| `source_commit == "a37735c68398fc8f795babaa269e2de6a5acd567"` | "ownership source_commit must freeze 4.8.0 merge SHA" |
| `current_owner in {"python", "typescript"}` for every domain | `"rust"` is **invalid** |

So `"current_owner": "rust"` is not an edit a reviewer can accept — the contract's own
validator rejects it, and `tests/test_native_runtime_ownership_contract.py` calls that
validator on every run.

Measured state: **0 of 48 domains are cut over** (47 `python`, one `production: false`
`typescript`). `skills_store` is in the `4.9.4` group with `memory_store`,
`reminders_store` and `project_metadata_store`.

## What a real revision touches

A revision that cuts those four domains over is not four field edits. It is:

1. **The validator** — `current_owner` gains `rust`, and the `current_production_authority`
   rule has to admit the partial state (the header is a single value, and for a per-domain
   cutover the overall authority is still Python because the default runtime still is).
2. **`source_version` / `source_commit`** — the freeze moves from 4.8.0 to the revision the
   cutover was verified at, which is the exact head CI ran on.
3. **The four domains** — `current_owner: "python"` -> `"rust"`.
4. **`tests/test_native_runtime_ownership_contract.py`** — `{item["current_owner"] for item
   in production} == {"python"}` becomes the assertion that the 4.9.4 group is `rust` and
   everything else is still `python`. This is a **requirement change**, not a fixup: that
   assertion is what pins "no cutover has happened".
5. **`tests/test_memory_failure_paths_332.py:192`** — it *discovers* the handed-over domains
   from `current_owner == "python" and target_owner == "rust" and durable_store ==
   "rust_data"`. After the flip `memory_store` drops out of that set, so the test would
   silently stop covering it unless the discovery is rewritten to read the target side.
6. **`release/native_runtime_5_0_evidence_v1.json`** — its `measurements` and `gates` are
   empty and its six blockers are open; a cutover revision has to carry the measurements the
   zero-Python invariant asks for.

## Preconditions, and their measured state

| precondition | state |
| --- | --- |
| the 4.9.4 stores are `rust_data` and each Python writer is denied | **met** for `skills_store`; the other three are declared at the same cutover |
| `scripts/check_zero_python_runtime.py` is 8/8 on the cutover head | **met locally** (was 7/8 until the `git_commit` subprocess came out) |
| exact-head CI + Evidence Assembly for that head | **not met** — needs a push, which is a separate approval |
| live-workload measurements (the readiness file's three evidence blockers) | **not met** — needs real provider fleets |

The ADR's rule is the one that decides this: *"Freeze before cutover. 4.8.1 captures Python
4.8.0 canonical behavior and ... it becomes authoritative. Unknown effects or parity
divergence stop cutover."* Two of the four preconditions are parity/evidence conditions that
no code change in this tree can satisfy, so a revision cut now would record a production
cutover that has not happened — the default launcher and image still start Python.

## The diff, for the day the preconditions land

```diff
--- a/release/native_runtime_ownership_v1.json
+++ b/release/native_runtime_ownership_v1.json
@@
-  "source_version": "4.8.0",
-  "source_commit": "a37735c68398fc8f795babaa269e2de6a5acd567",
+  "source_version": "<the cutover release>",
+  "source_commit": "<the exact head CI verified>",
@@
-  "current_production_authority": "python",
+  "current_production_authority": "python",
@@
   {"id": "memory_store",            "plane": "data", "current_owner": "python",
-   "target_owner": "rust", "cutover": "4.9.4", "durable_store": "rust_data"}
+   "target_owner": "rust", "cutover": "4.9.4", "durable_store": "rust_data"}
   {"id": "reminders_store",         ... "current_owner": "python" -> "rust" ...}
   {"id": "skills_store",            ... "current_owner": "python" -> "rust" ...}
   {"id": "project_metadata_store",  ... "current_owner": "python" -> "rust" ...}
```

…plus the validator, the two tests and the evidence file from the list above. The header's
`current_production_authority` stays `python` **by design**: the per-domain field is what
moves, and the overall authority follows only when the default runtime stops being Python.

## Rollback

Per ADR-0049, a rollback after a data-owner cutover is a **controlled ownership transfer**,
not a revert: the reverse revision has to fence the store, prove the native writer has
stopped, and hand the pen back with the Python side's gate removed in the same change that
moves `current_owner` back.
