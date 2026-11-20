# Retained media-library capacity compatibility

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

## Original behavior and measured failures

The original `library._load_store` reads regular JSON without a byte cutoff;
`_write_store` retains the last 1000 records after the mutation. Metadata accepts
multiple strings of up to 120000 characters each. The native 128 MiB cutoff
had no original product approval. The mandatory cloud fixture preserves the
original 18 records and adds 600 valid records with two 120000-character fields.
The actual file is 144182145 bytes and remains below the original retention
count. Its 128–160 MiB fixture bound is not a product limit.

Source `70df4d57` actually returned original HTTP200/native HTTP503
`EFFECT_UNKNOWN`; sources `b8211a50` and `52da6615` retained that failure.
A missing journal row, unchanged library or provider absence is never evidence
to repeat an uncertain write. All raw failures remain preserved.

A separate actual-journal test at `e6a76058` showed a physical publication before
the unchanged 2 MiB v1 signer rejected its result. Source `b8211a50` passed the
same regression by sharing the original validator before APPLYING, generation
or publication. Validation issues no signature; signing follows confirmed effects.
The original domain, validator, v1 bound and test remain unchanged.

## Independently qualified parent

Source `52da66155658891ea2c4355de7f4bc05a46b609e`,
[run41434727285](https://github.com/leizd/DeepSeek-Infra/actions/runs/41434727285),
passed the actual original/native/retained HTTP200 large-record case. Its original
4321460-byte typed record survived all metadata and a complete launcher restart;
the v2 signed envelope was 447 bytes. The separate record and dispatch digests,
original claim, generation, raw replies and original-reader comparisons were
checked. This is controlled public HTTP, not promotion or GUI evidence.

That exact parent also passed Clippy, 1679 Rust tests with one original Redis
ignore, 15 workspace crates, and independently counted LCOV 95108/115208 lines
(82.55329491007569%, original floor 80%). Eight targeted Go top-level tests passed.
Those are not full Go race/95% or Android qualification. The workflow still
failed the mandatory 144 MB library case, and does not qualify descendants.

## Streamed image candidate: execution pending

The current candidate removes the arbitrary whole-library byte cutoff without
materializing the entire library for journal custody or publication. File images
bind exact byte length and SHA-256. Active images are opened without links and
verified through EOF; publication streams into a synchronized temporary file,
checks the original lease immediately before atomic replacement, then confirms
the original bytes. Directory links are synchronized on Unix.

A temporary flat record spool plus a disk SQLite offset/digest index resolves
the complete JSON document and final top-level `media` key before normalization.
Discarded arrays consume no normalization clocks. Source/spool/index errors
propagate; an early syntax error drains the input so it cannot hide a later I/O
or final digest failure. Malformed complete JSON retains the original empty-store
semantics. First duplicate selection, delete-all matches, normalization order and
post-mutation last-1000 retention stay explicit. This bounds aggregate-record
memory, not arbitrary individual record/string width. Go typed result decoding
still materializes one record; its memory qualification remains open.

Private journal schema2 adds file-image manifests while preserving exact schema1
rows and original signed bytes. The old schema and workspace/signer identity are
validated before the transactional additive migration. A durable RETAINING row
owns both private image names before copying. PREPARED and SEALED commit together.
Failed copies or SQL preparation leave discoverable private ownership; bounded
fair retries retire unsealed preparations under the exclusive workspace gate.
This never settles a Go effect from row absence. Pending effects require both
verified raw images; positive PREPARED dispatch abort remains a separate phase proof.

Terminal receipt loading constructs inert image descriptors and verifies the
original signature without visiting image paths. Raw images may be collected
only after a durable signed terminal result and independent retained result custody.
Collection failure never hides the receipt. The immutable collection digest binds
the exact signed outcome; indexed bounded retries persist their cursor before
fallible cleanup, so restarts preserve fairness and completed history is not
scanned. A separate-connection startup/30-second maintenance task retires idle
images; terminal RPCs perform no image cleanup and do not acquire its workspace
gate. Preparation also processes a bounded pending batch.
Only the two known raw files and an empty operation directory are removed. Original
CREATE/PROCESS results, sources, segment/index evidence and PATCH result records
remain retained. Legacy inline journal BLOBs remain unchanged and may still be large.

New cloud tests cover partial-copy restart, failed prepared insertion, immutable
manifests/collection hashes, bounded fair cleanup, missing/corrupt active images,
positive abort, terminal replay through an inaccessible symlink path and a held workspace gate,
retained-result custody before GC, repeated-restart fairness, legacy
PREPARED/APPLYING/APPLIED rows, parser equivalence and 100 consecutive updates.
The original PROCESS/index crash cases, signability regression and mandatory
actual HTTP capacity case remain enabled. Native compilation/tests are cloud-only.
Source checks and test definitions are not execution evidence.

## Remaining acceptance

Actual 144 MB public-HTTP success, all new and original recovery tests, measured
resource behavior, original coverage floors and exact-source CI/platform/Evidence
must pass. Explicit journal export/downgrade and cross-version rollback remain
unfinished; accepting old rows is not a reversible migration claim. Windows
directory/power-loss durability, arbitrary-width record memory, old unsignable
APPLYING recovery and real-provider qualification remain open.

The original 30-second oracle, 10-second HTTP request and 15-minute step bounds
are preserved. Report actual request duration and process-identity-bound RSS/high
water; never retry uncertain writes. Only bounded reports and source return from
cloud runners, never libraries, compiler caches, images or volumes.

All server/desktop/Android/background functionality, unique writers, admission,
epoch fencing, complete recovery/cutover, Fleet/proofs, zero production Python
and server TypeScript, security/performance/upgrade and release evidence remain
required. Original MCP arbitrary pytest execution remains explicitly unfinished
without a Python production exception. Full readiness remains **NOT_READY**.
