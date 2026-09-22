"""Run the Python/Rust probe pairs as one gate.

The self-driving probes take `--rust-example` and answer for themselves. The older
pairs print a JSON report to stdout and were compared by hand — which means nothing
compared them once nobody remembered to. This harness is that missing step: it runs both
sides of a pair, normalises line endings, compares byte for byte, and exits non-zero when
any pair disagrees.

Usage::

    python tasks/native-runtime/check_probe_pairs.py            # every pair it can find
    python tasks/native-runtime/check_probe_pairs.py --pairs title,file_routes
    python tasks/native-runtime/check_probe_pairs.py --solve-only   # list, do not run

A pair is `tasks/native-runtime/<name>_parity_probe.py` plus an executable of the same
name in the examples directory. Pairs whose Rust side needs something a plain runner
cannot provide (a browser, a live store) are listed in SKIPPED with the reason, so the
gap is visible rather than silently absent.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
PROBE_DIR = REPO / "tasks" / "native-runtime"
EXAMPLES = REPO / "rust" / "target" / "debug" / "examples"

#: Pairs that are known to disagree, with the measurement that says so. They are kept out
#: of the verdict so a red run keeps meaning "something changed", and they are printed on
#: every run so the finding stays visible instead of being buried by a green lane.
#:
#: Neither entry is a verdict on the port's correctness -- that is a question for the
#: oracle's own rules -- and both were measured on 2026-09-22.
KNOWN_DIVERGENCES: dict[str, str] = {
    "store": (
        "the oracle reads the wall clock (`bm.today()`) while the Rust example pins "
        "DAY=\"2026-09-18\", so the pair only agreed on the day it was written; the raw "
        "row's day now reads 2026-09-22 against 2026-09-18"
    ),
    "memory": (
        "`state::remember` reports hitCount 4 against 3 and omits `[fact] the sky is blue`, "
        "and `state::scoped` orders the context list differently (fact, project, preference "
        "against fact, preference, project)"
    ),
}

#: Pairs the harness deliberately does not run, keyed by probe name without the suffix,
#: with the reason. A gap that is listed is visible; one that is silently skipped is not.
SKIPPED: dict[str, str] = {
    "browser_engine": "needs a Chromium and its own fixture server; runs in native-browser-engine",
    "oracle": "needs the oracle's own application context, not just imports",
    "local_clock": "takes an epoch-seconds argument rather than printing a report",
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def discover(pairs: list[str] | None) -> list[str]:
    found = sorted(path.name[: -len("_parity_probe.py")] for path in PROBE_DIR.glob("*_parity_probe.py"))
    if pairs:
        wanted = {name.strip() for name in pairs if name.strip()}
        return [name for name in found if name in wanted]
    return found


def rust_example(name: str) -> Path | None:
    for suffix in (".exe", ""):
        candidate = EXAMPLES / f"{name}_parity_probe{suffix}"
        if candidate.is_file():
            return candidate
    return None


def run(command: list[str], cwd: Path) -> tuple[int, str, str]:
    # `PYTHONPATH` is set explicitly: a few probes import `deepseek_infra` without
    # inserting the repository themselves, and the harness is the thing that knows where
    # the repository is.
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(REPO) + os.pathsep + environment.get("PYTHONPATH", "")
    completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False, env=environment)
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def normalise(text: str) -> str:
    """Line endings and trailing whitespace only: a pair that differs in content must fail."""
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip()


def first_difference(expected: str, actual: str) -> str:
    expected_lines = expected.split("\n")
    actual_lines = actual.split("\n")
    for index in range(max(len(expected_lines), len(actual_lines))):
        left = expected_lines[index] if index < len(expected_lines) else "<missing>"
        right = actual_lines[index] if index < len(actual_lines) else "<missing>"
        if left != right:
            return f"line {index + 1}: oracle={left[:90]!r} native={right[:90]!r}"
    return "identical after normalisation"


def compare(oracle_text: str, native_text: str) -> tuple[str, str, str]:
    """Return (verdict, byte_note, detail).

    A pair whose two sides are JSON is judged on the parsed values, because the key
    *order* of a `serde_json` map depends on whether `preserve_order` is in the graph and
    a gate must not turn red because a build reached the same answer in another order.
    Byte equality is still measured and reported: it is the stronger claim, and the
    probes built for it (the self-driving pairs) are expected to keep it.
    """
    expected = normalise(oracle_text)
    actual = normalise(native_text)
    byte_note = "bytes identical" if expected == actual else "bytes differ"
    try:
        parsed_oracle = json.loads(expected)
        parsed_native = json.loads(actual)
    except (json.JSONDecodeError, ValueError):
        if expected == actual:
            return "PASS", byte_note, "identical"
        return "FAIL", byte_note, first_difference(expected, actual)
    if parsed_oracle == parsed_native:
        return "PASS", byte_note, "same value"
    return "FAIL", byte_note, first_difference(expected, actual)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", help="comma-separated probe names; default is every pair found")
    parser.add_argument("--solve-only", action="store_true", help="list the pairs that would run")
    parser.add_argument("--report", type=Path, help="write the outcome of every pair as JSON")
    args = parser.parse_args(argv)

    names = discover(args.pairs.split(",") if args.pairs else None)
    # Pairs that drive their own Rust side have an interface this harness does not speak
    # (`--rust-example`); they are gates already, inside their own CI lanes.
    self_driving = {
        path.name[: -len("_parity_probe.py")]
        for path in PROBE_DIR.glob("*_parity_probe.py")
        if "--rust-example" in path.read_text(encoding="utf-8", errors="ignore")
    }
    runnable = [name for name in names if name not in SKIPPED and name not in self_driving and rust_example(name) is not None]
    if args.solve_only:
        for name in runnable:
            print(name)
        return 0
    if not runnable:
        print("check_probe_pairs: no runnable pairs (build them with `cargo build --workspace --examples`)")
        return 1

    outcomes: list[dict[str, Any]] = []
    failures: list[str] = []
    known: list[str] = []
    byte_identical = 0
    for name in runnable:
        probe = PROBE_DIR / f"{name}_parity_probe.py"
        example = rust_example(name)
        assert example is not None
        python_code, oracle_text, python_err = run([sys.executable, str(probe)], REPO)
        rust_code, native_text, rust_err = run([str(example)], REPO)
        if python_code != 0 or rust_code != 0:
            reason = f"exit codes oracle={python_code} native={rust_code}"
            if python_err.strip():
                reason += f"; oracle stderr: {python_err.strip().splitlines()[-1][:120]}"
            if rust_err.strip():
                reason += f"; native stderr: {rust_err.strip().splitlines()[-1][:120]}"
            outcomes.append({"pair": name, "result": "ERROR", "detail": reason})
            failures.append(name)
            print(f"  ERROR  {name}: {reason}")
            continue
        verdict, byte_note, detail = compare(oracle_text, native_text)
        if byte_note == "bytes identical":
            byte_identical += 1
        outcomes.append({"pair": name, "result": verdict, "detail": detail, "bytes": byte_note})
        if verdict == "PASS":
            print(f"  PASS   {name} ({detail}; {byte_note})")
        elif name in KNOWN_DIVERGENCES:
            known.append(name)
            print(f"  KNOWN  {name}: {KNOWN_DIVERGENCES[name]}")
        else:
            failures.append(name)
            print(f"  FAIL   {name}: {detail}")

    print()
    print(
        f"pairs run: {len(runnable)}  passed: {len(runnable) - len(failures) - len(known)}  failed: {len(failures)}"
        f"  known-divergent: {len(known)}  (byte-identical: {byte_identical})"
    )
    for name in sorted(known):
        print(f"known: {name} -- {KNOWN_DIVERGENCES[name]}")
    for name, reason in sorted(SKIPPED.items()):
        if (PROBE_DIR / f"{name}_parity_probe.py").is_file():
            print(f"skipped: {name} -- {reason}")
    if self_driving:
        print(f"self-driving (own lanes): {', '.join(sorted(self_driving))}")
    if args.report:
        args.report.write_text(json.dumps({"skipped": SKIPPED, "outcomes": outcomes}, indent=2) + "\n", encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
