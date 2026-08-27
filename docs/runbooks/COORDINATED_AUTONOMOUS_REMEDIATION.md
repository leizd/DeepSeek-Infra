# Runbook: Coordinated Autonomous Remediation & Crash-Recoverable Execution (DeepSeek Infra 4.7.2)

## 1. Overview & Architecture

DeepSeek Infra 4.7.2 elevates the storage resilience substrate from single-action execution to **Coordinated Autonomous Remediation & Crash-Recoverable Execution**. It resolves complex multi-risk situations through deterministic DAG coordination, strict transactional resource locking, CAS epoch-fenced execution leases, in-flight crash effect reconciliation, real post-condition outcome verification, and closed-loop scoped risk reduction.

```
+-----------------------------------------------------------------------------------+
|                        Multi-Risk Assessment Engine                                |
|    - Assesses replica lag, capacity exhaustion, DR drill staleness, authority     |
|    - Emits RiskSnapshot v1 with deterministic SHA-256 riskDigest                  |
+-----------------------------------------+-----------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
|                      Resilience Coordinator (Gate H & I)                          |
|    - Emits typed ResilienceCoordinationPlan v1 with deterministic planDigest       |
|    - Builds multi-risk DAG: [CREATE_REPAIR_JOB] -> [CREATE_REBALANCE_JOB]         |
|    - Invariant: never touches minCommittedCopies or minFailureDomains              |
|    - Authority Circuit Breaker: blocks mutations if authority degraded            |
+-----------------------------------------+-----------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
|               Immutable Action Journal & CAS Fencing (Gate A & B)                 |
|    - Create-once Plan & Action Identity (409 on mutation conflict, idempotent)    |
|    - CAS Claim: increments executionEpoch, generates claimToken, sets leaseUntil  |
|    - Transactional Resource Locks: (backup:pid:bid, target:tid, policy:pid)       |
+-----------------------------------------+-----------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
|                   Crash-Recoverable Execution & Effect Reconciliation             |
|    - Persists effectHandle (repairId, jobId, resilienceActionId)                  |
|    - Worker crash recovery reconciles in-flight jobs (Gate C)                     |
|    - Stale workers with expired epochs are fenced out with 409                    |
+-----------------------------------------+-----------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
|              Real Outcome & Scoped Risk Verification (Gate E & F)                 |
|    - Repair: verified to complete, authenticated with Receipt v4 + Commit v4      |
|    - Rebalance: executed to complete, destination copy committed & authenticated  |
|    - Scoped Risk Reduction: compares severityBefore vs severityAfter on subject   |
|    - Emits cryptographic Decision Proof v3                                        |
+-----------------------------------------+-----------------------------------------+
```

---

## 2. Core Gates & Guarantees

| Gate | Description | Enforcement Point |
| :--- | :--- | :--- |
| **Gate A** | **Immutable Identity** | `resilience_action_journal.py`: Create-once plan/action. Idempotent on identical digest, 409 conflict on mutation, never resets `SUCCEEDED` to `PENDING`. |
| **Gate B** | **CAS Fencing & Leases** | `claim_action()`, `update_action_state()`: Strict `execution_epoch` and `claim_token` fencing. Expired lease takeover increments epoch; stale workers rejected. |
| **Gate C** | **Effect Reconciliation** | `resilience_effect_reconciler.py`: Inspects `effectHandle` during worker recovery (`RESUME_SIMULATING`, `ADVANCE_TO_VERIFYING`, `RESUME_EXECUTION`, `TRIGGER_COMPENSATION`, `EFFECT_UNKNOWN`). |
| **Gate D** | **All-Subsystem Idempotency**| `backup_dr_readiness.py`: `run_dr_drill` accepts `resilience_action_id` and deduplicates against existing records. |
| **Gate E** | **Real Outcome Contracts** | `resilience_outcome_verifier.py`: Verifies full destination durability, Receipt v4/Commit v4 authentication, and failure domain objectives. |
| **Gate F** | **Scoped Risk Reduction** | `verify_scoped_risk_reduction()`: Matches exact `riskSubject` and derives `effectObserved = (severityAfter < severityBefore)`. |
| **Gate H/I**| **DAG Coordination Graph** | `resilience_coordinator.py`: Builds DAG dependencies (Repair before Rebalance), conflict sets, and transactional resource locks. |
| **Gate J** | **Atomic Safety Budgets** | Concurrency limits and priority preemption (Critical repair preempts Warning rebalance). |
| **Gate K** | **Blast-Radius Safety** | Invariant check guarantees active actions never drop copies below `minCommittedCopies` or `minFailureDomains`. |
| **Gate L** | **Typed Compensation** | Safe transitions: `NO_EFFECT -> FAILED_BEFORE_EFFECT`, `CANCELABLE -> COMPENSATED`, `EFFECT_UNKNOWN -> EFFECT_UNKNOWN`, `IRREVERSIBLE -> NEEDS_OPERATOR`. |

---

## 3. Operator Procedures & Troubleshooting

### 3.1 Inspecting Active Coordination & Leases

Query the journal API to inspect running actions and active leases:
```bash
# List all active actions
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/api/workspace/resilience/journal?limit=50

# Inspect latest coordination plan DAG
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/api/workspace/resilience/coordination-plan
```

### 3.2 Handling `NEEDS_OPERATOR` or `EFFECT_UNKNOWN` Actions

1. Identify the action:
   ```bash
   sqlite3 .resilience-journal/journal.sqlite3 "SELECT action_id, action_type, state, compensation_state, error_message FROM resilience_actions WHERE state IN ('NEEDS_OPERATOR', 'EFFECT_UNKNOWN');"
   ```
2. Check underlying subsystem job state:
   - For repairs: check `.backup-replication/repairs/<repair_id>.json`
   - For rebalances: check `.backup-rebalance/rebalances/<job_id>.json`
   - For DR drills: check `.restore-staging/<drill_id>/drill-result.json`
3. If remote mutation was executed cleanly:
   - Authenticate destination copy with `backup_replication.authenticate_committed_copy()`.
   - Update action state to `SUCCEEDED` or resolve manually.
4. If remote mutation failed:
   - Cancel job and clear locks using `resilience_action_journal.compensate_action(action_id, "Operator resolved", effect_class="CANCELABLE")`.

### 3.3 Emergency Circuit Breaker

If an authority degradation or target partition occurs:
1. The coordinator automatically marks all mutations with `requiresApproval = True` and sets `status = "BLOCKED"`.
2. To pause all autonomous actions globally, update the automation policy:
   ```json
   {
     "policyVersion": 1,
     "autonomousExecutionEnabled": false
   }
   ```
   persisted in `.resilience-policy/autonomous_policy.json`.

---

## 4. Verification Checkpoints

- **Immutable Journal**: `tests/test_backup_472_crash_recovery_leases.py`
- **Coordination Graph & Safety Budgets**: `tests/test_backup_472_resilience_coordination.py`
- **Outcome Contracts & Scoped Reduction**: `tests/test_backup_472_outcome_verification.py`
- **Three-MinIO Remediation**: `tests/test_backup_472_real_three_minio_remediation_e2e.py`
