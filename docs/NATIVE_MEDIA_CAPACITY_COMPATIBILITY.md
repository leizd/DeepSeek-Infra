# Retained media-library capacity compatibility

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

The original `library._load_store` reads regular JSON without a byte cutoff;
`_write_store` retains the last 1000 records. Metadata normalization accepts
multiple strings of up to 120000 characters each. The current native worker
instead bounds library reads, planned publications and retained-publication
confirmation at 128 MiB. That bound has no original product approval.

The new **cloud-only counterexample** keeps the original 18-record fixture
unchanged and appends 600 valid metadata records with two 120000-character
ASCII fields each. It stays below the original record limit and contains at
least 144000000 metadata bytes, above 134217728 bytes. The actual file must be
between 128 and 160 MiB for this fixture; this is a test bound, not a product
limit. Small offline checks call the unmodified original normalizer without
serializing or writing the full fixture locally.

The existing process verifier seeds only disposable media data while its actual
native launcher is stopped. It invokes the original authenticated HTTP PATCH,
then reads the original writer's real retained file in another offline oracle
process and requires all 600 metadata records to survive. The native launcher
must start healthy, preserve its auth token, advance the actual writer fence,
read the original record and refuse an unauthenticated write without mutation.
The same small legal authenticated PATCH must genuinely succeed through the
actual Go/Rust boundary, retain its effect identity, fence the workspace and
survive a complete launcher restart. Original response, timestamp, generation,
journal and restart assertions are retained. Both before and after restart, an
independent original reader must recover all 600 exact normalized records and
the complete native media-file hashes must stay unchanged. Neither SQL rows nor native
outcomes are seeded by the new capacity fixture.

Record actual request duration and process-identity-bound procfs RSS/high-water
samples. Preserve the original 30-second oracle, 10-second HTTP request and
15-minute step bounds. Never repeat an uncertain write. Save the observed HTTP
reply before resource diagnostics so diagnostic failure cannot erase it.
Return only bounded reports, raw small replies, logs and source context; the
large library and build caches stay on the cloud runner.

All original 34 checks and seven coverage steps remain, plus one mandatory
capacity process step. Production Rust/Go/Proto, current limits, dependencies
and original coverage floors are unchanged in this diagnostic source. Until an
actual run reaches the legal request and returns the relevant failure, this is
an **unproven counterexample**, not runtime RED or a capacity implementation.

The eventual repair must preserve original legal data with bounded resource
use and the original operation's durable recovery. Increasing an arbitrary
cutoff alone is not full capacity compatibility. Unique writers, admission,
epoch fencing, immutable bytes and proofs, `EFFECT_UNKNOWN`, retained recovery
cases and every full-product requirement remain mandatory. **NOT_READY**.
