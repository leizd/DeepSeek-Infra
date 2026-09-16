# The workspace mutation gate (slice A1)

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


Status: **ported and byte-verified; not wired.**

This is the prerequisite the data-layer measurement identified. It came before any
memory/reminder branch because every write to those domains is wrapped in it.

## Why it is not a mutex

`mutation_scope` does three separate things, and only the first is a lock:

1. **`assert_mutation_allowed`** — refuse while a restore owns the workspace
   (`423`, `"Workspace writes are fenced while restore is in progress"`). Checked
   **twice**: once to fail fast, once under the lock to close the race with a fence
   created in between.
2. **`exclusive_gate`** — an exclusive **OS** lock on
   `.workspace-mutation.lock`, so writers serialize across the HTTP server, CLI
   processes and workers for the whole mutation.
3. **`bump_generation`** — advance a durable counter **before and after** the
   mutation, with `flush` + `fsync` + `replace` + directory fsync.

The source states why step 3 is doubled: if a process dies mid-write, a concurrent
backup must still observe a changed generation rather than accept a package
assembled across that crash boundary.

The lock and the fence are deliberately separate. A process crash releases the OS
lock, but ordinary mutations stay blocked until restore recovery reconciles the
recorded transaction — which is what makes the fence durable rather than merely
mutual exclusion.

## Shape differences from the oracle, with reasons

| Oracle | This port | Why |
| --- | --- | --- |
| `root` defaults to `config.ROOT` | `root: &Path` on every function | avoids a hidden global |
| `msvcrt.locking(fd, LK_LOCK, 1)` | `LockFileEx` with the same ten-attempts-one-second-apart policy | `LK_LOCK` is a retry loop over `LockFileEx`; using `LockFileEx` without `LOCKFILE_FAIL_IMMEDIATELY` would block **forever** where the oracle raises |
| `fcntl.flock(LOCK_EX)` | `flock(LOCK_EX)` | identical |
| `threading.RLock` + thread-local depth | `Mutex` + thread-local depth | the observable contract — one writer per process, nesting only for the same root — is the same; Rust's `Mutex` is not reentrant, so reentrancy is handled by the depth counter instead |
| `threading.get_ident()` in temp names | a per-thread counter | the name is unlinked right after the durable replace, so only per-thread uniqueness is load-bearing |
| extra crates for locking | hand-written `extern "C"` | this workspace pins dependencies to what is already in `Cargo.lock`, so `libc`/`fs4` would be new crates for two functions |

## Quirks reproduced rather than fixed

- **`fsync_directory` stays best-effort.** It returns silently when the directory
  cannot be opened — the normal outcome on Windows — and swallows a failed fsync.
  Making it a hard error would fail writes where the oracle succeeds.
- **The lock file is created with `b"0"` only if absent**, and an existing one is
  reused.
- **Temp-file cleanup failure is ignored** after a committed replace: the oracle
  notes antivirus or another process may transiently deny the unlink, and that must
  not turn a committed write into a reported failure.
- **The fence message is fixed.** The oracle uses `raise ... from exc`, so the
  parse/IO detail lands in `__cause__` and never in the message; interpolating it
  would change what a caller sees.
- **`write_fence` and `bump_generation` build their temp names differently** —
  `write_fence` preserves the target's suffix, `bump_generation` drops it. Both are
  reproduced as written.

## Errors: three shapes, not one

The oracle raises `AppError` for the refusals a caller can expect (423 fenced, 423
unreadable fence, 409 foreign fence), a bare `RuntimeError` for misuse (nesting a
different root), and `OSError` for a failed read-back. `GateKind` carries which,
and **`code`/`status` are `Option`** — a `RuntimeError` has neither, and inventing
`internal`/500 would let a caller read a programming error as a routine refusal.
That was my first draft's mistake; the probe caught it.

## The two probe bugs this slice exposed

1. **`ast.get_source_segment` drops decorators.** Extracting `exclusive_gate` and
   `mutation_scope` gave bare generators, so they were not context managers. The
   probe now re-attaches `@contextmanager`.
2. **`@contextmanager` is lazy.** Calling `mutation_scope()` and discarding the
   result asserts nothing and bumps nothing — the body only runs on `__enter__`. A
   probe that "called" it would have reported a green tick over no behaviour at all.
   The Rust port acquires eagerly (returning a guard), so the probe must actually
   enter the manager for the two to be comparable.
3. **`_GATE_STATE` is a module-level global**, so the nested-different-root check
   only fires when one module instance sees a foreign root. My probe built a second
   namespace for the "other root", which gave the inner gate its own thread-local
   state and — correctly — no error. The Rust behaviour was right; the probe was
   wrong.

## Evidence

- Byte-level parity: **identical MD5 `57e0ede25273693e03863bffe024aadb`**, 32 keys,
  no differences — paths, generation read/bump/clamp, fence write/read/clear, both
  refusal paths, the scope's double bump, nesting (same root and different root),
  malformed fences, temp-file hygiene, and the lock file's content.
- `cargo test -p deepseek-policy` → 155 tests, all pass (14 new here), including a
  multi-threaded case asserting the generation ends at exactly `scopes × 2`, so the
  process lock really does serialize.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` →
  clean; `cargo fmt` applied.

## What this unblocks, and what it does not

It unblocks the data-layer slices, starting with **B: the reminders pair**
(`create_reminder`, `list_reminders`) — 138 lines, one JSON file, no retrieval, no
RAG. It does **not** wire anything: no branch is registered against it, and
`Branch::is_ported()` is unchanged for every data-layer branch.
