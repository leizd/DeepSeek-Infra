# Actual Mac JavaScript modal cancellation probe

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

Source `597f9f9a` independently passes the actual Intel/Apple-silicon five-program
application, raw public file/reader responses, original React HTML and graceful
whole-app restart workload. Those observations contain no active native dialog.
They do not prove cancellation while AppKit runs a nested modal event loop.

Apple documents distinct behavior for [application stop](https://developer.apple.com/documentation/appkit/nsapplication/stop(_:))
inside a modal loop and for [aborting the modal loop](https://developer.apple.com/documentation/appkit/nsapplication/abortmodal()).
The current native `Destroy` stops the application without an explicit modal
exit path. This is an unconfirmed runtime risk; the probe must establish the
actual behavior before a production fix is selected.

The new `TestNativeMacJavaScriptModalCancellationSettlesApplication` opens an
actual WebKit page whose JavaScript invokes `alert`. A test-only observer uses
public main-thread run-loop modes to inspect the real visible modal window.
It then cancels only the original application context and requires the original
navigation/dispatch path and application loop to settle within five seconds.
The observer never calls `abortModal`, `stopModal` or an alternative close path.
Its native main-thread observation, modal window ID, exact source/run/architecture
and copied executable hash are retained even when the cancellation assertion
fails. The independent 45-second process bound also contains a stranded UI loop.

Both actual Intel and Apple-silicon runners execute the case in the existing
window job. Its 15-minute limit, original window/profile cases and original
Windows job are unchanged. The workflow parser now checks five embedded Python
programs, including the added report checker; the original four still execute.
This baseline probe changes no production Go/Rust/Proto, coverage floor, timeout
or recovery semantics. No native compilation or UI execution occurs locally.

Until exact-source actual execution succeeds, modal cancellation remains
unqualified. Passing it does not qualify every chooser, dialog, download, user
close, real-product DOM flow, upgrade/backup/supported OS or remaining product
capability. Full migration remains **NOT_READY**, including the retained MCP
test engine and all original ownership/recovery/zero-Python/server-TypeScript,
security/performance and complete CI/Evidence requirements.
