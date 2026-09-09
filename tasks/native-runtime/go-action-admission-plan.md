# Go action admission and renewable resource leases

Status: implementation in progress; production authority remains disabled.
This is a dependency of ADR-0049, not a completed action-state parity migration.
The original recovery `tasks/plan.md` and `tasks/todo.md` remain untouched.

### State-parity correction (2026-09-09)

Earlier references below to a "frozen action graph" mean the **current native
qualification graph**, not the full Python 4.8.0 action lifecycle. Direct inspection
of `a37735c68398fc8f795babaa269e2de6a5acd567:deepseek_infra/infra/workspace/resilience_action_journal.py`
confirms expired active actions become RECONCILING, with VERIFYING and
ASSESSING_EFFECT also participating in admission and renewal. Go's current
`schema.go` graph does not implement those states. Moreover,
`compat/native-runtime/v1/state/legal_transitions.json` contains peer trust,
effect enums and four fail-closed labels, **not** a complete action transition oracle.
Thus the passing corpus/Go transition tests do not prove full action-state parity.

Required next work is to capture the actual baseline admission/reconciliation/
verification/compensation behavior in a versioned oracle and migrate the native
graph with explicit schema/history compatibility. Do not invent an UNKNOWN
self-transition simply to unblock repeated takeover, silently relabel Python's
RECONCILING behavior as UNKNOWN, or discard current intent/lease history.

## Invariants and boundaries

- The Go control database is the only writer of action admission and resource
  reservations. Rust receives fenced commands, never a database connection.
- A fresh admission must validate the persisted action revision/epoch, all budget
  dimensions and all resource conflicts in the same immediate transaction as the
  CLAIMED record, its event, and its lease/reservations. Rejections roll everything
  back, including opportunistic writer renewal. No caller-supplied live counts.
- Resource reservations survive lease expiry until the prior effect is reconciled.
  Time passing is not proof that a remote request stopped. Unknown effects continue
  occupying capacity. No automatic preemption or unsafe lock stealing.
- Renewal is compare-and-swap on action ID, execution epoch, owner and claim token,
  requires a still-live action and writer lease, and renews all resources atomically.
  Missing, substituted or stale resource ownership rolls back the whole renewal.
- Bound actions cannot use generic Put/ClaimStorageDispatch to bypass their action
  lease. Legacy qualification records retain their frozen semantics, but cannot be
  silently adopted as native admission records or used as production proof.
- Storage operation identities from schema v4 remain immutable. Takeover must retain
  the old dispatch identity and reconcile its effect before any replacement command.
- No endpoint, transport authentication, production throttle configuration or
  provider integration changes in these prerequisite slices. Transport auth remains
  awaiting the separate human decision in worker-execution-plan.md.

## Ordered increments

1. **Reusable transaction-owned record write (implemented locally).** Separate record preparation and the
   transaction-local journal write from transaction ownership. Both generic writes
   and future admission use the same revision/history/epoch validation. Test that
   the transaction-local helper neither commits nor renews the writer itself;
   caller rollback undoes record, event and dispatch together. Recheck writer expiry
   at commit for every generic write, not only dispatch claims.
2. **Additive admission persistence.** Versioned Go-local lease/reservation metadata,
   strict schema validation, no legacy backfill, migration failure rollback, and a
   downgrade barrier preserving admission history. Define typed internal inputs
   before introducing the schema. No frozen table or wire rewrite.
3. **Fresh atomic admission.** Derive scope from persisted action metadata, enforce
   global/target/policy/failure-domain and throughput budgets, acquire resources and
   claim atomically. Reject unqualified active history rather than undercounting it.
   Tests: each dimension, two contenders/one slot, late failure and expiry rollback.
4. **Fenced renewal and settlement.** CAS-renew action plus exact resources, reject
   expiry/stale owner/token/epoch, and make state/dispatch writes lease-aware. Release
   resources only with qualified terminal settlement; retain unknown effects.
5. **Coordinator and takeover qualification.** Integrate real control lifecycle,
   action heartbeats and signed live-epoch installation after the auth decision;
   force-terminate real processes, retain dispatch identity and reconcile against
   actual Three-MinIO before claiming provider-backed takeover.

Each increment must pass focused tests before the next. Full Go tests/vet,
internal/pkg coverage >=95%, race checks with the isolated Windows compiler and
frozen contract/codegen gates are required for the resulting local increment.
Exact-head CI and provider-backed evidence remain distinct release gates.

## Local evidence for increment 1 (2026-09-08)

- RED: ordinary PENDING -> CLAIMED and PENDING -> FAILED_BEFORE_EFFECT writes
  returned success after the transaction's renewed writer deadline had expired.
- GREEN: every generic write now checks the clock immediately before Commit. On
  expiry it rolls back the action, control event and tentative writer renewal,
  and leaves the cached writer unchanged. This is a pre-commit clock check, not
  a guarantee about wall-clock time spent inside SQLite Commit/fsync.
- The private transaction-local helper neither begins nor ends a transaction,
  nor renews the writer. Caller rollback removes record/event/dispatch together.
  A rejected second write leaves the caller's transaction and first pending write
  intact until the caller rolls back. Prepared canonical payload and dispatch
  operation metadata remain unchanged by later caller mutations.
- Focused regressions, full Go tests and vet pass. The unchanged internal/pkg
  coverage gate is 95.5%. Frozen contract/codegen checks pass: 40 corpora,
  29 corpus versions, 43 domains, seven proto files, ten generated outputs.
- Initial Windows full-race invocation passed 14 packages, but the store package
  hit the local 240-second whole-package timeout. There were no race warnings or
  assertion failures. At timeout, TestRenewWriterCancellationBeforeCommitPreservesPriorLease
  had run for one second and was in SQLite's v4 schema migration, not waiting on
  a leaked transaction. The log is retained at
  `.tools/native-race-20260826/go-control-transaction-race.jsonl`, SHA-256
  `0f7258840e26e59c23d4265b342921233bf10fd6081d4b49c2eb871632b649f4`.
  The store-only rerun uses a bounded 360-second timeout; no test, assertion,
  database creation or migration step is removed, and no CI timeout is changed.
- Store race rerun passes in 231.733 seconds: 319 passed test entries and one
  existing Windows symlink-privilege skip. Together with the 14 passing packages
  in the first run, all 15 test packages and 494 distinct test entries have passed
  under race instrumentation, with zero race warnings. This is a successful
  store-only follow-up, not a claim that the initial full command succeeded.
  Rerun log: `.tools/native-race-20260826/go-control-transaction-store-race.jsonl`,
  SHA-256 `76f3ed6023a13fda83bc21b568e5f342d3babee5ff835212557310c922d8899a`.
  The existing main-process helper builds a normal child binary; this does not
  claim every child executable was race-instrumented. Test processes were reaped.
- No schema, frozen state transitions, production authentication, admission policy,
  resource tables or provider behavior changed. Increments 2-5 are not implemented
  by this transaction refactor. No new provider or release PASS is claimed.

## Go-local admission implementation (2026-09-09)

Increments 2-4 now have a store-level implementation under qualification. This is
not completed coordinator integration or qualified terminal-effect settlement.
No production caller currently invokes the new lease methods, and the existing
coordinator cannot use its generic dispatch path for a lease-bound action.

### Internal contract

- `AdmitAndClaimAction` reads the persisted action and validates its canonical
  digest/history. Default budgets are 3 concurrent actions, 20 claims/hour,
  2 concurrent actions per target/policy and 1 distinct failure domain. Omitting
  policy selects these defaults; there is no budget-disable flag. Positive typed
  limits override individual defaults. These inputs are not a production policy
  source: binding them to a durable, authorized policy revision is still required.
- Global/hourly limits and target/policy/domain counts are evaluated inside the
  same immediate transaction as claim, lease, resource set and events. Active
  peers must have valid native lease/history/resource bindings; corrupt or legacy
  unqualified active peers are rejected, not skipped. Expiry alone never frees
  another action's capacity or reservations. Hourly accounting currently uses
  CLAIMED control events, not the Python creation-time accounting; parity review
  and recovery-specific budget semantics remain required before cutover.
- Explicit resource keys add to, never replace, persisted scope. All supported
  target aliases are deduplicated and reserved; the selected policy/backup aliases
  retain their existing precedence. The sorted exact set is recorded on immutable
  lease events. Update/delete/replace and a second claim manifest for one epoch
  are rejected by schema guards. Every renewal, leased dispatch and settlement
  verifies this set, owner, epoch, writer, deadline and claim history together.
- Fresh PENDING -> CLAIMED preserves the already allocated positive action epoch.
  A first expired CLAIMED/EXECUTING takeover advances that journal epoch exactly
  once and enters EFFECT_UNKNOWN, rotating the Go-local random CAS token. Missing
  leases, corrupt epoch jumps, scope expansion and epoch overflow are rejected.
  Previously requested extra resources survive omitted takeover arguments. The
  acquired-at timestamp and returned lease match the new persisted claim.
- Expiry is exclusive (`now >= leaseUntil`). Renewal cannot shorten a deadline
  or overflow it, and the original claim must still be live at the pre-commit
  check. All mutation paths check the writer and action deadlines before Commit;
  a clock reversal or elapsed deadline rolls back the entire transaction. As in
  increment 1, this does not measure time spent inside SQLite Commit/fsync.
- A successor Go writer cannot reuse the prior writer's claim token. Generic
  `Put` and `ClaimStorageDispatch` reject bound actions. The explicit
  `ClaimLeasedStorageDispatch` requires the matching current token and preserves
  the admitted payload while atomically recording EXECUTING plus immutable intent.
  That token is neither a transport credential nor provider authorization.
- `CompleteAction`/`FailAction` validate the canonical payload and frozen legal
  transition, and release resources transactionally. Their caller must eventually
  supply a qualified effect/no-effect outcome; these methods alone do not verify
  a signed Rust/provider proof and are not exposed as production success authority.

### Persistence and evidence boundaries

- Go schema v5 adds action leases, immutable lease events with the resource
  manifest, and resource reservations. v4 upgrade preserves existing control and
  storage-dispatch history without adopting or backfilling legacy actions.
  Any retained admission row/event/reservation blocks rollback to zero.
- v5 has not been released. The earlier uncommitted experimental v5 shape without
  the manifest/replace guard is not accepted as this schema. Preserve any such
  development database for diagnosis; do not delete its history or relabel it as
  v4. A separately qualified migration would be needed if it contains useful state.
- Regression tests reproduced and repaired expiry equality, action expiry during
  a transaction, old-writer token reuse, shortened/overflowed renewal, skipped
  resource scope, missing/substituted reservations, anonymous dispatch, mutated
  dispatch payload, corrupt journal renewal, unsafe takeover repair, disabled
  default budgets and ignored corrupt active peers. Rejections check persisted
  and cached writer state as well as action, event and reservation rollback.
- Real SQLite injected INSERT/UPDATE/DELETE errors cover admission, takeover,
  renewal and settlement rollback, including errors after earlier writes. These
  are database fault tests, not remote-effect evidence. Concurrent unit tests
  currently contend through one Control handle, not independent processes.
- The former crash-named admission test is now explicitly a **graceful reopen**
  test, preserving old operation identity and expiring the action lease. No
  actual process kill or provider effect is claimed for it.
- Full Go tests, vet and frozen contract/codegen checks pass. An initial coverage
  run reported 94.8%, below the unchanged 95% gate. After adding SQL-failure and
  integrity regressions, the final internal/pkg coverage gate passes at **95.5%**.
  Profile/log: `.tools/native-race-20260826/go-admission-coverage-final.out` and
  `go-admission-coverage-final.log`. No package or assertion was excluded to pass.
- The isolated Windows full `go test -race ./... -count=1 -timeout=600s -json`
  invocation passes: 15 test packages, 617 passed test entries, zero failures and
  zero race warnings. Store completes in 218.339 seconds. One existing Windows
  symlink-privilege test is skipped; this is not a zero-skip claim. Generated-only
  protobuf packages have no tests. The existing command helper still builds a
  normal child binary, not a race-instrumented child executable.
  Log: `.tools/native-race-20260826/go-admission-race.jsonl`, SHA-256
  `31ee68f40918e49bade20b5fba7a3bef0e54a19058cb67598340950454a06375`.
  Test processes were reaped. No provider-backed or exact-head release PASS is
  claimed by these local results.

### Remaining dependency work

1. Bind production admission to durable authorized policy identity/revision and
   resolve fresh-claim epoch, hourly accounting and takeover budget parity.
2. Integrate leased dispatch, heartbeat renewal and cancellation into the real
   coordinator, including typed proof-qualified settlement. Keep production
   authority disabled until those paths and Rust live-epoch installation qualify.
3. Define repeated EFFECT_UNKNOWN lease takeover without changing the frozen
   action transition graph. Currently it fails closed; it is not a completed
   recovery loop. Preserve the old dispatch identity throughout reconciliation.
4. After the separate transport-authentication decision, qualify signed operations
   and actual controller/worker process kills against real Three-MinIO, followed
   by the required Federation/provider gates and exact-head CI.
5. Continue all remaining production surfaces in the complete zero-Python scope.
   This Go store increment does not migrate server/desktop/Android/media/MCP or
   establish Rust ownership of every production security/data-plane path.

### Explicit claimed execution (2026-09-09, local qualification)

Implemented contract: `ExecuteClaimedStorageAction(ctx, claim, request)`
accepts a previously admitted Go-local lease, not a policy override or an inferred
token. It is separate from the existing unbound qualification entry point; neither
may enable production authority while cutover and transport auth are unqualified.
The implementation renews before dispatch, claims the exact immutable intent,
renews action/resources/writer together during RPC, cancels RPC on renewal failure,
stops and joins the heartbeat before settlement, and revalidates the claim afterwards.
Cancellation is uncertainty, never no-effect evidence. A late successful ACK after
lost lease cannot settle. A bound CONFIRMED/APPLIED result may use the local typed
completion path; a no-effect outcome requires a bound recorded NOT_APPLIED result,
not an arbitrary returned error code. Other outcomes retain resources through
`MarkActionEffectUnknown`; if the claim has already expired, leave EXECUTING durable
for successor reconciliation. These are control-path qualification tests until
signed operations and real provider-backed process-kill evidence are available.

Specific boundaries and local evidence:

- A nil request or mismatched supplied action/epoch is rejected before renewal or
  RPC; the caller's protobuf is cloned, not rewritten. Missing native lease methods
  never fall back to generic Put/dispatch. Production-authoritative mode still
  rejects the call before any native claim mutation.
- The default 60-second action renewal does not assume the writer has the same
  lifetime. Heartbeat intervals are capped by one third of the shorter remaining
  lease (and the configured interval), with bounded integer-to-duration conversion.
  A one-second writer regression initially timed out waiting for cancellation;
  the interval correction makes it pass. This is not a substitute for Rust-side
  signed live-lease enforcement while a Go process is paused or killed.
- The RPC inherits cancellation with an explicit failure cause through Go's
  [context API](https://pkg.go.dev/context#WithCancelCause). The heartbeat owns and
  stops its [ticker](https://pkg.go.dev/time#NewTicker), and is joined before any
  terminal store write. No background lease renewal intentionally survives the call.
- If cancellation is observed after durable intent but before RPC, no request is
  started. An in-flight cancellation, missing/mismatched result, or an error without
  a bound NOT_APPLIED record retains uncertainty. A late APPLIED ACK following
  renewal failure cannot settle or release resources.
- `MarkActionEffectUnknown` shares the existing transaction, history, canonical
  payload, writer and action-lease checks. It appends the frozen legal state
  transition without terminating the lease, changing its manifest, releasing
  reservations or replacing dispatch identity. Unknown actions remain renewable.
- Focused tests use real Go SQLite and an RPC double for controlled ACK loss,
  cancellation and late responses; those tests do not prove provider effects.
  New tests reproduced nil-request panic, overwritten request fences, dispatch
  after cancellation and the short-writer heartbeat failure before their fixes.
- Rebuilt the real no-S3 Rust worker using Rust 1.85 GNU. Five selected real
  Go/Rust gRPC boundary tests passed, including the new explicit leased execution
  against an uninitialized, mutation-denied worker. Its actual auth rejection
  leaves EFFECT_UNKNOWN, the reservation and exact operation ID durable in Go.
  No MinIO/provider was contacted, and the spawned Rust worker was reaped.
- Final post-interval-correction full Go tests and both normal/integration vet
  checks passed. The unchanged 95% internal/pkg coverage gate passed at 95.3%.
  Frozen contracts/codegen remain 40 corpora, 29 versions, 43 domains, 9 command
  codes, 7 proto files and 10 generated outputs. Gofmt and diff checks passed.
- The final isolated Windows race run exited 0: 15 passed packages, 647 passed
  test entries, zero failures/race warnings, store 441.685 seconds. One existing
  Windows symlink-privilege test skipped; this is not a zero-skip full-platform
  qualification. Evidence: `.tools/native-race-20260826/go-leased-execution-final-race.jsonl`,
  SHA-256 `0971999a6239d390215d55749dc15cc5b705bcdc9831cd1c77430eab464e317d`.
  The corresponding `go-leased-execution-final-full.log`,
  `go-leased-execution-final-coverage.log` and
  `go-leased-execution-final-rpc.log` capture the other final runs. These supersede
  the earlier intermediate snapshot, not provider-backed or exact-head CI evidence.

Still outstanding: a main-process/scheduler caller with durable authorized policy,
lease-aware effect reconciliation and repeated-unknown takeover, transport auth,
signed live-epoch/operation installation in Rust, provider-qualified settlement,
and actual provider-backed process kills. The current qualification request still
carries bounded test payload bytes through Go; production payload custody must move
to Rust-owned prepared handles/streams, not become a permanent Go byte-moving path.

### Leased recovery identity lookup (2026-09-09, local qualification)

`GetLeasedStorageDispatch(actionID, claimEpoch, claimToken)` now resolves the
original immutable storage intent from a live native claim. It verifies the
writer, exact lease/resources, journal history and claim event in one read-only
transaction. For an existing supported takeover, it follows the exact predecessor
of that claim event to the old dispatch epoch, not an arbitrary latest older
operation. The returned dispatch retains its original action/epoch/operation,
revision, writer token and timestamp. The method does not renew, rewrite, dispatch
or settle anything. Missing intent returns unbound; that is never no-effect proof.
The existing exact-epoch reader shares the same canonical/history validation.

Local tests cover live and successor lookup, missing dispatch, wrong/expired claim,
writer loss, clock rollback, expiry during read, missing reservations, terminal
leases and deliberately corrupted original intent. Actual Go process termination
after a durable leased dispatch and subsequent real-clock writer/action takeover
also preserves the original intent and uncertain resource reservation. This child
uses the real Go store, not the production deepseekd main or a storage provider;
no worker RPC, successful effect seed, Python writer or MinIO call is involved.

Verification: focused tests, full Go tests and vet passed. Full isolated Windows
race exited 0: 15 packages and 670 test entries passed, zero failures/race warnings,
one existing Windows symlink-privilege skip; store 239.338 seconds. The killed-owner
regression passed in 3.570 seconds under race. Log:
`.tools/native-race-20260826/go-leased-recovery-race.jsonl`, SHA-256
`45a4d240d6126d185125b765ff48b21d70e3488c5508091bbae3599ad9be1c6c`.
The unchanged 95% internal/pkg coverage gate passed at 95.1%, after correcting
the command's relative profile path (the report subprocess runs from `go/`). This
is lower than the preceding slice's 95.3%; the gate was not weakened. Full test and
coverage logs/profiles use the `go-leased-recovery-` prefix in that same directory.
Contract/codegen checks still pass at 40 corpora, 29 versions, 43 domains, 9 command
codes, 7 proto files and 10 generated outputs; their action-parity limitation above
remains explicit. Gofmt and diff checks passed.

This is the durable lookup dependency, not a completed reconciliation loop.
The leased coordinator must still consume it, query the old Rust effect while
renewing the current claim, and settle only a qualified bound result under that
current claim. Production authorization, provider evidence and full baseline
state parity remain mandatory before cutover. Original Python retirement and
all server/desktop/Android/media/MCP migration requirements remain in scope.

## Sources

The existing Python admission and renewal semantics were inspected directly in
resilience_action_journal.py and resilience_resource_locks.py. Their transaction
invariant is preserved, not their permissive expired-lock reuse or the preemption
early-return path that skips later budget checks. Such policy differences need
explicit parity/cutover review; they are not silently declared compatible.

Go uses [sql.Tx-owned operations](https://go.dev/doc/database/execute-transactions):
no nested sql.DB calls inside a transaction and no helper-owned Commit/Rollback.
The existing modernc.org/sqlite v1.58.0 DSN selects immediate transactions, matching
[SQLite's single-writer transaction semantics](https://www.sqlite.org/lang_transaction.html).
