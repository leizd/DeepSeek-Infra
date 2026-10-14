package store

import (
	"encoding/json"
	"errors"
	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	"strings"
	"testing"
)

func cancellationSettlementFixture(t *testing.T, retain bool) (*Control, SettleAgentCancellationRequest) {
	t.Helper()
	control, source, _ := agentOutcomeFixture(t)
	if retain {
		if _, err := control.RecordAgentNodeOutcome(source); err != nil {
			t.Fatal(err)
		}
	}
	run, found, err := control.AgentRunMetadata(source.RunID)
	if err != nil || !found {
		t.Fatal(err)
	}
	authority, err := control.GetCutover("agent_run")
	if err != nil {
		t.Fatal(err)
	}
	cancelled, err := control.AppendAgentRunEvent(AgentRunEventMutation{OperationID: "cancel-test-run", Actor: source.Actor, RunID: source.RunID, ExpectedIndex: run.NextIndex,
		Fence: &internalprotocol.ActionFence{ActionId: source.RunID, ExecutionEpoch: uint64(authority.Epoch)}, Event: agent.RunEvent{Type: "run_status", Status: "cancelled", Body: agent.ArtifactRef{SHA256: strings.Repeat("d", 64), Length: 20}}})
	if err != nil {
		t.Fatal(err)
	}
	lease, found, err := control.GetActionLease(source.ActionID)
	if err != nil || !found {
		t.Fatal(err)
	}
	return control, SettleAgentCancellationRequest{OperationID: "cancel-settle-test", Actor: source.Actor, ActionID: source.ActionID, RunID: source.RunID, Epoch: source.Epoch, Owner: lease.Owner, ClaimToken: lease.ClaimToken, ExpectedIndex: cancelled.Run.NextIndex}
}
func TestCancellationSettlementCommitsConfirmedCustodyWithoutTaskSuccess(t *testing.T) {
	control, input := cancellationSettlementFixture(t, true)
	before, _, err := control.AgentRunMetadata(input.RunID)
	if err != nil {
		t.Fatal(err)
	}
	result, err := control.SettleAgentCancellation(input)
	if err != nil {
		t.Fatal(err)
	}
	resources, err := control.GetResourceLeases(input.ActionID)
	if err != nil || len(resources) != 0 {
		t.Fatal("resources not atomically released", err)
	}
	if result.Action.State != "CANCELLED_CONFIRMED" || result.Run.Status != "cancelled" || !result.Run.Cancelled || result.Run.NextIndex != before.NextIndex || result.Run.Nodes()["coder"].State == "succeeded" {
		t.Fatal("cancelled phase fabricated task or run success", result)
	}
	replay, err := control.SettleAgentCancellation(input)
	if err != nil || !replay.AlreadyApplied {
		t.Fatal("cancel settlement replay changed state", err)
	}
	options := OpenOptions{Path: control.path, Owner: "cancel-successor", Now: control.now, AuthorizeCutover: true, PromotionSignerPublicKey: control.promotionSignerKey, AgentOutcomeSignerPublicKey: control.agentOutcomeSignerKey, FleetID: control.fleetID, Environment: control.environment}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	successor, err := OpenControl(options)
	if err != nil {
		t.Fatal(err)
	}
	defer successor.Close()
	replay, err = successor.SettleAgentCancellation(input)
	if err != nil || !replay.AlreadyApplied {
		t.Fatal("restart lost original proof", err)
	}
	if _, _, receipt, found, err := successor.AgentNodeOutcomeSnapshot(input.ActionID, "coder"); err != nil || !found || receipt == nil {
		t.Fatal("restart lost actual retained node proof", err)
	}
}
func TestCancellationSettlementRetainsUnknownAndRejectsStaleScope(t *testing.T) {
	for _, name := range []string{"unknown", "stale-cursor", "stale-epoch", "token", "owner", "lease-expired", "authority"} {
		t.Run(name, func(t *testing.T) {
			control, input := cancellationSettlementFixture(t, name != "unknown")
			switch name {
			case "stale-cursor":
				input.ExpectedIndex--
			case "stale-epoch":
				input.Epoch++
			case "token":
				input.ClaimToken = "wrong"
			case "owner":
				input.Owner = "wrong"
			case "lease-expired":
				lease, _, _ := control.GetActionLease(input.ActionID)
				control.now = func() int64 { return lease.LeaseUntil }
			case "authority":
				control.authorizeCutover = false
			}
			before, _, err := control.Get("action", input.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := control.SettleAgentCancellation(input); err == nil {
				t.Fatal("invalid cancellation settlement admitted")
			}
			after, _, err := control.Get("action", input.ActionID)
			if err != nil || after.Revision != before.Revision || !sameControlRecord(before, after) {
				t.Fatal("refusal changed action", err)
			}
			resources, err := control.GetResourceLeases(input.ActionID)
			if err != nil || len(resources) == 0 {
				t.Fatal("refusal discarded unresolved resources", err)
			}
		})
	}
}
func TestCancellationSettlementAtomicFailureAndForgedMarker(t *testing.T) {
	control, input := cancellationSettlementFixture(t, true)
	if _, err := control.db.Exec(`CREATE TRIGGER cancellation_fault BEFORE DELETE ON action_resource_leases BEGIN SELECT RAISE(ABORT,'cancel-test-storage-fault'); END`); err != nil {
		t.Fatal(err)
	}
	before, _, err := control.Get("action", input.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.SettleAgentCancellation(input); err == nil {
		t.Fatal("storage fault reported success")
	}
	after, _, err := control.Get("action", input.ActionID)
	if err != nil || !sameControlRecord(before, after) {
		t.Fatal("partial cancelled commit", err)
	}
	if _, err := control.db.Exec(`DROP TRIGGER cancellation_fault`); err != nil {
		t.Fatal(err)
	}
	result, err := control.SettleAgentCancellation(input)
	if err != nil {
		t.Fatal(err)
	}
	marker, found, err := readAgentCancellationSettlement(result.Action)
	if err != nil || !found {
		t.Fatal(err)
	}
	for _, name := range []string{"digest", "cursor", "writer", "timestamp", "epoch", "revision"} {
		changed := marker
		switch name {
		case "digest":
			changed.OutcomesSHA256 = strings.Repeat("9", 64)
		case "cursor":
			changed.MetadataIndex--
		case "writer":
			changed.WriterFencingToken++
		case "timestamp":
			changed.RecordedAt++
		case "epoch":
			changed.ExecutionEpoch++
		case "revision":
			changed.ActionRevision++
		}
		var fields map[string]json.RawMessage
		_ = json.Unmarshal(result.Action.Payload, &fields)
		fields[agentCancellationSettlementField], _ = json.Marshal(changed)
		forged := result.Action
		forged.Payload, _ = json.Marshal(fields)
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		err = control.validateAgentCancellationSettlementTx(tx, forged)
		_ = tx.Rollback()
		if !errors.Is(err, ErrCorruptRecord) {
			t.Fatal("forged marker admitted", name, err)
		}
	}
}
