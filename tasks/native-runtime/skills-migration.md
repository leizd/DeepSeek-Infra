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
authentication as the rest of `/api`, serves **22** of the 52 actions: `list`,
`builtin`, `get`, `export`, `validate`, `create`, `import`, `update`, `enable`,
`disable`, `delete`, `list_packs`, `get_pack`, `export_pack`, `validate_pack`,
`import_pack`, `delete_pack`, `security_review`, `security_review_pack`,
`trust_skill`, `untrust_skill`, `block_skill`. Phases 1 and 2.

The remaining **30** actions answer `501 NATIVE_SKILLS_ACTION_NOT_READY` by name
— `run`, `dry_run`, the four `eval_report`/`*_eval_case` actions,
`security_summary`, the seven `catalog_*`, the seven run-analytics actions, and
the nine version-diff/rollback/upgrade actions. They are listed in
`skills_routes.rs::ACTION_NOT_MIGRATED` with the phase that closes each. They are
**not** allowed to answer the oracle's own `400 Unsupported Skill action`: in the
5.0 topology no Python process serves them either, so that message would blame
the request for a migration that has not happened. An action nobody serves still
gets the oracle's 400, so unknown input keeps parity.
`POST /api/skills/{skill_id}/run` is registered for the same reason — otherwise
the path falls through to the Go control proxy, which owns no part of this
surface.

The split is measured rather than counted by hand: the 22 match arms and the 30
names in `ACTION_NOT_MIGRATED` are exactly the 52 `if action == …` branches in
`deepseek_infra/web/routes/skills.py`, with no overlap and nothing unaccounted
for.

### Present but not wired

`deepseek-policy/src/skills/` also carries `runner`, `templates`, `analytics`,
`catalog`, `project_integration`, `evidence`, `trace` and `media` (phases 3-5).
They compile and are reachable from the crate, but **no route reaches them**;
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
`security._append_review` and `security._write_trust_store`. Without it the
handover was asymmetric — Rust writable, Python still writing — which is the
dual write ADR-0049 forbids and which is most dangerous under
`DEEPSEEK_LEGACY_PYTHON=1`.

The scope covers `.skills/custom`, `.skills/packs`, `.skills/disabled.json`,
`.skills/history` and `.skills/security`. The run log (`.skills/runs`), the
catalog (`.skills/catalog`) and the eval-case file (`.skills/eval_cases.jsonl`)
share the directory as child stores and are **not** gated: Rust writes none of
them, so refusing them would take away a store nobody has taken over. Rust's
`Registry` writes exactly the scoped set and nothing else.

Writes are refused (`409 NATIVE_SKILLS_WRITE_NOT_OWNED`) unless the deployment is
`DEEPSEEK_RUNTIME_MODE=python_disabled` **and** the domain is declared.

### Verification

- `tasks/native-runtime/skills_parity_probe.py --rust-example …` → **PASS**,
  312 cases, `differences: []`, against the Python oracle. The 16 new
  `media_context` cases drive the real `skills::media::context` through both
  roots and cover: absent/empty/non-list ids, the single-`mediaId` form, a
  cross-project media, ids whose rows the oracle refuses to load (unresolvable
  `type`, absolute `path`, empty `mediaId`), rows that do not exist, non-string
  list elements (`7`, `{"a": 1}`), CJK titles and segment text, secret redaction
  inside segment text, the 1600-character segment cut, the 12-media and
  12-segment caps, and the 24 000-character context budget.
- The probe was shown **able to fail**: setting `MAX_SEGMENT_CHARS` to 1601
  produced `FAIL, 312 cases, differences: 3`, naming the truncation cases.
- `cargo test -p deepseek-gateway --test skills_routes` — one test driving the
  real production router: 401 without a credential, reads and validation, the
  `409` refusal before cutover, the write path after it, and the three new
  refusal assertions (a not-migrated action, the `run` path, unknown input).
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
registered route nor a green probe is migration evidence.

1. Phases 3-5: wire `catalog_*`, then `run`/`dry_run` (the runner cluster,
   including `media`), then the run analytics and the version
   diff/rollback/upgrade gates — each as its own verified slice, each removing
   entries from `ACTION_NOT_MIGRATED`.
2. Extend the probe with the runner, catalog and analytics surfaces the way
   `media_context` was added, so each leaves a case set that is able to fail.
3. Phase 6: `/api/skills/{skill_id}/run`'s real body, and the oracle comparison
   for the full 52.
