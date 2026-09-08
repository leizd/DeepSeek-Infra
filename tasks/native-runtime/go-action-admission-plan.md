# Go action admission and renewable resource leases

Status: implementation in progress; production authority remains disabled.
This is a dependency of ADR-0049, not a change to the frozen action state machine.
The original recovery `tasks/plan.md` and `tasks/todo.md` remain untouched.

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
