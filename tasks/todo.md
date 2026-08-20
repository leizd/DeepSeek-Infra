# 4.5.8 Durable Storage Control Plane Checklist

## Foundation

- [x] Verify clean 4.5.7 baseline at `008a278984dec1b5d207cde4b7538b24a7d437aa`
- [x] Freeze object-set-v1, Receipt v4, Commit v4, FastCDC v3, and randomized Age
- [x] Record dependency-ordered plan and hard Gate matrix
- [x] Create `codex/4.5.8-storage-control-plane`

## Gate A — Chain-preserving retirement

- [x] RED: prove current retirement deletes Receipt/Commit
- [x] Add commitment-bound Target-local retirement marker
- [x] Keep Formal Receipt/Commit history byte-identical
- [x] Make payload GC live-copy and active-job reference aware
- [x] Teach audit governed retirement vs corruption

## Gate B — Cross-process authority

- [x] Add `.backup-control/control.sqlite3`
- [x] Migrate Policy JSON into SQLite CAS authority
- [x] Make update/delete and promotion revisions process safe
- [x] Make Target drain/activation/topology generation process safe
- [x] Add real multi-process same-revision race tests

## Gate C — Placement and capacity

- [x] Pass `logicalRecoveryPointId` through placement calls
- [x] Compute copy/Failure Domain constraints from one replica set
- [x] Add independent `minRegions` enforcement
- [x] Add provider/jurisdiction/cost metadata and operator-estimate validation
- [x] Replace 500 MiB fallback with confidence-aware physical evidence
- [x] Fail closed on unknown Force Full capacity
- [x] Apply matching RecoveryClass P90 RTO eligibility

## Gate D — Multipart reconciliation

- [x] RED: remote-ahead, missing-upload, and conflicting-part cases
- [x] Reconcile local checkpoint against provider `ListParts`
- [x] Restart missing uploads from zero
- [x] Abort/quarantine ETag or size conflicts

## Gate E — Production QoS

- [x] Implement independent global/source-read/destination-write buckets
- [x] Share tokens and DR reservation across processes
- [x] Wire P1 Primary Publish
- [x] Wire P0 Restore
- [x] Wire P2/P3 Repair and required Replication
- [x] Wire P4 Scrub/Drill and P5/P6 Rebalance/Drain/best effort
- [x] Prove measured foreground reservation and background throttling

## Gate F — Maintenance and Drain

- [x] Add durable StorageMaintenanceSupervisor ownership and tick
- [x] Add keyset cursor covering arbitrary Target history
- [x] Route Drain destination through production Placement Planner
- [x] Block drained on writer/run/recovery/hold/job/retirement dependencies
- [x] Prove restart convergence and >500 Recovery Points

## Gates G-H — Evidence, compatibility, release

- [x] Add `run_storage_control_plane_minio_e2e.py`
- [x] Use three independent MinIO endpoints, boto3, S3TargetStore, production workers, and real Age
- [x] Remove real-Evidence ownership from legacy fake/stub runner
- [x] Add CI job and version-derived Evidence contract
- [ ] Update 4.5.8 version and release surfaces
- [ ] Run frontend check, Ruff, Mypy, full pytest >=95%, vendor JS check
- [ ] Run offline eval/security/release gates
- [ ] Perform final multi-axis code review
- [ ] Report CI-only Evidence as pending unless genuinely executed
