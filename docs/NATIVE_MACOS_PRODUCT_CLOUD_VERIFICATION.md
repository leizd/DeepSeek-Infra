# Actual native macOS product runtime gate

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

The existing [Mac window/profile workflow](../.github/workflows/native-windows-cloud.yml)
now accepts an additional explicit `verify_macos_product` input. Its original
Windows job, 15-minute controlled window job and original source/platform bounds
are preserved. The new product job has its own 60-minute limit on actual Intel
`macos-15-intel` and Apple silicon `macos-15` runners. It is not a cross-build
substitute for executing on those architectures.

The checksum-pinned Linux Go 1.27.1 compiler produces the actual control,
launcher and desktop programs for each Mac architecture with CGO disabled.
The original React application is built from its lockfile. A dedicated cloud
fixture seals every executable and static asset, including the hidden Vite
manifest. The consumer requires the same source commit/tree/run/attempt, exact
file set, SHA256/length and Mach-O architecture. This artifact stays in Actions.

Each Mac installs the unchanged Rust 1.85.0 compiler and builds the real Rust
worker and public gateway on that native host. The [runtime observer](../scripts/verify_native_macos_product.py)
then starts the original `deepseek-launch --app` with an empty disposable profile
outside the checkout and a sanitized session environment. It observes:

- The actual launcher/control/worker/gateway/UI process ancestry, public
  libproc identities with microsecond start times, executable paths and hashes.
- An on-screen WindowServer frame belonging to the actual native UI process,
  served original React HTML, authenticated control status and published native
  control provenance. Public provenance reads use SQLite read-only mode.
- Exactly one public UTF-8 file upload, reader output and equal original source
  bytes. An uncertain upload is never retried.
- Graceful launcher termination, the original observed native identities all
  leaving execution, a complete new app start, unchanged original provenance,
  advanced writer fencing and the same retained file bytes after restart.

The observer does not seed stores or inspect Rust signing keys. It sends signals
only through its own direct child's Popen wait state, never to PIDs obtained
from a process list. The direct dynamic dependencies and actual build logs are
retained. Only JSON/text/log reports return to the local host; executable
fixtures, profiles, compiler outputs and caches remain in the cloud.

The observer also retains seven original HTTP response bodies: healthy control
status before/after, the served React index, the single upload result, the reader
result, and the file source before/after the complete app restart. Each receipt
records the actual returned status and received bytes with its method, URI,
length and SHA256. The upload file ID must bind both source URIs; the original
UTF-8 bytes must survive restart unchanged; and the served React index must match
the exact static build fixture. Authentication headers are never recorded, and
a response containing the ephemeral bearer is refused before writing. Existing
evidence files cannot be overwritten. A separate raw-body checker recomputes
these relationships without relying on the producer's PASS fields.

Source `6fd0a5de` has six successful platform jobs, including the actual app
workload on both Mac architectures, but its seven-member reports did not retain
these original HTTP response bodies. Its
process/window/build observations remain useful; that older success must not be
used as independently rechecked file-response/persistence evidence for this
child source. This stronger child gate must execute again on both actual Macs.

The public ABI references are [Apple proc_bsdinfo](https://github.com/apple/darwin-xnu/blob/main/bsd/sys/proc_info.h)
and [WindowServer window information](https://developer.apple.com/documentation/coregraphics/cgwindowlistcopywindowinfo(_:_:)).
The observer checks its libproc ABI against its own actual PID, parent and UID
before interpreting other process identities.

Passing this gate qualifies only the stated app-frame/public-file/graceful-restart
workload for that exact source and two runner OS images. It does not qualify
React DOM interaction, user close confirmation, native chooser/dialog/download,
default browser, binary upgrades, legacy profiles, backup restoration, all
supported macOS versions, complete dynamic dependency closure, physical power
loss, all recovery phases or every product feature. Zero-Python workload and
complete desktop/product readiness flags remain false. These require their own
successful evidence; full migration remains **NOT_READY**.
