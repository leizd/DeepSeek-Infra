# Windows Go race-detector toolchain

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


This is development/test tooling only. It does not change the native runtime's
Rust/Go process boundary, introduce Go-to-Rust FFI, or enter production packages.
The existing Linux CI `native-go` race gate remains required for release evidence.

## Why the ambient compiler is insufficient

On the local Windows host, Go 1.27.1 with GCC 8.1 / MinGW runtime 6 produced race
executables that exited with `0xc0000139` before tests started. Inspection of the
executable's PE imports and the system DLL's exports identified `WaitOnAddress`,
`WakeByAddressSingle` and `WakeByAddressAll` incorrectly imported from kernel32.dll.
Ordinary Go tests passed; this was not evidence of a data race or a race-test PASS.

Go requires a C compiler with **MinGW runtime 8 or newer** for Windows race builds;
the GCC version alone does not establish that requirement. See the
[official race-detector requirements](https://go.dev/doc/articles/race_detector#Requirements).
Do not replace system DLLs, disable the detector or exclude tests to work around a
loader failure. Avoid mixing CRT/object files from different compiler toolchains.

## Isolated compiler used for local verification

The [LLVM-MinGW project](https://github.com/mstorsjo/llvm-mingw#releases) provides
unpack-only Windows toolchains. The following exact release was retrieved from the
maintainer's GitHub release, without an installer or global configuration changes:

| Field | Value |
| --- | --- |
| Release | [20260826](https://github.com/mstorsjo/llvm-mingw/releases/tag/20260826) |
| Asset | `llvm-mingw-20260826-ucrt-x86_64.zip` |
| Size | `190721391` bytes |
| SHA-256 | `ae601f4e0f72bbdf441ad2df8bb16f037e2e9251559ea6b37b4057aef39c06c3` |
| Digest source | GitHub release asset metadata (`digest`), checked before extraction |
| Compiler | Clang `23.1.0`, MinGW runtime `15.0` |

The digest check detects a different archive; it is not an independent signature
or a claim that the upstream release cannot be compromised. Use only the named
upstream release asset. A missing/different digest is a hard stop, not permission
to update this pin automatically.

Keep the archive and extraction under an isolated, gitignored task directory.
Before extraction, require the expected archive size/hash, reject any archive entry
whose resolved output escapes that directory, and reject unexpected expansion size.
The verified archive contained 9,317 entries and 749,526,687 expanded bytes. Do not
overwrite an existing tool directory without inspecting it first. Extraction does
not run package install scripts.

## Run from a dedicated PowerShell task process

The example assumes the verified archive has been extracted to the path below.
Adjust the repository/tool paths for another checkout, but do not substitute an
unverified archive or an ambient `gcc`. Use a new task shell so these process-local
variables disappear when it exits; do not use `go env -w` or change registry PATH.

```powershell
$nativeToolchain = 'D:\deepseek\.tools\native-race-20260826\toolchain\llvm-mingw-20260826-ucrt-x86_64'
$env:CC = Join-Path $nativeToolchain 'bin\x86_64-w64-mingw32-clang.exe'
$env:CXX = Join-Path $nativeToolchain 'bin\x86_64-w64-mingw32-clang++.exe'
$env:PATH = (Join-Path $nativeToolchain 'bin') + ';D:\Dev\Go\go\bin;' + $env:PATH
$env:GOTOOLCHAIN = 'local'
$env:CGO_ENABLED = '1'
Set-Location 'D:\deepseek\go'
go version
& $env:CC --version
go test -race ./internal/action -run '^TestExecutePathsRemainDenied$' -count=1 -timeout=90s
if ($LASTEXITCODE -ne 0) { throw 'Race runtime smoke test failed' }
go test -race ./... -count=1 -timeout=240s
if ($LASTEXITCODE -ne 0) { throw 'Full Go race gate failed' }
```

The focused test establishes that the race executable can start; it is not the
full gate. Preserve the terminal result and failure details from the full run.
Process-kill tests have bounded waits for real persisted lease expiry. Do not
restart a still-running test just because its output is quiet.

Local race results remain local development evidence. They do not prove
provider-backed takeover, Linux SIGKILL recovery, production authentication,
zero-Python packaging or an exact-head CI/release PASS.

## Verified local result (2026-09-08)

On implementation head `46d0bcf`, both commands above passed with the pinned
compiler. The full JSON event log reported 15 test packages passed, 487 test
entries passed, no failures and no data-race warnings. One existing symbolic-link
test skipped because the Windows account lacked link-creation privilege; this is
not zero-skip evidence. Seven generated-only protobuf packages had no tests.

The longest package was `internal/store` at 228.766 seconds under race
instrumentation. An observation timeout is not permission to restart its process.
Retained local log: `.tools/native-race-20260826/go-race-full.jsonl`, SHA-256
`e91b210225e943fca51763f1391403c021bfd12dad76942756445d9197a2e655`.
