"""Retrieval-scorer parity probe, Python side.

Covers `query_tokens`, `score_chunk`, `utc_now_iso` and `latest_user_query` from
`deepseek_infra/core/utils.py`.

**Why some comparisons are order-insensitive.** `query_tokens` ends with
`sorted(tokens, key=len, reverse=True)[:80]` over a `set`. Python's sort is stable,
so equal-length tokens keep the set's iteration order — which depends on
`PYTHONHASHSEED`. Measured directly:

    100 equal-length tokens, PYTHONHASHSEED=1 -> x70,x17,x04,x11,...
    100 equal-length tokens, PYTHONHASHSEED=2 -> x78,x43,x17,x67,...

so even two runs of the *oracle* disagree. This probe therefore reports the token
list **sorted**, and for inputs where more than 80 tokens survive it reports only the
count, because the surviving subset itself differs run to run. The Rust port is
deterministic (length descending, then lexicographic), which is recorded as a
deliberate divergence in `docs/RETRIEVAL_SCORER.md`.

Usage::

    python tasks/native-runtime/core_utils_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example core_utils_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import ast
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UTILS = REPO / "deepseek_infra" / "core" / "utils.py"

EXTRACT_NAMES = ("latest_user_query", "utc_now_iso", "query_tokens", "score_chunk")

# Inputs where at most 80 tokens survive, so the token *set* is stable on both
# sides and only the order differs.
TOKEN_CASES: list[tuple[str, str]] = [
    ("ascii-simple", "Rust   OWNERSHIP"),
    ("single-chars", "a bb ccc"),
    ("mixed-lengths", "aa bbbb c ddddd"),
    ("punctuation", "hello, world! (test)"),
    ("digits-underscore", "v1_2 item-3 x+y"),
    ("uppercase", "ALPHA Beta gamma"),
    ("cjk-short", "中文"),
    ("cjk-three", "中文测"),
    ("cjk-long", "中文测试用例"),
    ("cjk-mixed", "中文 abc 测试"),
    ("empty", ""),
    ("whitespace", "   "),
    ("newlines", "a\nbb\tcc"),
]

# Inputs where more than 80 tokens survive: only the count is comparable.
CAPPED_CASES: list[tuple[str, str]] = [
    ("many-short", " ".join(f"t{i:03}" for i in range(200))),
    ("many-cjk", "中" * 60),
]

# Score inputs stay small so the token set is stable on both sides.
SCORE_CASES: list[tuple[str, str, str]] = [
    ("counts-and-length", "ab ab", "ab"),
    ("ten-char-weight", "abcdefghij", "abcdefghij"),
    ("longer-than-ten", "abcdefghijk", "abcdefghijk"),
    ("case-insensitive", "RUST rust", "rust"),
    ("no-match", "nothing here", "zzz"),
    ("heading-bonus", "rust\n# Title", "rust"),
    ("heading-no-space", "rust\n#Title", "rust"),
    ("heading-too-deep", "rust\n####### deep", "rust"),
    ("multiple-tokens", "aa aa bb", "aa bb"),
    ("cjk", "中文测试", "中文"),
]

UTC_CASES: list[int] = [0, 1, 1_760_000_000, 1_780_272_000]

QUERY_CASES: list[tuple[str, dict]] = [
    ("last-user", {"messages": [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "  second  "},
    ]}),
    ("blank-skipped", {"messages": [
        {"role": "user", "content": "earlier"},
        {"role": "user", "content": "   "},
    ]}),
    ("non-string-content", {"messages": [
        {"role": "user", "content": "text"},
        {"role": "user", "content": ["parts"]},
        "not-an-object",
    ]}),
    ("no-user", {"messages": [{"role": "assistant", "content": "a"}]}),
    ("empty-messages", {"messages": []}),
    ("no-messages-key", {}),
    ("messages-not-list", {"messages": "no"}),
    ("non-dict-message", {"messages": ["x", 7, None]}),
]


def build_namespace() -> dict:
    namespace: dict = {}
    import datetime as datetime_module
    import re
    from datetime import datetime, timezone
    from typing import Any

    namespace.update(
        {
            "re": re,
            "datetime_module": datetime_module,
            "datetime": datetime,
            "timezone": timezone,
            "Any": Any,
        }
    )
    source = UTILS.read_text(encoding="utf-8")
    for name in EXTRACT_NAMES:
        segment = next(
            (
                ast.get_source_segment(source, node)
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.FunctionDef) and node.name == name
            ),
            None,
        )
        if segment is None:
            raise SystemExit(f"could not extract {name}")
        exec(compile(segment, str(UTILS), "exec"), namespace)  # noqa: S102
    return namespace


def main() -> int:
    if not UTILS.exists():
        print(f"missing {UTILS}", file=sys.stderr)
        return 2

    ns = build_namespace()
    query_tokens = ns["query_tokens"]
    score_chunk = ns["score_chunk"]
    utc_now_iso = ns["utc_now_iso"]
    latest_user_query = ns["latest_user_query"]

    out: dict = {}

    for label, query in TOKEN_CASES:
        tokens = query_tokens(query)
        out[f"tokens::{label}"] = {
            "count": len(tokens),
            # Sorted, because the oracle's own order varies between runs.
            "sorted": sorted(tokens),
        }

    for label, query in CAPPED_CASES:
        # The surviving subset depends on the hash seed, so only the count is
        # comparable.
        out[f"capped::{label}"] = {"count": len(query_tokens(query))}

    for label, text, token in SCORE_CASES:
        tokens = query_tokens(token)
        out[f"score::{label}"] = {
            "tokens": sorted(tokens),
            "count": len(tokens),
            "score": score_chunk(text, tokens),
        }

    # The oracle's `utc_now_iso()` reads the clock itself and takes no argument, so
    # the comparable core is the *rendering*: Python's own
    # `datetime.fromtimestamp(epoch, utc).isoformat(timespec="seconds")` versus this
    # port's `utc_now_iso(epoch)`. The signature difference is recorded in
    # docs/RETRIEVAL_SCORER.md.
    def utc_at(epoch: int) -> str:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")

    for epoch in UTC_CASES:
        out[f"utc::{epoch}"] = utc_at(epoch)

    for label, payload in QUERY_CASES:
        out[f"query::{label}"] = latest_user_query(payload)

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
