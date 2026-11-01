# Native media recovery: procfs exit observation

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

The exact-source media recovery [run 41288289654](https://github.com/leizd/DeepSeek-Infra/actions/runs/41288289654)
for `df16f98329d604a79fe12e2d612315a1455c0e74` fails the original vector
DELETE crash case. The other eight original cases pass. All seven recorded
SIGKILL sends, including the mandatory worker signal, succeed. The observer
then reports `ProcessLookupError`, errno 3, while awaiting the original
processes' exit. The raw report does not identify which of the two stat reads
raised the exception; it does not qualify recovery after this failure.

Both [process identity](../scripts/verify_native_media_reads.py) and
[process state](../scripts/verify_native_media_recovery.py) read
`/proc/<pid>/stat`. They previously accepted `ENOENT` only. The
[Linux procfs contract](https://www.kernel.org/doc/html/v6.12/filesystems/proc.html)
also permits `ESRCH` when the process exits after its procfs descriptor is
opened. The observation now returns no process for either of these specific
exit conditions. Permission, I/O and invalid-operation errors still propagate.

The existing pidfd/start-time checks remain mandatory. A process disappearing
after pidfd binding fails the second identity check, the descriptor is closed,
and no signal is sent. An already disappeared peer cannot count as stopped;
the worker's actual SIGKILL delivery and the launcher's SIGKILL wait result
remain required. The original timeout bounds, nine recovery cases, 24 PROCESS
cases, operation/epoch bindings and original coverage floors are unchanged.

Five new functional regressions fail on the parent implementation; all 15
new cases pass with the two observation fixes. The preliminary Windows test
fixture lacked a Linux-only signal constant; that fixture error is retained
separately and is excluded from the functional RED count. These are offline
observer tests, with no local native build or verification container.

The child source must pass all nine actual recovery cases and all 24 PROCESS
cases on GitHub Actions before receiving its own qualification. The parent
`082e4d57bef80163542e78bce763ffc7080463ff` has separately verified 9+24
evidence; it does not qualify this child or erase the later failure. The
controlled Intel/Apple-silicon window and separate-profile restart evidence
for `df16f983` also remains scoped to that source and those behaviors.

This change is confined to offline verification. Production Go/Rust code and
protocols are unchanged. Full product migration, every retained feature,
ownership/cutover/recovery, zero production Python/server TypeScript, full
desktop/Android/package compatibility, security, performance and exact-source
CI/Evidence remain required. Readiness remains **NOT_READY**.
