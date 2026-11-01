# Native macOS window verification

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

## AppKit/WebKit implementation under verification, 2026-11-01

The actual9ace5f9 run41280489970 cross-built both architectures successfully.
Intel and Apple silicon each executed the native test and failed at the same
unimplemented production window entry. Both original job logs are retained;
this is an observed runtime baseline, not a syntax-only failure.

The follow-up implements the native AppKit window and WKWebView on the initial
thread with the existing pinned purego package and CGO disabled. It retains the
private URL handoff, native event queue, context cancellation, atomic launcher
close guard, file chooser, JavaScript dialogs, native downloads and edit menus.
Browser opening uses the existing UI executable in a separate initial-thread
mode with a bounded lifetime and sanitized environment; no URL enters argv and
no backend/provider/loader credentials are inherited.

A canonical profile path derives a stable, distinct public CFBundleIdentifier.
The UI re-execs at the same supervised PID into one private profile-owned app
bundle before Foundation initializes its main bundle. This uses WebKit's public
persistent default data store on macOS13+, without private API, a global ATS
exception, an ephemeral profile or an implicit minimum-OS upgrade. Executable
updates replace one owned file atomically and retain the same identifier; owned
bundle member links and a different profile owner are refused. Default WebKit
data remains in the operating system's bundle-specific Library locations;
legacy profile migration/backup and actual restart persistence still need proof.

The cloud gate retains the complete original Windows job and visible-window
workload and adds native profile identity/update/link-denial and browser
credential tests on both actual architectures. Local source checks cannot
qualify Cocoa behavior. This implementation is not yet runtime accepted.
Actual file dialogs/downloads/close-confirmation/default-browser behavior,
persistent state across app restarts, the full Rust/Go product workload, backend
closure, supported OS versions, packaging/signing and full migration remain open.
All broad readiness and feature flags remain false/NOT_READY until proven.

The firstd1266c7 submission failed workflow validation before any native Mac
test ran: runner.temp is unavailable in job-level env. The corrected source
uses an ignored workspace profile path and passes actionlint. The real failing
baseline and subsequent AppKit/WebKit implementation are still pending; this
syntax correction is not platform acceptance.


The complete Rust/Go migration remains **NOT_READY**. The existing native macOS
window and default-browser entries return explicit unavailable errors. The first
cloud fixture records that actual baseline before replacing the window host.
No production Python, browser-tab substitute, business bridge or shared writer is
an accepted macOS implementation.

Dispatch the existing `native-windows-cloud.yml` with `verify_macos=true` on the
reviewed source branch. The complete original Windows job is unchanged. The new
Linux job uses the repository's existing checksum-pinned Go1.27.1 archive and
`CGO_ENABLED=0` to cross compile both real desktop programs and native test hosts.
It records their source commit/tree/run, toolchain and SHA256 before any fixture
crosses to the Mac runner. No new SDK pin, implicit Go upgrade or local build is
used. Executable fixtures and compiler outputs stay in GitHub Actions.

The matching `macos-15-intel` and `macos-15` runners verify the complete envelope,
both Mach-O CPU headers and binary hashes before execution. The native Go test
host creates a real visible AppKit window on the initial thread, loads an
authenticated fixture in WebKit, observes real DOM/JavaScript/layout and an
HttpOnly-cookie redirect, then requires context cancellation to stop its loop.
Both architectures run independently; one failure cannot hide the other.
Only bounded JSON and logs are selected for local collection. A missing native
report or failed test is a failure, never an empty success.

This fixture qualifies only those observed window/fixture/cancellation paths.
It does not prove the real Rust/Go product workload, profile persistence or
isolation, chooser/download/dialog/close-confirmation behavior, recovery of the
complete backend tree, packages or signing, all supported OS releases, all
desktop features, zero-Python product operation or whole-project readiness.
Those retained obligations require implementation and further actual evidence.

The platform implementation must retain independent persistent app/launcher
profiles, native file dialogs and downloads, editing shortcuts, safe closure
and the supervisor's existing ownership. Public AppKit/WebKit APIs and the
already pinned purego version are the intended platform glue; Go/Rust business
interfaces continue to use the existing typed protocols.

References:

- [Official runner architecture labels](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [Apple application bundle layout](https://developer.apple.com/documentation/bundleresources/placing-content-in-a-bundle)
- [Apple bundle identifiers](https://developer.apple.com/library/archive/documentation/General/Reference/InfoPlistKeyReference/Articles/CoreFoundationKeys.html)
- [Pinned purego native window example](https://github.com/ebitengine/purego/blob/v0.11.1/examples/window/main_darwin.go)
- [WebKit website data store interface](https://github.com/WebKit/WebKit/blob/main/Source/WebKit/UIProcess/API/Cocoa/WKWebsiteDataStore.h)
