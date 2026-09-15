# The data layer behind the remaining branches — measurement

Scope: the seven dispatch branches that read or write local data —
`suggest_memory`, `recall_memory`, `forget_memory`, `create_reminder`,
`list_reminders`, `list_project_files`, `read_file_chunk`.

This is a **measurement**, not a plan that has been executed. Everything below is
read off the source, with line counts and file paths so it can be re-checked.

---

## 1. Storage media

| Domain | Medium | Layout |
| --- | --- | --- |
| memory | one JSON array file | `MEMORY_DIR/memories.json`, plus `MEMORY_DIR/memories.lock` |
| reminders | one JSON array file | `REMINDERS_DIR/reminders.json` |
| projects | **a directory per project** | `PROJECTS_DIR/<id>/project.json` (+ project files) |

Written as `json.dumps(..., ensure_ascii=False, indent=2)` — so indentation is part
of the on-disk contract, not cosmetic.

Durability differs between the two JSON stores, and the difference is a bug-shaped
detail worth reproducing rather than "fixing":

- **memory** writes `<name>.<pid>.tmp` then `replace`.
- **reminders** writes `REMINDERS_FILE.with_suffix(".tmp")` → for
  `reminders.json` that is `reminders.tmp` — `with_suffix` **replaces** the
  suffix, it does not append. A second writer racing the same domain therefore
  collides on the same temp name.

Reads are uniformly **tolerant**: missing file, unreadable file, malformed JSON,
or a wrong top-level type all degrade to "empty", and non-dict entries are dropped.
Corruption is silent by construction.

## 2. Schemas

### memory — canonical shape after a write

```
{id, memoryId, content, category, type, scope, source, confidence,
 pinned, createdAt, updatedAt, expiresAt?}
```

Read path sorts by `(pinned, updatedAt || createdAt)` descending, then truncates to
`MEMORY_MAX_ITEMS`.

### reminders

```
{id, title, content, dueAt, createdAt, notified, notifiedAt?}
```

- `id` = `secrets.token_hex(8)`; `createdAt`/`notifiedAt` = milliseconds since epoch.
- `dueAt` = ISO-8601 **UTC**, produced by `datetime.isoformat()` after
  `parse_due_at` normalises `Z` → `+00:00` and attaches UTC to a naive input.
- Create drops already-notified entries, sorts by `dueAt`, keeps the **last**
  `MAX_REMINDERS = 200` (a tail slice, so it keeps the latest due dates).
- List accepts `status ∈ {active, notified, all}`, silently falling back to
  `active`; returns `reminders[:50]` but reports `count` as the **untruncated**
  filtered length — the count and the list length legitimately disagree.

### projects

`project.json` holds `{id, name, documents[], skills, skillRuns, savedItems,
artifacts, updatedAt, …}`; `MAX_PROJECTS = 40`, `MAX_PROJECT_DOCUMENTS = 120`.

## 3. Migration semantics

There is **no schema version field anywhere**. Migration is two mechanisms:

**(a) Normalise-on-write.** `_save_memories_unlocked` doubles as the migration: it
drops non-dicts and empty content, derives `id` from `memoryId → id →
memory_fingerprint(content, scope)` (a **content-addressed** fallback), coerces
`confidence` to a float defaulting to `0.9` and clamped to `[0,1]`, derives
`type` from `type → category → "fact"` lowercased, defaults both timestamps to
now, and truncates to `MEMORY_MAX_ITEMS`. `projects.py` does the same job through
its `normalize_*` family (`normalize_project_name`, `normalize_documents`,
`normalize_project_skills`, `normalize_skill_runs`, `normalize_saved_items`,
`normalize_project_artifacts`).

**(b) Tolerant read.** See §1 — bad data becomes empty data.

So the effective contract is: *the file is repaired on the next write, and until
then the reader silently discards whatever it cannot use*. Any port must keep both
halves, including the content-addressed id fallback, or ids will churn on upgrade.

## 4. The coupling that decides the slicing

Every write to memories or reminders is wrapped in

```python
with mutation_gate.mutation_scope(root=MEMORY_DIR.parent):
```

which is not a mutex. `mutation_scope` does:

1. `assert_mutation_allowed` — refuse while a restore owns the workspace
   (`423`, `"Workspace writes are fenced while restore is in progress"`),
   checked **twice**, before and under the lock, to close the race with a
   newly-created fence;
2. `exclusive_gate(root)` — an exclusive OS lock on
   `.workspace-mutation.lock`;
3. `bump_generation(root)` **before and after** the mutation.

`bump_generation` writes the counter with `flush` + `os.fsync` + `os.replace` and
then **fsyncs the directory**. The comment states the reason: if a process dies
mid-write, a concurrent backup must still observe a changed generation rather than
accept a package assembled across that crash boundary.

**So a memory write is not a file write — it is a participating write in the
backup-consistency protocol.** That is this project's own subsystem:
`deepseek_infra/infra/workspace/` is **76,140 lines**.

The good news from the measurement: `mutation_gate.py` is **251 lines and
self-contained**. Every participating function is defined in the module —
`lock_path`, `fence_path`, `generation_path`, `_fsync_directory`,
`workspace_root_for_path`, `_lock_file`/`_unlock_file`, `exclusive_gate`,
`read_fence`, `assert_mutation_allowed`, `write_fence`, `clear_fence`,
`read_generation`, `bump_generation`, `mutation_scope`. Its only outside
references are `core.config` (for `ROOT`) and `core.errors` plus the stdlib.

Paths, all relative to `(root or config.ROOT).resolve()`:

| File | Role |
| --- | --- |
| `.workspace-mutation.lock` | exclusive OS lock held during a mutation |
| `.workspace-restore-fence.json` | the fence the restore side writes |
| `.workspace-generation` | the counter a backup compares |

Two details a port must copy rather than improve:

- **`_fsync_directory` is best-effort.** It returns silently when `os.open(path,
  O_RDONLY)` raises — which is the normal outcome on Windows — and swallows an
  `fsync` failure too. The directory fsync is therefore POSIX-effective only, and
  a Rust port must stay equally tolerant instead of turning it into a hard error.
- The module also owns the **restore** side (`write_fence`, `clear_fence`) and
  `workspace_root_for_path`, which are not on the data-layer write path. Slice A
  can port the whole module; only the read/assert/lock/bump half is exercised by
  these branches.

## 5. Retrieval coupling (memory, and indirectly projects)

- `core.utils.query_tokens` — bilingual tokenizer: lowercase, collapse whitespace,
  ASCII `[a-z0-9_+-]{2,}` **or** CJK runs of 2+, then every 2-gram of any CJK run
  of 3+, de-duplicated via a set, **sorted by length descending**, capped at 80.
- `core.utils.score_chunk` — `Σ count(token) × max(2, min(len(token), 10))`, plus
  `+2` if the text contains a markdown heading.
- `local_rag` — **2,676 lines** (`local_rag.py` 1,182 + `files.py` 1,494). Memory
  writes sync into it (best-effort, exceptions swallowed); project deletion calls
  `local_rag.delete_items`.

`query_tokens`/`score_chunk` are pure and are **also what `search_files` (the RAG
branch) needs** — one port pays for two branches.

## 6. Remaining couplings, per branch

| Branch | Needs |
| --- | --- |
| `create_reminder`, `list_reminders` | JSON store + fence + ISO datetime + `secrets` |
| `recall_memory`, `forget_memory` | store + fence + **scorer** + fingerprint + scope/category/conflict/sensitive logic |
| `suggest_memory` | store + `build_memory_suggestion` + `memory_suggestion_callback` |
| `list_project_files` | project directory store + `normalize_*` + `project_document_for_tool` |
| `read_file_chunk` | **`load_cached_file` from `rag/files.py:626`** + the file cache |

Also shared: `utc_now_iso`, `latest_user_query`, `datetime.fromisoformat` /
`isoformat`, and cross-platform file locking (`msvcrt` on Windows, `flock`
elsewhere).

## 7. Recommended slicing

Ordered by increasing dependency, with the fence first because it is the real gate
and it unblocks all three domains at once.

| # | Slice | Why here |
| --- | --- | --- |
| **A** | **`mutation_gate`** (251 lines) — fence read/assert, exclusive lock, generation bump with fsync | Self-contained; it is *this project's* subsystem, so it adds no new external dependency; without it no data write is faithful |
| **B** | **reminders pair** (`create_reminder`, `list_reminders`) | Smallest domain: 138 lines, one file, no retrieval, no RAG |
| **C** | **scorer** (`query_tokens`, `score_chunk`, `utc_now_iso`, `latest_user_query`) | Pure, and shared with `search_files` — one port, two branches |
| **D** | **memory triple** (`suggest_memory`, `recall_memory`, `forget_memory`) | Needs B + C, plus the fingerprint / category / conflict / sensitive logic |
| **E** | **projects pair** (`list_project_files`, `read_file_chunk`) | Needs the directory store + `normalize_*` + `load_cached_file`, i.e. `rag/files.py` (1,494 lines). Largest and last |

### The one decision that needs the user

Slice A is a prerequisite, so the write path can either

- **A1 — port the real fence.** ~251 lines, faithful, and it is the project's own
  code. Recommended.
- **A2 — abstract the fence behind a trait with a no-op implementation.** Smaller
  now, but a no-op write path silently skips the generation bump. That is exactly
  the class of failure the fence exists to prevent, so the no-op would have to be
  test-only and the production path non-optional. I would not choose this.

### What stays out

`search_files` and `fetch_url` are unaffected by this measurement; the browser
family and `python_eval` remain blocked on a browser engine and a real sandbox
respectively, and nothing here changes that.

## 8. Honest statement of state

Nothing in this document has been implemented. No line of the data layer exists in
Rust today. The branches remain `Branch::is_ported() == false`, nothing is wired,
and the round loop stays blocked on them.
