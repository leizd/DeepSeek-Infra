# Native skills endpoint migration

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

Scope confirmed by the user on 2026-09-22: all 52 actions in
`deepseek_infra/web/routes/skills.py`, plus `/api/skills/{skill_id}/run`.
Python is an offline behavior oracle, never a native-route fallback.

Implementation sequence:

1. Schema normalization, instance validation, built-in/custom registry reads.
2. Security reviews and version snapshots; registry writes and skill packs.
3. Run analytics, catalog installation and project integration.
4. Offline and online runners, artifacts, permissions, traces and media context.
5. Evaluation cases/reports, version diffs, rollback and upgrade gates.
6. Register every action on the authenticated native edge; oracle comparisons,
   real production-router tests, native quality gates and packaging.

Every mutating branch must obey native store ownership and workspace fencing.
No phase is complete merely because a handler or crate exists. Progress and
remaining gaps will be recorded here as implementation is verified.

Baseline: branch `codex/native-a2a-stream-continuation`, HEAD `05067a89`.
The pre-existing dirty memory-probe/CI/documentation changes are preserved.
Before implementation, `deepseek-policy --lib` passes 529 tests.

## Status

### Wired on the native edge

`POST /api/skills` (`deepseek-gateway/src/skills_routes.rs`), behind the same
authentication as the rest of `/api`, serves **all 52** actions: `list`,
`builtin`, `get`, `export`, `validate`, `create`, `import`, `update`, `enable`,
`disable`, `delete`, `list_packs`, `get_pack`, `export_pack`, `validate_pack`,
`import_pack`, `delete_pack`, `security_review`, `security_review_pack`,
`trust_skill`, `untrust_skill`, `block_skill` (phases 1-2), `run`, `dry_run`,
`list_runs`, `get_run`, the run-analytics family (phases 3-4), all seven
`catalog_*` (phase 3), `security_summary` with the version family (phases 2-5),
the eval case store, and the eval report engine with the four actions whose payload
embeds its verdict (phase 5).

**One branch is dispatched but still refuses**: `run` with an API key returns a precise
`501 NATIVE_SKILLS_ACTION_NOT_READY`, because the model call is not ported. Without a key it
returns the oracle's own `MISSING_API_KEY`. That is what "all 52" means here — every branch
has a native arm, one of them is a named refusal.

Nothing is left in `skills_routes.rs::ACTION_NOT_MIGRATED` — it is an empty array, kept so
the next surface arrives with one. An action nobody serves still gets the oracle's own `400
Unsupported Skill action`, so unknown input keeps parity, and the route test asserts the
list's contents rather than a pinned name.
`POST /api/skills/{skill_id}/run` is registered for the same reason — otherwise
the path falls through to the Go control proxy, which owns no part of this
surface. It is the same dispatcher as the `run` action, with the path id winning.

The split is measured rather than counted by hand: the 52 match arms are exactly the 52
`if action == …` branches in `deepseek_infra/web/routes/skills.py`, with no overlap and
nothing unaccounted for.

### The eval case store (phase 5)

`deepseek-policy/src/skills/eval.rs` is a **new module**, and only the case half of
`eval.py` is in it: `normalize_eval_case`, `load_case_file` / `load_eval_cases`,
`save_eval_case` and `delete_eval_case`, over the golden corpus at
`evals/golden/skills/skill_eval_cases.jsonl` and `.skills/eval_cases.jsonl`. The
report half is deliberately absent — see the refusals below.

`list_eval_cases` reads; `create_eval_case` and `delete_eval_case` write, so they
join the `skills_store` gate. Three details are contract rather than style:
`save` always ends the file with a newline and **`delete` does not** once it
empties it; both write `json.dumps(..., sort_keys=True)`, so a record's key order
is sorted rather than the order it was built in; and `normalize_eval_case`'s
`"source": str(data.get("source") or "golden")` is **not** stripped — the probe
caught this port trimming it.

Taking the case file over was also the **last** third-place change: it was the last
child store under `.skills/` outside `registry.skill_store_scope()`, so that scope
now covers the whole directory tree Python used to own.

### The security overview and the version family (phases 2-5)

`security_summary` and the version reads write nothing; `rollback_skill` and
`rollback_pack` join the `skills_store` gate, because they rewrite the item, take
a history checkpoint and write a revision (and a pack rollback can change a
project binding too).

`_from_version` / `_to_version` default to `current` **after** the strip, and
`_version` is `version` else `revisionId` — a missing version is the route's own
`400 "version is required"`, not the store's, because the store gate has already
passed by then.

**Five actions stay refused, and it is one dependency, not five ports.** The
oracle's `_score_diff` → `eval_aware_upgrade_gate` →
`skills.eval.build_skill_eval_report` chain means the two version diffs carry an
`evalScoreDiff` and `upgrade_pack` / `eval_upgrade_gate` an `evalAwareUpgradeGate`.
Substituting a placeholder was rejected: `null` where the oracle answers a verdict
is a silent behaviour difference, not a refusal.

### The eval engine (phase 5)

That dependency has two halves, and the larger one is now ported.

`_run_case` is the **execution** half: `run_skill(..., offline=True, persist=True)`, a
real project per case when the case needs one, the media fixture, and five metrics.
**The execution half is now ported too**: `run_case` (a project per case when the case needs
one, `runner::offline` with `persist=true`, the five metrics), `report` (the corpus, one
`run_case` each, then the assembly) and `upgrade_gate_for` / `score_diff`. The five actions
are **writers, not reads** — every case persists a run and may create its own eval project —
so they carry both gates.

**The media fixture, and the mistake the probe caught.** Six built-in Skills
(`audio_transcript_summarizer`, `image_explainer`, `media_to_report`, `pdf_reader`,
`video_brief_generator`, `webpage_summarizer`) ship an `exampleInputs` entry that references
`media_example`, so a `scope: "all"` report walks six cases whose input needs the
media-ingestion pipeline. The first version refused the **whole report**; the probe caught it
on the spot, and per-case is the right granularity — the refusal is recorded as that case's
failure, the case is not run at all, and the report completes. `scope: "all"` is therefore
not byte-comparable and the probe drives the per-Skill and per-Pack scopes instead,
naming the six in a comment.

Three other things were outside the port, and one turned out to be a binding:

- `permissions.evaluate_skill_tool` — **done**, in `skills::permissions`. The Tool Policy
  Engine it binds to (`ToolPolicy`, `ToolPolicyConfig`, `evaluate`, the metadata table and
  its guards) was **already ported** in `tool_policy.rs`; the earlier "no Rust port exists"
  was a reading error — I checked the Python class's size without grepping the crate for
  the type. The port evaluates a Skill's grant with an empty argument object, as the oracle
  does, and **one divergence is a side effect**: the oracle's engine defaults to
  `audit=True` and writes an audit entry per evaluation; this one uses the crate's no-op
  sink and writes nothing.
- media **ingestion** (`register_from_payload` + processing) — needed only by a case whose
  input references the `media_example` fixture; the plan is a precise refusal for that case.
- Python's `re` for a case's `forbidden` patterns — `content_pass` uses the `regex` crate and
  refuses a pattern that cannot compile rather than treating it as a non-match.

Everything else is a function of `case_results` and is ported into
`deepseek-policy/src/skills/eval.rs`: `_json_path`, `_artifact_pass`, `_content_pass`,
`_sample_input`, `_synthetic_case`, `_selected_skill_ids`, `_pack_membership`,
`_cases_for_skills`, `_dedupe_case_results`, `_ratio`, `_aggregate_result`,
`_skill_results`, `_pack_results`, `_compare_item`, `compare_reports`, the assembly as
`report_from_results`, and the gate extraction as `upgrade_gate`.

Three of the report's own fields cannot match and are recorded divergences:
`environment.python` (the oracle's interpreter; the port reports an empty one),
`commit` (the oracle reads a CI source context and falls back to
`git rev-parse --short=12 HEAD`; the port reports an **empty** commit and spawns nothing —
see below), and `metrics.latencyMs` (wall-clock).

**The `commit` spawn is why the cutover's own gate failed.** `python
scripts/check_zero_python_runtime.py` — the executable Zero-Python release gate — returned
7/8 with `process_tree_isolation: rust\crates\deepseek-policy\src\skills\eval.rs spawns
python`. Its rule is a heuristic: a production `.rs` file containing both `command::new` and
a quoted `"python"` token. This file had both — a `Command::new("git")` in `git_commit`, and
the report's `environment` block carrying the field name `"python"`. **The gate was not
weakened**: the subprocess came out instead (the crate is a policy library and a report
stamp is not worth a process), the report's field shape is unchanged, and the gate is 8/8
again. That is the one mechanical thing the skills surface owed the cutover.

**A finding worth its own look**: `Registry::list` does not sort, while the oracle's
`list_skills` sorts by `(bool(builtin) is False, `name`)`. The catalog never noticed
because it sorts its own items; the eval selection applies the sort locally. Every
other caller of `list` whose order is observable has the same gap.


### The run journal (phases 3-4)

`list_runs`, `get_run`, `export_runs` and `analytics_summary` read the journal;
`delete_run`, `cleanup_runs` and `redact_run` write it. The writes join the same
`skills_store` gate `run` already sits behind, because it is the same store: the
offline `run` appends to `.skills/runs/runs.jsonl`. The reads answer without it.

`_limit` is the shape to watch here — `int(str(raw))`, the default when that
fails, clamped to 0..=1000, with a different default per key (`limit` 50,
`keepRecent` 0, `days` 7). A JSON float or boolean takes the default, because
`int("5.0")` and `int("True")` both raise in Python. `analytics_summary`'s window
is `_limit(payload, "days", 7) or 7`, so a zero falls back to a week and the trend
is clamped to 1..=30 days.


### The catalog (phase 3)

All seven catalog actions are wired. Only two of them write, and they write
**different stores**, so they carry different refusals rather than one blanket
gate:

- `catalog_refresh` rewrites `.skills/catalog/catalog.json` — the skills store,
  `409 NATIVE_SKILLS_WRITE_NOT_OWNED`.
- `catalog_install` without `dryRun`/`preview`, and `catalog_uninstall` — a
  project's Skill binding in `project.json`,
  `409 NATIVE_PROJECT_METADATA_WRITE_NOT_OWNED`.

A `dryRun` install returns before the oracle writes anything, so that branch stays
available while Python still owns `project.json`; gating the whole action would
have removed a working preview. The five reads write nothing.

The gateway's `item_id` mirrors `_item_id` — one `or` chain over `itemId`,
`skillId`, `packId`, `id`, so a whitespace-only `itemId` is *chosen* and then
strips to empty rather than falling through to `skillId`.

`catalog::refresh` renders the manifest through `python_json::OrderedJson` rather
than `serde_json`'s printer: the latter keeps insertion order only when the
`preserve_order` feature is on, which a single-crate build does not enable. The
review's own `manifest` block has **two shapes at one name and one path** — a skill
review carries `skillId` and puts `packId` after `toolGrantHash`, a pack review
carries `packId` fourth — which no by-name table can express, so `python_json`
gained `OrderedJson::from_value_with_orders_and_shapes`, selecting an order by
which key the object carries. The name-keyed entry points are unchanged.


### The runner and run analytics (phases 3-4)

`run` uses `runner::offline` when the request asks for it, and otherwise prepares
the run and finishes it with `MISSING_API_KEY` when neither the request nor
`DEEPSEEK_API_KEY` supplies a key — the oracle's behaviour, including the
prepared-failure record it persists. With a key available the branch answers
`501 NATIVE_SKILLS_ACTION_NOT_READY` by name: the model call is not ported, and a
`200` that quietly did nothing would be worse than a refusal.

`dry_run` writes nothing; `list_runs` and `get_run` read the journal. Only
mutations and `run` sit behind the store gate (`409
NATIVE_SKILLS_WRITE_NOT_OWNED` unless `DEEPSEEK_RUNTIME_MODE=python_disabled`),
because those are the branches that write.

This block was measured, not assumed. Three defects were in the tree when the
slice was picked up: the test file had a **cross-test environment leak** that made
the `409` assertion order-dependent (fixed with the repo's `EnvLock`/`EnvGuard`
convention, and verified in three run orders), a dead `action_not_ready` that
`-D warnings` rejects, and a `clippy::format_collect` failure in
`file_upload.rs`. Separately, fifty files in these two crates had been formatted
with rustfmt's 2021 item ordering while CI pins 1.85.0's rustfmt (2024 ordering);
they were re-formatted with the pinned toolchain, after which 23 of them matched
`HEAD` byte for byte. See `continuation.md` for the commands and the evidence.

### Present but not wired

`deepseek-policy/src/skills/` carries the phase 3-5 modules. `runner` and
`analytics` now have production callers (the `run`/`dry_run`/`list_runs`/`get_run`
branches above). `templates`, `catalog`, `project_integration`, `evidence` and
`trace` compile and are reachable from the crate, but **no route reaches them**;
they are inert groundwork, not delivered behavior.

`media.rs` is the one module written during this slice rather than before it: the
pre-existing `mod.rs` declared `pub mod media;` and `runner.rs` called it, so the
crate did not compile — and `rustfmt` could not descend into `src/skills/` at
all, which is why the formatting of all twelve modules was still unverified. It
ports `runner.py:_media_context` plus the two readers it calls —
`media.library.get_media` / `list_segments` and the record/segment normalization
in `media/schema.py` — **for the ten fields that consumer reads**. A record's
`path` and a segment's `framePath` are still *evaluated* because their
normalization decides whether the oracle keeps the row; everything else
`normalize_media_record` returns (`mimeType`, `source`, `metadata`, the
timestamps, a segment's `confidence`/`page`/`timeRange`/`framePath`) is
deliberately not produced. Finishing it belongs with the media slice, and the
rule `projects::delete_project` already states applies: say what was not done.

### Ownership

`skills_store` is declared python -> rust at 4.9.4 in
`release/native_runtime_ownership_v1.json`, is in the gateway's
`DECLARED_NATIVE_DATA_DOMAINS`, and — the part that was missing — is in Python's
`authority.RUST_DATA_DOMAINS`, gated at
`skills.registry.skill_store_scope()`. That scope is the single Python choke
point for the capability registry: `write_disabled_skill_ids`,
`write_custom_skill`, `write_custom_pack`, `delete_skill`, `delete_pack`,
`versioning.snapshot_skill`, `versioning.snapshot_pack`,
`security._append_review`, `security._write_trust_store` and
`catalog.catalog_refresh`. Without it the handover was asymmetric — Rust
writable, Python still writing — which is the dual write ADR-0049 forbids and
which is most dangerous under `DEEPSEEK_LEGACY_PYTHON=1`.

The scope covers `.skills/custom`, `.skills/packs`, `.skills/disabled.json`,
`.skills/history`, `.skills/security`, `.skills/catalog`, `.skills/runs` and
`.skills/eval_cases.jsonl` — **every** child store under `.skills/`. Three of them
joined later, each when Rust started writing them: the catalog at
`catalog::refresh`, the run log when the offline `run` and then the run-analytics
writers made it Rust's, and the case file at `skills::eval`. The scope's own rule
is that a store Rust writes is one Python must stop writing — before each of those
slices the store was listed as deliberately outside "because Rust writes none of
them", and each time the same sentence supplied the opposite conclusion. The run
log's writer is `analytics._write_runs`, the single choke point `append_run` and
the three analytics writes all pass through; the case file's are
`eval.save_eval_case` and `eval.delete_eval_case`.

`project.json` is a **second** domain (`project_metadata_store`), declared at the
same cutover; the catalog changes a project's Skill binding, so it is gated there
too, and Python's half already exists at
`infra/data/projects.py::write_project`.

Writes are refused (`409 NATIVE_SKILLS_WRITE_NOT_OWNED`, or
`NATIVE_PROJECT_METADATA_WRITE_NOT_OWNED` for the project binding) unless the
deployment is `DEEPSEEK_RUNTIME_MODE=python_disabled` **and** the domain is
declared.

`tests/test_skill_registry_failure_paths_332.py` pins both directions: the
handed-over paths raise `PythonWriterMechanicallyDeniedError` under
`python_disabled`, and the ones Rust does not write keep working — with
`catalog_refresh` moved from the second group to the first in the catalog slice.

### Verification

- `tasks/native-runtime/skills_parity_probe.py --rust-example …` → **PASS**,
  **523 cases**, `differences: []`, against the Python oracle. The
  `media_context` cases drive the real `skills::media::context` through both
  roots and cover: absent/empty/non-list ids, the single-`mediaId` form, a
  cross-project media, ids whose rows the oracle refuses to load (unresolvable
  `type`, absolute `path`, empty `mediaId`), rows that do not exist, non-string
  list elements (`7`, `{"a": 1}`), CJK titles and segment text, secret redaction
  inside segment text, the 1600-character segment cut, the 12-media and
  12-segment caps, and the 24 000-character context budget.
- **The runner and the run journal, added 2026-09-26.** `offline_output` renders
  each of the 18 built-in skills' own `exampleInputs` entry through
  `runner::prepare().offline_output()` and compares the whole payload with the
  oracle's `_offline_output` — including the media half of the context, composed
  the way `run_skill` composes it. `offline_refusal` drives the entry the route
  calls with inputs the schema rejects, with four ids no registry holds, and with
  non-object inputs, so the refusal **message** is compared and not just its
  status. `list_runs` and `get_run` read a **fixture journal** written byte-identically
  into both roots (a `runId`-alias record, a record `normalize_run` rejects and
  `_read_runs` must skip, a redacted run, and runs differing on every filter axis),
  across eleven filter/limit shapes and six id shapes. A file name is not a skill
  id (`code_review.json` declares `skill_code_review`), so the corpus is built from
  the built-in documents rather than from their names.
- The probe was shown **able to fail**: with `MAX_SEGMENT_CHARS` at 1601 it
  produced `FAIL, 312 cases, differences: 3` naming the truncation cases; and
  renaming `dry_run`'s `skillRunId` produced `FAIL, 391 cases, differences: 18` —
  exactly the 18 successful dry runs and nothing else. The file was restored
  byte-identically (md5 unchanged).
- **The catalog, added 2026-09-26.** Both roots hold the same `evals/reports/*`
  and one project, so `evalScore` and `installCount` are non-zero — a comparison
  over an empty repository would agree on `0.0` and prove nothing. Covered: the
  whole manifest, `catalog_get` across seven id shapes (including the
  whitespace-only `itemId` that is chosen and then strips to empty), fourteen
  search filter shapes, previews, `dryRun` installs, real installs and uninstalls
  of both a skill and a pack, and `catalog_refresh` — whose **file** is compared
  byte-for-byte with the oracle's, because a JSON parse is order-insensitive and
  the response comparison alone would have missed every key-order constant.
- **The run journal, added 2026-09-27.** `delete_run` and `redact_run` across four id
  shapes each (including the whitespace-padded one and the empty one, which the
  policy layer treats as "nothing to do" while the route refuses it), eight
  `cleanup_runs` filter/`keepRecent` shapes over the journal fixture, and seven
  `analytics_summary` scope/window shapes. Those cases **write** the fixture the
  earlier reads use, so they run last and both sides replay the same order. The
  trend's dates needed an anchor: `_recent_trend` calls `datetime.now(timezone.utc)`
  directly, so the probe patches `analytics.datetime` rather than only
  `utc_now_iso`.
- **The security overview and the version family, added 2026-09-27.** Both roots get a custom
  Skill and a custom Pack with two revisions each, written by the oracle and copied
  byte-for-byte, so the family is compared on how each side *reads* a revision rather than on
  what it writes. Covered: five `security_summary` scopes (including the empty one, which the
  route resolves to `all`), `list_versions` / `list_pack_versions` across built-in, custom,
  missing and blank ids, seven `migration_plan` from/to pairs, and six `rollback_skill` /
  `rollback_pack` shapes each — including a built-in's `403`, a missing revision and an empty
  version. The rollbacks **write** the fixture the reads use, so they run last.
- **Eight real divergences were found by these comparisons and fixed.** Two in
  `dry_run` (an extra `skillVersion` key; a payload contract that accepted a
  `{"skillId": …}`-only request the oracle refuses), two in `catalog` — see the next
  bullet — plus one only the file check could see, one in `analytics_summary`, and two in the
  version family: the pack history directory was built as one path component with a `/` inside
  (rendering `history/packs\…` where the oracle renders `history\packs\…`, and that string
  reaches the response as a revision `path`), and the rollback checkpoint's sentence was
  capitalised; the eighth is in the eval case store, where this port trimmed
  `normalize_eval_case`'s `source` and the oracle does not.

- **The eval case store, added 2026-09-27.** Thirteen `normalize_eval_case` shapes — every
  alias pair, the `deniedTool` → `[deniedTool]` promotion, string/list/other inputs, a
  numeric and a dict-valued `caseId` — then a listing, then eight `create_eval_case` shapes
  (including an overwrite, both missing ids, an unknown Skill and a replacement by `id`),
  then a listing again, then five `delete_eval_case` shapes. The writes mutate the file the
  listings read, so both sides replay the same order.
- **The catalog divergences.** `maxRiskScore` / `minEvalScore` were read through
  `text()`, whose falsy check swallows `0`, so `{"maxRiskScore": 0}` was not a
  filter at all: the oracle answered **5** items, the port **22**. Fixed with a
  `filter_number` helper mirroring the oracle's `float(str(value))`; the same pass
  found `tool` checked for truthiness before being trimmed, where the oracle trims
  first. And the review `manifest` key-order split described under “The catalog”
  above, which only the file comparison could see.
- **The `analytics_summary` divergence.** `averageLatencyMs` was `0.0` where the
  oracle writes an int `0` — `round(statistics.fmean(latencies), 2) if latencies
  else 0` returns an int when nothing completed, and `0` and `0.0` are not the same
  bytes on the wire.
- `cargo test -p deepseek-gateway --test skills_routes` — **6 tests** driving the
  real production router **and** an isolated on-disk registry: 401 without a
  credential, reads and validation, the `409` refusal before cutover, the write
  path after it, a not-migrated action and unknown input refused, and — from the
  runner slice — an offline `run` whose journal record is readable back through
  `list_runs`/`get_run`, a `dry_run` that leaves the journal bytes unchanged **and
  whose payload contract is the oracle's** (a `{"skillId": …}`-only payload is the
  oracle's `400 "Skill config missing required fields: …"`; a config payload is
  `200` with `dryRun: true`), and a run refused with `409` that writes no journal
  and no trace; the third covers the catalog — its five reads, both of its
  refusals, and the refresh write under `python_disabled`; the fourth covers the
  run journal — its three writers refusing while its four readers answer, then
  `redact_run`, a `keepRecent` cleanup, an idempotent delete and a summary over the
  emptied log; and the fifth covers `security_summary`'s scope default together
  with the version family — a built-in's `403`, the route's own `version is
  required`, `revisionId` as the other spelling, and a custom Skill's revision
  count before and after a rollback; the sixth covers the eval case store — its
  golden-first listing, both refusals before cutover, the bare create form, both
  required ids, an unknown Skill, and the empty file with no trailing newline. All
  six are independent of execution order
  (see `continuation.md`). The by-name `501` assertion checks its action against
  the exported `ACTION_NOT_MIGRATED` first, so implementing that action fails at
  the guard with the reason instead of silently weakening the assertion.
- `pytest tests/test_skill_registry_failure_paths_332.py` (8) and the nine
  neighbouring skills suites (95 in total) pass;
  `test_every_skill_registry_write_path_is_denied_once_python_is_de_authorized`
  is the new one.
- `cargo fmt --all --check` clean (it could not check `src/skills/` before),
  `ruff check` and `mypy` clean on every changed Python file.

### Known divergences, measured and recorded

- A segment file whose `index` or `page` is not an integer raises a raw
  `ValueError` in Python, which `list_segments` does not catch, so the whole
  skill run aborts with a 500. This port answers `invalid_payload` (400) for
  those two cases. `save_segments` normalizes `index` through the same `int()`,
  so a store the application wrote cannot contain one.
- A segment with no `segmentId` gets a fresh id on both sides, so its locator is
  not reproducible and the probe cannot carry such a fixture. `save_segments`
  always writes one.
- `py_strip` also removes `\x1c`-`\x1f`, which Rust's `is_whitespace` does not
  cover; `posix_suffix` reproduces Python's `rfind('.')` rule (`.pdf` and `x.`
  have no suffix), reachable only from a hand-written name.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run. Production HTTP is still Python and
`release/native_runtime_5_0_evidence_v1.json` remains `NOT_READY`. Neither a
registered route nor a green probe nor a green clippy is migration evidence. The
whole migration is **未完成**, and skills are **集成通过**, not **完成切换**.

1. **Six oracle comparisons are in place** (523 cases, above) and closed eight
   divergences between them. What they still do not cover is the **online** `run`
   — the model call is not ported, so that branch stays
   `501 NATIVE_SKILLS_ACTION_NOT_READY` by name. The gateway already has the pieces
   for it (`chat_execution::exchange_turn`, `tool_rounds::decide_round`).
2. The **eval engine** — `eval_report`, and the four actions whose payload embeds
   its verdict (`diff_versions`, `diff_pack_versions`, `upgrade_pack`,
   `eval_upgrade_gate`). `skills.eval.build_skill_eval_report` executes each case
   through the runner and scores the result; its response also carries Python's own
   identity (`environment.python`, `commit`), which a port has to record as a known
   divergence rather than compare.
3. Phase 6: the oracle comparison for the full 52, native quality gates and
   packaging.

Each is its own verified slice, each removing entries from `ACTION_NOT_MIGRATED`.
