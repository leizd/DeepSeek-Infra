# Native Windows app platform verification

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

`native-windows-cloud.yml` runs for matching Windows-input pushes on the reviewed
`codex/rust-cloud/control-rpc-mtls-*` branch or an explicit manual dispatch. It is
a disposable GitHub-hosted Windows2025 platform gate and builds the checksum-pinned Go control/launcher/WebView2 host,
Rust worker and edge, and the existing React application from one clean revision.
Compiler outputs and caches remain on the cloud runner; only reports return.
The [runner image inventory](https://github.com/actions/runner-images/blob/main/images/windows/Windows2025-Readme.md)
describes the hosted image. Runtime availability is established by the actual
production WebView2 loader, rather than inferred from the inventory.

The production `deepseek-launch --app` starts a genuinely absent profile without
offline authority seeding. The gate observes its five native processes, a visible
Win32 window belonging to its desktop child, published native origin and twelve
Go-owned domains. The existing explicit WebView2 integration test then loads the
real React app in the real platform host and uploads, reads and downloads one
nonempty file. Only that test host enables an observation message callback;
production host messages and developer tools remain disabled. WebView2 host
configuration follows the [Microsoft environment API](https://learn.microsoft.com/en-us/microsoft-edge/webview2/reference/win32/webview2-idl).

An ordinary window-close message must stop the original native tree. Restarting
the same app/profile must create different processes, advance the Go writer
fence and retain exact original origin/publication/public-file hashes. The
original uploaded bytes must still be readable, followed by another successful
window close. SQLite inspection is readonly and never advances a fence or
repairs a store. Python remains an offline verifier outside the production tree.

This gate can fail at Windows compilation, WebView2 availability, native startup,
window behavior or recovery. An uploaded report or compiled executable alone is
insufficient. The controlled gate does not qualify every desktop feature, the
user's Windows11 machine, real provider effects, Android, power loss, credential
renewal or full migration. Original complete CI and95/80coverage floors remain;
product readiness stays `NOT_READY` until every retained requirement is proved.
