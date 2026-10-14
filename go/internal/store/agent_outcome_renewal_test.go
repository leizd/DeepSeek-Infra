package store

import (
	"context"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
)

// The first task settles under its original lease. Real writer/action renewals
// keep the same admitted phase alive; its dependent node starts after the first
// admission's deadline. No new action, token, epoch or forged live time is used.
func agentRenewedDependentFixture(t *testing.T) (*Control, AgentNodeDispatchResult, AgentNodeOutcomeRequest, ed25519.PrivateKey, int64) {
	t.Helper()
	control, task, private := taskSettlementFixture(t, false)
	if _, err := control.SettleAgentTask(task); err != nil {
		t.Fatal(err)
	}
	original, found, err := control.GetActionLease(task.ActionID)
	if err != nil || !found {
		t.Fatal(err)
	}
	current := control.now()
	control.now = func() int64 { return current }
	for current <= original.LeaseUntil {
		current += 5
		if err := control.RenewWriter(context.Background()); err != nil {
			t.Fatal("writer renewal", err)
		}
		if _, err := control.RenewActionLease(ActionLeaseRenewal{ActionID: task.ActionID, Epoch: task.Epoch,
			Owner: task.Owner, ClaimToken: task.ClaimToken, LeaseSeconds: 60}); err != nil {
			t.Fatal("action renewal", err)
		}
	}
	admitted, err := control.BeginAgentNodeDispatch(taskDependentRequest(task))
	if err != nil || !admitted.Fresh || admitted.Intent.RecordedAt <= original.LeaseUntil {
		t.Fatal("late dependent admission", err)
	}
	intent := agentOutcomeIntent(admitted.Intent)
	receipt := &agentv1.AgentNodeOutcomeReceipt{SchemaVersion: 1, SignerPublicKey: control.agentOutcomeSignerKey, Evidence: &agentv1.AgentNodeOutcomeEvidence{
		SchemaVersion: 1, Fence: intent.Fence, OperationId: intent.OperationId, RunId: intent.RunId, NodeId: intent.NodeId, Phase: intent.Phase, Request: intent.Request,
		PlanDigest: intent.PlanDigest, MetadataIndex: intent.MetadataIndex, MetadataEpoch: intent.MetadataEpoch, WriterFencingToken: admitted.Admission.Lease.WriterFencingToken,
		CreatedAt: time.Unix(intent.RecordedAt, 0).UTC().Format(time.RFC3339), ConfirmedAt: time.Unix(intent.RecordedAt+1, 0).UTC().Format(time.RFC3339),
		OutcomeSha256: strings.Repeat("c", 64), OutcomeLength: 30, PublicEvent: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("d", 64), Length: 20},
		TaskOutput: &agentv1.AgentTaskOutputMetadata{SchemaVersion: 1, PromptTokens: 3, CompletionTokens: 1, LatencyMs: 2}, FleetId: control.fleetID, Environment: control.environment}}
	agentOutcomeSign(t, receipt, private)
	request := AgentNodeOutcomeRequest{OperationID: "renewed-dependent-outcome", Actor: task.Actor, ActionID: task.ActionID,
		Epoch: task.Epoch, RunID: task.RunID, NodeID: "critic", Receipt: receipt}
	return control, admitted, request, private, original.LeaseUntil
}

func TestAgentOutcomeRetainsRenewedDependentAdmissionBeyondOriginalDeadline(t *testing.T) {
	control, admitted, input, _, initialDeadline := agentRenewedDependentFixture(t)
	before, _, err := control.AgentRunMetadata(input.RunID)
	if err != nil {
		t.Fatal(err)
	}
	result, err := control.RecordAgentNodeOutcome(input)
	if err != nil || result.AlreadyRecorded || input.Receipt.Evidence.WriterFencingToken != admitted.Admission.Lease.WriterFencingToken {
		t.Fatal("real renewed lease was rejected", err)
	}
	replay, err := control.RecordAgentNodeOutcome(input)
	if err != nil || !replay.AlreadyRecorded || replay.ReceiptSHA256 != result.ReceiptSHA256 {
		t.Fatal("immutable receipt replay", err)
	}
	after, _, err := control.AgentRunMetadata(input.RunID)
	if err != nil || after.NextIndex != before.NextIndex || after.Nodes()["critic"].State == "succeeded" {
		t.Fatal("proof ingestion settled a task", err)
	}
	settled, err := control.SettleAgentTask(AgentTaskSettlementRequest{OperationID: "settle-renewed-critic", Actor: input.Actor,
		ActionID: input.ActionID, Epoch: input.Epoch, RunID: input.RunID, NodeID: input.NodeID, Owner: admitted.Admission.Lease.Owner,
		ClaimToken: admitted.Admission.Lease.ClaimToken, ExpectedIndex: before.NextIndex, SourceReceiptSHA256: result.ReceiptSHA256})
	if err != nil || settled.Run.Nodes()["critic"].State != "succeeded" || settled.Action.ExecutionEpoch != input.Epoch {
		t.Fatal("signed renewed result could not settle", err)
	}
	if admitted.Intent.RecordedAt <= initialDeadline {
		t.Fatal("test did not cross the original deadline")
	}
}

func TestAgentOutcomeRenewalWitnessRefusesReboundGappedAndFutureHistory(t *testing.T) {
	cases := []struct{ name, column, value string }{
		{"owner", "owner", "'foreign-owner'"}, {"claim", "claim_token", "'foreign-claim'"},
		{"revision", "claim_revision", "claim_revision+1"}, {"writer", "writer_fencing_token", "0"},
		{"resources", "resource_keys_json", "'[]'"}, {"shortened", "lease_until", "0"},
		{"backward-clock", "recorded_at", "-1"}, {"future", "recorded_at", "recorded_at+100000"},
		{"gap", "recorded_at", "recorded_at+60"}, {"non-integer", "lease_until", "'invalid-deadline'"},
		{"row-read-error", "lease_until", "json_extract('invalid-json','$')"},
	}
	for _, change := range cases {
		t.Run(change.name, func(t *testing.T) {
			control, admitted, input, _, _ := agentRenewedDependentFixture(t)
			columns := []string{"event_id", "action_id", "event_type", "owner", "epoch", "claim_token", "lease_until", "claim_revision", "writer_fencing_token", "recorded_at", "resource_keys_json"}
			for index, column := range columns {
				if column == change.column {
					columns[index] = fmt.Sprintf("CASE WHEN event_type='RENEWED' THEN %s ELSE %s END AS %s", change.value, column, column)
				}
			}
			if _, err := control.db.Exec("CREATE TEMP VIEW action_lease_events AS SELECT " + strings.Join(columns, ",") + " FROM main.action_lease_events"); err != nil {
				t.Fatal(err)
			}
			locks, err := control.GetResourceLeases(input.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			keys := make([]string, len(locks))
			for index, lock := range locks {
				keys[index] = lock.ResourceKey
			}
			manifest, err := json.Marshal(keys)
			if err != nil {
				t.Fatal(err)
			}
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback()
			if _, err := readAgentOutcomeAdmissionTx(tx, admitted.Intent, string(manifest)); err == nil {
				t.Fatal("corrupt renewal authorized historical proof")
			}
		})
	}
}

func TestAgentOutcomeMissingRenewalCannotRetroactivelyAuthorizeLateDispatch(t *testing.T) {
	control, admitted, input, _, _ := agentRenewedDependentFixture(t)
	if _, err := control.db.Exec("CREATE TEMP VIEW action_lease_events AS SELECT * FROM main.action_lease_events WHERE event_type<>'RENEWED'"); err != nil {
		t.Fatal(err)
	}
	locks, err := control.GetResourceLeases(input.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	keys := make([]string, len(locks))
	for index, lock := range locks {
		keys[index] = lock.ResourceKey
	}
	manifest, err := json.Marshal(keys)
	if err != nil {
		t.Fatal(err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := readAgentOutcomeAdmissionTx(tx, admitted.Intent, string(manifest)); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatal(err)
	}
	tx.Rollback()
	expired, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	expired.Rollback()
	witness := admitted.Admission.Lease
	if err := readAgentOutcomeRenewalsTx(expired, admitted.Intent, &witness, string(manifest)); err == nil {
		t.Fatal("closed transaction accepted renewal proof")
	}
}
