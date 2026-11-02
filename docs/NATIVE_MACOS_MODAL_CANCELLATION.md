# Actual Mac modal cancellation and orderly application exit

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

Source `597f9f9a` independently passes the actual Intel/Apple-silicon five-program
application, raw public file/reader responses, original React HTML and graceful
whole-app restart workload. Those observations contain no active native dialog.
They do not prove cancellation while AppKit runs a nested modal event loop.

Apple documents distinct behavior for [application stop](https://developer.apple.com/documentation/appkit/nsapplication/stop(_:))
inside a modal loop and for [aborting the modal loop](https://developer.apple.com/documentation/appkit/nsapplication/abortmodal()).
Baseline `3303e0b46a64115cb71e33b1d640021a77dbb26c` kept production code
unchanged. [Run 41312771718](https://github.com/leizd/DeepSeek-Infra/actions/runs/41312771718)
actually reproduced the failure on both architectures: a real visible modal
was observed on the initial thread, `Destroy` was reached, and original context
cancellation failed to settle within five seconds. Each test reported failure
before the original 15-minute job bound expired. This is a verified runtime RED.

Every owned native dialog now enters a common modal-stack lifecycle: JavaScript
alert/confirm/prompt, open/save panels, the stop confirmation and download errors.
Cancellation aborts and hides the current modal. Each returning modal unwinds
one owned stack level; only after the last level returns does application stop
target the outer loop. The original wake event is retained. Existing accepted
responses and WebKit completion calls remain in their original handlers; a
closed host cannot start another dialog.

The new `TestNativeMacJavaScriptModalCancellationSettlesApplication` opens an
actual WebKit page whose JavaScript invokes `alert`, `confirm` or `prompt` in
separate processes. A fourth case opens the original production stop-confirmation
inside a live JavaScript alert, requires two distinct actual modal windows and
depth two, and verifies cancellation declines that confirmation. A test-only observer uses
public main-thread run-loop modes to inspect the real visible modal window.
It then cancels only the original application context and requires the original
navigation/dispatch path and application loop to settle within five seconds.
The observer never calls `abortModal`, `stopModal` or an alternative close path.
Its native main-thread observation, modal window ID, exact source/run/architecture
and copied executable hash are retained even when the cancellation assertion
fails. The test host now exits with the original `m.Run` result even if its initial
AppKit thread is stranded: the baseline showed that a test failure otherwise
left `TestMain` waiting inside the UI loop after Go's test timer was stopped.
The original independent 45-second test bound is retained for every process.

Both actual Intel and Apple-silicon runners execute the case in the existing
window job. Its 15-minute limit, original window/profile cases and original
Windows job are unchanged. The workflow parser now checks five embedded Python
programs, including the added report checker; the original four still execute.
This repair changes only the native Mac window implementation and its test/gate
coverage; Rust/Proto, coverage floors and original timeout/recovery requirements
are unchanged. No native compilation or UI execution occurs locally.

Until this repair's exact-source actual execution succeeds, modal cancellation
remains unqualified. Passing these cases does not qualify every chooser, download, user
close, real-product DOM flow, upgrade/backup/supported OS or remaining product
capability. Full migration remains **NOT_READY**, including the retained MCP
test engine and all original ownership/recovery/zero-Python/server-TypeScript,
security/performance and complete CI/Evidence requirements.
