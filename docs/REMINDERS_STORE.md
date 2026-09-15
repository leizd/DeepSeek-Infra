# The reminders store and its branches (slice B)

Status: **ported and byte-verified; not wired.**

The smallest data-layer domain: one JSON file, no retrieval, no RAG. It is the
first slice that exercises the mutation gate from slice A1, and it is where the
gate finally pays for itself.

## Storage

`<root>/.reminders/reminders.json`, written as Python's
`json.dumps(..., ensure_ascii=False, indent=2)` with the record's key order
preserved. Every write is wrapped in `mutation_scope`, so a reminder write
participates in the backup-consistency protocol rather than being a plain file
write — the probe records the generation advancing by **two per create** as
evidence.

### Two quirks reproduced deliberately

- The temp file is `REMINDERS_FILE.with_suffix(".tmp")`, which **replaces** the
  suffix: `reminders.json` becomes `reminders.tmp`, not `reminders.json.tmp`. Two
  writers racing the same directory therefore collide on one temp name. That is the
  oracle's behaviour; "fixing" it would change which file a crash can leave behind.
- Reads are silent. A missing file, an unreadable file, malformed JSON or a wrong
  top-level type all degrade to an empty list, and non-dict entries are dropped.
  Corruption becomes "no reminders", never an error.

### Key order is part of the contract

`serde_json` is compiled here without `preserve_order`, so a `Value`'s object keys
iterate **sorted**. Python dicts iterate in insertion order and the store file
carries that order, so writing from a plain `Value` would produce different bytes
for equivalent JSON. Because this repository's subject is a backup system, file
bytes are part of the contract — hence
[`OrderedJson`](../rust/crates/deepseek-policy/src/python_json.rs), which takes an
explicit key order. A test pins the exact expected file text.

## Date handling

`parse_due_at` reproduces the subset of `datetime.fromisoformat` this module meets,
plus Python's `isoformat()` rendering. Both are pinned by measurement rather than
by reading the docs:

**Accepted:** `YYYY-MM-DD`, `YYYYMMDD`, `YYYY-Www-D`, `T`/`t`/space separator,
`HH`, `HH:MM`, `HH:MM:SS`, `HH:MM:SS.ffffff`, compact time `HHMMSS`, and a trailing
`Z`, `+HH`, `+HH:MM` or `+HHMM` offset. A naive value is taken as UTC, a fraction
is padded to six digits (`.1` → `.100000`), and the output omits microseconds when
they are zero.

**Rejected:** a lowercase `z` (only an uppercase trailing `Z` is rewritten, and the
parser then refuses the original), hour 25, February 30, and anything else — all
raising the oracle's own two messages, `Reminder dueAt is required` for a blank
value and `Reminder dueAt must be an ISO datetime` otherwise.

Anything outside that set raises rather than being guessed at, so an unported form
fails visibly instead of parsing wrongly.

## The seventh "guessed instead of measured"

My first ISO-week implementation validated the week by checking that the resulting
date's **year** matched the stated year. That is wrong in both directions, and
`date.fromisocalendar` settled it:

| Input | Oracle | My first version |
| --- | --- | --- |
| `2026-W01-1` | `2025-12-29` — week 1 starts in December | rejected |
| `2026-W53-1` | `2026-12-28` — 2026 has 53 ISO weeks | rejected |
| `2025-W53-1` | error — 2025 has 52 weeks | (would have accepted) |

The rule is: validate the week against *how many ISO weeks that year actually
has*, computed from the distance between consecutive week-1 Mondays. My unit test
asserted the wrong expectation too, and now carries the measured values with a note
saying so.

## Evidence

- Byte-level parity: **identical MD5 `a636cd4590cff26d7809e8866fb3e500`**, 74 keys,
  no differences — 34 date forms, 8 create shapes, the exact store bytes, the
  generation counter and lock file, the temp-file hygiene, 8 status variants across
  two store states, 5 tolerant-read shapes, and the delete outcomes.
- `cargo test -p deepseek-policy` → 171 tests, all pass (16 new here).
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` →
  clean; `cargo fmt` applied.

## Non-determinism is injected, not hidden

`secrets.token_hex(8)` and `int(time.time() * 1000)` are the store's only
non-deterministic inputs, and they arrive through an `Entropy` trait. The
production implementation uses the OS CSPRNG (`BCryptGenRandom` on Windows,
`/dev/urandom` on Unix) and **fails loudly rather than falling back** to a weaker
source — `secrets` is explicitly the secure option, and a reminder id is handed to
the model in tool output. The probe pins both with a counter and a frozen clock, so
`id` and `createdAt` are comparable without weakening production behaviour.

## What this does not do

It does not register the branches. `Branch::is_ported()` is unchanged, nothing is
wired, and the round loop stays blocked. `due_reminders` is ported and unit-tested
but is **not** in the compared corpus, because calling it mutates the store after
the probe's last observation; it needs its own probe pass.
