"""python_eval parity probe, Python side.

Pins the AST-allowlisted sandbox: arithmetic, factorial/math, comparisons,
refusals (empty, oversize, import, unknown name). Result strings are `repr`
from the isolated runner.

Usage::

    python tasks/native-runtime/python_eval_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example python_eval_parity_probe > ../rust.json
"""

from __future__ import annotations

import json
import sys
from typing import Any

REPO = __import__("pathlib").Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from deepseek_infra.core.errors import AppError  # noqa: E402
from deepseek_infra.infra.tool_runtime import tools  # noqa: E402


def view(expression: str) -> Any:
    try:
        result = tools.python_eval(expression)
        return {"ok": True, "result": result}
    except AppError as exc:
        return {"ok": False, "error": str(exc), "code": exc.code.value, "status": exc.status}


def main() -> int:
    cases = [
        "factorial(6)",
        "1 + 2 * 3",
        "2+2",
        "math.sqrt(4)",
        "1 < 2 < 3",
        "min(3, 1, 2)",
        "max([1, 8, 3])",
        "sum([1, 2, 3])",
        "pow(2, 10)",
        "round(2.5)",
        "abs(-7)",
        "len([1, 2, 3])",
        "True and 0 or 4",
        "1 if 0 else 2",
        "(1, 2)",
        "[1, 2][0]",
        "gcd(12, 8)",
        "comb(5, 2)",
        "math.pi > 3",
        "",
        "1" * 1001,
        "__import__('os').system('whoami')",
        "unknown_name",
        "pi()",
        "open('x')",
        "1 / 0",
    ]
    out = {f"case::{index}::{expression[:40]}": view(expression) for index, expression in enumerate(cases)}
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
