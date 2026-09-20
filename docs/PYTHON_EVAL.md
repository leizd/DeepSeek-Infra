# python_eval (in-process AST sandbox)

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

Status: **ported, wired into the tool loop, sandbox probe-verified locally.**
Exact-head CI has not run against this slice.

`deepseek-policy::python_eval` mirrors `tools.python_eval` + `PYTHON_EVAL_RUNNER`.
The oracle shells out to `sys.executable -I -c …`. This port **does not**: it
parses, allowlists and evaluates the same expression subset in-process. That
is the sandbox, not a CPython child.

Allowlist (same as the runner): `Expression` / `BinOp` / `UnaryOp` / `BoolOp` /
`Compare` / `IfExp` / `Call` / `Name` / `Constant` / containers / `math.<public>`,
plus the named builtins `abs` `round` `min` `max` `sum` `pow` `len` `factorial`
`comb` `perm` `gcd` `lcm` `sqrt` `log` `sin` `cos` `tan` `pi` `e`. Results are
Python `repr` truncated to 4000 characters.

## Verification

- `tasks/native-runtime/python_eval_parity_probe.py` ↔
  `rust/crates/deepseek-policy/examples/python_eval_parity_probe.rs`
  (md5 `b545a8f9c49c5fbbb6e2010c595ae3f2`, 3458 chars)
- `chat_route_evals_a_python_expression` — the wired loop returns `720` for
  `factorial(6)` instead of `Tool did not run`
