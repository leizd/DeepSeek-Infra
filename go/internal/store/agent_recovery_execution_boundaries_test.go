package store

import (
	"crypto/ed25519"
	"database/sql"
	"strings"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"github.com/leizd/DeepSeek-Infra/go/pkg/protocol"
)

func recoveryBoundaryCompleteSelection(t *testing.T, control *Control, run agent.RunMetadata, private ed25519.PrivateKey, settleOnly ...bool) CompleteAgentTasksResult {
	t.Helper()
	claim := recoveryExecutionClaim(t, control, run, "tasks")
	lease := claim.Admission.Lease
	role := run.Recovery.Metadata.Execute[0].Id
	node, err := control.BeginAgentNodeDispatch(AgentNodeDispatchRequest{OperationID: "boundary-selected", Actor: "operator:recovery-boundary",
		ActionID: lease.ActionID, Epoch: lease.Epoch, RunID: run.RunID, NodeID: role, Owner: lease.Owner, ClaimToken: lease.ClaimToken})
	if err != nil {
		t.Fatal(err)
	}
	intent := agentOutcomeIntent(node.Intent)
	receipt := &agentv1.AgentNodeOutcomeReceipt{SchemaVersion: 1, SignerPublicKey: control.agentOutcomeSignerKey,
		Evidence: &agentv1.AgentNodeOutcomeEvidence{SchemaVersion: 1, Fence: intent.Fence, OperationId: intent.OperationId,
			RunId: intent.RunId, NodeId: intent.NodeId, Phase: intent.Phase, Request: intent.Request, PlanDigest: intent.PlanDigest,
			MetadataIndex: intent.MetadataIndex, MetadataEpoch: intent.MetadataEpoch, WriterFencingToken: lease.WriterFencingToken,
			CreatedAt: time.Unix(intent.RecordedAt, 0).UTC().Format("2006-01-02T15:04:05Z"), ConfirmedAt: time.Unix(intent.RecordedAt+1, 0).UTC().Format("2006-01-02T15:04:05Z"),
			OutcomeSha256: strings.Repeat("a", 64), OutcomeLength: 30, PublicEvent: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("b", 64), Length: 20},
			FleetId: control.fleetID, Environment: control.environment, TaskOutput: &agentv1.AgentTaskOutputMetadata{SchemaVersion: 1, PromptTokens: 1, CompletionTokens: 1, LatencyMs: 1}}}
	agentOutcomeSign(t, receipt, private)
	consumed, err := control.RecordAgentNodeOutcome(AgentNodeOutcomeRequest{OperationID: "boundary-outcome", Actor: "operator:recovery-boundary",
		ActionID: lease.ActionID, Epoch: lease.Epoch, RunID: run.RunID, NodeID: role, Receipt: receipt})
	if err != nil {
		t.Fatal(err)
	}
	settled, err := control.SettleAgentTask(AgentTaskSettlementRequest{OperationID: "boundary-settlement", Actor: "operator:recovery-boundary",
		ActionID: lease.ActionID, Epoch: lease.Epoch, RunID: run.RunID, NodeID: role, Owner: lease.Owner, ClaimToken: lease.ClaimToken,
		ExpectedIndex: run.NextIndex, SourceReceiptSHA256: consumed.ReceiptSHA256})
	if err != nil {
		t.Fatal(err)
	}
	if len(settleOnly) > 0 && settleOnly[0] {
		action, found, err := control.Get("action", lease.ActionID)
		if err != nil || !found {
			t.Fatal(err)
		}
		return CompleteAgentTasksResult{Run: settled.Run, Action: action}
	}
	completed, err := control.CompleteAgentTasks(CompleteAgentTasksRequest{OperationID: "boundary-completion", Actor: "operator:recovery-boundary",
		ActionID: lease.ActionID, Epoch: lease.Epoch, RunID: run.RunID, Owner: lease.Owner, ClaimToken: lease.ClaimToken, ExpectedIndex: settled.Run.NextIndex})
	if err != nil {
		t.Fatal(err)
	}
	return completed
}

func recoveryBoundaryResumeFixture(t *testing.T) (*Control, agent.RunMetadata, ed25519.PrivateKey) {
	t.Helper()
	control, final, private := synthesisSettlementFixture(t)
	if _, err := control.SettleAgentSynthesis(final); err != nil {
		t.Fatal(err)
	}
	run, _, err := control.AgentRunMetadata(final.RunID)
	if err != nil {
		t.Fatal(err)
	}
	for _, event := range []agent.RunEvent{{Type: "run_status", Status: "running"}, {Type: "agent_reset", Phase: "critic"}, {Type: "run_status", Status: "failed"}} {
		mutation := eventMutation(t, control, run.NextIndex, event)
		mutation.Create = nil
		result, err := control.AppendAgentRunEvent(mutation)
		if err != nil {
			t.Fatal(err)
		}
		run = result.Run
	}
	expected := recoveryExpectedFromRun(t, control, run.RunID, &agentv1.AgentRunRecoverySelection{Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RESUME})
	input, preview := recoveryInputFromPreview(t, control, expected)
	if len(preview.Context.Execute) != 1 || preview.Context.Execute[0].Id != "critic" || len(preview.Context.RetainedOutputs) != 1 {
		t.Fatal("partial recovery lost its selected/retained separation")
	}
	committed, err := control.CommitAgentRunRecovery(input)
	if err != nil {
		t.Fatal(err)
	}
	return control, committed.Run, private
}

func TestPartialResumeSynthesisCombinesRetainedAndNewTypedSettlements(t *testing.T) {
	control, run, private := recoveryBoundaryResumeFixture(t)
	completed := recoveryBoundaryCompleteSelection(t, control, run, private)
	scope := AgentRunExecutionBinding{SchemaVersion: 1, RunID: run.RunID, Phase: "synthesis", MetadataIndex: completed.Run.NextIndex,
		MetadataEpoch: run.LastEvent.ExecutionEpoch, Request: completed.Run.Request, PlanDigest: executionPlanDigest(completed.Run)}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	source, err := control.verifiedAgentSynthesisSourceTx(tx, scope)
	tx.Rollback()
	if err != nil || source.ID != completed.Action.ID {
		t.Fatal("partial resume did not use the newly completed selected phase", err)
	}
	claim := recoveryExecutionClaim(t, control, completed.Run, "synthesis")
	if claim.Admission.Record.State != "CLAIMED" {
		t.Fatal("verified mixed sources did not admit synthesis")
	}
	lease := claim.Admission.Lease
	if _, err := control.RenewActionLease(ActionLeaseRenewal{ActionID: lease.ActionID, Epoch: lease.Epoch, Owner: lease.Owner, ClaimToken: lease.ClaimToken, LeaseSeconds: 60}); err != nil {
		t.Fatal("synthesis renewal lost its original source binding", err)
	}
}

func TestPartialResumeCannotSynthesizeBeforeIndependentTaskTermination(t *testing.T) {
	control, run, private := recoveryBoundaryResumeFixture(t)
	settled := recoveryBoundaryCompleteSelection(t, control, run, private, true)
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	_, err = control.verifiedAgentSynthesisSourceTx(tx, AgentRunExecutionBinding{SchemaVersion: 1, RunID: run.RunID, Phase: "synthesis", MetadataIndex: settled.Run.NextIndex,
		MetadataEpoch: run.LastEvent.ExecutionEpoch, Request: settled.Run.Request, PlanDigest: executionPlanDigest(settled.Run)})
	if err == nil {
		t.Fatal("public success flags replaced the independent terminal task marker")
	}
}

// Coherent metadata corruption is deliberate and isolated to a rolled-back
// transaction. Matching a record digest must not turn projected success or a
// forged recovery context into effect authority.
func recoveryBoundarySnapshotTx(t *testing.T, control *Control, run agent.RunMetadata) *sql.Tx {
	t.Helper()
	if run.Recovery != nil {
		digest, err := agentRecoveryContextDigest(run.Recovery.Metadata)
		if err != nil {
			t.Fatal(err)
		}
		run.Recovery.ContextSHA256 = digest
	}
	record := recoveryBoundaryRecord(t, run)
	canonical, err := canonicalControlPayload(record.Payload)
	if err != nil {
		t.Fatal(err)
	}
	record.Payload = canonical
	digest, err := protocol.Digest(record)
	if err != nil {
		t.Fatal(err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DROP TRIGGER control_events_no_update"); err != nil {
		tx.Rollback()
		t.Fatal(err)
	}
	if _, err := tx.Exec(`UPDATE control_events SET state=?,payload_json=?,record_digest=? WHERE domain='agent_run' AND record_id=? AND revision=?`, record.State, string(record.Payload), digest, record.ID, record.Revision); err != nil {
		tx.Rollback()
		t.Fatal(err)
	}
	return tx
}

func TestRecoverySynthesisRejectsCoherentButUnverifiedMetadata(t *testing.T) {
	changes := []string{"schema", "cancelled", "bad-retained-proof", "too-many-outputs", "duplicate-output", "missing-output", "late-reset"}
	for _, change := range changes {
		t.Run(change, func(t *testing.T) {
			target := "synth"
			if change == "late-reset" {
				target = "critic"
			}
			control, run, _ := recoveryExecutionFixture(t, target, true)
			switch change {
			case "schema":
				run.SchemaVersion = 99
			case "cancelled":
				run.Status = "cancelled"
				run.Cancelled = true
				run.CancellationGeneration = run.Recovery.Generation
			case "bad-retained-proof":
				run.Recovery.Metadata.RetainedOutputs[0].Outcome.Signature[0] ^= 1
			case "too-many-outputs":
				run.Recovery.Metadata.SynthesisOutputIds = []string{"coder", "critic", "coder", "critic", "coder"}
			case "duplicate-output":
				run.Recovery.Metadata.SynthesisOutputIds = []string{"coder", "coder"}
			case "missing-output":
				run.Recovery.Metadata.SynthesisOutputIds = []string{"researcher"}
			case "late-reset":
				run.BaseNodes["critic"] = agent.RunNode{ID: "critic", State: "succeeded"}
			}
			tx := recoveryBoundarySnapshotTx(t, control, run)
			defer tx.Rollback()
			scope := AgentRunExecutionBinding{SchemaVersion: 1, RunID: run.RunID, Phase: "synthesis", MetadataIndex: run.NextIndex,
				MetadataEpoch: run.LastEvent.ExecutionEpoch, Request: run.Request, PlanDigest: executionPlanDigest(run)}
			if _, err := control.verifiedAgentSynthesisSourceTx(tx, scope); err == nil {
				t.Fatal("unverified metadata admitted synthesis", change)
			}
		})
	}
}

func TestRecoveryOutputVerificationRejectsWrongScopePhaseAndLaterReset(t *testing.T) {
	for _, change := range []string{"scope", "phase", "read-failure", "reset"} {
		t.Run(change, func(t *testing.T) {
			control, run, _ := recoveryExecutionFixture(t, "critic", true)
			if change == "scope" {
				run.RuntimeRevision++
			}
			if change == "phase" {
				records, err := control.ListAuthoritativeRecords("action")
				if err != nil {
					t.Fatal(err)
				}
				for _, record := range records {
					binding, err := decodeAgentExecution(record)
					if err == nil && binding.Phase == "synthesis" {
						run.Recovery.Metadata.RetainedOutputs[0].Dispatch.Fence.ActionId = record.ID
					}
				}
				digest, err := agentRecoveryContextDigest(run.Recovery.Metadata)
				if err != nil {
					t.Fatal(err)
				}
				run.Recovery.ContextSHA256 = digest
			}
			if change == "reset" {
				mutation := eventMutation(t, control, run.NextIndex, agent.RunEvent{Type: "agent_reset", Phase: "coder"})
				mutation.Create = nil
				result, err := control.AppendAgentRunEvent(mutation)
				if err != nil {
					t.Fatal(err)
				}
				run = result.Run
			}
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback()
			if change == "read-failure" {
				if _, err := tx.Exec("ALTER TABLE action_journal RENAME TO unavailable_original_actions"); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := control.verifiedAgentRecoveryOutputsTx(tx, run); err == nil {
				t.Fatal("wrong original scope/phase/reset admitted a dependency", change)
			}
		})
	}
}

func TestRecoverySynthesisSnapshotReadFailureCannotBecomeEmptySuccess(t *testing.T) {
	control, run, _ := recoveryExecutionFixture(t, "synth", true)
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := tx.Exec("ALTER TABLE control_events RENAME TO unavailable_original_metadata"); err != nil {
		t.Fatal(err)
	}
	_, err = control.verifiedAgentSynthesisSourceTx(tx, AgentRunExecutionBinding{Phase: "synthesis", RunID: run.RunID, MetadataIndex: run.NextIndex, MetadataEpoch: run.LastEvent.ExecutionEpoch,
		Request: run.Request, PlanDigest: executionPlanDigest(run)})
	if err == nil {
		t.Fatal("missing immutable metadata admitted synthesis")
	}
}

func TestRecoveryResetDeniesFreshClaimRenewalAndDispatchWithoutChangingActions(t *testing.T) {
	for _, mode := range []string{"claim", "renewal", "dispatch"} {
		t.Run(mode, func(t *testing.T) {
			control, run, _ := recoveryExecutionFixture(t, "critic", true)
			var claim AgentRunExecutionResult
			if mode != "claim" {
				claim = recoveryExecutionClaim(t, control, run, "tasks")
			}
			before, err := control.ListAuthoritativeRecords("action")
			if err != nil {
				t.Fatal(err)
			}
			reset := eventMutation(t, control, run.NextIndex, agent.RunEvent{Type: "agent_reset", Phase: "coder"})
			reset.Create = nil
			changed, err := control.AppendAgentRunEvent(reset)
			if err != nil {
				t.Fatal(err)
			}
			switch mode {
			case "claim":
				expected := recoveryExpectedFromRun(t, control, run.RunID, &agentv1.AgentRunRecoverySelection{})
				_, err = control.ClaimAgentRunExecution(AgentRunExecutionRequest{OperationID: "denied-reset-claim", Actor: "operator:recovery-boundary", RunID: run.RunID, Phase: "tasks", ExpectedIndex: changed.Run.NextIndex, Fence: expected.Fence, Owner: "recovery-execution", LeaseSeconds: 60})
			case "renewal":
				lease := claim.Admission.Lease
				_, err = control.RenewActionLease(ActionLeaseRenewal{ActionID: lease.ActionID, Epoch: lease.Epoch, Owner: lease.Owner, ClaimToken: lease.ClaimToken, LeaseSeconds: 60})
			case "dispatch":
				lease := claim.Admission.Lease
				_, err = control.BeginAgentNodeDispatch(AgentNodeDispatchRequest{OperationID: "denied-reset-dispatch", Actor: "operator:recovery-boundary", RunID: run.RunID, NodeID: "critic", ActionID: lease.ActionID, Epoch: lease.Epoch, Owner: lease.Owner, ClaimToken: lease.ClaimToken})
			}
			if err == nil {
				t.Fatal("removed dependency still authorized current work", mode)
			}
			after, err := control.ListAuthoritativeRecords("action")
			if err != nil || len(after) != len(before) {
				t.Fatal("denied dependency admission wrote an action", err)
			}
		})
	}
}

func TestRecoveryTaskCompletionRetainsReservationWhenDependencyWasReset(t *testing.T) {
	control, run, private := recoveryExecutionFixture(t, "critic", true)
	settled := recoveryBoundaryCompleteSelection(t, control, run, private, true)
	lease, found, err := control.GetActionLease(settled.Action.ID)
	if err != nil || !found {
		t.Fatal(err)
	}
	reset := eventMutation(t, control, settled.Run.NextIndex, agent.RunEvent{Type: "agent_reset", Phase: "coder"})
	reset.Create = nil
	changed, err := control.AppendAgentRunEvent(reset)
	if err != nil {
		t.Fatal(err)
	}
	_, err = control.CompleteAgentTasks(CompleteAgentTasksRequest{OperationID: "denied-reset-completion", Actor: "operator:recovery-boundary", ActionID: lease.ActionID, Epoch: lease.Epoch,
		RunID: run.RunID, Owner: lease.Owner, ClaimToken: lease.ClaimToken, ExpectedIndex: changed.Run.NextIndex})
	if err == nil {
		t.Fatal("invalidated dependency completed a phase")
	}
	resources, err := control.GetResourceLeases(lease.ActionID)
	if err != nil || len(resources) == 0 {
		t.Fatal("failed completion released an unresolved reservation", err)
	}
}

func TestRecoveryResetHistoryCorruptionCannotReuseAnOldOutput(t *testing.T) {
	control, run, _ := recoveryExecutionFixture(t, "critic", true)
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _, _ = tx.Exec("PRAGMA ignore_check_constraints=OFF"); _ = tx.Rollback() }()
	// Simulate corrupt bytes on disk in this isolated, rolled-back database.
	// The original action and its output/settlement rows remain intact.
	if _, err := tx.Exec("DROP TRIGGER control_events_no_update"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("PRAGMA ignore_check_constraints=ON"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(`UPDATE control_events SET payload_json='invalid-json' WHERE domain='agent_run' AND record_id=? AND revision=?`, run.RunID, run.NextIndex); err != nil {
		t.Fatal(err)
	}
	_, err = control.verifiedAgentRecoveryOutputsTx(tx, run)
	if err == nil || !strings.Contains(strings.ToLower(err.Error()), "json") {
		t.Fatal("invalid reset history became a reusable original output", err)
	}
}
