# Native macOS window verification

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

The full Rust/Go migration remains **NOT_READY**. The production Go UI now has
an AppKit/WKWebView host using the existing pinned purego package and CGO disabled.
Its real Intel/Apple-silicon window fixtures pass at source475124e in
[run41284368750](https://github.com/leizd/DeepSeek-Infra/actions/runs/41284368750).
The complete original Windows job also passes in that workflow. These controlled
fixtures do not qualify every desktop feature or the complete Rust/Go product.

## Native host and profile boundary

The UI runs on the initial main thread, requires actual Regular activation
state, creates a visible native WKWebView, and retains native edit menus,
chooser/dialog/download hooks, queued cancellation and the launcher close guard.
Formal Objective-C protocol metadata is adopted when present; all required
platform classes and registered public callbacks must exist. Native action,
response and dispatch callbacks must actually run in the window probe, including
completion through the original Apple block ABI. No business JavaScript bridge,
production Python or private WebKit API is installed.

A canonical profile path derives a stable distinct public CFBundleIdentifier.
Before Foundation initializes its main bundle, the UI re-execs at the same PID
into one private profile-owned app bundle containing the current native program.
Owned member links and a different profile owner are refused; bounded atomic
updates retain the profile identifier. The public persistent default data store
supports the existing Go1.27 minimum macOS13 without an implicit14-only API.
Browser data remains in WebKit's bundle-specific Library locations; legacy
profile migration/backup and actual binary-upgrade history remain unqualified.
Only local networking is allowed by the bundle ATS exception.

Default-browser opening uses the existing native UI executable on its initial
thread and NSWorkspace. The helper has a fixed sibling binary, validated public
instance URL, session-only environment,8s deadline and checked exit; the URL
never enters its argv. The zero-Python source gate audits this exact boundary
and the owned bundle self-exec separately. Arbitrary exec, program substitution,
credential inheritance and altered replacement paths remain refused. Source
admission does not prove built process trees or actual default-browser behavior.

## Cloud verification

Dispatch `native-windows-cloud.yml` with `verify_macos=true` at the exact reviewed
branch. The complete original Windows job remains unchanged. A Linux job uses
the unchanged checksum-pinned Go1.27.1 archive with CGO disabled to cross compile
the real desktop program and integration host for amd64/arm64. The matching
`macos-15-intel` and `macos-15` runners verify the source/tree/run/attempt, SDK,
all binary SHA256 and Mach-O CPU headers before native execution. Executable
fixtures, browser profiles and compiler outputs stay in the cloud; only bounded
JSON/log reports return.

At475124e, both architectures pass four native unit families and the real
visible/authenticated WebKit window/cancellation test:10 top-level native tests
in total, including12 activation-state subcases. Independent report validation
binds the actual copied executable to the original build receipt, main-thread
Regular state, real native window/layout, authenticated DOM/HttpOnly exchange,
original navigation completions and dispatched shutdown. The system omits formal
NSApplicationDelegate/WKDownloadDelegate metadata while providing their required
classes/methods; this is recorded rather than mistaken for a missing API.

The cumulative source adds an unexecuted profile continuity probe. Three actual
UI processes at one origin must write A's localStorage/IndexedDB/persistent
HttpOnly cookie, restart A and read all original values, then observe none in B.
Read pages receive no expected value to echo. The parent records actual storage
and cookie hashes/presence, binds every child to the exact source/binary/run and
same supervised PID across bundle exec, requires native windows/callbacks and
settles SIGTERM plus process exit. No SQL seed, alternate storage, sleep or retry
substitutes for retention. The helper exists only in the integration test binary.
Existing native tests, the60s window bound, pins and coverage floors are retained.

The complete475124e CI has three failing Python-version jobs because its source
admission gate predates the new bounded browser helper. The follow-up synchronizes
that explicit contract and adds24 refusal cases, including generic/aliased/dot
process replacement. The original eight source checks and explicit deployment
UNVERIFIED result remain intact. Coverage floors are unchanged.

## Remaining acceptance

Actual profile restart/isolation, default-browser opening, chooser/download/
dialog/close-confirmation behavior, the complete Rust/Go product and backend
lifecycle, supported OS releases, legacy profiles, binary upgrades, backup,
packaging/signing and full exact-source CI/Evidence remain required. Cold or
physical power loss and whole-product readiness remain unqualified. No controlled
window or source-gate result can promote those obligations.

## Primary references

- [Official runner architecture labels](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [Apple application bundle layout](https://developer.apple.com/documentation/bundleresources/placing-content-in-a-bundle)
- [Apple bundle identifiers](https://developer.apple.com/library/archive/documentation/General/Reference/InfoPlistKeyReference/Articles/CoreFoundationKeys.html)
- [Apple activation policy](https://developer.apple.com/documentation/appkit/nsapplication/activationpolicy())
- [Pinned purego native window example](https://github.com/ebitengine/purego/blob/v0.11.1/examples/window/main_darwin.go)
- [WebKit website data store](https://github.com/WebKit/WebKit/blob/main/Source/WebKit/UIProcess/API/Cocoa/WKWebsiteDataStore.h)
- [WebKit delegate dispatch](https://github.com/WebKit/WebKit/blob/main/Source/WebKit/UIProcess/Cocoa/UIDelegate.mm)
- [Apple Blocks ABI](https://clang.llvm.org/docs/Block-ABI-Apple.html)
