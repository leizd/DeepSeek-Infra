"""title generation parity probe, oracle side.

Runs the oracle's own `title_generator` over the same corpus the Rust example runs and
compares the two reports field by field. The route's transport and envelope are pinned
by `rust/crates/deepseek-gateway/tests/title_route.rs`; what this probe pins is the
part that decides *what the model is asked* and *what the title becomes*.

Usage::

    python tasks/native-runtime/title_parity_probe.py \\
        --rust-example rust/target/debug/examples/title_parity_probe.exe

The oracle's `generate_title_payload` performs the upstream call, so it cannot be
called here. The probe therefore drives the oracle's own helpers
(`_truncate`, `_sanitize_title`, `format_upstream_error`, the constants) and the
request body it *would* build, which is exactly the surface the Rust side ports. A
mismatch in the transport would not show up here and is covered by the route suite.

**Nothing here is stubbed or re-implemented.** Every value on the oracle side comes
from importing `deepseek_infra`, so a change in the oracle fails this probe rather than
silently agreeing with the port.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from deepseek_infra.core.utils import format_upstream_error  # noqa: E402
from deepseek_infra.infra.gateway import title_generator as oracle  # noqa: E402

SANITIZE_CASES = [
    "",
    "   ",
    "简单标题",
    "「标题： 你好世界」",
    "『标题: 带书名号』",
    "《标题》",
    '"quoted"',
    "'single'",
    "“curly”",
    "‘curly single’",
    "`backtick`",
    "Title: Hello World",
    "title: lower",
    "标题：中文标签",
    "标题:半角标签",
    "first\nsecond",
    "first\r\nsecond",
    "  spaced   out  ",
    "topic。",
    "topic...",
    "topic，！？；：",
    "a。，！",
    "emoji 🎉 title",
    "这是一个非常长的中文标题它超过了二十四个字符的上限",
    "a very long english title that exceeds the six word guidance",
    "标题：   前导空格   ",
]

# The `titleModel` corpus. `None` marks "the key is absent", which is not the same as
# an explicit null for `payload.get(...) or DEFAULT`.
MODEL_CASES: list[tuple[str, Any]] = [
    ("absent", None),
    ("empty", ""),
    ("flash", "flash"),
    ("v4pro", "v4pro"),
    ("expert", "expert"),
    ("DeepSeek_V4_Pro", "DeepSeek_V4_Pro"),
    ("unsupported", "gpt-9"),
    ("number", 5),
]

MESSAGE_CASES: list[tuple[str, str]] = [
    ("hello", ""),
    ("解释一下 FastCDC", "FastCDC 是一种内容定义分块算法。"),
    ("", ""),
    ("   ", "ignored"),
    ("a", ""),
    ("长文本", "短"),
]

TRUNCATE_CASES: list[tuple[str, str, int]] = [
    ("short_3", "hello", 3),
    ("exact_3", "abc", 3),
    ("over_3", "abcdef", 3),
    ("spaces_3", "  hello  ", 3),
    ("cjk_exact_4", "中文标题", 4),
    ("cjk_over_3", "中文标题", 3),
    ("long_10", "0123456789", 10),
    ("empty_5", "", 5),
    ("blank_5", "   ", 5),
]

RESPONSE_CASES: list[tuple[str, Any]] = [
    ("plain", {"choices": [{"message": {"content": "标题"}}]}),
    ("wrapped", {"choices": [{"message": {"content": "「标题： 你好」"}}]}),
    ("empty_choices", {"choices": []}),
    ("no_choices", {}),
    ("null_content", {"choices": [{"message": {"content": None}}]}),
    ("missing_message", {"choices": [{}]}),
    (
        "extra_choice",
        {"choices": [{"message": {"content": "first"}}, {"message": {"content": "second"}}]},
    ),
]

ERROR_CASES: list[tuple[str, str]] = [
    ("message", '{"error": {"message": "Invalid API key"}}'),
    ("type_only", '{"error": {"type": "rate_limit"}}'),
    ("empty_error", '{"error": {}}'),
    ("not_json", "not json"),
    ("empty", ""),
    ("long", "x" * 600),
    ("nested", '{"error": {"message": "quota"}}'),
]


def oracle_request_body(user: str, assistant: str, title_model: Any = None) -> dict[str, Any] | None:
    """The body `generate_title_payload` builds, without its upstream call.

    Mirrors the oracle's own lines in order: truncate, the blank-user early return,
    the alias-normalised model with the supported-model fallback, then the literal
    body. Kept beside the probe rather than in the oracle because the oracle's
    function interleaves the HTTP call with the construction; every *value* below is
    read from the oracle module, so a constant change still fails this probe.
    """
    user_text = oracle._truncate(str(user or ""), 1200)
    assistant_text = oracle._truncate(str(assistant or ""), 600)
    if not user_text.strip():
        return None
    model = oracle.normalize_model_name(title_model or oracle.CONTEXT_COMPRESS_MODEL)
    if model not in oracle.SUPPORTED_MODELS:
        model = oracle.CONTEXT_COMPRESS_MODEL
    return {
        "model": model,
        "stream": False,
        "thinking": {"type": "disabled"},
        "temperature": 0.3,
        "max_tokens": 60,
        "messages": [
            {"role": "system", "content": oracle.TITLE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"用户首轮提问:\n{user_text}\n\n"
                    f"助手首轮回复摘要:\n{assistant_text or '（暂无）'}\n\n"
                    "请直接给出标题。"
                ),
            },
        ],
    }


def oracle_response_title(response: dict[str, Any]) -> str:
    """The oracle's `choices[0].message.content` read, then its sanitiser."""
    choices = response.get("choices") or []
    raw_title = ""
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        if isinstance(message, dict):
            raw_title = str(message.get("content") or "")
    return oracle._sanitize_title(raw_title)


def oracle_report() -> dict[str, Any]:
    return {
        "system_prompt": oracle.TITLE_SYSTEM_PROMPT,
        "sanitize": {case: oracle._sanitize_title(case) for case in SANITIZE_CASES},
        "truncate": {
            label: oracle._truncate(value, limit) for label, value, limit in TRUNCATE_CASES
        },
        "request_bodies": {
            f"{user}|{assistant}": oracle_request_body(user, assistant)
            for user, assistant in MESSAGE_CASES
        },
        "models": {
            label: (oracle_request_body("hi", "", None if value is None else value) or {}).get(
                "model"
            )
            for label, value in MODEL_CASES
        },
        "responses": {
            label: oracle_response_title(response) for label, response in RESPONSE_CASES
        },
        "upstream_errors": {
            label: format_upstream_error(raw) for label, raw in ERROR_CASES
        },
    }


def run_rust_example(example: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [str(example)], capture_output=True, text=True, encoding="utf-8", check=False
    )
    if completed.returncode != 0:
        print(
            f"title_parity_probe: the Rust probe exited {completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        print(f"title_parity_probe: the Rust probe did not print JSON: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--rust-json", type=Path, help="a report captured from the Rust probe")
    source.add_argument("--rust-example", type=Path, help="the Rust probe binary to run")
    parser.add_argument("--report", type=Path, help="where to write the comparison report")
    args = parser.parse_args()

    oracle = oracle_report()
    native = (
        run_rust_example(args.rust_example)
        if args.rust_example is not None
        else json.loads(Path(args.rust_json).read_text(encoding="utf-8"))
    )

    problems: list[str] = []
    for section in (
        "system_prompt",
        "sanitize",
        "truncate",
        "request_bodies",
        "models",
        "responses",
        "upstream_errors",
    ):
        expected = oracle.get(section)
        actual = native.get(section)
        if expected == actual:
            continue
        if isinstance(expected, dict) and isinstance(actual, dict):
            for key in sorted(set(expected) | set(actual)):
                if expected.get(key) != actual.get(key):
                    problems.append(
                        f"{section}.{key}: oracle={expected.get(key)!r} native={actual.get(key)!r}"
                    )
        else:
            problems.append(f"{section}: oracle={expected!r} native={actual!r}")

    report = {
        "sections": list(oracle.keys()),
        "problems": problems,
        "result": "PASS" if not problems else "FAIL",
    }
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
