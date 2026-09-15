# The retrieval scorer (slice C)

Status: **ported and byte-verified; not wired.**

`core_utils.rs` mirrors four functions from `deepseek_infra/core/utils.py`:
`query_tokens`, `score_chunk`, `utc_now_iso` and `latest_user_query`. They are used
by the memory branches **and** by the RAG `search_files` branch, so this one port
serves two branches — which is why it came before the memory slices.

## A measured defect in the oracle: its retrieval score is non-deterministic

`query_tokens` ends with

```python
return sorted(tokens, key=len, reverse=True)[:80]
```

over a **`set`** of `str`. Python's `sorted` is stable, so tokens of *equal length*
keep the set's iteration order — and a set of strings iterates in an order that
depends on `PYTHONHASHSEED`. When more than 80 tokens survive, **which** 80 are kept
changes from run to run. Measured directly:

```text
100 equal-length tokens, PYTHONHASHSEED=1 -> x70,x17,x04,x11,x87,x92,x35,x65
100 equal-length tokens, PYTHONHASHSEED=2 -> x78,x43,x17,x67,x63,x36,x59,x91
100 equal-length tokens, PYTHONHASHSEED=3 -> x65,x16,x26,x87,x82,x12,x29,x94
```

This is not an internal detail — it changes the score, and the score ranks memories.
A query of 60 three-character tokens followed by 40 two-character tokens, scored
against a text that repeats one of the short tokens ten times:

```text
PYTHONHASHSEED=1 -> 360
PYTHONHASHSEED=5 -> 390
```

The same input, the same code, two different scores, because a heavier-weight token
survived the cap in one run and not the other.

## The port is deterministic, deliberately

`query_tokens` orders by **length descending, then lexicographically**. Ties are
therefore fixed rather than hash-dependent. This is a behaviour change — the oracle
has no single behaviour to preserve here — and it narrows a varying result to one
fixed one. It does not weaken anything, and the alternative is not available:
reproducing CPython's set iteration order is impossible by construction, and the
oracle does not reproduce its own.

The probe is written to match that reality rather than to hide it:

- token lists are compared **sorted**, so the oracle's instability cannot make the
  diff flake;
- inputs where more than 80 tokens survive report **only the count**, because the
  surviving subset itself differs run to run;
- `query_tokens_is_stable_across_calls_where_the_oracle_is_not` pins the determinism
  as a property of this port.

## A signature difference

`utc_now_iso()` reads the clock internally and takes no argument. This port is
`utc_now_iso(epoch_seconds: i64)`, so the clock is supplied by the caller and can be
pinned. The parity probe compares the *rendering* — CPython's
`datetime.fromtimestamp(epoch, utc).isoformat(timespec="seconds")` against
`utc_now_iso(epoch)` — for four epochs, including the epoch itself. `timespec="seconds"`
is why there is no fractional part.

## Detail worth knowing

- **Two length rules, not one.** The tokenizer's character classes need **two or
  more** characters (`[a-z0-9_+-]{2,}` / CJK `{2,}`), while the weight is
  `max(2, min(len, 10))` — so a two-character token weighs 2 and anything of ten or
  more weighs 10. Two small integers that mean different things, both easy to
  conflate.
- **CJK bigrams are additive.** Every two-character window of a run of three or more
  is added on top of the run itself, so `中文测试` yields `中文测试` plus `中文`,
  `文测`, `测试`.
- A `set` dedupes the windows, which is why `"中" * 60` yields exactly **two**
  tokens: the 60-character run, and the single distinct bigram `中中`.
- The heading bonus requires `^#{1,6}\s+`, so `#Title` (no space) and `####### deep`
  (seven hashes) both miss it.

## Evidence

- Byte-level parity: **identical MD5 `2170fb900a543b67163f1417d8af2c15`**, 37 keys,
  no differences — 13 tokenizer inputs, 2 capped inputs, 10 scoring inputs with their
  token lists, 4 epoch renderings, 8 `latest_user_query` payload shapes.
- `cargo test -p deepseek-policy` → 180 tests, all pass (9 new here).
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` →
  clean; `cargo fmt` applied.

Also corrected in this slice: a unit test asserted `query_tokens("Rust   OWNERSHIP")
== ["rust", "ownership"]`, which is the wrong order — `ownership` is longer, so it
comes first. Same mistake class as the other six; the ordering rule is now asserted
explicitly in its own test.

## What this unblocks

**Slice D, the memory triple** (`suggest_memory`, `recall_memory`, `forget_memory`):
the store plus this scorer plus the fingerprint/category/conflict/sensitive logic.
Nothing is wired, and `Branch::is_ported()` is unchanged for every data-layer branch.
